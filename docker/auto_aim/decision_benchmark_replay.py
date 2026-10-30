"""Corner/pose/initial-selection interventions and fixed-input algorithm replay.

All truth is read after inference. Counterfactual replays never produce hit rates.
"""
import argparse
import copy
import itertools
import json
import math
import os
from pathlib import Path
import statistics
import subprocess

import manage
from decision_benchmark import initialize, sha
from report import load, write
from sync_first_divergence import run_one
from sync_first_divergence_analysis import primary, truth_centers
from sync_timing_alignment import first


def center(result):
    x = result.get('filter_state')
    return [x[i] for i in (0, 2, 4)] if x is not None else None


def centroid(observation):
    return [sum(p[k] for p in observation['corners']) / 4 for k in (0, 1)]


def match_observations(a, b):
    if not a or len(a) != len(b):
        raise ValueError('This paired intervention requires equally many detections')
    def cost(order):
        return sum(math.dist(centroid(x), centroid(b[j])) for x, j in zip(a, order))
    order = min(itertools.permutations(range(len(b))), key=cost)
    paired = [b[j] for j in order]
    if any(math.dist(centroid(x), centroid(y)) > 16 or
           any(x[k] != y[k] for k in ('name', 'color', 'type')) for x, y in zip(a, paired)):
        raise ValueError('Ambiguous image-space correspondence; do not force plate identity')
    return paired, list(order)


def clean(observations):
    return [{k: copy.deepcopy(x[k]) for k in ('name', 'color', 'type', 'corners', 'confidence')}
            for x in observations]


def hybrid(a, b, corners=False, confidence=False, geometry=None):
    out = clean(a)
    for x, y, z in zip(a, b, out):
        if confidence:
            z['confidence'] = y['confidence']
        if corners:
            z['corners'] = copy.deepcopy(y['corners'])
        if geometry:
            ca, cb = centroid(x), centroid(y)
            points = x['corners'] if geometry == 'translation' else y['corners']
            sign = 1 if geometry == 'translation' else -1
            z['corners'] = [[p[k] + sign * (cb[k] - ca[k]) for k in (0, 1)] for p in points]
    return out


