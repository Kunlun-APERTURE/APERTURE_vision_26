"""Evidence review for the separately registered unchanged-candidate replication."""
import argparse
from collections import Counter
import os
from pathlib import Path
import subprocess
import time

import background_jobs as jobs
import manage
from decision_benchmark import sha
from decision_benchmark_report import aggregate, unified_score
from perception_study import protected, quantile
from pixel_rotation_report import diagnostics
from pixel_rotation_replication import initialize, effects, repeatability, VARIANTS
from report import load, write


def analyze(root):
    import numpy as np
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    c, s = initialize(root), load(root / 'state.json')
    comparisons = []
    for phase in ('audit', 'regression'):
        rows = [r for r in s['runs'] if r['phase'] == phase]
        scenes = sorted({r['scene'] for r in rows})
        if phase == 'audit':
            scenes.append('all')
        for scene in scenes:
            for reference in ('baseline', 'BS4'):
                a = [r for r in rows if r['variant'] == reference and (scene == 'all' or r['scene'] == scene)]
                b = [r for r in rows if r['variant'] == 'R4' and (scene == 'all' or r['scene'] == scene)]
                assert {(r['scene'], r['sensor_seed']) for r in a} == {(r['scene'], r['sensor_seed']) for r in b}
                seeds = sorted({r['sensor_seed'] for r in a})
                def totals(rr):
                    return np.asarray([[sum(r[k] for r in rr if r['sensor_seed'] == seed)
                                        for k in ('shots', 'hits', 'weight', 'duration_s')] for seed in seeds], float)
                aa, bb = totals(a), totals(b)
                draw = np.random.default_rng(c['bootstrap']['random_seed']).integers(0, len(seeds), (10000, len(seeds)))
                ax, bx = aa[draw].sum(axis=1), bb[draw].sum(axis=1)
                av, bv = aa.sum(axis=0), bb.sum(axis=0)
                metrics = {}
                for name, num, den in (('weighted', 2, 0), ('exact', 1, 0), ('hits_per_s', 1, 3)):
                    metrics[name] = dict(delta=float(bv[num] / bv[den] - av[num] / av[den]) if av[den] and bv[den] else None,
                                         ci95=np.quantile(bx[:, num] / bx[:, den] - ax[:, num] / ax[:, den], [.025, .975]).tolist()
                                         if np.all(ax[:, den] > 0) and np.all(bx[:, den] > 0) else None)
                comparisons.append(dict(phase=phase, scene=scene, reference=reference, candidate='R4', seeds=len(seeds), metrics=metrics))
    write(root / 'paired-effects.json', dict(comparisons=comparisons, samples=10000,
                                           unit='sensor seed; both main scenes drawn together',
                                           regression_intervals='Descriptive per-scene intervals, no multiplicity adjustment'))
    scenes = ('hf_uniform_3', 'hf_turn90_3', *c['regression_scenes'])
    fig, axes = plt.subplots(2, 3, figsize=(14, 8), layout='constrained')
    for ax, scene in zip(axes.flat, scenes):
        rr = [r for r in s['runs'] if r['phase'] in ('audit', 'regression') and r['scene'] == scene]
        values = [aggregate([r for r in rr if r['variant'] == v]) for v in VARIANTS]
        ax.bar([i - .18 for i in range(3)], [100 * x['target_hit_rate'] if x['target_hit_rate'] is not None else 0 for x in values], .36, label='Exact hit %', color='#3568a8')
        ax.bar([i + .18 for i in range(3)], [100 * x['weighted_hit_rate'] if x['weighted_hit_rate'] is not None else 0 for x in values], .36, label='Weighted hit %', color='#2d9a89')
        ax.set(xticks=range(3), xticklabels=[v + '\nn=' + str(a['shots']) for v, a in zip(VARIANTS, values)],
               ylim=(0, 105), title=scene, ylabel='Percent of shots')
        ax.grid(axis='y', alpha=.2)
        ax.set_axisbelow(True)
    axes.flat[0].legend(fontsize=8)
    fig.savefig(root / 'shooting-comparison.png', dpi=150)
    plt.close(fig)


