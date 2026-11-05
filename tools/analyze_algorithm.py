#!/usr/bin/env python3
"""Read sealed suite evidence; write diagnostic tables outside the source runs."""
import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'docker/auto_aim'))


def load(path):
    return json.loads(path.read_text())


def lines(path):
    with path.open() as stream:
        for line in stream:
            yield json.loads(line)


def stage(result):
    """First observed blocking stage, not causal attribution."""
    observations = result.get('observations')
    if observations is None:
        return 'unknown_detection'
    if not observations:
        return 'no_detection'
    if all(o.get('pose_valid') is False for o in observations):
        return 'pose_invalid'
    if result.get('tracker') is None:
        return 'unknown_tracking'
    if result['tracker'] != 'tracking':
        return 'not_tracking'
    if result.get('valid') is not True:
        return 'aim_invalid' if result.get('valid') is False else 'unknown_aim'
    check = result.get('fire_check') or {}
    for key, reason in [('valid', 'fire_measurement_or_target'), ('pose_valid', 'fire_pose'),
                        ('aligned', 'fire_alignment')]:
        if type(check.get(key)) is not bool:
            return 'unknown_fire_check'
        if not check[key]:
            return reason
    if result.get('fire_advice') is True:
        return 'advice'
    if result.get('fire_advice') is False:
        return 'stability_or_other'
    return 'unknown_advice'


