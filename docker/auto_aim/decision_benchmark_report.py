"""Paired algorithm effects and engine interaction; uncertainty is never a pass."""
import argparse
import copy
import hashlib
import html
import json
import math
import os
from pathlib import Path
import random
import re
import statistics
import subprocess

import manage
from decision_benchmark import check_workspace, initialize, sha
from report import load, write

METRICS = ('weighted_hit_rate', 'target_hit_rate', 'hits_per_s', 'fire_frequency')


def aggregate(rows):
    n = sum(r['shots'] for r in rows)
    hits = sum(r['hits'] for r in rows)
    seconds = sum(r['duration_s'] for r in rows)
    return dict(shots=n, hits=hits, duration_s=seconds,
                weighted_hit_rate=sum(r['weight'] for r in rows) / n if n else None,
                target_hit_rate=hits / n if n else None,
                hits_per_s=hits / seconds if seconds else None,
                fire_frequency=n / seconds if seconds else None)


def direction(interval, margin):
    if interval is None:
        return 'uncertain'
    lo, hi = interval
    if lo > margin:
        return 'gain'
    if hi < -margin:
        return 'loss'
    if lo >= -margin and hi <= margin:
        return 'neutral'
    return 'uncertain'


def same_contract(rows):
    """Refuse to turn changes of experiment definition into algorithm regressions."""
    signatures = {json.dumps(r['comparison_contract'], sort_keys=True) for r in rows}
    if len(signatures) != 1:
        raise ValueError('Experiment definitions differ; do not pool or rank these runs')
    cells = {(r['engine'], r['variant']) for r in rows}
    if cells != {('ui', 'v3'), ('ui', 'v3_no_refine'),
                 ('backend', 'v3'), ('backend', 'v3_no_refine')}:
        raise ValueError('All four paired algorithm/engine cells are required')
    bycell = {c: [r['sensor_seed'] for r in rows if (r['engine'], r['variant']) == c] for c in cells}
    if any(len(v) != len(set(v)) for v in bycell.values()):
        raise ValueError('Repeated seeds are not independent samples')
    if len({tuple(sorted(v)) for v in bycell.values()}) != 1:
        raise ValueError('Different seed sets cannot form a paired algorithm comparison')


def compare(rows, contract):
    same_contract(rows)
    cells = {(e, v): {r['sensor_seed']: r for r in rows if r['engine'] == e and r['variant'] == v}
             for e in ('ui', 'backend') for v in contract['variants']}
    seeds = sorted(next(iter(cells.values())))
    def values(indices):
        a = {c: aggregate([rs[i] for i in indices]) for c, rs in cells.items()}
        out = {e: {m: a[e, 'v3_no_refine'][m] - a[e, 'v3'][m]
                   if a[e, 'v3_no_refine'][m] is not None and a[e, 'v3'][m] is not None else None
                   for m in METRICS} for e in ('ui', 'backend')}
        out['interaction'] = {m: out['backend'][m] - out['ui'][m]
                              if out['backend'][m] is not None and out['ui'][m] is not None else None
                              for m in METRICS}
        return out
    point = values(seeds)
    rng = random.Random(contract['bootstrap']['random_seed'])
    samples = {e: {m: [] for m in METRICS} for e in point}
    for _ in range(contract['bootstrap']['samples']):
        sample = values(rng.choices(seeds, k=len(seeds)))
        for e in samples:
            for m in METRICS:
                samples[e][m].append(sample[e][m])
    intervals = {}
    for e in samples:
        intervals[e] = {}
        for m, raw in samples[e].items():
            # Zero-shot resamples remain uncertainty, rather than silently dropping them.
            if any(x is None for x in raw):
                intervals[e][m] = None
            else:
                v = sorted(raw); intervals[e][m] = [v[math.floor(.025*(len(v)-1))], v[math.ceil(.975*(len(v)-1))]]
    margins = dict(weighted_hit_rate=contract['decision']['weighted_practical_margin'],
                   target_hit_rate=.03, hits_per_s=contract['decision']['hits_per_s_practical_margin'],
                   fire_frequency=.1)
    directions = {e: {m: direction(intervals[e][m], margins[m]) for m in METRICS}
                  for e in ('ui', 'backend')}
    totals = {e + '/' + v: aggregate(list(rr.values())) for (e, v), rr in cells.items()}
    eligible = len(seeds) >= contract['decision']['minimum_pairs'] and all(
        t['shots'] >= contract['decision']['minimum_shots_per_cell'] for t in totals.values())
    p = contract['decision']['primary']; a, b = directions['ui'][p], directions['backend'][p]
    opposite = a in ('gain', 'loss') and b in ('gain', 'loss') and a != b
    guard_a, guard_b = directions['ui']['hits_per_s'], directions['backend']['hits_per_s']
    guard_opposite = guard_a in ('gain', 'loss') and guard_b in ('gain', 'loss') and guard_a != guard_b
    supported = eligible and a != 'uncertain' and a == b and not guard_opposite
    return dict(seeds=seeds, cells=totals, effect_definition='v3_no_refine minus v3',
                effects=point, intervals_95=intervals, directions=directions,
                sufficient_pairs_and_shots=eligible,
                ranking_transfer='supported' if supported else 'reversed' if eligible and opposite else 'inconclusive',
                hits_per_s_ranking_reversed=guard_opposite,
                effect_magnitude_agreement=direction(intervals['interaction'][p],
                    contract['decision']['interaction_margin']) == 'neutral',
                limitation='Five paired sensor seeds, one algorithm ablation. A shared point-estimate sign is insufficient. '
                           'No general ranking guarantee for other algorithms, scenes or concurrency.')