def verify(root):
    c, s = initialize(root), load(root / 'state.json')
    assert protected() == c['protected']
    for p, h in c['source_sha256'].items():
        assert sha(Path(p)) == h, p
    counts = Counter(r['phase'] for r in s['runs'])
    assert counts['audit'] == counts['regression'] == 60
    for phase, scenes, seeds in (('audit', ('hf_uniform_3', 'hf_turn90_3'), c['audit_seeds']),
                                 ('regression', c['regression_scenes'], c['regression_seeds'])):
        assert {(r['variant'], r['scene'], r['sensor_seed']) for r in s['runs'] if r['phase'] == phase} == {
            (v, sc, seed) for v in VARIANTS for sc in scenes for seed in seeds}
    seen, evidence = set(), {}
    for r in s['runs']:
        key = tuple(r[k] for k in ('phase', 'variant', 'scene', 'sensor_seed', 'repeat_id'))
        assert key not in seen
        seen.add(key)
        folder = manage.OPS / 'experiments' / r['run_id']
        m, meta = load(folder / 'manifest.json'), load(Path(c['bundles'][r['variant']]) / 'bundle.json')
        assert m['algorithm_binary_sha256'] == c['candidate_binary_sha256']
        assert m['configuration_sha256']['algorithm.yaml'] == meta['files']['sp_vision_25/configs/simulator.yaml']
        for role in ('simulator', 'bridge', 'evaluator', 'algorithm'):
            assert m['containers'][role + '_image_id'] == meta['images']['simulator' if role == 'bridge' else role]
        for runtime_path, h in c['runtime_files'].items():
            key = 'ops/' + Path(runtime_path).name
            if key in m['configuration_sha256']:
                assert m['configuration_sha256'][key] == h
        assert all(m.get(k) == v for k, v in dict(scene=r['scene'], sensor_seed=r['sensor_seed'], terrain='flat_range', input_contract='aligned-v1', warmup_frames=30).items())
        assert all(m['execution'].get(k) == v for k, v in dict(mode='lockstep', timing_profile=c['backend_timing_profile'], clock_domain='simulation', detector_history='session').items())
        assert all(m['shooting'].get(k) == v for k, v in dict(bullet_speed=25., reported_bullet_speed=25., physics='no-drag', aim_origin='muzzle', fire_policy='advice', fire_rate_limit_hz=5., gimbal_profile='feedforward', algorithm_profile='tracking_fire_v3').items())
        assert r['health']['healthy'] and r['wall_s'] <= 40
        assert load(folder / 'session-evidence-audit.json')['passed']
        for name, h in r['source_hashes'].items():
            assert sha(folder / name) == h
        with jobs.connect() as db:
            attempts = [dict(a) for a in db.execute('SELECT a.* FROM attempts a JOIN items i ON a.item_id=i.id WHERE i.job_id=?', (r['job_id'],))]
        assert len(attempts) == 1 and attempts[0]['state'] == 'finished' and attempts[0]['verdict'] != 'invalid'
        score = unified_score(folder, root / 'normalized-scores' / r['run_id'], c)['metrics']
        assert (score['shots_fired'], score['target_armor_hits']) == (r['shots'], r['hits'])
        assert abs(score['weighted_hit_score_sum'] - r['weight']) < 1e-10
        evidence[r['run_id']] = {name: sha(folder / name) for name in ('manifest.json', 'summary.json', 'run-state.json', 'telemetry.jsonl', 'session-evidence/events.jsonl')}
    result = effects(c, s['runs'])
    assert result == load(root / 'effect-result.json') and s['effects_passed'] == result['passed']
    if result['passed']:
        assert counts['repeat'] == 6 and counts['repeat_regression'] == 12
        repeated = repeatability(root, s['runs'])
        assert s['independently_verified'] == repeated['passed']
    else:
        assert counts['repeat'] == counts['repeat_regression'] == 0 and not s['independently_verified']
    diagnostics(root)
    write(root / 'run-evidence-hashes.json', evidence)
    write(root / 'validation.json', dict(passed=True, independently_verified=s['independently_verified'],
          trial_counts=dict(counts), backend_trials=len(s['runs']), maximum_complete_trial_s=max(r['wall_s'] for r in s['runs']),
          original_failed_study_unchanged=True, unchanged_candidate=True, original_gates_retained=True,
          raw_scores_recomputed=True, protected_unchanged=True, duplicate_attempts=False,
          repeatability_all_six_scenes=result['passed'] and s['independently_verified'],
          ui_acquisition=0, automatic_promotion=False, capacity_registration=False))


