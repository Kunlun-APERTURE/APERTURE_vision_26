#!/usr/bin/env python3
"""Audit the frozen continuity matrix without changing evidence or baselines."""
import argparse
import collections
import hashlib
import html
import json
from pathlib import Path

from continuity_replay import prepare,metrics,aggregate,q,steady,load,write

OPS=Path(__file__).resolve().parent
SCENES=('move_ref','line','ego_sweep','ego_sweep_reverse','spin')
MOVING=('line','ego_sweep','ego_sweep_reverse')


def steady_seconds(scene,duration):
    # Midpoint integration uses the same frozen intervals as the frame scorer.
    n=round(duration*1000)
    return sum(steady(scene,(i+.5)/1000) for i in range(n))/1000


def audit_run(folder,expected):
    manifest=load(folder/'manifest.json');summary=load(folder/'summary.json')
    if summary['verdict']=='invalid' or not all(r['passed'] for r in summary['rules'] if r['category'] in ('validity','safety','performance')):
        raise RuntimeError('Invalid evidence: '+folder.name)
    if manifest['algorithm_binary_sha256']!=expected['algorithm_binary_sha256']:
        raise RuntimeError('Algorithm binary changed: '+folder.name)
    if manifest['containers']['simulator_image_id']!=expected['simulator_image_id']:
        raise RuntimeError('Simulator changed: '+folder.name)
    if manifest['configuration_sha256']['algorithm.yaml']!=expected['algorithm_yaml_sha256']:
        raise RuntimeError('Algorithm config changed: '+folder.name)
    for name,digest in load(folder/'evidence-sha256.json').items():
        if hashlib.sha256((folder/name).read_bytes()).hexdigest()!=digest:
            raise RuntimeError('Evidence hash mismatch: '+str(folder/name))
    _,context=prepare(folder)
    rows=[];front_id=None
    for line in (folder/'telemetry.jsonl').open():
        packet=json.loads(line);data=packet['data']
        if packet['kind']=='observation':
            row=dict(data['result']);row['stamp_ns']=data['frame']['stamp_ns'];rows.append(row)
        elif packet['kind']=='armors' and front_id is None:
            front_id=next((a['id'] for a in data['armors'] if a['robot_id']==1 and a['slot']==0),None)
    m=metrics(rows,context)
    # The live evaluator additionally checks blue-three identity and visibility.
    m['correct_tracking_fraction']=summary['metrics']['correct_tracking_fraction']
    m['coverage_frames']=summary['counts']['visible_frames']
    shots=[json.loads(line) for line in (folder/'shots.jsonl').open()]
    phase_shots=[s for s in shots if steady(manifest['scene'],s['fire_time_s'])]
    front=lambda s:s['outcome']=='target_armor' and s['armor_id']==front_id
    first_hits=[]
    for event in m['recovery_events']:
        # Use contact time: a request alone does not establish a useful hit.
        times=[(s['terminal_stamp_ns']-event['stamp_ns'])*1e-9 for s in shots
            if front(s) and s['fire_stamp_ns']>=event['stamp_ns'] and s.get('terminal_stamp_ns') is not None]
        delay=min((v for v in times if 0<=v<=2),default=None)
        first_hits.append(dict(stamp_ns=event['stamp_ns'],first_front_hit_s=delay,censored_at_s=2 if delay is None else None))
    seconds=steady_seconds(manifest['scene'],manifest['duration_s'])
    m.update(run_id=folder.name,scene=manifest['scene'],physics=manifest['shooting']['physics'],
        algorithm_profile=manifest['algorithm_profile'],shots=len(shots),target_hits=sum(s['outcome']=='target_armor' for s in shots),
        front_hits=sum(front(s) for s in shots),steady_shots=len(phase_shots),steady_front_hits=sum(front(s) for s in phase_shots),
        steady_seconds=seconds,first_front_hit_events=first_hits,total_wall_s=summary['metrics']['total_wall_s'])
    return m


def group_runs(rows):
    grouped=collections.defaultdict(list)
    for r in rows:grouped[(r['algorithm_profile'],r['scene'],r['physics'])].append(r)
    result={}
    for key,runs in grouped.items():
        a=aggregate(runs);seconds=sum(r['steady_seconds'] for r in runs)
        frames=sum(r['coverage_frames'] for r in runs)
        a['correct_tracking_fraction']=sum(r['correct_tracking_fraction']*r['coverage_frames'] for r in runs)/frames
        for field in ('shots','target_hits','front_hits','steady_shots','steady_front_hits'):
            a[field]=sum(r[field] for r in runs)
        a['steady_seconds']=seconds;a['jump_hz']=a['steady_angle_gt_1deg_frames']/seconds
        a['steady_front_hit_rate']=a['steady_front_hits']/a['steady_shots'] if a['steady_shots'] else None
        a['steady_front_hits_per_s']=a['steady_front_hits']/seconds
        a['front_hit_rate']=a['front_hits']/a['shots'] if a['shots'] else None
        a['target_hit_rate']=a['target_hits']/a['shots'] if a['shots'] else None
        events=[e for r in runs for e in r['first_front_hit_events']]
        a['first_front_hit_p90_s']=q([e['first_front_hit_s'] if e['first_front_hit_s'] is not None else 2 for e in events],.9)
        a['first_front_hit_failures']=sum(e['first_front_hit_s'] is None for e in events)
        a['runs']=[r['run_id'] for r in runs]
        result['/'.join(key)]=a
    return result


