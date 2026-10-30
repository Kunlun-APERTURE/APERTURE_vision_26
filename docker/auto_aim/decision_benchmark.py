"""Frozen algorithm A/B in UI and lockstep; no score-based tuning or promotion."""
import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import uuid

import background_jobs as jobs
import experiments
import manage
from report import load, write
from sync_contract_study import settings
from sync_history_study import matched_ui

SCENES = ('hf_uniform_3', 'hf_turn90_3')
SEEDS = (1801, 1802, 1803, 1804, 1805)
VARIANTS = ('v3', 'v3_no_refine')
PROFILE = 'ui-aligned-v7-clock'
PARENT = manage.OPS / 'experiments/timing-alignment-20261023'


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def verify_bundle(bundle):
    meta = load(bundle / 'bundle.json')
    bad = [name for name, digest in meta['files'].items() if sha(bundle / name) != digest]
    if bad:
        raise ValueError('Frozen payload changed: ' + repr(bad))
    return meta


def freeze_variant(base, variant):
    """Hardlink immutable payloads, replacing (never editing) the variant config."""
    old = verify_bundle(base)
    config_name = 'sp_vision_25/configs/simulator.yaml'
    text = (base / config_name).read_text()
    if variant == 'v3_no_refine':
        if text.count('  color_refinement: true') != 1:
            raise ValueError('Expected one enabled color-refinement setting')
        text = text.replace('  color_refinement: true', '  color_refinement: false')
    elif variant != 'v3':
        raise ValueError('Unknown frozen algorithm variant')
    identity = hashlib.sha256((sha(base / 'bundle.json') + variant + text).encode()).hexdigest()[:16]
    target = jobs.STATE / 'bundles' / ('decision_' + variant + '_' + identity)
    if target.exists():
        verify_bundle(target)
        if (target / config_name).read_text() != text:
            raise ValueError('Existing variant has a different config')
        return target
    shutil.copytree(base, target, copy_function=os.link)
    config = target / config_name
    config.unlink()
    config.write_text(text)
    meta = copy.deepcopy(old)
    meta.update(bundle_id=target.name, created=time.time(),
                decision_variant=variant, parent_bundle=str(base),
                declared_algorithm_changes={} if variant == 'v3' else {'robustness_v3.color_refinement': False})
    meta['files'][config_name] = sha(config)
    (target / 'bundle.json').unlink()
    write(target / 'bundle.json', meta)
    verify_bundle(target)
    return target