def unified_score(folder, output, contract):
    """Normalize legacy UI/native backend reports without altering source evidence."""
    from shots import score
    if output.resolve().is_relative_to(folder.resolve()):
        raise ValueError('Normalized scores must be outside the original evidence directory')
    for name in ('shots.py', 'impact_quality.py'):
        if sha(manage.OPS / name) != contract['runtime_files']['docker/auto_aim/' + name]:
            raise ValueError('Frozen scorer implementation changed: ' + name)
    output.mkdir(parents=True, exist_ok=True)
    m = load(folder / 'manifest.json'); raw = load(folder / 'summary.json')
    unified = copy.deepcopy(m); unified['scoring_version'] = 2
    metrics, errors = score(folder, unified, load(folder / 'run-state.json'), output)
    if errors:
        raise ValueError('Unified scoring failure: ' + repr((folder.name, errors)))
    result = dict(scoring_version=2, native_scoring_version=raw['metrics']['scoring_version'],
                  metrics=metrics, original_weighted_hit_rate=raw['metrics']['weighted_hit_rate'],
                  source_hashes={name: sha(folder / name) for name in
                                 ('manifest.json', 'run-state.json', 'summary.json')},
                  scorer_sha256=contract['runtime_files']['docker/auto_aim/shots.py'])
    write(output / 'score.json', result)
    return result


def historical_scores(root, contract):
    """Audit all prior timing cells, without mixing their seeds into the new study."""
    parent = Path(contract['parent'])
    definitions = load(parent / 'result.json')['runs'] + load(parent / 'holdout-result.json')['trials']
    rows = []
    for entry in definitions:
        folder = manage.OPS / 'experiments' / entry['run_id']
        scored = unified_score(folder, root / 'historical-normalized-scores' / folder.name, contract)
        old = load(folder / 'summary.json')['metrics']; new = scored['metrics']
        rows.append(dict(mode=entry['mode'], scene=entry['scene'], run_id=folder.name,
            sensor_seed=entry['sensor_seed'], shots=new['shots_fired'], hits=new['target_armor_hits'],
            native_scoring_version=scored['native_scoring_version'], old_weight=old['weighted_hit_score_sum'],
            unified_weight=new['weighted_hit_score_sum'], source_hashes=scored['source_hashes']))
    groups = []
    for mode, scene in dict.fromkeys((p['mode'], p['scene']) for p in rows):
        rr = [p for p in rows if (p['mode'], p['scene']) == (mode, scene)]
        shots = sum(p['shots'] for p in rr)
        groups.append(dict(mode=mode, scene=scene, shots=shots,
                           exact=sum(p['hits'] for p in rr) / shots if shots else None,
                           original_weighted=sum(p['old_weight'] for p in rr) / shots if shots else None,
                           unified_weighted=sum(p['unified_weight'] for p in rr) / shots if shots else None))
    result = dict(completed=True, scoring_version=2, runs=rows, groups=groups,
        finding='Native UI v1 weighted scoring excludes non-designated plates, backend v2 does not. '
                'All source shots are rescored with the same frozen v2 implementation. '
                'Exact target-armor hit counts are unchanged; weighted values can change. '
                'Historical calibration and independent seeds remain separate; none enter the new algorithm ranking.')
    write(root / 'historical-score-audit.json', result)
    return result