def observation_replay(root, contract):
    output = root / 'corner-interventions'
    if (output / 'result.json').exists():
        return load(output / 'result.json')
    output.mkdir(exist_ok=True)
    original = load(Path(contract['parent']) / 'first-cross.json')
    cases, pairs = [], []
    for p in original['causal']:
        rr = [r for r in original['results'] if r['scene'] == p['scene'] and
              r['repeat_id'] == p['repeat_id'] and r['header_mode'] == 'ui']
        ui = next(r for r in rr if r['image_mode'] == 'ui')
        back = next(r for r in rr if r['image_mode'] == 'backend')
        a = ui['result']['observations']
        b, order = match_observations(a, back['result']['observations'])
        source = manage.OPS / 'experiments' / ui['image_run']
        packet, observed, _ = first(source)
        key = p['scene'] + '_' + str(p['repeat_id'])
        definitions = {
            'base': clean(a), 'repeat': clean(a),
            'corners_only': hybrid(a, b, corners=True),
            'confidence_only': hybrid(a, b, confidence=True),
            'both': hybrid(a, b, corners=True, confidence=True),
            'centroid_only': hybrid(a, b, geometry='translation'),
            'shape_only': hybrid(a, b, geometry='shape'),
        }
        for i, (x, y) in enumerate(zip(a, b)):
            for side, detection in (('ui', x), ('backend', y)):
                one = clean([detection]); one[0]['confidence'] = .99
                definitions[f'single_{i}_{side}'] = one
        for name, detections in definitions.items():
            # Only image-derived corners/classes/confidences and own-camera metadata.
            frame = copy.deepcopy(packet['data']); frame.update(session=key + '_' + name, seq=1)
            data = dict(frame=frame, receipt_ns=observed['receipt_ns'], result=dict(observations=detections))
            cases.append(dict(id=key + '_' + name, pair=key, intervention=name,
                              packet=dict(kind='observation', data=data)))
        pairs.append(dict(key=key, scene=p['scene'], repeat_id=p['repeat_id'],
                          ui_run=ui['image_run'], backend_run=back['image_run'],
                          matching_backend_order=order,
                          ui_observations=clean(a), backend_observations=clean(b),
                          original_ui_center=center(ui['result']), original_backend_center=center(back['result']),
                          jpeg_sha256=[ui['jpeg_sha256'], back['jpeg_sha256']]))
    write(output / 'cases.json', [{k: v for k, v in c.items() if k != 'packet'} for c in cases])
    inputs = output / 'inputs.jsonl'
    inputs.write_text(''.join(json.dumps(c['packet']) + '\n' for c in cases))
    bundle = Path(contract['bundles']['v3']); meta = load(bundle / 'bundle.json')
    work = output / 'work'; work.mkdir(exist_ok=True)
    for name in ('assets', 'configs'):
        if not (work / name).exists():
            (work / name).symlink_to('/snapshot/sp_vision_25/' + name)
    command = ['docker', 'run', '--rm', '--network', 'none', '--cpus', '2',
               '--user', f'{os.getuid()}:{os.getgid()}', '-e', 'OMP_NUM_THREADS=1',
               '-e', 'OPENBLAS_NUM_THREADS=1', '-v', str(bundle) + ':/snapshot:ro',
               '-v', str(output) + ':/output', '-w', '/output/work',
               '--entrypoint', '/snapshot/sp_vision_25/build/simulator/sim_auto_aim', meta['images']['algorithm'],
               '--config=/snapshot/sp_vision_25/configs/simulator.yaml',
               '--runtime=/output/work/calibration.yaml', '--algorithm-profile=tracking_fire_v3',
               '--diagnosis=true', '--view=false', '--fire=off', '--replay=/output/inputs.jsonl',
               '--replay-output=/output/results.jsonl']
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    (output / 'algorithm.log').write_text(result.stdout + result.stderr)
    result.check_returncode()
    rows = [json.loads(line) for line in (output / 'results.jsonl').open()]
    if len(rows) != len(cases):
        raise ValueError('Missing intervention output')
    indexed = {case['id']: row for case, row in zip(cases, rows)}
    findings = []
    for p in pairs:
        rr = {c['intervention']: indexed[c['id']] for c in cases if c['pair'] == p['key']}
        cc = {k: center(v) for k, v in rr.items()}
        if any(v is None for v in cc.values()):
            raise ValueError('An intervention failed initialization; retain evidence for inspection')
        if cc['base'] != cc['repeat']:
            raise ValueError('Identical corner replay was not deterministic')
        baseline_error = math.dist(cc['base'], p['original_ui_center'])
        both_error = math.dist(cc['both'], p['original_backend_center'])
        if max(baseline_error, both_error) > 1e-5:
            raise ValueError('Observation-only replay did not reproduce native initialization')
        initial = []
        for name in ('base', 'corners_only', 'confidence_only', 'both'):
            side = 'backend' if name in ('corners_only', 'both') else 'ui'
            distances = [math.dist(cc[name], cc[f'single_{i}_{side}'])
                         for i in range(len(p['ui_observations']))]
            chosen = min(range(len(distances)), key=distances.__getitem__)
            if distances[chosen] > 1e-5:
                raise ValueError('Initial plate could not be matched to a single-observation replay')
            initial.append(dict(case=name, image_plate_index=chosen, center=cc[name]))
        plate_effects = []
        for i, (a, b) in enumerate(zip(p['ui_observations'], p['backend_observations'])):
            ua, ub = rr[f'single_{i}_ui']['filter_state'], rr[f'single_{i}_backend']['filter_state']
            def xyz(x):
                return [x[0] - x[8] * math.cos(x[6]), x[2] - x[8] * math.sin(x[6]), x[4]]
            plate_effects.append(dict(image_plate_index=i,
                maximum_endpoint_delta_px=max(math.dist(x, y) for x, y in zip(a['corners'], b['corners'])),
                centroid_delta_px=math.dist(centroid(a), centroid(b)),
                solved_plate_position_delta_m=math.dist(xyz(ua), xyz(ub)),
                solved_yaw_delta_deg=abs(math.remainder(ub[6] - ua[6], 2 * math.pi)) * 180 / math.pi,
                ui_plate_position=xyz(ua), backend_plate_position=xyz(ub)))
        effects = {name: math.dist(cc['base'], value) for name, value in cc.items()
                   if not name.startswith('single_')}
        interaction = [cc['both'][i] - cc['corners_only'][i] - cc['confidence_only'][i] + cc['base'][i]
                       for i in range(3)]
        findings.append(dict(p, effects_m=effects, initial_plate_selection=initial,
                             per_plate_pose_effects=plate_effects,
                             interaction_vector_m=interaction, interaction_norm_m=math.sqrt(sum(x*x for x in interaction)),
                             native_initialization_reproduction_error_m=[baseline_error, both_error]))
    result = dict(completed=True, cases=len(cases), pairs=findings, identical_replay_exact=True,
                  binary_sha256=sha(bundle / 'sp_vision_25/build/simulator/sim_auto_aim'),
                  inputs_sha256=sha(inputs), output_sha256=sha(output / 'results.jsonl'),
                  truth_in_algorithm=False,
                  scope='Observed-corner/pose/initial-choice causal replay. No flight simulation or new hit rate. '
                        'Centroid-only and shape-only interventions do not imply physically realizable images. '
                        'Image-space matching does not assert ground-truth physical plate identity.')
    write(output / 'result.json', result)
    return result