def compare(batch,output,tracking=None):
    data=load(batch);rows=[audit_run(OPS/'experiments'/r['run_id'],data) for r in data['runs']]
    expected={(p,s,physics,n) for p in data['profiles'] for s in SCENES for physics in ('no-drag','drag') for n in range(1,4)}
    actual=[(r['algorithm_profile'],r['scene'],r['shooting']['physics'],r['repeat']) for r in data['runs']]
    complete=data.get('complete',False) and len(actual)==len(expected) and set(actual)==expected
    groups=group_runs(rows);checks=[dict(name='complete_frozen_matrix',passed=complete,actual=len(actual),expected=len(expected))]
    def check(name,actual,limit,op):
        passed=actual is not None and (actual<=limit if op=='<=' else actual>=limit)
        checks.append(dict(name=name,passed=passed,actual=actual,comparison=op,limit=limit))
    phase=data['phase']
    if phase=='tracking':
        checks.append(dict(name='offline_parameter_selection',passed=data.get('offline_selection_passed',False) and not data.get('diagnostic_only',False)))
        for scene in MOVING:
            for physics in ('no-drag','drag'):
                prefix=scene+'/'+physics;a=groups.get('legacy/'+prefix);b=groups.get('tracking_v2/'+prefix)
                if not a or not b:continue
                check(prefix+'/jump_hz',b['jump_hz'],a['jump_hz']*.5,'<=')
                check(prefix+'/coverage',b['correct_tracking_fraction'],a['correct_tracking_fraction']-.02,'>=')
                check(prefix+'/velocity_recovery_p90',b['recovery_p90_s'],.3,'<=')
                if a['recovery_p90_s'] is not None:check(prefix+'/recovery_regression',b['recovery_p90_s'],a['recovery_p90_s']+.05,'<=')
    else:
        if tracking is None:raise RuntimeError('Fire audit requires the fresh tracking acceptance report')
        previous=load(tracking)
        checks.append(dict(name='tracking_accepted_same_binary',passed=previous['passed'] and previous['algorithm_binary_sha256']==data['algorithm_binary_sha256']))
        for physics in ('no-drag','drag'):
            for scene in SCENES:
                prefix=scene+'/'+physics;b=groups.get('tracking_fire_v2/'+prefix);a=previous['groups'].get('legacy/'+prefix)
                if not a or not b:continue
                if scene in MOVING:
                    check(prefix+'/steady_front_hit_rate',b['steady_front_hit_rate'],.8,'>=')
                    check(prefix+'/useful_hits_per_s',b['steady_front_hits_per_s'],a['steady_front_hits_per_s'],'>=')
                    check(prefix+'/first_front_hit_p90',b['first_front_hit_p90_s'],.8,'<=')
                elif scene=='move_ref':check(prefix+'/static_front_hit_rate',b['front_hit_rate'],.95,'>=')
                else:check(prefix+'/spin_target_hit_rate',b['target_hit_rate'],a['target_hit_rate']-.05,'>=')
    report=dict(phase=phase,passed=all(c['passed'] for c in checks),checks=checks,groups=groups,runs=rows,
        quantile_method='nearest_rank',comparison_scorer_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        replay_metrics_sha256=hashlib.sha256((OPS/'continuity_replay.py').read_bytes()).hexdigest(),
        invalid_attempts=data.get('invalid_attempts',[]),
        algorithm_binary_sha256=data['algorithm_binary_sha256'],simulator_image_id=data['simulator_image_id'],
        algorithm_yaml_sha256=data['algorithm_yaml_sha256'],batch=str(batch),visual_review='pending',baseline_promoted=False,
        limitations=['Ground truth is used only by this offline scorer.',
        'Plate CV velocity includes plate rotation; whole-car center velocity is approximate in tracking_v2.',
        'First useful hit uses impact time from a post-reversal launch; absent hits are censored at 2 s.',
        'Numerical gates do not provide a manual visual pass.'])
    write(output,report)
    lines=['# 连续跟踪与开火对照验收','',f"阶段：{phase}；规则结果：{'通过' if report['passed'] else '未通过'}；人工视觉验收：待确认。",'',
        '| 条件 | 跳变/秒 | 有效跟踪 | 速度恢复 P90/s | 稳态正面命中 | 正面命中/秒 |',
        '|---|---:|---:|---:|---:|---:|']
    def fmt(v):return '—' if v is None else f'{v:.3f}'
    for key,g in groups.items():lines.append(f"| {key} | {fmt(g['jump_hz'])} | {fmt(g['correct_tracking_fraction'])} | {fmt(g['recovery_p90_s'])} | {g['steady_front_hits']}/{g['steady_shots']} | {fmt(g['steady_front_hits_per_s'])} |")
    lines.extend(['','## 未通过的规则',''])
    lines.extend('- '+c['name']+': '+str(c.get('actual')) for c in checks if not c['passed'])
    output.with_suffix('.md').write_text('\n'.join(lines)+'\n')
    output.with_suffix('.html').write_text('<meta charset="utf-8"><pre>'+html.escape('\n'.join(lines))+'</pre>')
    print(json.dumps(dict(phase=phase,passed=report['passed'],failed=[c for c in checks if not c['passed']]),ensure_ascii=False),flush=True)
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--batch',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--tracking',type=Path)
    a=p.parse_args();compare(a.batch,a.output,a.tracking)


if __name__=='__main__':main()