def analyze_run(folder, result, destination):
    state = load(folder / 'run-state.json')
    manifest = load(folder / 'manifest.json')
    start, end = state['measurement_start_stamp_ns'], state['measurement_end_stamp_ns']
    frames = {f['stamp_ns']: f for f in lines(folder / 'frames.jsonl')
              if f.get('measurement') and start <= f['stamp_ns'] < end}
    if not frames:
        raise ValueError('No measurement frames: ' + folder.name)
    rows, seen = [], set()
    for packet in lines(folder / 'telemetry.jsonl'):
        if packet['kind'] != 'observation':
            continue
        obs = packet['data']; frame = obs['frame']; stamp = frame['stamp_ns']
        if stamp not in frames:
            continue
        if stamp in seen:
            raise ValueError('Duplicate measurement result: ' + str(stamp))
        seen.add(stamp)
        r = obs['result']; f = frames[stamp]; check = r.get('fire_check') or {}
        row = dict(stamp_ns=stamp, seq=frame['seq'], t_s=(stamp-start)/1e9,
                   stage=stage(r), detections=len(r.get('observations') or []),
                   pose_valid_count=sum(o.get('pose_valid') is True for o in r.get('observations') or []),
                   tracker=r.get('tracker'), aim_valid=r.get('valid'), fire_advice=r.get('fire_advice'),
                   command_emitted=obs.get('command_emitted'), fire_forwarded=obs.get('fire_forwarded'),
                   selected_plate_id=r.get('selected_plate_id'), track_generation=r.get('track_generation'))
        row.update({k: check.get(k) for k in ('valid', 'pose_valid', 'aligned', 'eligible',
                                             'stable_time_s', 'discontinuity', 'plate_changed',
                                             'yaw_error_rad', 'yaw_tolerance_rad')})
        row.update({k: f.get(k) for k in ('correct_tracking', 'detection_matches', 'visible_plates',
                                         'truth_available',
                                         'target_speed_mps', 'target_yaw_rate',
                                         'selected_plate_position_error_m', 'yaw_execution_error_deg')})
        continuity = r.get('continuity') or {}
        updates = (continuity.get('filter') or {}).get('updates') or []
        row.update(reset_reason=continuity.get('reset_reason'),
                   trusted_update_age_s=continuity.get('trusted_update_age_s'),
                   update_reasons=[u.get('reason') for u in updates],
                   association_reasons=[a.get('reason') for a in continuity.get('associations') or []])
        rows.append(row)
    if set(frames) != seen:
        raise ValueError('Missing observation records: ' + folder.name)
    destination.write_text(''.join(json.dumps(r, allow_nan=False) + '\n' for r in rows))
    duration = manifest['duration_s']; m = result['metrics']; n = len(rows)
    summary = dict(run_id=folder.name, scene=manifest['scene'], sensor_seed=manifest['sensor_seed'],
                   frames=n, duration_s=duration, shots=m['shots_fired'], hits=m['target_armor_hits'],
                   exact=m['target_armor_hits']/m['shots_fired'] if m['shots_fired'] else None,
                   weighted=m['weighted_hit_rate'], weighted_points_per_s=m['weighted_points_per_s'],
                   shots_per_s=m['shots_fired']/duration, hits_per_s=m['hits_per_s'],
                   stage_counts=dict(Counter(r['stage'] for r in rows)),
                   overlapping_fire_blocks={k: dict(false=sum(r[k] is False for r in rows),
                                                       unknown=sum(r[k] is None for r in rows))
                                            for k in ('valid', 'pose_valid', 'aligned', 'eligible')},
                   flow_frames=dict(detection=sum(r['detections']>0 for r in rows),
                                    visible_plate=sum((r['visible_plates'] or 0)>0 for r in rows),
                                    no_detection_visible=sum(r['detections']==0 and (r['visible_plates'] or 0)>0 for r in rows),
                                    valid_pose=sum(r['pose_valid_count']>0 for r in rows),
                                    tracking=sum(r['tracker']=='tracking' for r in rows),
                                    aim=sum(r['aim_valid'] is True for r in rows),
                                    advice=sum(r['fire_advice'] is True for r in rows),
                                    forwarded=sum(r['fire_forwarded'] is True for r in rows),
                                    discontinuity=sum(r['discontinuity'] is True for r in rows)),
                   difficulty=result.get('difficulty', {}))
    summary['tracker_states'] = dict(Counter(r['tracker'] or 'unknown' for r in rows))
    summary['reset_reasons'] = dict(Counter(r['reset_reason'] for r in rows if r['reset_reason']))
    summary['update_reasons'] = dict(Counter(v for r in rows for v in r['update_reasons'] if v))
    summary['association_reasons'] = dict(Counter(v for r in rows for v in r['association_reasons'] if v))
    plan_path = folder/'configuration/scenario-plan.json'
    plan = load(plan_path) if plan_path.exists() else {}
    summary['phase_frames'] = []
    for phase in plan.get('phases', []):
        subset = [r for r in rows if phase['t0'] <= r['t_s'] < phase['t1']]
        summary['phase_frames'].append(dict(name=phase['name'], t0=phase['t0'], t1=phase['t1'],
                                           frames=len(subset), stage_counts=dict(Counter(r['stage'] for r in subset))))
    assert sum(summary['stage_counts'].values()) == n
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--repeat', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = load(args.suite / 'results.json'); repeated = load(args.repeat / 'results.json')
    for result in (source, repeated):
        if not result['complete'] or not result['all_evidence_valid']:
            raise ValueError('Only complete, evidence-valid suites can support this diagnostic')
    a, b = load(args.suite/'contract.json'), load(args.repeat/'contract.json')
    for key in ('algorithm', 'environment', 'renderer', 'payload_identity'):
        if a[key] != b[key]:
            raise ValueError('Repeated trial differs: ' + key)
    folders = ROOT / 'docker/auto_aim/experiments'
    output = args.output.resolve()
    if output.is_relative_to(folders.resolve()):
        raise ValueError('Diagnostics must be outside sealed evidence')
    output.mkdir(parents=True, exist_ok=False)
    rows = [analyze_run(folders/r['run_id'], r, output/(r['run_id']+'.frames.jsonl')) for r in source['runs']]
    from benchmark.diagnostics import relative_events, relative_shots
    comparisons = []
    for r in repeated['runs']:
        ref = next(x for x in source['runs'] if (x['scene'],x['sensor_seed']) == (r['scene'],r['sensor_seed']))
        left, right = folders/ref['run_id'], folders/r['run_id']
        le, re = relative_events(left), relative_events(right)
        first = next((i for i,(x,y) in enumerate(zip(le,re)) if x != y), None)
        if first is None and len(le) != len(re): first = min(len(le),len(re))
        lh = [x['jpeg'] for x in le if x['kind']=='input']; rh = [x['jpeg'] for x in re if x['kind']=='input']
        def changed_paths(x, y, prefix=''):
            if isinstance(x, dict) and isinstance(y, dict):
                return [p for key in sorted(set(x)|set(y))
                        for p in changed_paths(x.get(key), y.get(key), prefix+'.'+key)]
            if isinstance(x, list) and isinstance(y, list):
                if len(x)!=len(y):return [prefix+'.length']
                return [p for i,(xx,yy) in enumerate(zip(x,y)) for p in changed_paths(xx,yy,prefix+f'[{i}]')]
            return [] if x==y else [prefix]
        paths = changed_paths(le[first], re[first]) if first is not None and first<min(len(le),len(re)) else []
        comparisons.append(dict(scene=r['scene'], seed=r['sensor_seed'], reference=ref['run_id'],
                                repeat=r['run_id'], event_counts=[len(le),len(re)], first_event_difference=first,
                                first_changed_fields=paths,
                                identical_relative_events=le==re, identical_jpegs=lh==rh,
                                identical_relative_shots=relative_shots(left)==relative_shots(right),
                                reference_metrics=ref['metrics'], repeat_metrics=r['metrics']))
    selected = [min((r for r in rows if r['scene']==scene), key=lambda r:(r['weighted_points_per_s'] or 0,r['sensor_seed']))
                for scene in sorted({r['scene'] for r in rows if r['scene'].startswith('basic_move')})]
    data = dict(runs=rows, repeated_trials=comparisons, selected_runs=[r['run_id'] for r in selected],
                limitations=['Stage counts are ordered observations, not causal ablations.',
                             'Frame advice/forwarding counts are not shot counts.',
                             'Sensor seeds vary noise, not motion trajectories.',
                             'A single repeat does not establish statistical stability.'])
    (output/'diagnosis.json').write_text(json.dumps(data, indent=2, allow_nan=False)+'\n')
    fields = ['scene','sensor_seed','run_id','frames','shots','hits','exact','weighted','shots_per_s','hits_per_s','weighted_points_per_s']
    with (output/'trials.csv').open('w') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore'); writer.writeheader(); writer.writerows(rows)
    report = ['# B1 / E1 多种子算法诊断', '',
              '固定原始 B1/E1 冻结包和 NVIDIA 后端。阶段计数是观察归类，不代表已证明的因果关系。', '',
              '|场景|轮次|命中/发射|精确命中率|发射/秒|命中/秒|加权得分/秒|',
              '|---|---:|---:|---:|---:|---:|---:|']
    for scene in sorted({r['scene'] for r in rows}):
        group = [r for r in rows if r['scene']==scene]
        shots, hits = sum(r['shots'] for r in group), sum(r['hits'] for r in group)
        seconds = sum(r['duration_s'] for r in group)
        weighted = sum((r['weighted_points_per_s'] or 0)*r['duration_s'] for r in group)
        exact = f'{hits/shots:.1%}' if shots else 'N/A'
        report.append(f'|{scene}|{len(group)}|{hits}/{shots}|{exact}|{shots/seconds:.3f}|{hits/seconds:.3f}|{weighted/seconds:.3f}|')
    report += ['', '## 计分阶段的观察', '',
               '各列按计分帧独立计数，并非单调递减漏斗；角度对准等 fire_check 条件可以重叠。', '',
               '|场景|计分帧|无检测|其中存在可见装甲|未跟踪阶段|未对准阶段|建议帧|',
               '|---|---:|---:|---:|---:|---:|---:|']
    for scene in sorted({r['scene'] for r in rows}):
        group = [r for r in rows if r['scene']==scene]
        count = lambda key: sum(r['stage_counts'].get(key,0) for r in group)
        report.append(f"|{scene}|{sum(r['frames'] for r in group)}|{count('no_detection')}|{sum(r['flow_frames']['no_detection_visible'] for r in group)}|{count('not_tracking')}|{count('fire_alignment')}|{sum(r['flow_frames']['advice'] for r in group)}|")
    report += ['', '## 同种子重复', '']
    for c in comparisons:
        report.append(f"- {c['scene']} / {c['seed']}：JPEG 序列相同={c['identical_jpegs']}，归一化事件相同={c['identical_relative_events']}，归一化逐发记录相同={c['identical_relative_shots']}；首个事件差异={c['first_event_difference']}。")
    report += ['', '## 逐帧复核样本', '', '每个移动旋转场景按加权得分/秒最低选择，同分取较小 seed。', '']
    report += [f"- {r['scene']} / {r['sensor_seed']}：`{r['run_id']}`。" for r in selected]
    report += ['', '逐轮数值见 trials.csv，逐帧记录与重复对照细节见 diagnosis.json 和各 *.frames.jsonl。',
               '零发射保持 N/A；传感器种子只改变噪声，不代表新路线。单次重复不能证明统计稳定性。',
               '实车时延与标定仍未认证；仿真分数不能代替实车性能。', '']
    (output/'report.md').write_text('\n'.join(report))
    print(json.dumps(dict(runs=len(rows), repeated=len(comparisons), selected=data['selected_runs']), indent=2))


if __name__ == '__main__':
    main()