def read_trial(entry, contract, output=None):
    folder = manage.OPS / 'experiments' / entry['run_id']
    m = load(folder / 'manifest.json'); s = load(folder / 'summary.json')
    audit = load(folder / 'session-evidence-audit.json')
    if s['verdict'] == 'invalid' or not audit['passed']:
        raise ValueError('Invalid evidence cannot be used in ranking: ' + folder.name)
    expected_mode = 'realtime' if entry['engine'] == 'ui' else 'lockstep'
    bundle = Path(contract['bundles'][entry['variant']]); meta = load(bundle / 'bundle.json')
    if output is None:
        raise ValueError('An independent normalized-score output is required')
    scored = unified_score(folder, output / folder.name, contract)
    metrics = dict(s['metrics'], **scored['metrics'])
    checks = dict(mode=m['execution']['mode'] == expected_mode,
        scene=m['scene'] == entry['scene'], sensor_seed=m['sensor_seed'] == entry['sensor_seed'],
        input_contract=m['input_contract'] == 'aligned-v1', warmup=m['warmup_frames'] == 30,
        binary=m['algorithm_binary_sha256'] == meta['files']['sp_vision_25/build/simulator/sim_auto_aim'],
        simulator=m['containers']['simulator_image_id'] == meta['images']['simulator'],
        evaluator=m['containers']['evaluator_image_id'] == meta['images']['evaluator'],
        config=m['configuration_sha256']['algorithm.yaml'] == meta['files']['sp_vision_25/configs/simulator.yaml'],
        terrain=m['terrain'] == 'flat_range', fire_policy=m['shooting']['fire_policy'] == 'advice',
        fire_limit=m['shooting']['fire_rate_limit_hz'] == 5.,
        detector=m['execution']['detector_history'] == 'session', scoring=metrics['scoring_version'] == 2)
    if not all(checks.values()):
        raise ValueError('Frozen contract violation: ' + repr((folder.name, checks)))
    if entry['engine'] == 'backend':
        sync = load(folder / 'synchronization.json')
        # The actual profile, not the launcher label, must be fixed.
        actual = sync.get('profile', {})
        if isinstance(actual, str):
            actual = load(folder / 'configuration' / 'timing-profile.json')
        profile_name = actual.get('name') if isinstance(actual, dict) else None
        if profile_name != contract['backend_timing_profile']:
            raise ValueError('Unexpected backend timing profile: ' + repr(sync.keys()))
    common = dict(scene=m['scene'], duration_s=m['duration_s'], random_seed=m['random_seed'],
                  terrain=m['terrain'], terrain_geometry=m['terrain_geometry'],
                  shooting=m['shooting'], input_contract=m['input_contract'], warmup=m['warmup_frames'],
                  detector_history=m['execution']['detector_history'], algorithm_binary=m['algorithm_binary_sha256'],
                  weights=m['algorithm_weights_sha256'],
                  containers={k: m['containers'][k] for k in
                              ('simulator_image_id', 'bridge_image_id', 'evaluator_image_id')},
                  scenario=m['configuration_sha256']['scenario.toml'],
                  scoring_version=metrics['scoring_version'],
                  impact_quality_version=metrics['impact_quality_version'])
    shots = metrics['shots_fired']; hits = metrics['target_armor_hits']
    rows = [json.loads(line) for line in (output / folder.name / 'shots.jsonl').open()]
    if len(rows) != shots:
        raise ValueError('Shot accounting mismatch')
    begin = load(folder / 'run-state.json')['measurement_start_stamp_ns']
    first_hit = min(((p['terminal_stamp_ns'] - begin) * 1e-9 for p in rows
                     if p['outcome'] == 'target_armor'), default=None)
    return dict(entry, shots=shots, hits=hits, weight=metrics['weighted_hit_score_sum'],
                duration_s=m['duration_s'], metrics=metrics, first_hit_s=first_hit,
                native_scoring_version=scored['native_scoring_version'],
                original_weighted_hit_rate=scored['original_weighted_hit_rate'],
                wall_s=load(folder / 'timing.json')['wall_seconds'],
                comparison_contract=common, contract_checks=checks,
                evidence_hashes={p: sha(folder / p) for p in
                                 ('manifest.json', 'summary.json', 'shots.jsonl', 'frames.jsonl',
                                  'session-evidence/events.jsonl', 'session-evidence/input.mjpg')})