def render(root):
    c, s, v = load(root / 'contract.json'), load(root / 'state.json'), load(root / 'validation.json')
    effects_result, paired = load(root / 'effect-result.json'), load(root / 'paired-effects.json')
    pct = lambda x: '—' if x is None else f'{100*x:.3f}%'
    lines = ['# 图像联合旋转模型：固定代码独立复核', '',
             '本轮单独登记，原开发批次仍为未通过，没有改写其选型、门槛或报告。该批次主场景首次命中P90越界约0.292ms、自车移动加权命中损失2.243pp超过2pp门槛；其旋转命中吞吐明显提高。新复核固定R4代码、参数和全部原验收门槛，使用原先预留的十个新主场景种子与五个新回归种子，检验这些收益和退化能否重复。', '',
             '[登记定义](contract.json) · [原批次报告](../pixel-rotation-20261027/report.md) · [完整验收](effect-result.json) · [证据核验](validation.json) · [机制诊断](live-diagnostics.json)', '',
             f'独立效果门槛通过={s["effects_passed"]}；包含六场景重复性核验后的通过={s["independently_verified"]}。完成{len(s["runs"])}轮射击，单次最长{v["maximum_complete_trial_s"]:.2f}s。正式二进制和配置未变，新增UI0，未自动推广。', '',
             'baseline是正式v3；BS4包含既有BS感知和4.329533ms短补偿；R4在BS4上新增默认关闭的图像角点联合旋转模型。几何、弹道、开火窗口、5Hz advice和评分一致。', '',
             '![射击对照](shooting-comparison.png)', '']
    for phase, title in (('audit', '十种子独立主场景'), ('regression', '五种子独立回归')):
        rows = [r for r in s['runs'] if r['phase'] == phase]
        lines += ['## ' + title, '', '| 场景 | 配置 | 命中/发射 | 精确命中 | 加权命中 | 命中/秒 | 首次命中P90/s |', '|---|---|---:|---:|---:|---:|---:|']
        scenes = sorted({r['scene'] for r in rows}) + (['all'] if phase == 'audit' else [])
        for scene in scenes:
            for variant in VARIANTS:
                rr = [r for r in rows if r['variant'] == variant and (scene == 'all' or r['scene'] == scene)]
                a = aggregate(rr)
                lines.append(f'| {scene} | {variant} | {a["hits"]}/{a["shots"]} | {pct(a["target_hit_rate"])} | {pct(a["weighted_hit_rate"])} | {a["hits_per_s"]:.3f} | {quantile([r["first_hit_s"] for r in rr],.9):.3f} |')
    lines += ['', '## 配对效果', '', '| 阶段/场景 | 对照 | 加权差/pp及95%区间 | 精确差/pp及95%区间 | 每秒命中差及95%区间 |', '|---|---|---:|---:|---:|']
    for row in paired['comparisons']:
        values = []
        for metric in ('weighted', 'exact', 'hits_per_s'):
            x = row['metrics'][metric]
            scale = 1 if metric == 'hits_per_s' else 100
            values.append('—' if x['delta'] is None or x['ci95'] is None else f'{scale*x["delta"]:+.3f} [{scale*x["ci95"][0]:+.3f}, {scale*x["ci95"][1]:+.3f}]')
        lines.append('| ' + row['phase'] + '/' + row['scene'] + ' | ' + row['reference'] + ' | ' + ' | '.join(values) + ' |')
    lines += ['', '主场景合并时以种子为单位保留两场景共同抽样；弹丸和帧不作为独立样本。回归区间用于描述，未做多重比较校正。', '', '## 原门槛逐项核验', '']
    for row in effects_result['main']:
        lines.append('R4/' + row['reference'] + '通过=' + str(row['passed']) + '；未通过=' + str([k for k, x in row['checks'].items() if not x]) + '。')
    reg = effects_result['regression']
    lines += ['', '四回归通过=' + str(reg['passed']) + '；未通过=' + str([k for k, x in reg['checks'].items() if not x]) + '。', '',
              '未通过就按本次固定预算结束，不切换候选、不追加种子、不降低门槛。独立效果通过后才追加18轮重复性检查，要求六场景三配置的全部输入/算法行为和射击评分完全复现。', '',
              '额外旋转/自车场景采用固定逻辑时序，仍不继承主场景UI效果资格；本复核不能替代实车时延、姿态噪声和实时计算截止时间验收。4.329533ms不能直接用于实车。']
    (root / 'report.md').write_text('\n'.join(lines) + '\n')


def finish(root):
    c, s = initialize(root), load(root / 'state.json')
    if s['state'] == 'completed' and (root / 'report-seal.json').exists():
        assert all(sha(root / n) == h for n, h in load(root / 'report-seal.json').items())
        return
    assert len(s['runs']) >= 120
    meta = load(Path(c['bundles']['baseline']) / 'bundle.json')
    subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--cpus', '2', '--user', f'{os.getuid()}:{os.getgid()}',
                    '-v', str(manage.ROOT) + ':' + str(manage.ROOT) + ':ro', '-v', str(root) + ':' + str(root),
                    '-w', str(manage.ROOT), '--entrypoint', 'python3', meta['images']['evaluator'],
                    '-B', str(Path(__file__)), 'analyze', '--output', str(root)], check=True, timeout=90)
    verify(root)
    s = load(root / 'state.json')
    s.update(state='completed', report_completed=time.time())
    s.pop('error', None)
    write(root / 'state.json', s)
    render(root)
    write(root / 'report-seal.json', {p.name: sha(p) for p in root.iterdir() if p.is_file() and p.suffix != '.log' and p.name not in ('report-seal.json', 'controller.lock')})
    print(dict(completed=True, independently_verified=s['independently_verified'], trials=len(s['runs'])), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('analyze', 'finish'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'analyze':
        analyze(args.output.resolve())
    else:
        finish(args.output.resolve())
