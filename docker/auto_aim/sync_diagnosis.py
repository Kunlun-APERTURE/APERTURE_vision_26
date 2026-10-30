#!/usr/bin/env python3
"""Read-only phase attribution and bounded, frozen lockstep diagnostics.

Truth is used after a trial finishes. It never changes algorithm inputs or
selects a timing profile. Diagnostic batches do not grant concurrency capacity.
"""
import argparse
import bisect
import fcntl
from collections import Counter,defaultdict
from datetime import datetime,timezone
import hashlib
import html
import json
import math
from pathlib import Path
import statistics
import sys
import time
import uuid

import background_jobs as jobs
import experiments
import manage
from report import load,write
from sync_study import background,bootstrap,row

FOCUS=('hf_uniform_3','hf_turn90_3','hf_static_8')


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rows(path):
    with path.open() as stream:
        for line in stream:yield json.loads(line)


def stats(values):
    v=sorted(x for x in values if x is not None and math.isfinite(x))
    return dict(n=len(v),mean=statistics.mean(v) if v else None,
        p50=v[len(v)//2] if v else None,p95=v[min(len(v)-1,int(len(v)*.95))] if v else None)


def phase(scene,t,duration):
    if not 0<=t<=duration:return 'outside'
    if scene=='hf_static_8':return 'static'
    if t<1:return 'stationary'
    if t<2:return 'acceleration'
    if scene=='hf_turn90_3':
        if t<3.2:return 'straight'
        if t<3.7:return 'turn'
        return 'after_turn'
    return 'straight' if t<4 else 'late_straight'


def rate(n,d):return n/d if d else None


def analyze_run(folder,entry,mode,output,scored_folder=None):
    manifest=load(folder/'manifest.json');state=load(folder/'run-state.json')
    start=state['measurement_start_stamp_ns'];duration=manifest['duration_s'];scene=entry['scene']
    frames=[r for r in rows(folder/'frames.jsonl') if r.get('measurement') and 0<=r['time']<=duration]
    score_root=scored_folder or folder
    shots=list(rows(score_root/'shots.jsonl'))
    observations=[];traces=[]
    for packet in rows(folder/'telemetry.jsonl'):
        if packet['kind']=='observation':observations.append(packet['data'])
        if packet['kind']=='gimbal_trace':traces.append(packet['data'])
    observed={d['frame']['stamp_ns']:d for d in observations}
    pulses=sorted((d['receipt_ns'],d) for d in observations if d.get('fire_forwarded'))
    pulse_times=[p[0] for p in pulses]
    frame_rows=[];shot_rows=[];groups=defaultdict(lambda:dict(frames=[],shots=[]))
    for f in frames:
        d=observed.get(f['stamp_ns'],{});header=d.get('frame',{});r=d.get('result',{})
        fire=r.get('fire_check') or {};track=r.get('track_estimate') or {}
        gate_reasons=[]
        if not r.get('valid'):gate_reasons.append('invalid_solution')
        for name in ('aligned','pose_valid','target_face_valid','valid'):
            if fire.get(name) is False:gate_reasons.append(name)
        if fire.get('qualification_held'):gate_reasons.append('qualification_held')
        if fire.get('discontinuity'):gate_reasons.append('discontinuity')
        item=dict(mode=mode,run_id=folder.name,repeat_id=entry['repeat_id'],scene=scene,
            time_s=f['time'],phase=phase(scene,f['time'],duration),stamp_ns=f['stamp_ns'],
            correct_tracking=f.get('correct_tracking'),tracker=r.get('tracker'),track_present=bool(track),
            center_xy_error_m=f.get('center_xy_error_m'),selected_plate_error_m=f.get('selected_plate_position_error_m'),
            plate_position_error_m=f.get('plate_position_error_m'),target_speed_mps=f.get('target_speed_mps'),
            capture_to_result_ms=(d['receipt_ns']-f['stamp_ns'])*1e-6 if 'receipt_ns' in d else None,
            capture_to_send_ms=(header['sent_ns']-f['stamp_ns'])*1e-6 if 'sent_ns' in header else None,
            pipeline_wall_ms=r.get('pipeline_ms'),fire_advice=r.get('fire_advice',False),
            fire_forwarded=d.get('fire_forwarded',False),gate_reasons=gate_reasons,
            pose_age_s=fire.get('pose_age_s'),prediction_horizon_s=r.get('prediction_horizon_s'),
            selected_plate=r.get('selected_plate_id'),video_index=f.get('video_index'))
        frame_rows.append(item);groups[item['phase']]['frames'].append(item)
    for shot in shots:
        t=(shot['fire_stamp_ns']-start)*1e-9
        if not 0<=t<=duration:continue
        i=bisect.bisect_right(pulse_times,shot['fire_stamp_ns'])-1
        observation=pulses[i][1] if i>=0 and shot['fire_stamp_ns']-pulse_times[i]<=100_000_000 else None
        item=dict(mode=mode,run_id=folder.name,repeat_id=entry['repeat_id'],scene=scene,
            time_s=t,phase=phase(scene,t,duration),shot_seq=shot['shot_seq'],
            hit=shot['outcome']=='target_armor',weight=shot.get('weighted_hit_score') or 0.,
            outcome=shot['outcome'],armor_id=shot.get('armor_id'),
            impact_x_fraction=shot.get('impact_x_fraction'),impact_y_fraction=shot.get('impact_y_fraction'),
            pulse_matched=observation is not None,
            pulse_to_launch_ms=(shot['fire_stamp_ns']-observation['receipt_ns'])*1e-6 if observation else None,
            capture_to_launch_ms=(shot['fire_stamp_ns']-observation['frame']['stamp_ns'])*1e-6 if observation else None)
        shot_rows.append(item);groups[item['phase']]['shots'].append(item)
    begin=load(folder/'begin.json')['bridge_status'];last=begin.get('last_result') or {}
    all_measured=[o for o in observations if start<=o['frame']['stamp_ns']<=start+round(duration*1e9)]
    stamps=[o['frame']['stamp_ns'] for o in all_measured]
    signature=dict(measurement_start_stamp_ns=start,warm_last_stamp_ns=last.get('stamp_ns'),
        warm_last_seq=last.get('seq'),first_image_delay_ns=stamps[0]-start if stamps else None)
    images=hashlib.sha256()
    with (folder/'input.mjpg').open('rb') as source:
        for frame in frames:
            source.seek(frame['jpeg_offset']);data=source.read(frame['jpeg_bytes'])
            if len(data)!=frame['jpeg_bytes']:raise ValueError('Incomplete measured JPEG: '+folder.name)
            images.update(data)
    summary=load(score_root/'rescore.json',load(folder/'summary.json'))
    result=dict(mode=mode,scene=scene,run_id=folder.name,repeat_id=entry['repeat_id'],seed=entry.get('seed'),
        startup=signature,metrics=summary['metrics'],duration_s=duration,
        sample_interval_ms=stats([(b-a)*1e-6 for a,b in zip(stamps,stamps[1:])]),
        measured_image_sha256=images.hexdigest(),
        measured_frame_count=len(frame_rows),phase_history=(load(folder/'synchronization.json',{}) or {}).get('phase_history',[]),
        begin_selected_plate=last.get('selected_plate_id'),begin_fire_check=last.get('fire_check'),
        source_sha256={name:digest(folder/name) for name in ('manifest.json','run-state.json','frames.jsonl','telemetry.jsonl','begin.json')})
    return result,frame_rows,shot_rows


def aggregate(frames,shots):
    return dict(frames=len(frames),shots=len(shots),hits=sum(r['hit'] for r in shots),
        target_hit_rate=rate(sum(r['hit'] for r in shots),len(shots)),
        weighted_hit_rate=rate(sum(r['weight'] for r in shots),len(shots)),
        correct_tracking_fraction=rate(sum(r['correct_tracking'] is True for r in frames),len(frames)),
        tracker_tracking_fraction=rate(sum(r['tracker']=='tracking' for r in frames),len(frames)),
        center_over_05m= sum(r['center_xy_error_m'] is not None and r['center_xy_error_m']>.5 for r in frames),
        fire_advice_frames=sum(r['fire_advice'] for r in frames),
        fire_forwarded_frames=sum(r['fire_forwarded'] for r in frames),
        shot_armor_counts=dict(Counter(str(r['armor_id']) for r in shots if r['hit'])),
        gate_reasons=dict(Counter(k for r in frames for k in r['gate_reasons'])),
        **{k:stats([r[k] for r in frames]) for k in ('center_xy_error_m','selected_plate_error_m',
            'capture_to_result_ms','capture_to_send_ms','pose_age_s','prediction_horizon_s','target_speed_mps')},
        **{k:stats([r[k] for r in shots]) for k in ('pulse_to_launch_ms','capture_to_launch_ms')})


def analyze(source,output,candidate=None,repeats=5):
    output.mkdir(parents=True,exist_ok=True);original=load(source/'study.json')
    definitions={'ui':original['reference']}
    definitions.update({k:v['runs'] for k,v in original['modes'].items() if v['level']==1})
    if candidate:
        c=load(candidate/'diagnosis-state.json')
        definitions.update({'boundary-'+k:v['runs'] for k,v in c['modes'].items()})
    runs=[];frames=[];shots=[]
    for mode,entries in definitions.items():
        for entry in entries:
            if entry['scene'] not in FOCUS or entry['repeat_id']>=repeats:continue
            folder=manage.OPS/'experiments'/entry['run_id'];scored=source/'rescore'/folder.name
            result,ff,ss=analyze_run(folder,entry,mode,output,scored if mode=='ui' else None)
            runs.append(result);frames+=ff;shots+=ss
    grouped=[]
    for mode,entries in definitions.items():
        for scene in FOCUS:
            ff=[r for r in frames if r['mode']==mode and r['scene']==scene]
            ss=[r for r in shots if r['mode']==mode and r['scene']==scene]
            for part in ['all',*dict.fromkeys(r['phase'] for r in ff+ss)]:
                f=ff if part=='all' else [r for r in ff if r['phase']==part]
                s=ss if part=='all' else [r for r in ss if r['phase']==part]
                if f or s:grouped.append(dict(mode=mode,scene=scene,phase=part,**aggregate(f,s)))
    # Keep every historical repeat here: identify startup branches without
    # selecting or discarding an unfavourable run.
    branches=[]
    for mode,scene in [('ui-fixed-v1_p1','hf_turn90_3'),('ui-trace-v1_p1','hf_static_8')]:
        grouped_runs=defaultdict(list)
        for entry in original['modes'][mode]['runs']:
            if entry['scene']!=scene:continue
            folder=manage.OPS/'experiments'/entry['run_id'];state=load(folder/'run-state.json')
            summary=load(folder/'summary.json')['metrics'];begin=load(folder/'begin.json')['bridge_status']['last_result']
            signature=(state['measurement_start_stamp_ns'],begin['stamp_ns'])
            grouped_runs[signature].append(dict(run_id=folder.name,repeat_id=entry['repeat_id'],seed=entry['seed'],
                shots=summary['shots_fired'],weighted_hit_rate=summary['weighted_hit_rate']))
        branches.append(dict(mode=mode,scene=scene,total=sum(len(v) for v in grouped_runs.values()),
            branches=[dict(start_ns=k[0],warm_last_stamp_ns=k[1],count=len(v),
                scores=sorted(set(x['weighted_hit_rate'] for x in v)),runs=v) for k,v in sorted(grouped_runs.items())]))
    result=dict(version=1,source=str(source),candidate=str(candidate) if candidate else None,
        repeats_per_condition=repeats,runs=runs,groups=grouped,startup_branches=branches,
        limitations=['Phase is assigned by actual launch time; a projectile may cross a motion boundary in flight.',
            'Correct tracking requires a tracking state and center XY error <= 0.5m; it is not equivalent to detector availability.',
            'A forwarded pulse is associated with a launch only within 100ms; matching is inferred and coverage is reported.',
            'Historical UI scores are read-only unified v2 rescoring. No historical evidence is modified.',
            'Source trials have five UI repeats per scene. Phase bins with few or no shots cannot establish equivalence.',
            'A startup correlation alone is not proof of its cause; the intervention changes only phase-transition synchronization.'])
    write(output/'diagnosis.json',result)
    for name,data in [('frames',frames),('shots',shots)]:
        (output/(name+'.jsonl')).write_text(''.join(json.dumps(r,allow_nan=False)+'\n' for r in data))
    lines=['# 同步模式差异诊断','',f'来源：`{source.name}`。每条件前 {repeats} 轮；启动分支分析使用全部历史重复。','',
        '## 分段成绩','', '| 条件 | 场景 | 发射阶段 | 发数 | 命中率 | 加权命中率 | 正确跟踪 | 跟踪器处于 tracking |',
        '|---|---|---|---:|---:|---:|---:|---:|']
    def pct(x):return '—' if x is None else f'{x*100:.2f}%'
    for r in grouped:
        lines.append(f"| {r['mode']} | {r['scene']} | {r['phase']} | {r['shots']} | {pct(r['target_hit_rate'])} | {pct(r['weighted_hit_rate'])} | {pct(r['correct_tracking_fraction'])} | {pct(r['tracker_tracking_fraction'])} |")
    lines+=['','## 启动分支','']
    for r in branches:
        for b in r['branches']:
            lines.append(f"- {r['mode']} / {r['scene']}：开始时间 {b['start_ns']}，{b['count']}/{r['total']} 轮，加权命中率 {', '.join(pct(v) for v in b['scores'])}。")
    lines+=['','## 范围限制','',*['- '+v for v in result['limitations']]]
    text='\n'.join(lines)+'\n';(output/'diagnosis.md').write_text(text)
    (output/'diagnosis.html').write_text('<!doctype html><meta charset="utf-8"><title>同步差异诊断</title><style>body{font:14px system-ui;margin:2em}pre{white-space:pre-wrap}</style><pre>'+html.escape(text)+'</pre>')
    return result


def trial_rows(entry,folder,minimum=0,maximum=5,shift=0):
    result={}
    for r in entry['runs']:
        if minimum<=r['repeat_id']<maximum:
            result.setdefault(r['scene'],{})[r['repeat_id']-shift]=row(manage.OPS/'experiments'/r['run_id'],folder/'rescore'/r['run_id'],r['repeat_id']-shift)
    return result


def freeze_candidate(identifier,simulator_image=None,evaluator_image=None):
    image=manage.IMAGE;inspect=manage.inspect
    try:
        if simulator_image:manage.IMAGE=simulator_image
        if evaluator_image:
            manage.inspect=lambda name:inspect(evaluator_image if name=='daedalus:rm26-evaluator' else name)
        return jobs.freeze(identifier)
    finally:manage.IMAGE=image;manage.inspect=inspect


def run(source,output,repeats,profiles=('ui-fixed-v1','ui-trace-v1'),levels=(1,2),independent=True,
        simulator_image=None,evaluator_image=None):
    output.mkdir(parents=True,exist_ok=True)
    with (output/'diagnostic-controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return run_locked(source,output,repeats,profiles,levels,independent,simulator_image,evaluator_image)


def run_locked(source,output,repeats,profiles=('ui-fixed-v1','ui-trace-v1'),levels=(1,2),independent=True,
        simulator_image=None,evaluator_image=None):
    output.mkdir(parents=True,exist_ok=True);path=output/'diagnosis-state.json';state=load(path)
    if not state:
        with jobs.connect() as db:
            if db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running','pausing')").fetchone()[0]:
                raise RuntimeError('Drain other tasks before diagnostic batches')
        state=dict(version=1,name=output.name,source=str(source),created=time.time(),state='freezing',modes={},repeats=repeats,
            profiles=list(profiles),levels=list(levels),independent=independent,
            simulator_image=simulator_image,evaluator_image=evaluator_image,
            intervention='Frozen named timing profiles; algorithm, physics and scores unchanged. Phase transitions use acknowledged step boundaries.',
            certified=False,capacity_registration=False)
        write(path,state);state['bundle']=str(freeze_candidate('sync_boundary_'+uuid.uuid4().hex[:12],simulator_image,evaluator_image));write(path,state)
    if (state['repeats']!=repeats or state.get('profiles',list(profiles))!=list(profiles)
            or state.get('levels',list(levels))!=list(levels) or state.get('independent',independent)!=independent
            or state.get('simulator_image')!=simulator_image or state.get('evaluator_image')!=evaluator_image
            or state['source']!=str(source)):
        raise ValueError('Resume with the same source, repeats, profiles, lane levels, repeat policy and images')
    state['state']='running';write(path,state)
    try:
        # Check both priority scenes before collecting the bounded remainder.
        background(path,state,profiles[0],1,1,scenes=('hf_uniform_3','hf_turn90_3'))
        for profile in profiles:
            fixed=profile.startswith('ui-fixed-')
            scenes=('hf_uniform_3','hf_turn90_3') if fixed else ('hf_uniform_3','hf_static_8')
            repeat_scene='hf_turn90_3' if fixed else 'hf_static_8'
            background(path,state,profile,1,repeats,scenes=scenes)
            if independent:background(path,state,profile,1,2*repeats,repeat_ids=range(repeats,2*repeats),scenes=(repeat_scene,))
            if 2 in levels:background(path,state,profile,2,repeats,scenes=scenes)
        state['comparisons']={}
        original=load(source/'study.json')
        ui={}
        for r in original['reference']:
            ui.setdefault(r['scene'],{})[r['repeat_id']]=row(manage.OPS/'experiments'/r['run_id'],output/'rescore'/r['run_id'],r['repeat_id'])
        for profile in profiles:
            one=trial_rows(state['modes'][profile+'_p1'],output,maximum=repeats)
            repeated=trial_rows(state['modes'][profile+'_p1'],output,minimum=repeats,maximum=2*repeats,shift=repeats)
            two=trial_rows(state['modes'].get(profile+'_p2',dict(runs=[])),output,maximum=repeats)
            state['comparisons'][profile]=dict(ui={s:bootstrap(ui[s],r,.05,True) for s,r in one.items()},
                independent_repeats={s:bootstrap(one[s],r,.02) for s,r in repeated.items()},
                one_vs_two={s:bootstrap(one[s],two[s],.02) for s in two})
        state.update(state='completed',ended=time.time());write(path,state)
        analyze(source,output,candidate=output,repeats=repeats)
        print(json.dumps(dict(state=state['state'],output=str(output),comparisons=state['comparisons'])),flush=True)
    except BaseException as error:
        state.update(state='failed',error=type(error).__name__+': '+str(error));write(path,state);raise


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['analyze','run'])
    parser.add_argument('--source',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--candidate',type=Path);parser.add_argument('--repeats',type=int,default=5)
    parser.add_argument('--profiles',default='ui-fixed-v1,ui-trace-v1')
    parser.add_argument('--levels',choices=['1','1,2'],default='1,2')
    parser.add_argument('--independent-repeats',choices=['on','off'],default='on')
    parser.add_argument('--simulator-image');parser.add_argument('--evaluator-image')
    args=parser.parse_args()
    if not 5<=args.repeats<=30:parser.error('Expected 5..30 paired repeats')
    if args.action=='analyze':analyze(args.source.resolve(),args.output.resolve(),args.candidate,args.repeats)
    else:run(args.source.resolve(),args.output.resolve(),args.repeats,profiles=tuple(args.profiles.split(',')),
        levels=tuple(int(v) for v in args.levels.split(',')),independent=args.independent_repeats=='on',
        simulator_image=args.simulator_image,evaluator_image=args.evaluator_image)
