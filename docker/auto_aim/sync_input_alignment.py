"""Predeclared input-history ablation and recorder coverage audit.

Only diagnostic archive views are written. Original trials, runtime, algorithm,
images and measured input bytes are never changed. Commands are not fed back.
"""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import time

from report import load, write
from sync_first_divergence import archive, run_one, sha, STATE_FIELDS
from sync_first_divergence_analysis import primary, truth_centers

ROOT = Path(__file__).resolve().parent
CONDITIONS = ('full', 'last30', 'last15', 'none')


def packets(path):
    return [json.loads(line) for line in path.open()]


def entries(parent):
    state = load(parent/'history-live.json')
    return [(mode, entry) for mode, group in state['modes'].items() for entry in group['runs']]


def view(source, target, keep):
    """Keep the final N premeasurement inputs; suffix JPEGs/headers stay exact."""
    start, rows = archive(source, 6.)
    warm = [p for p, _ in rows if p['data']['stamp_ns'] < start]
    if keep == 'first_last30':
        assert len(warm) > 30
        selected = warm[:1] + warm[-29:]
    elif keep == 'drop_second':
        assert len(warm) > 2
        selected = warm[:1] + warm[2:]
    else:
        selected = warm if keep is None else (warm[-keep:] if keep else [])
    keys = {(p['data']['session'], p['data']['seq'], p['data']['stamp_ns']) for p in selected}
    target.mkdir(parents=True)
    for name in ('manifest.json', 'run-state.json', 'telemetry.jsonl', 'frames.jsonl'):
        (target/name).symlink_to(source/name)
    evidence = target/'session-evidence'
    evidence.mkdir()
    (evidence/'input.mjpg').symlink_to(source/'session-evidence/input.mjpg')
    with (evidence/'events.jsonl').open('w') as output:
        for p in packets(source/'session-evidence/events.jsonl'):
            if p['kind'] not in ('input', 'result'):
                continue
            header = p['data'] if p['kind'] == 'input' else p['data']['frame']
            key = (header['session'], header['seq'], header['stamp_ns'])
            if header['stamp_ns'] >= start or key in keys:
                output.write(json.dumps(p, separators=(',', ':'))+'\n')
    return dict(available_warmup=len(warm), retained_warmup=len(selected),
                start_ns=start, source=str(source))


def measured_records(source, records):
    state = load(source/'run-state.json')
    return [p for p in records if state['measurement_start_stamp_ns'] <= p['frame']['stamp_ns']
            <= state['measurement_end_stamp_ns']]


def input_digest(records):
    digest = hashlib.sha256()
    for p in records:
        digest.update(json.dumps(p['frame'], sort_keys=True, separators=(',', ':')).encode())
        digest.update(p['jpeg_sha256'].encode())
    return digest.hexdigest()


def coverage(parent, output):
    result = []
    for mode, entry in entries(parent):
        source = ROOT/'experiments'/entry['run_id']
        state = load(source/'run-state.json')
        start, end = state['measurement_start_stamp_ns'], state['measurement_end_stamp_ns']
        scored = {p['stamp_ns']: p for p in packets(source/'frames.jsonl') if p['measurement']}
        telemetry = packets(source/'telemetry.jsonl')
        running = next(p for p in telemetry if p['kind'] == 'scenario' and p['data']['phase'] == 1)
        observed = [p for p in telemetry if p['kind'] == 'observation'
                    and start <= p['data']['frame']['stamp_ns'] <= end]
        lost = [p for p in observed if p['data']['frame']['stamp_ns'] not in scored]
        archive_results = [p['data'] for p in packets(source/'session-evidence/events.jsonl')
                           if p['kind'] == 'result' and start <= p['data']['frame']['stamp_ns'] <= end]
        rows = []
        for p in lost:
            data = p['data']
            rows.append(dict(seq=data['frame']['seq'], stamp_ns=data['frame']['stamp_ns'],
                capture_ms=(data['frame']['stamp_ns']-start)*1e-6,
                logged_before_running_status=p['received_ns'] < running['received_ns'],
                log_to_running_wall_ms=(running['received_ns']-p['received_ns'])*1e-6,
                fire_advice=data['result'].get('fire_advice'), fire_forwarded=data.get('fire_forwarded'),
                observation_preserved=True))
        n = len(scored)
        correct = sum(p.get('correct_tracking') is True for p in scored.values())
        result.append(dict(mode=mode, **entry, measured_inputs=len(archive_results),
            observed_inputs=len(observed), scored_frames=n, missing_frames=rows,
            running_status=dict(stamp_ns=running['data']['stamp_ns'],
                scenario_time_s=running['data']['time'], received_ns=running['received_ns']),
            tracking_fraction_existing=correct/n,
            tracking_fraction_if_all_missing_wrong=correct/(n+len(lost)),
            tracking_fraction_if_all_missing_correct=(correct+len(lost))/(n+len(lost)),
            source_hashes={name:sha(source/name) for name in
                ('telemetry.jsonl', 'frames.jsonl', 'run-state.json', 'session-evidence/events.jsonl')}))
    write(output/'coverage.json', result)
    return result