def behavior(folder):
    h = hashlib.sha256()
    end = load(folder / 'run-state.json')['measurement_end_stamp_ns']
    for line in (folder / 'session-evidence/events.jsonl').open():
        p = json.loads(line)
        if p['kind'] == 'input':
            if p['data']['stamp_ns'] > end:
                continue
            value = dict(kind='input', jpeg=p['jpeg_sha256'], sensor=p['data']['sensor'])
            value['sensor'] = {k: v for k, v in value['sensor'].items() if k in ('index', 'phase', 'seed')}
        elif p['kind'] == 'result':
            if p['data']['frame']['stamp_ns'] > end:
                continue
            value = {k: p['data']['result'].get(k) for k in
                     ('observations', 'filter_state', 'selected_plate_id', 'yaw', 'pitch', 'tracker', 'fire_advice')}
        else:
            continue
        h.update(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())
    return h.hexdigest()


def analyze(root):
    contract = initialize(root); state = load(root / 'state.json')
    if state['state'] != 'completed':
        raise ValueError('Complete the fixed acquisition before effect conclusions')
    rows = [read_trial(e, contract, root / 'normalized-scores') for e in state['runs']]
    scenes = [dict(scene=scene, **compare([r for r in rows if r['scene'] == scene and r['purpose'] == 'ranking'], contract))
              for scene in contract['scenes']]
    repeatability = []
    for repeat in (r for r in rows if r['purpose'] == 'repeatability'):
        original = next(r for r in rows if r['purpose'] == 'ranking' and r['engine'] == 'backend' and
                        all(r[k] == repeat[k] for k in ('variant', 'scene', 'sensor_seed')))
        hashes = [behavior(manage.OPS / 'experiments' / r['run_id']) for r in (original, repeat)]
        repeatability.append(dict(scene=repeat['scene'], variant=repeat['variant'],
            runs=[original['run_id'], repeat['run_id']], behavior_sha256=hashes,
            behavior_scope='All warmup and inputs/results captured through physical measurement end. '
                           'Post-measurement acquisition tail is archived but cannot change already-fired shots.',
            full_session_input_counts=[sum(json.loads(line)['kind'] == 'input' for line in
                (manage.OPS / 'experiments' / r['run_id'] / 'session-evidence/events.jsonl').open())
                for r in (original, repeat)],
            exact_same_input_and_behavior=hashes[0] == hashes[1],
            exact_score_repeat=all(original[k] == repeat[k] for k in ('shots', 'hits', 'weight'))))
    history = historical_scores(root, contract)
    sensitivity = []
    for pair in load(root / 'corner-interventions/result.json')['pairs']:
        deltas = {}
        for variant in contract['variants']:
            centers = []
            for run in (pair['ui_run'], pair['backend_run']):
                first = load(root / 'fixed-input' / (run + '_' + variant) / 'result.json')['first_result']
                centers.append([first['filter_state'][i] for i in (0, 2, 4)])
            deltas[variant] = math.dist(*centers)
        sensitivity.append(dict(pair=pair['key'], initialized_center_delta_m=deltas))
    write(root / 'fixed-input/first-frame-sensitivity.json', dict(pairs=sensitivity,
        scope='Existing six native image pairs; each retains its own recorded own-pose header. '
              'Within each image, only the frozen algorithm config changes. No new closed-loop score.'))
    result = dict(completed=True, contract_sha256=sha(root / 'contract.json'), trials=rows,
                  scenes=scenes, repeatability=repeatability,
                  all_ranking_transfers_supported=all(s['ranking_transfer'] == 'supported' for s in scenes),
                  all_backend_repeats_exact=all(r['exact_same_input_and_behavior'] and r['exact_score_repeat']
                                                for r in repeatability),
                  maximum_trial_wall_s=max(r['wall_s'] for r in rows),
                  ui_equivalence_certified=False, capacity_registration=False,
                  historical_score_audit_runs=len(history['runs']),
                  fixed_input_first_pair_sensitivity=sensitivity, human_video_acceptance=False)
    write(root / 'result.json', result)
    return result