def initialize(root):
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'contract.json'
    if path.exists():
        contract = load(path)
        if sha(path) != load(root / 'state.json')['contract_sha256']:
            raise ValueError('The preregistered experiment contract changed')
        for folder in contract['bundles'].values():
            verify_bundle(Path(folder))
        return contract
    parent = load(PARENT / 'study.json')
    if parent['state'] != 'completed':
        raise ValueError('Requires the completed frozen timing study')
    base = Path(parent['candidate_bundle'])
    bundles = {v: str(freeze_variant(base, v)) for v in VARIANTS}
    cases = []
    for engine in ('ui', 'backend'):
        for i, seed in enumerate(SEEDS):
            scenes = SCENES if i % 2 == 0 else SCENES[::-1]
            variants = VARIANTS if i % 2 == 0 else VARIANTS[::-1]
            for scene in scenes:
                for variant in variants:
                    cases.append(dict(engine=engine, variant=variant, scene=scene,
                                      sensor_seed=seed, repeat_id=i, purpose='ranking'))
    # Exact same-seed repeat checks are separate from the five independent pairs.
    for variant in VARIANTS:
        for scene in SCENES:
            cases.append(dict(engine='backend', variant=variant, scene=scene,
                              sensor_seed=SEEDS[0], repeat_id=5, purpose='repeatability'))
    protected = ('sp_vision_25/build/simulator/sim_auto_aim', 'sp_vision_25/configs/simulator.yaml',
                 'sp_vision_25/src/sim_auto_aim.cpp', 'sp_vision_25/tests/auto_aim_test.cpp',
                 'docker/auto_aim/input-contract-runtime.json')
    common = settings(SCENES[0], SEEDS[0])
    common.pop('scene'); common.pop('sensor_seed')
    contract = dict(version=1, created=time.time(), parent=str(PARENT),
        base_bundle=str(base), bundles=bundles, scenes=list(SCENES), sensor_seeds=list(SEEDS),
        variants=list(VARIANTS), common=common, backend_timing_profile=PROFILE,
        pairing=['scene', 'sensor_seed'], maximum_planned_trials=len(cases), cases=cases,
        bootstrap=dict(unit='paired sensor seed, all four cells resampled together',
                       samples=10000, random_seed=20261023, confidence=.95),
        decision=dict(primary='weighted_hit_rate', weighted_practical_margin=.03,
                      hits_per_s_practical_margin=.1, interaction_margin=.05,
                      minimum_pairs=5, minimum_shots_per_cell=30,
                      uncertain_is_not_agreement=True),
        intervention='Same frozen binary/model; only robustness_v3.color_refinement true versus false. '
                     'This is a preregistered ablation, not a presumed improved algorithm.',
        exclusions='No score-based rerun, exclusions, timing adjustment or sample extension. '
                   'Infrastructure failures stay visible and invalidate qualification.',
        qualification='Algorithm-ranking diagnostic only; no UI-equivalence, capacity or real-hardware certification.',
        replay_sources=parent['reference'] + [p for p in parent['control'] if p['repeat_id'] < 3],
        protected_workspace={p: sha(manage.ROOT / p) for p in protected},
        runtime_files={p: digest for p, digest in verify_bundle(base)['files'].items()
                       if p.startswith('docker/auto_aim/') or p.startswith('vision_simulator_26/')},
        original_profile=manage.profile(),
        original_default_images={tag: manage.inspect(tag)['Id'] for tag in
                                 ('daedalus:rm26-integration', 'daedalus:rm26-evaluator')})
    write(path, contract)
    write(root / 'state.json', dict(state='frozen', contract_sha256=sha(path), runs=[], jobs=[],
                                  capacity_registration=False, ui_equivalence_certified=False))
    return contract


def case_key(case):
    return tuple(case[k] for k in ('engine', 'variant', 'scene', 'sensor_seed', 'repeat_id'))


def save(root, state):
    write(root / 'state.json', state)


def check_workspace(contract):
    for name, digest in contract['protected_workspace'].items():
        if sha(manage.ROOT / name) != digest:
            raise ValueError('Protected workspace changed during the study: ' + name)


def record_ui(root, state, contract, case):
    value = dict(settings(case['scene'], case['sensor_seed']),
                 repeat_id=case['repeat_id'], label=root.name + '-' + case['variant'])
    # Declare the attempted case before acquisition; interrupted attempts cannot disappear.
    state['current_case'] = case
    save(root, state)
    folder = experiments.run_one(manage, case['scene'], argparse.Namespace(**value))
    state['runs'].append(dict(case, run_id=folder.name))
    state.pop('current_case', None)
    save(root, state)
    summary = load(folder / 'summary.json')
    if summary['verdict'] == 'invalid':
        raise RuntimeError('Invalid UI evidence retained: ' + folder.name)
    if load(folder / 'timing.json')['wall_seconds'] > 40:
        raise RuntimeError('Trial exceeded the 40-second acceptance budget: ' + folder.name)


