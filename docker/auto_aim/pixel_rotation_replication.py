"""Prospective fixed-candidate replication; preserves the failed development study."""
import argparse
import fcntl
import json
from pathlib import Path
import time
import uuid

import background_jobs as jobs
import manage
from decision_benchmark import sha, verify_bundle
from decision_benchmark_report import behavior
from motion_study import regression_settings
from perception_repair import batch, collect
from perception_study import protected
from pixel_rotation_study import evaluate, regression_gate
from report import load, write

ROOT = manage.OPS / 'experiments/pixel-rotation-replication-20261027'
PARENT = manage.OPS / 'experiments/pixel-rotation-20261027'
VARIANTS = ('baseline', 'BS4', 'R4')


def initialize(root):
    root.mkdir(parents=True, exist_ok=True)
    if (root / 'contract.json').exists():
        c = load(root / 'contract.json')
        assert sha(root / 'contract.json') == load(root / 'state.json')['contract_sha256']
        assert protected() == c['protected']
        for p, h in c['historical_files'].items():
            assert sha(Path(p)) == h, p
        for path in c['bundles'].values():
            verify_bundle(Path(path))
        return c
    pc, ps = load(PARENT / 'contract.json'), load(PARENT / 'state.json')
    assert ps['state'] == 'completed' and ps['selected'] is None
    assert load(PARENT / 'validation.json')['passed']
    assert load(PARENT / 'parallel-result.json')['passed']
    assert protected() == pc['protected']
    seeds = set(pc['confirm_seeds']) | set(range(4701, 4706))
    with jobs.connect() as db:
        used = {json.loads(x[0]).get('sensor_seed') for x in db.execute('SELECT settings FROM items')}
        assert not seeds & used
        assert db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running','pausing')").fetchone()[0] == 0
    history = {str(PARENT / n): h for n, h in load(PARENT / 'report-seal.json').items()}
    history[str(PARENT / 'report-seal.json')] = sha(PARENT / 'report-seal.json')
    assert all(sha(Path(p)) == h for p, h in history.items())
    c = dict(version=1, created=time.time(), parent=str(PARENT), variants=VARIANTS,
             candidate='R4', bundles={v: pc['bundles'][v] for v in VARIANTS},
             protected=pc['protected'], runtime_files=pc['runtime_files'],
             candidate_binary_sha256=pc['candidate_binary_sha256'],
             backend_timing_profile=pc['backend_timing_profile'], effect_gate=pc['effect_gate'],
             bootstrap=pc['bootstrap'], audit_seeds=pc['confirm_seeds'],
             regression_seeds=list(range(4701, 4706)), regression_scenes=pc['regression_scenes'],
             maximum_trials=dict(audit=60, regression=60, repeat=6, repeat_regression=12, ui=0),
             parallelism=2, qualification_source=str(PARENT / 'parallel-result.json'),
             reason='Separate prospective replication of an unchanged candidate. The development screen remains failed: '
                    'first-hit P90 exceeded its 100ms allowance by 0.291768ms; ego weighted loss was 2.243pp against a 2pp allowance. '
                    'Strong primary and rotation gains motivate an independent full check, not retrospective acceptance. '
                    'Use the ten previously reserved unused main seeds; five fresh regression seeds reduce dependence on three-seed boundaries.',
             acceptance='Exactly the original main +5pp weighted, per-scene exact/weighted/throughput/latency and censor gates; '
                        'paired independent gain lower bound >0; BS4 noninferiority and all four original regression gates. '
                        'No threshold changes, no candidate switching, no extension after failure. '
                        'If effects pass, exact score and behavior repetition is required in all six scenes.',
             maximum_complete_trial_s=40, no_truth_input=True, automatic_promotion=False,
             ui_equivalence_certified=False, capacity_registration=False, historical_files=history,
             source_sha256={str(p): sha(p) for p in (Path(__file__), manage.OPS / 'pixel_rotation_replication_report.py')})
    write(root / 'contract.json', c)
    write(root / 'state.json', dict(state='frozen', contract_sha256=sha(root / 'contract.json'),
                                  runs=[], jobs=[], parallelism=2, candidate_promoted=False))
    return c


