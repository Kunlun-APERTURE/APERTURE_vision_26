"""Seal frozen definitions, raw evidence, media and negative/uncertain findings."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import subprocess
import time

import background_jobs as jobs
import manage
from decision_benchmark import check_workspace, initialize, sha, verify_bundle
from report import load, write


def media(root, contract, state):
    from background_pipeline import enqueue
    own = {p['id'] for p in state['jobs']}
    with jobs.connect() as db:
        for entry in state['runs']:
            if entry['engine'] == 'backend':
                enqueue(db, entry['job_id'], contract['bundles'][entry['variant']], [entry['run_id']])
    deadline = time.monotonic() + 600
    while True:
        with jobs.connect() as db:
            rows = [dict(p) for p in db.execute('SELECT * FROM artifacts') if p['job_id'] in own]
        if any(p['state'] in ('failed', 'cancelled') for p in rows):
            write(root / 'media-queue.json', rows)
            raise RuntimeError('Offline media failure retained')
        if len(rows) == 24 and all(p['state'] == 'completed' for p in rows):
            break
        if time.monotonic() > deadline:
            raise RuntimeError('Offline media deadline exceeded; keep pending artifacts')
        print(json.dumps(dict(media_completed=sum(p['state'] == 'completed' for p in rows),
                              media_expected=24)), flush=True)
        time.sleep(10)
    write(root / 'media-queue.json', rows)


def audit_one(entry):
    import cv2
    import numpy as np
    from background_evidence import audit as measured_audit
    from session_evidence import audit as session_audit
    folder = manage.OPS / 'experiments' / entry['run_id']
    state = load(folder / 'run-state.json')
    measured = measured_audit(folder)
    history = session_audit(folder / 'session-evidence', state['measurement_start_stamp_ns'],
                            state['measurement_end_stamp_ns'], folder)
    decoded = 0
    with (folder / 'session-evidence/input.mjpg').open('rb') as images:
        for line in (folder / 'session-evidence/events.jsonl').open():
            p = json.loads(line)
            if p['kind'] != 'input':
                continue
            images.seek(p['jpeg_offset']); data = images.read(p['jpeg_bytes'])
            image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if image is None or image.shape[:2] != (1080, 1440):
                raise ValueError('Undecodable original session image: ' + folder.name)
            decoded += 1
    passed = measured['passed'] and history['passed'] and bool(measured['video'])
    return dict(entry, passed=passed, measured=measured, history=history,
                decoded_full_session_jpegs=decoded)


def evidence_audit(root):
    import cv2
    state = load(root / 'state.json')
    cv2.setNumThreads(1)
    rows = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for row in pool.map(audit_one, state['runs']):
            rows.append(row)
            print(json.dumps(dict(audited=len(rows), total=len(state['runs']),
                                  run_id=row['run_id'], passed=row['passed'])), flush=True)
    evidence = dict(passed=all(r['passed'] for r in rows), runs=len(rows), results=rows,
        decoded_full_session_jpegs=sum(r['decoded_full_session_jpegs'] for r in rows),
        decoded_measured_jpegs=sum(r['measured']['decoded_jpegs'] for r in rows),
        decoded_mp4_frames=sum(int(r['measured']['video'].get('nb_read_frames', 0)) for r in rows))
    write(root / 'evidence-audit.json', evidence)
    return evidence


def validate(root):
    contract = initialize(root); state = load(root / 'state.json'); result = load(root / 'result.json')
    if state['state'] != 'completed' or len(state['runs']) != contract['maximum_planned_trials']:
        raise ValueError('Incomplete acquisition')
    if not result['completed'] or result['contract_sha256'] != sha(root / 'contract.json'):
        raise ValueError('Unbound analysis')
    check_workspace(contract)
    metadata = {v: verify_bundle(Path(p)) for v, p in contract['bundles'].items()}
    left, right = [metadata[v] for v in contract['variants']]
    changed = [p for p, digest in left['files'].items() if right['files'][p] != digest]
    if changed != ['sp_vision_25/configs/simulator.yaml']:
        raise ValueError('Algorithm ablation changed more than its declared config: ' + repr(changed))
    if left['images'] != right['images']:
        raise ValueError('A/B runtime images differ')
    for tag, image in contract['original_default_images'].items():
        if manage.inspect(tag)['Id'] != image:
            raise ValueError('Default image changed: ' + tag)
    for p in result['trials']:
        folder = manage.OPS / 'experiments' / p['run_id']
        if any(sha(folder / name) != digest for name, digest in p['evidence_hashes'].items()):
            raise ValueError('Raw source evidence changed during normalization')
    historical = load(root / 'historical-score-audit.json')
    for p in historical['runs']:
        folder = manage.OPS / 'experiments' / p['run_id']
        if any(sha(folder / name) != digest for name, digest in p['source_hashes'].items()):
            raise ValueError('Historical source report changed')
    causal = load(root / 'corner-interventions/result.json'); replay = load(root / 'fixed-input/state.json')
    if not causal['completed'] or causal['cases'] != 66 or not replay['completed'] or len(replay['replays']) != 24:
        raise ValueError('Incomplete causal evidence')
    if (sha(root / 'corner-interventions/inputs.jsonl') != causal['inputs_sha256'] or
            sha(root / 'corner-interventions/results.jsonl') != causal['output_sha256']):
        raise ValueError('Causal replay evidence changed')
    for entry in replay['replays']:
        if entry['variant'] == 'v3' and any(entry['baseline_reproduction'][field]['different_frames']
            for field in ('observations', 'filter_state', 'selected_plate_id')):
            raise ValueError('Baseline replay reproduction failed')
    media(root, contract, state)
    # Use the frozen evaluator's existing codecs; do not install packages on the host.
    image = left['images']['evaluator']
    command = ['docker', 'run', '--rm', '--network', 'none', '--cpus', '2',
        '--user', '1000:1000', '-v', str(manage.ROOT) + ':' + str(manage.ROOT) + ':ro',
        '-v', str(root) + ':' + str(root), '-w', str(manage.ROOT), '--entrypoint', 'python3', image,
        '-B', 'docker/auto_aim/decision_benchmark_validate.py', '--audit-only', '--output', str(root)]
    subprocess.run(command, check=True, timeout=300)
    evidence = load(root / 'evidence-audit.json')
    if not evidence['passed']:
        raise ValueError('Raw/session/video audit failed')
    log = (root / 'unit-checks.log').read_text()
    if not log.rstrip().endswith('OK'):
        raise ValueError('Unit checks failed')
    with jobs.connect() as db:
        attempts = [dict(r) for r in db.execute('SELECT a.*,i.job_id FROM attempts a JOIN items i ON i.id=a.item_id')
                    if r['job_id'] in {p['id'] for p in state['jobs']}]
    if len(attempts) != 24 or any(a['verdict'] == 'invalid' for a in attempts):
        raise ValueError('A failed or duplicate acquisition attempt needs explicit review')
    validation = dict(passed=True, frozen_contract_preserved=True, formal_algorithm_unchanged=True,
        original_reports_unchanged=True, all_trials_unified_scoring_version=2,
        maximum_trial_wall_s=result['maximum_trial_wall_s'], within_40s=result['maximum_trial_wall_s'] <= 40,
        live_trials=len(result['trials']), independent_pairs_per_scene=5, separate_repeat_trials=4,
        corner_intervention_cases=66, fixed_input_replays=24,
        fixed_input_frames=sum(p['frames'] for p in replay['replays']),
        historical_trials_rescored=len(historical['runs']),
        unit_checks=int(re.search(r'Ran (\d+) tests', log)[1]), evidence=evidence.copy(),
        scenes=[dict(scene=s['scene'], ranking_transfer=s['ranking_transfer']) for s in result['scenes']],
        backend_repeatability_passed=result['all_backend_repeats_exact'],
        algorithm_ranking_transfer_passed=result['all_ranking_transfers_supported'],
        ui_equivalence_certified=False, capacity_registration=False, human_video_acceptance=False,
        vt=Path('/sys/class/tty/tty0/active').read_text().strip(),
        diagnostic_sources={p.name: sha(p) for p in manage.OPS.glob('decision_benchmark*.py')})
    validation['evidence'].pop('results')
    if not validation['within_40s']:
        raise ValueError('Trial budget exceeded')
    write(root / 'validation.json', validation)
    print(json.dumps(validation, indent=2), flush=True)
    return validation


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--audit-only', action='store_true')
    args = parser.parse_args()
    if args.audit_only:
        evidence_audit(args.output.resolve())
    else:
        validate(args.output.resolve())
