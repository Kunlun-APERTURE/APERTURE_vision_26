"""Eight conditional UI feature checks after independent backend acceptance."""
import argparse
import fcntl
import json
from pathlib import Path
import subprocess
import time

import background_jobs as jobs
import experiments
import manage
from decision_benchmark import sha, verify_bundle
from decision_benchmark_report import aggregate
from perception_study import protected, trial_metrics
from report import load, write
from sync_contract_study import settings
from sync_history_study import matched_ui

ROOT = manage.OPS / 'experiments/pixel-rotation-ui-probe-20261027'
PARENT = manage.OPS / 'experiments/pixel-rotation-replication-20261027'
SCENES = ('hf_turn90_3', 'hf_spin_pos', 'hf_spin_neg', 'ego_right_centered')
VARIANTS = ('baseline', 'R4')


def initialize(root):
    root.mkdir(parents=True, exist_ok=True)
    if (root / 'contract.json').exists():
        c = load(root / 'contract.json')
        assert sha(root / 'contract.json') == load(root / 'state.json')['contract_sha256']
        assert protected() == c['protected']
        return c
    pc, ps = load(PARENT / 'contract.json'), load(PARENT / 'state.json')
    assert ps['state'] == 'completed' and ps['independently_verified']
    assert load(PARENT / 'validation.json')['passed'] and load(PARENT / 'repeatability.json')['passed']
    assert Path('/sys/class/tty/tty0/active').read_text().strip() == 'tty8'
    history = {str(PARENT / n): h for n, h in load(PARENT / 'report-seal.json').items()}
    history[str(PARENT / 'report-seal.json')] = sha(PARENT / 'report-seal.json')
    assert all(sha(Path(p)) == h for p, h in history.items())
    cases, backend = [], []
    for i, scene in enumerate(SCENES):
        seed = pc['audit_seeds'][0] if scene == 'hf_turn90_3' else pc['regression_seeds'][0]
        for variant in VARIANTS if i % 2 == 0 else VARIANTS[::-1]:
            cases.append(dict(phase='ui_probe', engine='ui', variant=variant, scene=scene, sensor_seed=seed, repeat_id=i))
            backend.append(next(r for r in ps['runs'] if r['phase'] in ('audit', 'regression') and
                                (r['variant'], r['scene'], r['sensor_seed']) == (variant, scene, seed)))
    c = dict(version=1, created=time.time(), parent=str(PARENT), cases=cases, backend=backend,
             bundles={v: pc['bundles'][v] for v in VARIANTS}, protected=pc['protected'],
             runtime_files=pc['runtime_files'], original_profile=manage.profile(), historical_files=history,
             source_sha256=sha(Path(__file__)), maximum_ui_trials=8, maximum_complete_trial_s=40,
             automatic_promotion=False, ui_equivalence_certified=False, capacity_registration=False,
             purpose='Bounded characteristic UI check after independent backend gates and six-scene repetition. '
                     'First previously registered seed per scene, never selected by outcome. One pair per scene is descriptive; '
                     'not independent statistical confirmation or a new UI fidelity certificate.')
    for bundle in c['bundles'].values():
        verify_bundle(Path(bundle))
    write(root / 'contract.json', c)
    write(root / 'state.json', dict(state='frozen', contract_sha256=sha(root / 'contract.json'), runs=[]))
    return c