def summary(source, records):
    truth = truth_centers(source)
    measured = {p['stamp_ns']: p for p in packets(source/'frames.jsonl') if p['measurement']}
    traces = []
    for p in records:
        frame, r = p['frame'], p['result']
        scored = measured.get(frame['stamp_ns'])
        if scored is None:
            continue
        target, track = truth(frame['stamp_ns']), r.get('track_estimate')
        error = None
        if target is not None and track is not None:
            error = sum((track['center'][k]+frame['gimbal_position_map'][k]-target[k])**2 for k in (0, 1))**.5
        traces.append(dict(seq=frame['seq'], time_s=scored['time'], center_error_m=error,
            fire_advice=r.get('fire_advice'), selected_plate_id=r.get('selected_plate_id'),
            tracker=r.get('tracker'), yaw=r.get('yaw'), pitch=r.get('pitch')))
    def part(a, b):
        subset = [p for p in traces if a <= p['time_s'] < b]
        errors = [p['center_error_m'] for p in subset if p['center_error_m'] is not None]
        return dict(interval_s=[a, b], frames=len(subset), tracking_frames=sum(p['tracker']=='tracking' for p in subset),
            advice_frames=sum(p['fire_advice'] is True for p in subset),
            median_center_error_m=statistics.median(errors) if errors else None,
            within_05m_fraction=sum(e <= .5 for e in errors)/len(subset) if subset else None)
    return dict(intervals=[part(0, 1), part(1, 2), part(2, 3.2), part(3.2, 3.7), part(3.7, 6)], traces=traces)


def analyze(output):
    state = load(output/'study.json')
    (output/'traces').mkdir(exist_ok=True)
    comparisons = []
    for mode, entry in entries(Path(state['parent'])):
        stem = mode+'_'+entry['scene']+'_'+str(entry['repeat_id'])
        source = ROOT/'experiments'/entry['run_id']
        base = measured_records(source, primary(output/'replays'/str(stem+'_full')))
        base_hash = input_digest(base)
        for condition in CONDITIONS:
            records = measured_records(source, primary(output/'replays'/(stem+'_'+condition)))
            assert input_digest(records) == base_hash, 'Measured headers or JPEGs changed'
            changes = {name:sum(a['result'].get(name) != b['result'].get(name)
                       for a, b in zip(base, records)) for name in STATE_FIELDS}
            first = next((dict(seq=a['frame']['seq'],
                capture_ms=(a['frame']['stamp_ns']-load(source/'run-state.json')['measurement_start_stamp_ns'])*1e-6,
                fields=[name for name in STATE_FIELDS if a['result'].get(name) != b['result'].get(name)])
                for a, b in zip(base, records)
                if any(a['result'].get(name) != b['result'].get(name) for name in STATE_FIELDS)), None)
            effect = summary(source, records)
            write(output/'traces'/(stem+'_'+condition+'.json'), effect['traces'])
            comparisons.append(dict(mode=mode, **entry, condition=condition, measured_frames=len(records),
                measured_input_sha256=base_hash, changed_frames=changes, first_difference=first,
                intervals=effect['intervals']))
    write(output/'warmup-result.json', dict(comparisons=comparisons,
        limitation='Same recorded geometry and control trajectory; advice frames are not shots or hit rates. '
            'Retained warmup includes its native noise and detector history. This isolates total input history, not filter age alone.'))