def ui(root, state, contract):
    pending = [c for c in contract['cases'] if c['engine'] == 'ui' and
               case_key(c) not in {case_key(r) for r in state['runs']}]
    with (manage.RUNTIME / 'operation.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            for case in pending:
                check_workspace(contract)
                with matched_ui(Path(contract['bundles'][case['variant']])):
                    try:
                        manage.stop()
                        start = dict(settings('static', case['sensor_seed']), control='off', view='off',
                                     replace=False, fire='auto')
                        manage.start(argparse.Namespace(**start))
                        record_ui(root, state, contract, case)
                        print(json.dumps(dict(completed=len(state['runs']), **state['runs'][-1])), flush=True)
                    finally:
                        manage.stop()
        finally:
            manage.save_profile(contract['original_profile'])


def backend(root, state, contract):
    cases = [c for c in contract['cases'] if c['engine'] == 'backend']
    for case in cases:
        if case_key(case) in {case_key(r) for r in state['runs']}:
            continue
        check_workspace(contract)
        saved_job = next((p for p in state['jobs'] if p['case'] == case), None)
        if not saved_job:
            opts = dict(settings(case['scene'], case['sensor_seed']),
                        execution_mode='lockstep', timing_profile=PROFILE, max_fps=240,
                        label=root.name + '-' + case['variant'])
            row = jobs.validate({k: v for k, v in opts.items() if k in jobs.FIELDS})
            row.update(repeat_id=case['repeat_id'], execution_profile='lockstep-' + PROFILE)
            identifier = 'job_decision_' + uuid.uuid4().hex[:12]
            now = time.time()
            with jobs.connect() as db, jobs.transaction(db):
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',
                           (identifier, 'queued', now, now, json.dumps(dict(version=1, _protocol=2,
                            _decision_benchmark=True)), contract['bundles'][case['variant']], 1, 0, None))
                db.execute('INSERT INTO items(job_id,ordinal,state,settings) VALUES(?,?,?,?)',
                           (identifier, 0, 'pending', json.dumps(row)))
                jobs.event(db, identifier, 'One preregistered algorithm-ranking trial; no capacity registration')
            saved_job = dict(id=identifier, case=case)
            state['jobs'].append(saved_job); save(root, state)
        identifier = saved_job['id']
        deadline = time.monotonic() + 300
        while True:
            with jobs.connect() as db:
                status = jobs.status(db, identifier)
            if status['state'] == 'completed':
                break
            if status['state'] in ('paused', 'cancelled') or time.monotonic() > deadline:
                raise RuntimeError('Inspect retained backend failure: ' + identifier)
            time.sleep(1)
        with jobs.connect() as db:
            attempts = [dict(r) for r in db.execute(
                'SELECT a.* FROM attempts a JOIN items i ON a.item_id=i.id WHERE i.job_id=?', (identifier,))]
        saved_job['attempts'] = attempts
        save(root, state)
        if len(attempts) != 1 or not attempts[0]['run_id'] or attempts[0]['verdict'] == 'invalid':
            raise RuntimeError('All attempts retained; acquisition needs inspection: ' + identifier)
        folder = manage.OPS / 'experiments' / attempts[0]['run_id']
        state['runs'].append(dict(case, run_id=folder.name, job_id=identifier))
        save(root, state)
        if load(folder / 'timing.json')['wall_seconds'] > 40:
            raise RuntimeError('Trial exceeded the 40-second acceptance budget: ' + folder.name)
        print(json.dumps(dict(completed=len(state['runs']), **state['runs'][-1])), flush=True)


def live(root):
    contract = initialize(root)
    with (root / 'controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = load(root / 'state.json')
        if state.get('current_case'):
            raise RuntimeError('Interrupted UI attempt retained; inspect it before any resume')
        with jobs.connect() as db:
            active = db.execute("SELECT id FROM jobs WHERE state IN ('queued','running','pausing')").fetchall()
        if any(p['id'] not in {j['id'] for j in state['jobs']} for p in active):
            raise RuntimeError('Other experiment jobs are active; do not contaminate the UI reference')
        try:
            state.update(state='running', started=state.get('started', time.time())); save(root, state)
            ui(root, state, contract)
            backend(root, state, contract)
            expected = {case_key(c) for c in contract['cases']}
            if len(state['runs']) != len(expected) or {case_key(r) for r in state['runs']} != expected:
                raise RuntimeError('Incomplete or duplicate acquisition')
            state.update(state='completed', ended=time.time()); save(root, state)
        except BaseException as error:
            state.update(state='failed', error=repr(error)); save(root, state)
            raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'live'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    if args.action == 'freeze':
        c = initialize(root)
        print(json.dumps(dict(contract=str(root / 'contract.json'), trials=c['maximum_planned_trials'],
                              bundles=c['bundles']), indent=2))
    else:
        live(root)