def fixed_input(root, contract):
    output = root / 'fixed-input'; output.mkdir(exist_ok=True)
    path = output / 'state.json'; state = load(path, dict(completed=False, replays=[]))
    for entry in contract['replay_sources']:
        source = manage.OPS / 'experiments' / entry['run_id']
        for variant in contract['variants']:
            if any(r['run_id'] == source.name and r['variant'] == variant for r in state['replays']):
                continue
            case = output / (source.name + '_' + variant)
            replay = load(case / 'result.json') if (case / 'result.json').exists() else run_one(
                source, case, Path(contract['bundles'][variant]), seconds=2.,
                detector_history='session', diagnosis=True)
            records = primary(case)
            # Independent truth is accessed only after the frozen algorithm has exited.
            truth = truth_centers(source)
            measured = load(source / 'run-state.json')['measurement_start_stamp_ns']
            errors = []; measured_errors = []; valid = 0; measured_frames = 0
            trace = []
            for p in records:
                frame, r = p['frame'], p['result']
                gt = truth(frame['stamp_ns']); estimate = center(r)
                if frame['stamp_ns'] >= measured:
                    measured_frames += 1
                if gt is None or estimate is None:
                    trace.append(dict(stamp_ns=frame['stamp_ns'], center_error_m=None)); continue
                xy = [estimate[k] + frame['gimbal_position_map'][k] for k in (0, 1)]
                error = math.dist(xy, gt[:2]); errors.append(error)
                if frame['stamp_ns'] >= measured:
                    valid += 1; measured_errors.append(error)
                trace.append(dict(stamp_ns=frame['stamp_ns'], center_error_m=error, tracker=r['tracker']))
            if variant == 'v3' and any(replay['differences_from_recorded'][field]['different_frames']
                                       for field in ('observations', 'filter_state', 'selected_plate_id')):
                raise ValueError('The unmodified fixed-input baseline was not reproduced')
            write(case / 'posthoc-errors.json', trace)
            state['replays'].append(dict(run_id=source.name, variant=variant, scene=entry['scene'],
                repeat_id=entry['repeat_id'], source_mode=load(source / 'manifest.json')['execution']['mode'],
                frames=len(records), measured_frames=measured_frames, measured_valid=valid,
                source_jpeg_sha256=replay['primary_input_sha256'],
                measured_center_error_p50_m=statistics.median(measured_errors) if measured_errors else None,
                measured_center_error_p95_m=sorted(measured_errors)[math.ceil(.95*len(measured_errors))-1] if measured_errors else None,
                measured_correct_center_fraction=sum(x <= .5 for x in measured_errors) / measured_frames if measured_frames else None,
                baseline_reproduction=replay['differences_from_recorded'] if variant == 'v3' else None,
                wall_s=replay['wall_s'], replay=str(case)))
            write(path, state)
            print(json.dumps(dict(fixed_input_completed=len(state['replays']), run_id=source.name,
                                  variant=variant)), flush=True)
    for entry in contract['replay_sources']:
        pair = [r for r in state['replays'] if r['run_id'] == entry['run_id']]
        if len(pair) != 2 or pair[0]['source_jpeg_sha256'] != pair[1]['source_jpeg_sha256']:
            raise ValueError('Algorithm A/B did not receive exactly the same fixed inputs')
    state.update(completed=True, truth_in_algorithm=False,
                 scope='Fixed recorded camera/control trajectory, warmup plus first two measurement seconds. '
                       'Offline center-estimation diagnostic; no counterfactual hit rate or feedback.')
    write(path, state)
    return state


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('corners', 'fixed-input', 'all'))
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args(); root = args.output.resolve(); contract = initialize(root)
    if args.action in ('corners', 'all'):
        result = observation_replay(root, contract)
        print(json.dumps(dict(intervention_cases=result['cases'], identical_replay_exact=True)), flush=True)
    if args.action in ('fixed-input', 'all'):
        fixed_input(root, contract)