def run(parent, output):
    output.mkdir(parents=True, exist_ok=True)
    target = output/'study.json'
    state = load(target)
    if not state:
        state = dict(parent=str(parent), bundle=load(parent/'candidate.json')['bundle'],
            plan='All eight completed session-reset archives; full/final30/final15/no warmup. '
                'Exact measured JPEG/header suffix, fresh process, detector-history=session, six seconds. '
                'Repeat both backend full controls. 34 replays total. No score-based selection.',
            conditions=list(CONDITIONS), maximum_replays=34, results=[], created=time.time(),
            truth_in_algorithm=False, live_simulator=False, capacity_registration=False)
        write(target, state)
    coverage(parent, output)
    for mode, entry in entries(parent):
        for condition in CONDITIONS + (('repeat',) if mode == 'lockstep_session' else ()):
            stem = mode+'_'+entry['scene']+'_'+str(entry['repeat_id'])+'_'+condition
            if any(r['case'] == stem for r in state['results']):
                continue
            source = ROOT/'experiments'/entry['run_id']
            keep = dict(full=None, last30=30, last15=15, none=0, repeat=None)[condition]
            source_view = output/'views'/stem
            info = view(source, source_view, keep)
            result = run_one(source_view, output/'replays'/stem, Path(state['bundle']),
                seconds=6., detector_history='session')
            state['results'].append(dict(case=stem, mode=mode, **entry, **info,
                frames=result['frames'], primary_input_sha256=result['primary_input_sha256'],
                differences_from_recorded=result['differences_from_recorded']))
            write(target, state)
    for mode, entry in entries(parent):
        if mode != 'lockstep_session':
            continue
        stem = mode+'_'+entry['scene']+'_'+str(entry['repeat_id'])
        a, b = primary(output/'replays'/(stem+'_full')), primary(output/'replays'/(stem+'_repeat'))
        assert input_digest(a) == input_digest(b)
        assert all(x['result'].get(name) == y['result'].get(name)
            for x, y in zip(a, b) for name in STATE_FIELDS), 'Nonrepeatable replay'
    analyze(output)
    state.update(state='completed', ended=time.time())
    write(target, state)


def confirm(parent, output):
    """Keep the exact original initialization frame and timing extent."""
    target = output/'warmup-confirmation.json'
    state = load(target)
    if not state:
        state = dict(state='planned', parent=str(parent), bundle=load(parent/'candidate.json')['bundle'],
            plan='All eight sources, two conditions: first frame + final29 warmup inputs; '
                'all warmup except second input. Same first frame, last frame, elapsed warmup interval, '
                'and exact measured suffix as full control. Six seconds; session reset; 16 replays. '
                'Backend conditions intentionally identical: 31 available minus second = first+last29.',
            maximum_replays=16, results=[], created=time.time(), capacity_registration=False)
        write(target, state)
    while load(output/'sensor-study.json').get('state') != 'completed':
        if load(output/'sensor-study.json').get('state') == 'failed':
            raise RuntimeError('Live study failed; do not overlap diagnostics')
        time.sleep(1)
    for mode, entry in entries(parent):
        stem = mode+'_'+entry['scene']+'_'+str(entry['repeat_id'])
        source = ROOT/'experiments'/entry['run_id']
        original = primary(output/'replays'/(stem+'_full'))
        measured = measured_records(source, original)
        for condition in ('first_last30', 'drop_second'):
            name = stem+'_'+condition
            if any(r['case'] == name for r in state['results']):
                continue
            folder = output/'views'/name
            info = view(source, folder, condition)
            r = run_one(folder, output/'replays'/name, Path(state['bundle']),
                seconds=6., detector_history='session')
            candidate = primary(output/'replays'/name)
            assert input_digest(original[:1]) == input_digest(candidate[:1])
            assert all(original[0]['result'].get(k) == candidate[0]['result'].get(k) for k in STATE_FIELDS)
            suffix = measured_records(source, candidate)
            assert input_digest(measured) == input_digest(suffix)
            changes = {k:sum(a['result'].get(k) != b['result'].get(k) for a,b in zip(measured,suffix))
                       for k in STATE_FIELDS}
            effect = summary(source, suffix)
            write(output/'traces'/(name+'.json'), effect['traces'])
            state['results'].append(dict(case=name, mode=mode, **entry, **info, measured_frames=len(suffix),
                unchanged_initialization=True, measured_input_sha256=input_digest(suffix),
                changed_frames=changes, intervals=effect['intervals']))
            write(target, state)
    state.update(state='completed', ended=time.time())
    write(target, state)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--parent', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--confirm', action='store_true')
    args = p.parse_args()
    (confirm if args.confirm else run)(args.parent.resolve(), args.output.resolve())