def figures(root, result):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5), layout='constrained')
    for ax, scene in zip(axes, result['scenes']):
        for i, (engine, color) in enumerate((('ui', 'tab:blue'), ('backend', 'tab:orange'))):
            values = [100 * scene['cells'][engine + '/' + variant]['weighted_hit_rate'] for variant in ('v3', 'v3_no_refine')]
            ax.plot([0, 1], values, 'o-', color=color, label=engine)
        ax.set(xticks=[0, 1], xticklabels=['v3', 'v3 without color refinement'],
               ylabel='Weighted hit rate (%)', title=scene['scene'] + '\nRanking: ' + scene['ranking_transfer'])
        ax.grid(alpha=.2); ax.legend()
    for ext in ('png', 'svg'):
        fig.savefig(root / ('algorithm-effects.' + ext), dpi=170)
    plt.close(fig)
    causal = load(root / 'corner-interventions/result.json')
    names = ('corners_only', 'confidence_only', 'both')
    fig, ax = plt.subplots(figsize=(11, 4), layout='constrained')
    x = np.arange(len(causal['pairs']))
    for i, name in enumerate(names):
        ax.bar(x + (i-1)*.25, [p['effects_m'][name] for p in causal['pairs']], width=.25, label=name)
    ax.set(xticks=x, xticklabels=[p['key'] for p in causal['pairs']], ylabel='Initialized center displacement (m)',
           title='Fixed own-pose metadata; corners and confidence changed independently')
    ax.tick_params(axis='x', labelrotation=15); ax.legend(); ax.grid(axis='y', alpha=.2)
    for ext in ('png', 'svg'):
        fig.savefig(root / ('initialization-attribution.' + ext), dpi=170)
    plt.close(fig)
    # Illustration of the previously reported largest branch, not a selected ranking sample.
    import cv2
    from sync_timing_alignment import first
    pair = max(causal['pairs'], key=lambda p: p['effects_m']['both'])
    fig, axes = plt.subplots(1, 3, figsize=(13, 4), layout='constrained')
    images = []
    for run in (pair['ui_run'], pair['backend_run']):
        _, _, jpeg = first(manage.OPS / 'experiments' / run)
        image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        images.append(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
    for ax, image, label in zip(axes[:2], images, ('UI native image + observed corners', 'Backend native image + observed corners')):
        ax.imshow(image, interpolation='nearest'); ax.set_title(label)
    colors = ('lime', 'orange')
    for side, observations in enumerate((pair['ui_observations'], pair['backend_observations'])):
        for p in observations:
            points = np.asarray(p['corners']); closed = np.vstack((points, points[0]))
            axes[side].plot(closed[:, 0], closed[:, 1], '.-', color=colors[side], lw=1)
    axes[2].imshow(images[0], interpolation='nearest'); axes[2].set_title('Both corner sets on the SAME UI image')
    for a, b in zip(pair['ui_observations'], pair['backend_observations']):
        for observations, color in ((a, colors[0]), (b, colors[1])):
            xy = np.asarray(observations['corners']); closed = np.vstack((xy, xy[0]))
            axes[2].plot(closed[:, 0], closed[:, 1], '.-', color=color, lw=1)
        for x, y in zip(a['corners'], b['corners']):
            axes[2].plot([x[0], y[0]], [x[1], y[1]], color='yellow', lw=.7)
    for ax in axes:
        ax.set(xlim=(665, 780), ylim=(608, 563)); ax.set_aspect('equal'); ax.grid(alpha=.15)
    fig.suptitle(pair['key'] + ': observed shape changes; center initialization delta '
                 + f"{pair['effects_m']['both']:.3f} m (not impact error)")
    for ext in ('png', 'svg'):
        fig.savefig(root / ('native-corner-overlay.' + ext), dpi=190)
    plt.close(fig)


def report(root, result):
    pct = lambda x: '—' if x is None else f'{x:.2%}'
    interval = lambda a: '不确定' if a is None else f'[{a[0]*100:+.2f}, {a[1]*100:+.2f}] pp'
    lines = ['# 同条件算法对照与后台判断可信度', '',
        '本轮只建立基准、诊断并对照，不修改正式算法、默认配置、物理或评分。', '',
        '## 事先冻结的定义', '',
        '两场景 × 五个新传感器种子 1801–1805 × UI/后台 × 两个算法配置，共 40 轮独立闭环。'
        '另做 4 轮后台同种子重复，单独评价复现性，不增加独立样本数。', '',
        '两个版本共享同一二进制与模型，仅将 robustness_v3.color_refinement 从 true 改成 false。'
        '这是单因素消融，没有预设关闭后更好。后台固定 ui-aligned-v7-clock；advice、0.2 秒限频、'
        '平坦靶场、暖机 30 帧、枪口与无阻力弹道保持冻结。', '',
        '[冻结契约](contract.json) · [全部逐轮结果](result.json) · [运行状态](state.json)', '',
        '## 发现并统一的历史评分口径', '',
        '原 UI 的原始报告使用 v1：加权分只计指定正面装甲；后台 v2 计目标全部有效装甲。'
        '此前部分加权分横比混合了两种定义，不能直接作为效果差距。本轮原始证据保持只读，'
        '所有新旧射击在独立目录统一按冻结的 v2 重算。精确命中发数不受此项口径影响。', '',
        '[评分兼容说明](scoring-policy.json) · [历史统一重算审计](historical-score-audit.json)', '',
        '| 历史批次 | 场景 | 原报告加权分 | 统一 v2 加权分 |',
        '|---|---|---:|---:|']
    history = load(root / 'historical-score-audit.json')
    for p in history['groups']:
        if p['mode'] in ('ui', 'v6', 'ui-aligned-v7-clock', 'holdout-ui', 'holdout-backend'):
            lines.append(f"| {p['mode']} | {p['scene']} | {pct(p['original_weighted'])} | {pct(p['unified_weighted'])} |")
    lines += ['', '校准集和独立集仍然分开，历史数据不并入下面的新种子算法对照。', '',
        '## 闭环效果', '',
        '| 场景 | 执行方式 | 算法配置 | 命中/发数 | 精确命中 | 加权命中 | 命中/秒 | 发射/秒 |',
        '|---|---|---|---:|---:|---:|---:|---:|']
    for scene in result['scenes']:
        for cell, value in scene['cells'].items():
            engine, variant = cell.split('/')
            lines.append(f"| {scene['scene']} | {engine} | {variant} | {value['hits']}/{value['shots']} | "
                         f"{pct(value['target_hit_rate'])} | {pct(value['weighted_hit_rate'])} | "
                         f"{value['hits_per_s']:.3f} | {value['fire_frequency']:.3f} |")
    lines += ['', '## 改进方向是否可迁移', '',
        '变化统一为“关闭颜色矫正 − 当前 v3”。按传感器种子成组，同时重采样四个算法/执行方式单元，'
        '计算 95% 配对 bootstrap 区间。区间跨零或跨实际意义门槛时记为不确定，不能按点估计同号判通过。', '',
        '| 场景 | UI 加权变化及 95% 区间 | 后台加权变化及 95% 区间 | 后台−UI 的变化差区间 | 判断 |',
        '|---|---|---|---|---|']
    for s in result['scenes']:
        lines.append(f"| {s['scene']} | {s['effects']['ui']['weighted_hit_rate']*100:+.2f} pp "
                     f"{interval(s['intervals_95']['ui']['weighted_hit_rate'])} | "
                     f"{s['effects']['backend']['weighted_hit_rate']*100:+.2f} pp "
                     f"{interval(s['intervals_95']['backend']['weighted_hit_rate'])} | "
                     f"{interval(s['intervals_95']['interaction']['weighted_hit_rate'])} | {s['ranking_transfer']} |")
    lines += ['', 'supported 仅支持本次两场景、这一项算法消融；reversed 表示有明确反向证据；'
              'inconclusive 表示现有固定样本不足以作可靠判断。未通过不会追加种子或挑选成绩。', '',
              '## 首帧误差拆解', '',
              '使用已有六对原生图像提取的角点和置信度，固定自身位姿，交叉替换角点、置信度；'
              '另做每块装甲单独初始化，隔离解算敏感性与初始选板。共 66 个无闭环诊断案例。', '',
              '| 场景/重复 | 仅换角点的中心差 | 仅换置信度的中心差 | 两者都换 | 初始选板变化 |',
              '|---|---:|---:|---:|---|']
    causal = load(root / 'corner-interventions/result.json')
    for p in causal['pairs']:
        choices = {c['case']: c['image_plate_index'] for c in p['initial_plate_selection']}
        lines.append(f"| {p['key']} | {p['effects_m']['corners_only']:.4f} m | "
                     f"{p['effects_m']['confidence_only']:.4f} m | {p['effects_m']['both']:.4f} m | "
                     f"{choices['base']} → {choices['both']} |")
    largest = max(causal['pairs'], key=lambda p: p['effects_m']['both'])
    switch = [p for p in causal['pairs'] if p['initial_plate_selection'][0]['image_plate_index'] !=
              p['initial_plate_selection'][-1]['image_plate_index']]
    lines += ['', f"最大差异案例 {largest['key']}：只换角点产生 {largest['effects_m']['corners_only']:.4f} m "
              f"初始化差；仅整体平移角点产生 {largest['effects_m']['centroid_only']*1000:.2f} mm，"
              f"去平移后的形状变化产生 {largest['effects_m']['shape_only']:.4f} m。"
              '这定位到角点形状进入位姿解算后的敏感性，而非主要由图像中心漂移引起。']
    for p in switch:
        lines += ['', f"{p['key']}：只换置信度产生 {p['effects_m']['confidence_only']:.4f} m 初始化差，"
                  f"只换角点为 {p['effects_m']['corners_only']:.4f} m。"
                  '初始装甲选择发生变化；这也暴露出不同可见装甲推算出的车体中心不一致。'
                  '仅固定置信度排序可能隐藏不一致，不能替代解算几何一致性验证。']
    lines += ['', '上述距离是初始化估计的变化，不是弹着误差。单块装甲和形状/平移干预的完整结果见'
              ' [角点干预记录](corner-interventions/result.json)。干预效应存在交互，不能相加后声称各占多少百分比。', '',
              '## 固定图像回放', '',
              '12 条既有完整输入前缀 × 两个冻结配置；每条包括全部暖机和测量前两秒。'
              '每对配置接收相同原始 JPEG、相机位姿和时钟。真实目标数据仅在算法退出后用于评分，'
              '不参与算法输入；回放不产生新的命中率。', '',
              '原版观测、滤波状态、选板通过精确复现检查；附加诊断与时间派生浮点字段单独保留差异，'
              '不宣称全部输出逐位一致。[回放与误差统计](fixed-input/state.json)', '',
              '| 既有图对 | 当前 v3 首帧中心差 | 关闭颜色矫正后的首帧中心差 |',
              '|---|---:|---:|']
    for p in result['fixed_input_first_pair_sensitivity']:
        v = p['initialized_center_delta_m']
        lines.append(f"| {p['pair']} | {v['v3']:.4f} m | {v['v3_no_refine']:.4f} m |")
    lines += ['', '每张图保留原始自身位姿头，两算法配置在同一张图上的输入完全一致。'
              '这项诊断支持颜色矫正参与放大首帧差异，但初始化更稳定不代表闭环命中更好，'
              '是否关闭必须看独立闭环结果。', '', '## 复现性与验收范围', '']
    for r in result['repeatability']:
        lines.append(f"- {r['scene']} / {r['variant']}：输入与主要输出完全一致={r['exact_same_input_and_behavior']}，"
                     f"逐轮成绩一致={r['exact_score_repeat']}，全会话输入帧数={r['full_session_input_counts']}。")
    lines += ['', '输入/输出复现检查覆盖全部暖机至物理测量结束。测量后的收尾采集帧数可能不同，'
              '完整归档并单列，不把这些无法改变已发射弹丸的帧混入测量复现结论。', '',
              f"44 轮最大用时：{result['maximum_trial_wall_s']:.2f} 秒。",
              '不授予 UI 绝对等效、实车效果或新的并发容量资格；完整人工视频验收未进行。'
              '单因素消融的结果不构成对其他算法改动的普遍排序保证。', '',
              '![算法闭环效果](algorithm-effects.png)', '',
              '![初始化误差归因](initialization-attribution.png)', '']
    lines += ['![原生图片与角点叠加](native-corner-overlay.png)', '']
    (root / 'report.md').write_text('\n'.join(lines))
    try:
        import markdown
        body = markdown.markdown('\n'.join(lines), extensions=['tables'])
    except ImportError:
        parts = []; table = False
        def inline(text):
            text = html.escape(text)
            text = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', r'<img alt="\1" src="\2">', text)
            return re.sub(r'\[([^\]]*)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)
        for line in lines:
            if line.startswith('|'):
                if not table:
                    parts.append('<table>'); table = True
                cells = [p.strip() for p in line.strip('|').split('|')]
                if all(re.fullmatch(r'[-:]+', p) for p in cells):
                    continue
                parts.append('<tr>' + ''.join('<td>' + inline(p) + '</td>' for p in cells) + '</tr>')
                continue
            if table:
                parts.append('</table>'); table = False
            if not line:
                continue
            if line.startswith('#'):
                n = len(line) - len(line.lstrip('#'))
                parts.append(f'<h{n}>' + inline(line[n:].strip()) + f'</h{n}>')
            else:
                parts.append('<p>' + inline(line) + '</p>')
        if table:
            parts.append('</table>')
        body = '\n'.join(parts)
    (root / 'report.html').write_text('<!doctype html><meta charset="utf-8"><title>算法对照与可信度</title>'
        '<style>body{font:16px system-ui;max-width:1200px;margin:2em auto;line-height:1.6}'
        'table{border-collapse:collapse}td,th{border:1px solid #bbb;padding:.4em}img{max-width:100%}</style>' + body)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--render-only', action='store_true')
    args = parser.parse_args(); root = args.output.resolve()
    if args.render_only:
        result = load(root / 'result.json'); figures(root, result); report(root, result)
        raise SystemExit(0)
    result = analyze(root)
    import importlib.util
    if importlib.util.find_spec('matplotlib') is None:
        contract = load(root / 'contract.json')
        image = load(Path(contract['bundles']['v3']) / 'bundle.json')['images']['evaluator']
        subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--cpus', '2',
            '--user', f'{os.getuid()}:{os.getgid()}', '-e', 'MPLCONFIGDIR=/tmp/matplotlib',
            '-v', str(manage.ROOT) + ':' + str(manage.ROOT) + ':ro', '-v', str(root) + ':' + str(root),
            '-w', str(manage.ROOT), '--entrypoint', 'python3', image, '-B',
            'docker/auto_aim/decision_benchmark_report.py', '--render-only', '--output', str(root)],
            check=True, timeout=120)
    else:
        figures(root, result); report(root, result)
    print(json.dumps(dict(scenes=[{k: s[k] for k in ('scene', 'ranking_transfer', 'effects', 'intervals_95')}
                                 for s in result['scenes']], all_backend_repeats_exact=result['all_backend_repeats_exact']), indent=2))