def submit_extra(root, c, s, phase, variant, scene, seed, repeat):
    assert phase in ('regression', 'repeat_regression')
    assert scene in c['regression_scenes'] and variant in VARIANTS
    assert seed in c['regression_seeds']
    key = (phase, variant, scene, seed, repeat)
    old = next((x for x in s['jobs'] if tuple(x[k] for k in ('phase', 'variant', 'scene', 'sensor_seed', 'repeat_id')) == key), None)
    if old:
        return old
    row = regression_settings(c, scene, seed, variant)
    row['repeat_id'] = repeat
    identifier, now = 'job_pixel_replication_' + uuid.uuid4().hex[:12], time.time()
    with jobs.connect() as db, jobs.transaction(db):
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',
                   (identifier, 'queued', now, now, json.dumps(dict(version=1, _protocol=2,
                    _motion_regression=True, parallelism=2, study=str(root),
                    scope='Prospective study-local fixed-clock regression; no global scene qualification')),
                    c['bundles'][variant], 2, 1, None))
        db.execute('INSERT INTO items(job_id,ordinal,state,settings) VALUES(?,?,?,?)',
                   (identifier, 0, 'pending', json.dumps(row)))
        jobs.event(db, identifier, 'Unchanged frozen candidate independent replication; no automatic promotion')
    entry = dict(phase=phase, variant=variant, scene=scene, sensor_seed=seed,
                 repeat_id=repeat, id=identifier, level=2)
    s['jobs'].append(entry)
    write(root / 'state.json', s)
    return entry


def effects(c, runs):
    main = [x for x in runs if x['phase'] == 'audit']
    reg = [x for x in runs if x['phase'] == 'regression']
    comparisons = [evaluate(main, 'R4', c, ref, True) for ref in ('baseline', 'BS4')]
    regression = regression_gate(reg, 'R4')
    return dict(main=comparisons, regression=regression,
                passed=all(x['passed'] for x in comparisons) and regression['passed'])


def repeatability(root, runs):
    pairs = []
    for row in (x for x in runs if x['phase'] in ('repeat', 'repeat_regression')):
        phase = 'audit' if row['phase'] == 'repeat' else 'regression'
        original = next(x for x in runs if x['phase'] == phase and
                        all(x[k] == row[k] for k in ('variant', 'scene', 'sensor_seed')))
        pairs.append(dict(variant=row['variant'], scene=row['scene'],
                          original=original['run_id'], repeat=row['run_id'],
                          same_scores=all(row[k] == original[k] for k in ('shots', 'hits', 'weight')),
                          same_behavior=behavior(manage.OPS / 'experiments' / row['run_id']) ==
                                        behavior(manage.OPS / 'experiments' / original['run_id'])))
    result = dict(passed=len(pairs) == 18 and all(x['same_scores'] and x['same_behavior'] for x in pairs), pairs=pairs)
    write(root / 'repeatability.json', result)
    return result


def run(root):
    c = initialize(root)
    with (root / 'controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        s = load(root / 'state.json')
        if s['state'] == 'completed':
            return
        try:
            s['state'] = 'running'
            s.pop('error', None)
            write(root / 'state.json', s)
            batch(root, c, s, 'audit', list(VARIANTS), c['audit_seeds'])
            entries = []
            for i, seed in enumerate(c['regression_seeds']):
                for scene in c['regression_scenes'] if i % 2 == 0 else c['regression_scenes'][::-1]:
                    for v in VARIANTS if i % 2 == 0 else VARIANTS[::-1]:
                        entries.append(submit_extra(root, c, s, 'regression', v, scene, seed, i))
            collect(root, c, s, entries)
            result = effects(c, s['runs'])
            write(root / 'effect-result.json', result)
            s['effects_passed'] = result['passed']
            write(root / 'state.json', s)
            if result['passed']:
                batch(root, c, s, 'repeat', list(VARIANTS), [c['audit_seeds'][0]])
                entries = [submit_extra(root, c, s, 'repeat_regression', v, scene,
                                       c['regression_seeds'][0], 0)
                           for scene in c['regression_scenes'] for v in VARIANTS]
                collect(root, c, s, entries)
                s['independently_verified'] = repeatability(root, s['runs'])['passed']
            else:
                s['independently_verified'] = False
            s.update(state='acquisition_complete', ended=time.time())
            write(root / 'state.json', s)
            from pixel_rotation_replication_report import finish
            finish(root)
        except BaseException as error:
            s.update(state='failed', error=repr(error))
            write(root / 'state.json', s)
            raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('freeze', 'run'))
    parser.add_argument('--output', type=Path, default=ROOT)
    args = parser.parse_args()
    if args.action == 'freeze':
        initialize(args.output.resolve())
    else:
        run(args.output.resolve())