def summarize(root):
    c, s = initialize(root), load(root / 'state.json')
    assert len(s['runs']) == 8 and protected() == c['protected']
    assert sha(Path(__file__)) == c['source_sha256']
    for p, h in c['historical_files'].items():
        assert sha(Path(p)) == h
    rows, evidence = [], {}
    for entry in [*c['backend'], *s['runs']]:
        row = trial_metrics(root, entry, c)
        folder = manage.OPS / 'experiments' / entry['run_id']
        m, meta = load(folder / 'manifest.json'), load(Path(c['bundles'][entry['variant']]) / 'bundle.json')
        assert m['algorithm_binary_sha256'] == meta['files']['sp_vision_25/build/simulator/sim_auto_aim']
        assert m['configuration_sha256']['algorithm.yaml'] == meta['files']['sp_vision_25/configs/simulator.yaml']
        assert m['containers']['simulator_image_id'] == meta['images']['simulator']
        assert m['containers']['evaluator_image_id'] == meta['images']['evaluator']
        assert all(m.get(k) == v for k, v in dict(scene=entry['scene'], sensor_seed=entry['sensor_seed'],
                   terrain='flat_range', input_contract='aligned-v1', warmup_frames=30).items())
        assert all(m['shooting'].get(k) == v for k, v in dict(bullet_speed=25., reported_bullet_speed=25.,
                   physics='no-drag', aim_origin='muzzle', fire_policy='advice', fire_rate_limit_hz=5.,
                   gimbal_profile='feedforward', algorithm_profile='tracking_fire_v3').items())
        state, active, frames = load(folder / 'run-state.json'), 0, 0
        for line in (folder / 'session-evidence/events.jsonl').open():
            p = json.loads(line)
            if p['kind'] != 'result':
                continue
            f, result = p['data']['frame'], p['data']['result']
            if state['measurement_start_stamp_ns'] <= f['stamp_ns'] <= state['measurement_end_stamp_ns']:
                frames += 1
                active += bool((result.get('continuity', {}).get('pixel_rotation') or {}).get('active'))
        row.update(measurement_frames=frames, pixel_active_frames=active)
        rows.append(row)
        evidence[entry['run_id']] = row['source_hashes']
    comparisons = []
    for scene in SCENES:
        for engine in ('backend', 'ui'):
            a, b = [aggregate([r for r in rows if (r['scene'], r['engine'], r['variant']) == (scene, engine, v)]) for v in VARIANTS]
            comparisons.append(dict(scene=scene, engine=engine, baseline=a, R4=b,
                weighted_gain=None if a['weighted_hit_rate'] is None or b['weighted_hit_rate'] is None else b['weighted_hit_rate'] - a['weighted_hit_rate'],
                exact_gain=None if a['target_hit_rate'] is None or b['target_hit_rate'] is None else b['target_hit_rate'] - a['target_hit_rate'],
                hits_per_s_gain=b['hits_per_s'] - a['hits_per_s']))
    write(root / 'ui-result.json', dict(rows=rows, comparisons=comparisons, descriptive_only=True,
          ui_equivalence_certified=False, candidate_promoted=False))
    write(root / 'validation.json', dict(passed=True, ui_trials=8, protected_unchanged=True,
          parent_evidence_unchanged=True, raw_scores_recomputed=True, vt8_preserved=True,
          maximum_complete_trial_s=max(r['complete_wall_s'] for r in s['runs']), ui_equivalence_certified=False))
    write(root / 'run-evidence-hashes.json', evidence)
    pct = lambda x: '—' if x is None else f'{100*x:.2f}%'
    lines = ['# 图像联合旋转模型：八轮UI特征检查', '',
             '仅在独立后台复核和六场景重复性全部通过后采集。四场景各一个预先固定种子、两配置，属于成本受控的特征检查，不构成UI等价认证。没有修改原后台报告或正式配置。', '',
             '[登记定义](contract.json) · [结果与原始记录](ui-result.json) · [证据校验](validation.json)', '',
             '| 场景 | 引擎 | v3精确/加权/命中每秒 | R4精确/加权/命中每秒 |', '|---|---|---:|---:|']
    for x in comparisons:
        a, b = x['baseline'], x['R4']
        fmt = lambda z: pct(z['target_hit_rate']) + '/' + pct(z['weighted_hit_rate']) + f'/{z["hits_per_s"]:.3f}'
        lines.append('| ' + x['scene'] + ' | ' + x['engine'] + ' | ' + fmt(a) + ' | ' + fmt(b) + ' |')
    lines += ['', '## UI完整证据', '']
    for r in s['runs']:
        lines.append('- ' + r['scene'] + '/' + r['variant'] + ': [逐轮报告](../' + r['run_id'] + '/report.html)，完整用时' + f'{r["complete_wall_s"]:.2f}s。')
    lines += ['', '单种子效果有较大偶然性；不将图像观感或一个场景的同方向变化当成统计等价。完整录像/图像保留在逐轮记录，未代替用户整段人工视觉验收。']
    (root / 'report.md').write_text('\n'.join(lines) + '\n')


def run(root):
    c = initialize(root)
    with (root / 'controller.lock').open('a') as controller:
        fcntl.flock(controller, fcntl.LOCK_EX | fcntl.LOCK_NB)
        s = load(root / 'state.json')
        if s['state'] == 'completed':
            return
        if s.get('current_case'):
            raise RuntimeError('Retained interrupted UI case requires inspection before resuming')
        with jobs.connect() as db:
            assert db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running','pausing')").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM artifacts WHERE state IN ('queued','running')").fetchone()[0] == 0
        subprocess.run(['python3', str(manage.ROOT / 'docker/daedalus/display_auth.py')], check=True)
        with (manage.RUNTIME / 'operation.lock').open('a') as operation:
            fcntl.flock(operation, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                s['state'] = 'running'
                write(root / 'state.json', s)
                for case in c['cases']:
                    if any((r['variant'], r['scene']) == (case['variant'], case['scene']) for r in s['runs']):
                        continue
                    assert protected() == c['protected']
                    assert Path('/sys/class/tty/tty0/active').read_text().strip() == 'tty8'
                    started = time.monotonic()
                    s['current_case'] = case
                    write(root / 'state.json', s)
                    with matched_ui(Path(c['bundles'][case['variant']])):
                        try:
                            manage.stop()
                            manage.start(argparse.Namespace(**dict(settings('static', case['sensor_seed']), control='off', view='off', replace=False, fire='auto')))
                            args = argparse.Namespace(**dict(settings(case['scene'], case['sensor_seed']), repeat_id=case['repeat_id'], label='pixel-rotation-ui-' + case['variant']))
                            folder = experiments.run_one(manage, case['scene'], args)
                        finally:
                            manage.stop()
                    entry = dict(case, run_id=folder.name, complete_wall_s=time.monotonic() - started)
                    s['runs'].append(entry)
                    s.pop('current_case', None)
                    write(root / 'state.json', s)
                    assert entry['complete_wall_s'] <= 40, 'Retain over-budget trial and stop'
                    trial_metrics(root, entry, c)
                    print(json.dumps(dict(done=len(s['runs']), **entry)), flush=True)
                assert Path('/sys/class/tty/tty0/active').read_text().strip() == 'tty8'
                summarize(root)
                s.update(state='completed', ended=time.time())
                write(root / 'state.json', s)
                write(root / 'report-seal.json', {p.name: sha(p) for p in root.iterdir() if p.is_file() and p.suffix != '.log' and p.name not in ('report-seal.json', 'controller.lock')})
            except BaseException as error:
                s.update(state='failed', error=repr(error))
                write(root / 'state.json', s)
                raise
            finally:
                manage.save_profile(c['original_profile'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('freeze', 'run'))
    parser.add_argument('--output', type=Path, default=ROOT)
    args = parser.parse_args()
    if args.action == 'freeze':
        initialize(args.output.resolve())
    else:
        run(args.output.resolve())
