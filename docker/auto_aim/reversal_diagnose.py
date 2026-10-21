#!/usr/bin/env python3
"""Read-only algorithm diagnosis. Writes only a separate report directory.

shadow: host stage, invokes the unchanged C++ Shooter in a stdin/stdout harness.
analyze: evaluator stage (numpy/matplotlib/ffmpeg); never sends ROS or admin commands.
"""
import argparse
import bisect
import collections
import csv
import hashlib
import html
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import subprocess

from continuity_replay import Interpolate, q, recovery, steady, wrap

STUDY='continuity-v2-20261020'
BATCH='tracking_20261020T114455_dc2d1b.json'
MOVING=('line','ego_sweep','ego_sweep_reverse')
PILOT='20261020T112110_move_ref_shoot_muzzle_no-drag_25p0_25p0_feedforward_5p0hz_tracking_fire_v2_0b49621f'
VARIANTS=('receipt','receipt_minus_1ms','receipt_minus_2ms','receipt_minus_pipeline','receipt_minus_pipeline_minus_5ms')
PRIMARY_TIMING_VARIANTS=VARIANTS[:3]

def load(p):return json.loads(Path(p).read_text())
def write(p,v):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(v,ensure_ascii=False,allow_nan=False,indent=2)+'\n')
def jsonl(p):
    with Path(p).open() as f:
        for line in f:
            yield json.loads(line)
def lines(p,rows):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(''.join(json.dumps(r,ensure_ascii=False,allow_nan=False)+'\n' for r in rows))
def digest(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()
def table(p,rows):
    keys=list(dict.fromkeys(k for r in rows for k in r))
    with Path(p).open('w') as f:
        w=csv.DictWriter(f,keys);w.writeheader()
        for r in rows:w.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v for k,v in r.items()})
def paths(root):
    exp=root/'docker/auto_aim/experiments';study=exp/STUDY
    return exp,study,load(study/BATCH)
def protected(root):
    _,study,_=paths(root)
    names=set(load(study/'frozen-source-sha256.json'))
    names.update('docker/auto_aim/.runtime/'+n for n in ('profile.json','launch.json','config.toml'))
    return {n:digest(root/n) for n in sorted(names) if (root/n).is_file()}
def evidence(root,batch):
    exp,study,_=paths(root);out={}
    for entry in batch['runs']:
        f=exp/entry['run_id'];expected=load(f/'evidence-sha256.json')
        for n,h in expected.items():
            if digest(f/n)!=h:raise RuntimeError('Evidence changed: '+str(f/n))
        # Also protect the old derived reports; they are not rewritten here.
        out[f.name]={n:digest(f/n) for n in set(expected)|{'summary.json','report.html','evidence-sha256.json'}}
    out[STUDY]={n:digest(study/n) for n in (BATCH,'tracking-acceptance.json','report.html','completion.json')}
    return out

def gate_inputs(observations,variant='receipt',exact=False):
    rows=[];previous=None
    for o in observations:
        f,r=o['frame'],o['result'];diag=r.get('continuity') or {};stamp=f['stamp_ns']
        session=(f['session'],f['calibration']['id'])
        reset=(previous is None or session!=previous[0] or stamp<=previous[1] or stamp-previous[1]>250_000_000)
        pose=f.get('latest_muzzle_pose');receipt=o['receipt_ns'];adjust=0
        if variant=='receipt_minus_1ms':adjust=1_000_000
        elif variant=='receipt_minus_2ms':adjust=2_000_000
        elif variant!='receipt':adjust=float(r.get('pipeline_ms',0))*1e6
        if variant.endswith('minus_5ms'):adjust+=5_000_000
        age=(receipt-adjust-pose['stamp_ns'])*1e-9 if pose else 1.
        if exact and r.get('fire_check'):age=r['fire_check']['pose_age_s']
        rows.append(dict(seq=f['seq'],stamp_ns=stamp,reset=reset,invoke=r.get('diagnostics_version')==2,
            valid=bool(r.get('valid')),yaw=r.get('yaw',0.),pitch=r.get('pitch',0.),
            tracking=r.get('tracker')=='tracking',measurement_fresh=diag.get('trusted_update_age_s',1.)<=.05,
            generation=r.get('track_generation',0),plate_id=r.get('selected_plate_id',-1),
            pose_yaw=pose['yaw'] if pose else 0.,pose_elevation=pose['elevation'] if pose else 0.,pose_age_s=age))
        previous=(session,stamp)
    return rows

def cpp(inputs):
    cmd=['docker','exec','-i','--user','1000','sp_vision_sunshine',
        '/workspace/sp_vision_25/build/diagnostic_fire_replay/diagnostic_fire_replay','/tmp/rm26-diagnostic-fire-config.yaml']
    p=subprocess.run(cmd,input=''.join(json.dumps(r)+'\n' for r in inputs),capture_output=True,text=True,check=True)
    result=[json.loads(l) for l in p.stdout.splitlines()]
    if len(result)!=len(inputs):raise RuntimeError('Shadow output count mismatch')
    for a,b in zip(inputs,result):
        if (a['stamp_ns'],a['seq'])!=(b['stamp_ns'],b['seq']):raise RuntimeError('Shadow output alignment mismatch')
    return result

def opportunities(observations,outputs,start,end):
    last=None;indices=[]
    for i,(o,g) in enumerate(zip(observations,outputs)):
        t=o['receipt_ns']
        if start<=t<=end and o.get('command_emitted') and g['eligible'] and (last is None or t-last>=200_000_000):
            indices.append(i);last=t
    return indices

def boundary_checks():
    base=dict(seq=0,stamp_ns=1_000_000_000,reset=True,invoke=True,valid=True,yaw=0.,pitch=0.,tracking=True,
        measurement_fresh=True,generation=1,plate_id=0,pose_yaw=0.,pose_elevation=0.,pose_age_s=.005)
    def series(**kwargs):return [dict(base,**dict(seq=i,stamp_ns=1_000_000_000+i*33_333_333,reset=i==0),**kwargs) for i in range(8)]
    aligned=series();a=cpp(aligned)
    checks={'three_stable_frames':not any(x['eligible'] for x in a[:3]) and a[3]['eligible'],
        'yaw_wrap':cpp(series(yaw=-math.pi+.001,pose_yaw=math.pi-.001))[-1]['eligible']}
    for name,change in [('pitch',{'pose_elevation':.04}),('pose_stale',{'pose_age_s':.051}),
                        ('pose_future',{'pose_age_s':-.001}),('tracking_lost',{'tracking':False}),
                        ('measurement_stale',{'measurement_fresh':False}),('invalid',{'valid':False}),
                        ('generation',{'generation':2}),('plate',{'plate_id':1}),
                        ('jump',{'yaw':.05,'pose_yaw':.05}),('session_reset',{'reset':True}),
                        ('timestamp_rollback',{'stamp_ns':1_000_000_000})]:
        test=aligned+[dict(aligned[-1],seq=8,stamp_ns=1_266_666_664,reset=False,**{})]
        test[-1].update(change);checks[name+'_blocks']=not cpp(test)[-1]['eligible']
    obs=[{'receipt_ns':i*30_000_000,'command_emitted':True} for i in range(40)]
    picks=opportunities(obs,[{'eligible':True}]*len(obs),0,2_000_000_000)
    checks['rate_limit_200ms']=all(obs[b]['receipt_ns']-obs[a]['receipt_ns']>=200_000_000 for a,b in zip(picks,picks[1:]))
    checks['deterministic']=cpp(aligned)==a
    if not all(checks.values()):raise RuntimeError(checks)
    return checks

def shadow_stage(root,out):
    exp,study,batch=paths(root);out.mkdir(parents=True,exist_ok=True)
    baseline=protected(root)
    changed=[n for n,h in load(study/'frozen-source-sha256.json').items() if digest(root/n)!=h]
    if changed:raise RuntimeError('Frozen source differs: '+str(changed))
    write(out/'protected-before.json',baseline);print('Checking 60 evidence bundles',flush=True)
    write(out/'evidence-before.json',evidence(root,batch))
    chosen=[e for e in batch['runs'] if e['algorithm_profile']=='tracking_v2']
    config=exp/chosen[0]['run_id']/'configuration/runtime-calibration.yaml'
    if 'auto_fire: true' not in config.read_text():raise RuntimeError('Frozen auto_fire must be enabled')
    subprocess.run(['docker','cp',str(config),'sp_vision_sunshine:/tmp/rm26-diagnostic-fire-config.yaml'],check=True)
    shutil.copy2(config,out/'shadow-runtime.yaml')
    write(out/'boundary-checks.json',boundary_checks())
    for i,e in enumerate(chosen):
        f=exp/e['run_id'];obs=[p['data'] for p in jsonl(f/'telemetry.jsonl') if p['kind']=='observation'];dest=out/'shadow'/f.name
        for variant in VARIANTS:
            ins=gate_inputs(obs,variant);res=cpp(ins);lines(dest/(variant+'-input.jsonl'),ins);lines(dest/(variant+'.jsonl'),res)
            if i==0 and variant=='receipt':
                check=cpp(ins);write(out/'full-run-repeatability.json',{'passed':check==res,'frames':len(ins)});assert check==res
        print(f'Shadow {i+1}/{len(chosen)} {f.name}',flush=True)
    pilot=[p['data'] for p in jsonl(exp/PILOT/'telemetry.jsonl') if p['kind']=='observation']
    actual=cpp(gate_inputs(pilot,exact=True));mismatch=[];compared=0;proxy=cpp(gate_inputs(pilot));age_errors=[];synchronized=False;unrecorded_counter=[]
    for i,(o,g) in enumerate(zip(pilot,actual)):
        expected=o['result'].get('fire_check')
        if not expected or i<12:continue # recorded stream can begin inside a warmup track
        if expected['stable_frames']==0 and g['fire_check']['stable_frames']==0:synchronized=True
        compared+=1
        for key in ('eligible','stable_frames','valid','aligned','discontinuity'):
            if key=='stable_frames' and not synchronized:
                if expected[key]!=g['fire_check'][key]:unrecorded_counter.append({'seq':g['seq'],'live':expected[key],'replay':g['fire_check'][key]})
                continue
            if expected[key]!=g['fire_check'][key]:mismatch.append({'seq':g['seq'],'field':key,'live':expected[key],'replay':g['fire_check'][key]})
        if o['frame'].get('latest_muzzle_pose'):
            age_errors.append((proxy[i]['fire_check']['pose_age_s']-expected['pose_age_s'])*1000)
    check={'compared_frames':compared,'mismatches':mismatch,'passed':not mismatch,
        'warmup_counter_before_first_observed_reset':unrecorded_counter,'state_synchronized_after_reset':synchronized,
        'receipt_proxy_minus_actual_pose_age_ms_p50':q(age_errors,.5),'receipt_proxy_minus_actual_pose_age_ms_p95':q(age_errors,.95),
        'proxy_eligibility_disagreements':sum(bool(o['result'].get('fire_check')) and g['eligible']!=o['result']['fire_check']['eligible'] for o,g in zip(pilot[12:],proxy[12:]))}
    write(out/'live-pilot-replay-check.json',check)
    if mismatch:raise RuntimeError('Existing C++ live pilot not reproduced')
    write(out/'shadow-provenance.json',{'algorithm_binary_sha256':batch['algorithm_binary_sha256'],
        'harness_binary_sha256':digest(root/'sp_vision_25/build/diagnostic_fire_replay/diagnostic_fire_replay'),
        'shooter_source_sha256':digest(root/'sp_vision_25/tasks/auto_aim/shooter.cpp'),
        'config_sha256':digest(config),'truth_sent_to_harness':False,'variants':VARIANTS,
        'timing_note':'Receipt time is a proxy. Primary sensitivity subtracts 1 and 2 ms (live pilot receipt latency P95 about 0.95 ms); these are scenarios, not bounds. Subtracting the full pipeline and another 5 ms is a separate stress scenario that can predate the pose snapshot and is not an equally plausible call time.',
        'history_note':'Starts at beginning of recorded warmup; unrecorded preceding frames cannot be reconstructed. Session/intrinsic/gap resets reproduced.'})
    assert protected(root)==baseline


def recovery_detail(rows,stamp,end):
    limit=min(stamp+2_000_000_000,end);future=[r for r in rows if stamp<=r['stamp_ns']<=limit]
    direction=None;origin=None;confirmation=None;previous=None;identity=None;broken_gaps=0;identity_breaks=0
    for r in future:
        good=r['gt_vy'] is not None and r['estimated_vy'] is not None and abs(r['gt_vy'])>.1 and r['estimated_vy']*r['gt_vy']>0
        if good and direction is None:direction=(r['stamp_ns']-stamp)*1e-9
        current=(r['generation'],r['selected_plate_id'])
        if previous is not None and r['stamp_ns']-previous>100_000_000:
            origin=None;broken_gaps+=1
        if identity is not None and current!=identity:
            origin=None;identity_breaks+=1
        ok=good and abs(r['estimated_vy']-r['gt_vy'])<=.15 and r['valid']
        if ok:
            if origin is None:origin=r['stamp_ns']
            if r['stamp_ns']-origin>=100_000_000:
                confirmation=r['stamp_ns'];break
        else:origin=None
        previous=r['stamp_ns'];identity=current
    trustworthy=next((r for r in future if r['accepted_update']),None)
    return {'first_correct_direction_s':direction,'continuous_velocity_recovery_s':(origin-stamp)*1e-9 if confirmation else None,
        'velocity_confirmation_s':(confirmation-stamp)*1e-9 if confirmation else None,
        'first_trusted_update_s':(trustworthy['stamp_ns']-stamp)*1e-9 if trustworthy else None,
        'sample_gap_breaks':broken_gaps,'identity_breaks':identity_breaks,'right_censored':limit<stamp+2_000_000_000}

def intervals(rows,key,start,end):
    # Time-weighted, hold each observed frame for at most 100 ms. Larger gaps
    # are reported separately and never silently become evidence of a state.
    total=0;longest=0;current=0
    for i,r in enumerate(rows):
        a=max(start,r['stamp_ns']);b=min(end,rows[i+1]['stamp_ns'] if i+1<len(rows) else end,r['stamp_ns']+100_000_000)
        if b<=a:continue
        if r.get(key):current+=(b-a)*1e-9;total+=(b-a)*1e-9;longest=max(longest,current)
        else:current=0
        if i+1<len(rows) and rows[i+1]['stamp_ns']-r['stamp_ns']>100_000_000:current=0
    return total,longest

def event_table(rows,event,shots,start,end):
    stamp=event['stamp_ns'];window=[r for r in rows if stamp-750_000_000<=r['stamp_ns']<=min(end,stamp+2_000_000_000)]
    for r in window:r['event_dt_s']=(r['stamp_ns']-stamp)*1e-9
    detail=recovery_detail(rows,stamp,end)
    first=next((s for s in shots if s['front_hit'] and s['fire_stamp_ns']>=stamp and s.get('terminal_stamp_ns') and s['terminal_stamp_ns']<=stamp+2_000_000_000),None)
    horizon=min(end,stamp+int(min(event['recovery_s'] if event['recovery_s'] is not None else 2.,2.)*1e9)+100_000_000)
    after=[r for r in window if r['stamp_ns']>=stamp]
    durations={}
    for key in ('no_enemy_detection','quality_rejected','incompatible','innovation_rejected','association_unresolved',
                'selected_plate_unobserved','temp_lost','lost','detecting','not_aligned','raw_advice_false','maneuver'):
        total,longest=intervals(window,key,stamp,horizon);durations[key]={'seconds':total,'longest_s':longest}
    equal_window={}
    for key in ('no_enemy_detection','innovation_rejected','association_unresolved','selected_plate_unobserved','not_aligned','maneuver'):
        total,longest=intervals(window,key,stamp,min(end,stamp+300_000_000));equal_window[key]={'seconds':total,'longest_s':longest}
    identities=[(r['generation'],r['selected_plate_id']) for r in window]
    changes=[{'stamp_ns':r['stamp_ns'],'dt_s':r['event_dt_s'],'generation':r['generation'],'plate_id':r['selected_plate_id'],'reset_reason':r['reset_reason']}
        for i,r in enumerate(window) if i and identities[i]!=identities[i-1]]
    resets=[r for r in window if r['reset_reason'] or (r['generation'] is not None and r['generation']!=next((a['generation'] for a in reversed(window[:window.index(r)]) if a['generation'] is not None),r['generation']))]
    post_shots=[s for s in shots if stamp<=s['fire_stamp_ns']<=min(end,stamp+2_000_000_000)]
    direction=next((1 if r['gt_vy']>0 else -1 for r in after if r['gt_vy'] is not None and abs(r['gt_vy'])>.15),None)
    tags=[]
    for key,label in [('no_enemy_detection','观测缺失'),('quality_rejected','姿态质量拒绝'),('incompatible','类别或尺寸不兼容'),('innovation_rejected','创新门控拒绝'),('association_unresolved','关联未建立（日志不能确定候选原因）')]:
        if durations[key]['seconds']>=.06:tags.append(label)
    if any(r['reset_reason'] for r in resets) or len({r['generation'] for r in window if r['generation'] is not None})>1:tags.append('轨迹重建')
    if any(a['generation']==b['generation'] and a['selected_plate_id']!=b['selected_plate_id'] for a,b in zip(window,window[1:])):tags.append('装甲选择变化')
    if durations['not_aligned']['seconds']>=.06:tags.append('双轴未对齐')
    if not tags:tags.append('连续观测下估计响应或测量偏差，待逐帧确认')
    return dict(event,**detail,direction=direction,time_s=(stamp-start)*1e-9,
        first_front_hit_s=(first['terminal_stamp_ns']-stamp)*1e-9 if first else None,
        first_launch_s=(post_shots[0]['fire_stamp_ns']-stamp)*1e-9 if post_shots else None,
        first_request_s=(post_shots[0]['request_receipt_ns']-stamp)*1e-9 if post_shots and post_shots[0].get('request_receipt_ns') else None,
        first_post_reversal_shot_seq=post_shots[0]['shot_seq'] if post_shots else None,
        first_front_hit_shot_seq=first['shot_seq'] if first else None,
        missed_shots_before_first_front=sum(not s['front_hit'] for s in post_shots if first is None or s['fire_stamp_ns']<first['fire_stamp_ns']),
        explanatory_tags=tags,durations=durations,first_300ms_durations=equal_window,identity_changes=changes,
        evidence_window={'start_ns':stamp-750_000_000,'end_ns':min(end,stamp+2_000_000_000)},
        window_frames=len(window),window_seq=[window[0]['seq'],window[-1]['seq']] if window else [],
        attributed_causes_are_observations_not_causal_proof=True),window

def analyze_run(root,out,entry,reference):
    import numpy as np
    from diagnostics import Truth
    from moving_fire import firing_timing
    exp,_,_=paths(root);folder=exp/entry['run_id'];manifest=load(folder/'manifest.json');state=load(folder/'run-state.json')
    start,end=state['measurement_start_stamp_ns'],state['measurement_end_stamp_ns'];duration=manifest['duration_s']
    obs=[];robots=[];armors=[];traces=[];commands=[]
    for p in jsonl(folder/'telemetry.jsonl'):
        k,d=p['kind'],p['data']
        if k=='observation':obs.append(d)
        elif k=='robots':robots.append(d)
        elif k=='armors':armors.append(d)
        elif k=='gimbal_trace':traces.append(d)
        elif k=='command':commands.append(d)
    rt,at=Truth(robots),Truth(armors);frame_index={r['stamp_ns']:r for r in jsonl(folder/'frames.jsonl')}
    front=next(a['id'] for a in armors[0]['armors'] if a['robot_id']==1 and a['slot']==0)
    velocities=[]
    for p in robots:
        by={v['id']:v for v in p['robots']}
        if 0 in by and 1 in by:velocities.append((p['stamp_ns'],[v-u for v,u in zip(by[1]['velocity'],by[0]['velocity'])]))
    truth=Interpolate(velocities)
    shots=list(jsonl(folder/'shots.jsonl'));pulses=[o for o in obs if o.get('fire_forwarded')];pulse_stamps=[o['receipt_ns'] for o in pulses]
    for s in shots:
        ego=rt.at(s['fire_stamp_ns'],0,'robots')
        if ego:s['muzzle_relative_velocity']=(np.array(s['initial_velocity'])-np.array(ego['velocity'])).tolist()
        s['front_hit']=s['outcome']=='target_armor' and s['armor_id']==front;s['scene']=manifest['scene']
    joins=firing_timing(folder,shots)
    for s in shots:
        i=bisect.bisect_right(pulse_stamps,s['fire_stamp_ns'])-1
        if i>=0 and s.get('request_frame_stamp_ns')==pulses[i]['frame']['stamp_ns']:
            s['request_seq']=pulses[i]['frame']['seq'];s['request_receipt_ns']=pulses[i]['receipt_ns']
    rows=[];prev=None
    for o in obs:
        f,r=o['frame'],o['result'];stamp=f['stamp_ns'];c=r.get('continuity') or {};fil=c.get('filter') or {};updates=fil.get('updates') or []
        seen=c.get('observations') or [];raw=r.get('observations') or [];enemy=[a for a in raw if a.get('color')=='blue']
        compatible=[a for a in seen if a.get('compatible')];passed=[a for a in updates if a.get('accepted')]
        trusted_age=c.get('trusted_update_age_s');selected=r.get('selected_plate_id',-1);x=r.get('filter_state');pose=f.get('latest_muzzle_pose');v=truth.at(stamp)
        match=frame_index.get(stamp,{});valid=bool(r.get('valid'));xy=(r.get('track_estimate') or {}).get('plates',[])
        yaw_error=math.degrees(wrap(r.get('yaw',0)-pose['yaw'])) if pose and valid else None
        pitch_error=math.degrees(-r.get('pitch',0)-pose['elevation']) if pose and valid else None
        row=dict(stamp_ns=stamp,receipt_ns=o['receipt_ns'],seq=f['seq'],video_index=match.get('video_index'),
            time_s=(stamp-start)*1e-9,measured=start<=stamp<=end,valid=valid,tracker=r.get('tracker'),generation=r.get('track_generation'),
            selected_plate_id=selected,reset_reason=c.get('reset_reason'),trusted_age_s=trusted_age,
            accepted_update=bool(passed) or bool(c.get('initialized')),selected_update=any(u['plate_id']==selected for u in passed),
            selected_plate_unobserved=selected>=0 and not any(u['plate_id']==selected for u in passed),
            raw_detection_count=len(raw),enemy_detection_count=len(enemy),raw_observations=raw,association_observations=seen,updates=updates,
            no_enemy_detection=not enemy,quality_rejected=bool(compatible) and all(not a.get('pose_valid') or (a.get('reprojection_rms_px') or 0)>8 for a in compatible),
            incompatible=bool(seen) and not compatible,innovation_rejected=bool(updates) and not passed and any(a.get('reason')=='innovation' for a in updates),
            association_unresolved=bool(compatible) and not updates and not c.get('initialized'),
            temp_lost=r.get('tracker')=='temp_lost',lost=r.get('tracker')=='lost',detecting=r.get('tracker')=='detecting',
            maneuver=bool(fil.get('translation_maneuver')),maneuver_evidence_frames=fil.get('translation_evidence_frames'),
            raw_advice_false=not r.get('fire_advice',False),fire_forwarded=o.get('fire_forwarded',False),command_emitted=o.get('command_emitted',False),
            gt_vy=v[1] if v else None,estimated_vy=x[3] if x else None,estimated_vx=x[1] if x else None,
            yaw_deg=math.degrees(r.get('yaw',0)) if valid else None,pitch_deg=math.degrees(r.get('pitch',0)) if valid else None,
            pose_age_receipt_ms=(o['receipt_ns']-pose['stamp_ns'])/1e6 if pose else None,
            yaw_error_deg=yaw_error,pitch_error_deg=pitch_error,not_aligned=valid and (pose is None or abs(yaw_error)>.5 or abs(pitch_error)>.5),
            filter_state=x,prediction_horizon_s=r.get('prediction_horizon_s'),
            truth_visible_plates=match.get('visible_plates'),truth_available=match.get('truth_available'),
            selected_gt_slot=None,selected_gt_distance_m=None,selected_gt_margin_m=None,
            selected_gt_velocity_y=None,prediction_lateral_error_cm=None,prediction_vertical_error_cm=None,
            radial_velocity_error_mps=None,lateral_velocity_error_mps=None,
            sample_gap_s=(stamp-prev['stamp_ns'])*1e-9 if prev else None)
        if valid and x and v:
            bearing=math.atan2(x[2],x[0]);dv=np.array([x[1]-v[0],x[3]-v[1]])
            row['radial_velocity_error_mps']=float(dv@np.array([math.cos(bearing),math.sin(bearing)]))
            row['lateral_velocity_error_mps']=float(dv@np.array([-math.sin(bearing),math.cos(bearing)]))
        if 0<=selected<len(xy):
            pos=np.array(xy[selected])+np.array(f['gimbal_position_map']);near=[]
            for a0 in armors[0]['armors']:
                if a0['robot_id']!=1:continue
                a=at.at(stamp,a0['id'],'armors')
                if a:near.append((float(np.linalg.norm(pos-np.array(a['position']))),a))
            near.sort(key=lambda a:a[0])
            if near:
                row['selected_gt_distance_m']=near[0][0];row['selected_gt_margin_m']=near[1][0]-near[0][0] if len(near)>1 else None
                # This is an evaluator association, not an internal model ID.
                if near[0][0]<.25 and len(near)>1 and near[1][0]-near[0][0]>.03:
                    plate=near[0][1];row['selected_gt_slot']=plate['slot']
                    a,b=at.at(stamp-20_000_000,plate['id'],'armors'),at.at(stamp+20_000_000,plate['id'],'armors');ego=rt.at(stamp,0,'robots')
                    if a and b and ego:row['selected_gt_velocity_y']=(b['position'][1]-a['position'][1])/.04-ego['velocity'][1]
                    horizon=r.get('prediction_horizon_s');aim=r.get('aim_point')
                    if horizon is not None and 0<horizon<1 and aim:
                        future=stamp+round(horizon*1e9);target=at.at(future,plate['id'],'armors');ego_future=rt.at(future,0,'robots')
                        if target and ego and ego_future:
                            predicted=np.array(aim)+np.array(f['gimbal_position_map'])+np.array(ego_future['position'])-np.array(ego['position'])
                            error=predicted-np.array(target['position']);normal=np.array(target['normal']);normal/=np.linalg.norm(normal)
                            lateral=np.cross([0,0,1],normal);lateral/=np.linalg.norm(lateral);vertical=np.cross(normal,lateral)
                            row['prediction_lateral_error_cm']=float(error@lateral)*100;row['prediction_vertical_error_cm']=float(error@vertical)*100
        rows.append(row);prev=row
    # Preserve the old metric exactly for side-by-side audit.
    original=recovery([dict(stamp_ns=o['frame']['stamp_ns'],filter_state=o['result'].get('filter_state')) for o in obs],truth,start,end)
    expected=reference.get('recovery_events',[])
    if original!=expected:raise RuntimeError('Original recovery reproduction failed: '+folder.name)
    dest=out/'runs'/folder.name;dest.mkdir(parents=True,exist_ok=True)
    lines(dest/'frames.jsonl',rows);lines(dest/'gimbal-trace.jsonl',traces);lines(dest/'commands.jsonl',commands);write(dest/'shots.json',shots)
    events=[]
    for i,event in enumerate(original):
        detail,window=event_table(rows,event,shots,start,end);detail.update(run_id=folder.name,event_id=folder.name+f'_e{i+1}',
            scene=manifest['scene'],physics=manifest['shooting']['physics'],repeat=entry['repeat'],algorithm_profile=entry['algorithm_profile'],ordinal=i+1,
            slow=event['recovery_s'] is None or event['recovery_s']>.3)
        events.append(detail);lines(dest/f'event-{i+1}.jsonl',window)
    write(dest/'events.json',events)
    info=dict(run_id=folder.name,scene=manifest['scene'],physics=manifest['shooting']['physics'],repeat=entry['repeat'],
        algorithm_profile=entry['algorithm_profile'],start_ns=start,end_ns=end,duration_s=duration,
        original_metric_reproduced=True,shot_join=joins,events=len(events),shadow=None)
    if entry['algorithm_profile']=='tracking_v2':
        info['shadow']=shadow_metrics(out,folder.name,obs,rows,shots,start,end)
    write(dest/'summary.json',info)
    return info,events

def shadow_metrics(out,run_id,obs,rows,shots,start,end):
    dest=out/'shadow'/run_id;all_variants={v:list(jsonl(dest/(v+'.jsonl'))) for v in VARIANTS}
    primary=all_variants['receipt'];ins=list(jsonl(dest/'receipt-input.jsonl'));measured=[];counts=collections.Counter();exclusive=collections.Counter();unknown=0
    eligible_ns=0;covered_ns=0;blocked_longest=0;blocked_start=None;blackouts=[];prev=None
    for i,(o,g,r,input_row) in enumerate(zip(obs,primary,rows,ins)):
        if not start<=o['receipt_ns']<=end:continue
        reasons=[];fire=g.get('fire_check') or {}
        if not input_row['invoke']:reasons.append('adapter_invalid')
        else:
            if not input_row['valid']:reasons.append('invalid_solution')
            if not input_row['tracking']:reasons.append('tracker_not_tracking')
            if not input_row['measurement_fresh']:reasons.append('measurement_stale')
            if input_row['plate_id']<0:reasons.append('missing_plate')
            if not o['frame'].get('latest_muzzle_pose'):reasons.append('pose_missing')
            elif input_row['pose_age_s']<0 or input_row['pose_age_s']>.05:reasons.append('pose_stale_or_future')
            if abs(fire.get('yaw_error_rad',0))>math.radians(.5):reasons.append('yaw_misaligned')
            if abs(fire.get('pitch_error_rad',0))>math.radians(.5):reasons.append('pitch_misaligned')
            if fire.get('discontinuity'):reasons.append('discontinuity')
            if not g['eligible'] and not reasons:reasons.append('three_stable_frames')
        counts.update(reasons)
        if len(reasons)==1:exclusive.update(reasons)
        timing_disagreement=any(all_variants[v][i]['eligible']!=g['eligible'] for v in PRIMARY_TIMING_VARIANTS)
        t=o['receipt_ns'];next_t=obs[i+1]['receipt_ns'] if i+1<len(obs) else end
        dwell=max(0,min(end,next_t,t+50_000_000)-max(start,t));covered_ns+=dwell
        if g['eligible']:eligible_ns+=dwell
        # Qualification expires at 50 ms; gaps beyond that do not imply permission.
        segments=[(t,min(end,next_t,t+50_000_000),g['eligible'])]
        if next_t>t+50_000_000:segments.append((t+50_000_000,min(next_t,end),False))
        for a,b,enabled in segments:
            if b<=a:continue
            if enabled:
                if blocked_start is not None:blackouts.append((a-blocked_start)*1e-9);blocked_start=None
            elif blocked_start is None:blocked_start=a
        measured.append(dict(seq=r['seq'],stamp_ns=r['stamp_ns'],receipt_ns=t,time_s=r['time_s'],
            eligible=g['eligible'],reasons=reasons,stable_frames=fire.get('stable_frames'),
            pose_age_proxy_s=input_row['pose_age_s'],timing_uncertain=True,timing_sensitivity_changed=timing_disagreement,
            yaw_error_deg=r['yaw_error_deg'],pitch_error_deg=r['pitch_error_deg'],tracker=r['tracker'],
            generation=r['generation'],plate_id=r['selected_plate_id'],fire_forwarded=o.get('fire_forwarded',False),
            command_residual_deg=fire.get('command_residual_deg'),discontinuity=fire.get('discontinuity')))
    if blocked_start is not None:blackouts.append((end-blocked_start)*1e-9)
    variants={}
    for v,res in all_variants.items():
        picks=opportunities(obs,res,start,end);variants[v]={'eligible_frames':sum(g['eligible'] for o,g in zip(obs,res) if start<=o['receipt_ns']<=end),
            'candidate_requests':len(picks),'candidate_request_hz':len(picks)/((end-start)*1e-9),
            'candidate_seq':[obs[i]['frame']['seq'] for i in picks]}
    by_stamp={(o['frame']['stamp_ns'],o['frame']['seq']):(i,g) for i,(o,g) in enumerate(zip(obs,primary))};matched=[]
    receipts=[o['receipt_ns'] for o in obs]
    for shot in shots:
        key=(shot.get('request_frame_stamp_ns'),shot.get('request_seq'));pair=by_stamp.get(key)
        row=dict(shot)
        if pair:
            i,g=pair;row['shadow_eligible_at_historical_request']=g['eligible'];row['timing_sensitivity_changed']=any(all_variants[v][i]['eligible']!=g['eligible'] for v in PRIMARY_TIMING_VARIANTS)
            row['shadow_stable_frames']=(g.get('fire_check') or {}).get('stable_frames')
        else:row['shadow_eligible_at_historical_request']=None
        j=bisect.bisect_right(receipts,shot['fire_stamp_ns'])-1
        row['shadow_latest_qualification_allows_historical_launch']=None
        if j>=0 and shot.get('request_frame_stamp_ns') is not None:
            row['shadow_latest_qualification_allows_historical_launch']=bool(primary[j]['eligible']
                and obs[j].get('command_emitted') and 0<=shot['fire_stamp_ns']-receipts[j]<=50_000_000
                and obs[j]['frame']['stamp_ns']>=shot['request_frame_stamp_ns'])
            row['shadow_latest_qualification_age_ms']=(shot['fire_stamp_ns']-receipts[j])/1e6
        matched.append(row)
    removed=[s for s in matched if s['shadow_eligible_at_historical_request'] is False];kept=[s for s in matched if s['shadow_eligible_at_historical_request'] is True]
    summary={'frames':len(measured),'eligible_frames':sum(r['eligible'] for r in measured),
        'eligible_fraction':sum(r['eligible'] for r in measured)/len(measured),
        'qualified_time_fraction_with_50ms_expiry':eligible_ns/(end-start),'observed_time_fraction':covered_ns/(end-start),
        'blocked_reason_counts_overlapping':dict(counts),'sole_blocker_counts':dict(exclusive),
        'wide_timing_stress_changed_frames':sum(any(all_variants[v][i]['eligible']!=primary[i]['eligible'] for v in VARIANTS[3:]) for i,o in enumerate(obs) if start<=o['receipt_ns']<=end),
        'three_frame_wait_only_frames':exclusive['three_stable_frames'],'longest_no_qualification_s':max(blackouts,default=0),
        'timing_sensitivity_changed_frames':sum(r['timing_sensitivity_changed'] for r in measured),'variants':variants,
        'historical_shots':len(shots),'matched_historical_shots':len(kept)+len(removed),'removed_historical_shots':len(removed),
        'removed_historical_front_hits':sum(s['front_hit'] for s in removed),'removed_historical_front_misses':sum(not s['front_hit'] for s in removed),
        'retained_historical_front_hits':sum(s['front_hit'] for s in kept),'retained_historical_front_misses':sum(not s['front_hit'] for s in kept),
        'historical_request_eligible_but_launch_disqualified':sum(s['shadow_eligible_at_historical_request'] is True and s['shadow_latest_qualification_allows_historical_launch'] is False for s in matched),
        'new_hit_rate_estimated':False,'candidate_requests_are_not_launches':True}
    lines(dest/'frame-audit.jsonl',measured);table(dest/'frame-audit.csv',measured);write(dest/'historical-shot-audit.json',matched);table(dest/'historical-shot-audit.csv',matched);write(dest/'summary.json',summary)
    return summary

def plot_event(out,event):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    rows=list(jsonl(out/'runs'/event['run_id']/f"event-{event['ordinal']}.jsonl"));t=[r['event_dt_s'] for r in rows]
    fig,axes=plt.subplots(5,1,figsize=(12,11),sharex=True,layout='constrained')
    def values(key):return [np.nan if r.get(key) is None else r[key] for r in rows]
    axes[0].plot(t,values('gt_vy'),label='truth relative Vy',color='black');axes[0].plot(t,values('estimated_vy'),label='estimated Vy',color='#19796e')
    axes[0].plot(t,values('selected_gt_velocity_y'),label='matched plate truth Vy',linestyle=':',alpha=.7);axes[0].set_ylabel('velocity m/s')
    axes[1].plot(t,values('radial_velocity_error_mps'),label='radial velocity error');axes[1].plot(t,values('lateral_velocity_error_mps'),label='lateral velocity error');axes[1].set_ylabel('error m/s')
    axes[2].plot(t,values('prediction_lateral_error_cm'),label='future plate lateral error');axes[2].plot(t,values('prediction_vertical_error_cm'),label='future plate vertical error');axes[2].set_ylabel('prediction cm')
    axes[3].plot(t,values('yaw_error_deg'),label='yaw request - own pose');axes[3].plot(t,values('pitch_error_deg'),label='pitch request - own pose');axes[3].axhline(.5,color='gray',linestyle=':');axes[3].axhline(-.5,color='gray',linestyle=':');axes[3].set_ylabel('alignment deg')
    for j,key in enumerate(['accepted_update','temp_lost','lost','detecting','maneuver']):
        axes[4].scatter([r['event_dt_s'] for r in rows if r[key]],[j]*sum(bool(r[key]) for r in rows),s=10,label=key)
    shots=load(out/'runs'/event['run_id']/'shots.json');stamp=event['stamp_ns']
    for s in shots:
        dt=(s['fire_stamp_ns']-stamp)*1e-9
        if -.75<=dt<=2:
            axes[4].scatter([dt],[5],marker='|' if s['front_hit'] else 'x',color='#19796e' if s['front_hit'] else '#bc3e32',s=70)
    axes[4].set_yticks(range(6),['accepted','temp_lost','lost','detecting','maneuver','shot H/M']);axes[4].set_xlabel('Seconds from truth velocity zero crossing')
    for ax in axes:
        ax.axvline(0,color='black',linestyle='--',linewidth=.8);ax.grid(alpha=.2);ax.set_xlim(-.75,2)
    for ax in axes[:4]:ax.legend(loc='upper right',fontsize=8)
    fig.suptitle(event['scene']+' / '+event['physics']+' / '+event['algorithm_profile']+' / event '+str(event['ordinal'])+'\n'+event['run_id'],fontsize=10)
    dest=out/'events'/event['event_id'];dest.mkdir(parents=True,exist_ok=True)
    fig.savefig(dest/'timeline.png',dpi=140);plt.close(fig)
    table(dest/'frames.csv',rows);write(dest/'event.json',event)

def clip_event(root,out,event):
    exp,_,_=paths(root);folder=exp/event['run_id'];frames=list(jsonl(folder/'frames.jsonl'));stamps=[r['stamp_ns'] for r in frames]
    a=max(0,bisect.bisect_left(stamps,event['evidence_window']['start_ns']));b=min(len(frames),bisect.bisect_right(stamps,event['evidence_window']['end_ns']))
    if a>=b:raise RuntimeError('Empty clip')
    first,last=frames[a]['video_index'],frames[b-1]['video_index'];dest=out/'events'/event['event_id'];dest.mkdir(parents=True,exist_ok=True)
    subprocess.run(['ffmpeg','-v','error','-y','-i',str(folder/'annotated.mp4'),'-vf',f'trim=start_frame={first}:end_frame={last+1},setpts=PTS-STARTPTS',
        '-an','-c:v','libx264','-preset','fast','-crf','21','-threads','2',str(dest/'clip.mp4')],check=True)
    res=subprocess.run(['ffprobe','-v','error','-count_frames','-select_streams','v:0','-show_entries','stream=nb_read_frames','-of','json',str(dest/'clip.mp4')],capture_output=True,text=True,check=True)
    count=int(json.loads(res.stdout)['streams'][0]['nb_read_frames']);assert count==last-first+1
    mapping={'source':event['run_id']+'/annotated.mp4','source_first_video_index':first,'source_last_video_index':last,
        'clip_frames':count,'nominal_fps':30,'capture_stamp_start_ns':frames[a]['stamp_ns'],'capture_stamp_end_ns':frames[b-1]['stamp_ns'],
        'event_source_frame_offset':bisect.bisect_left(stamps,event['stamp_ns'])-a,'precise_times':'frames.csv uses authoritative capture timestamps; clip playback uses 30 FPS'}
    write(dest/'clip-index.json',mapping);return mapping

def diagnostics_checks():
    rows=[dict(stamp_ns=int(t*1e9),gt_vy=.5,estimated_vy=.5,generation=1,selected_plate_id=0,valid=True,accepted_update=True) for t in [1,1.033,1.066,1.1]]
    good=recovery_detail(rows,1_000_000_000,3_000_000_000)
    gap=[dict(rows[0]),dict(rows[-1],stamp_ns=1_200_000_000)]
    identity=[dict(r,generation=1 if i<2 else 2) for i,r in enumerate(rows)]
    checks={'continuous_100ms':good['continuous_velocity_recovery_s']==0,
        'confirmation_time':abs(good['velocity_confirmation_s']-.1)<1e-9,
        'gap_breaks_continuity':recovery_detail(gap,1_000_000_000,3_000_000_000)['continuous_velocity_recovery_s'] is None,
        'identity_breaks_continuity':recovery_detail(identity,1_000_000_000,3_000_000_000)['continuous_velocity_recovery_s'] is None,
        'nearest_rank_two':q([.1,1.2],.9)==1.2}
    assert all(checks.values());return checks

def analyze_stage(root,out,media=True):
    exp,study,batch=paths(root);reference={r['run_id']:r for r in load(study/'tracking-acceptance.json')['runs']}
    if not (out/'shadow-provenance.json').exists():raise RuntimeError('Run shadow stage first')
    write(out/'analysis-boundary-checks.json',diagnostics_checks())
    all_runs=[];all_events=[]
    for i,entry in enumerate(batch['runs']):
        info,events=analyze_run(root,out,entry,reference[entry['run_id']]);all_runs.append(info);all_events.extend(events)
        print(f"Analyze {i+1}/60 {entry['run_id']}",flush=True)
    events=[e for e in all_events if e['algorithm_profile']=='tracking_v2'];slow=[e for e in events if e['slow']];normal=[e for e in events if not e['slow']]
    assert len(events)==48 and len(slow)==11
    pairs=[]
    for e in slow:
        pool=[n for n in normal if (n['scene'],n['physics'],n['direction'])==(e['scene'],e['physics'],e['direction'])]
        fallback=False
        if not pool:pool=[n for n in normal if (n['scene'],n['physics'])==(e['scene'],e['physics'])];fallback=True
        median=statistics.median(n['recovery_s'] for n in pool)
        n=min(pool,key=lambda n:(n['run_id']!=e['run_id'],abs(n['recovery_s']-median),n['event_id']))
        old=next((a for a in all_events if a['algorithm_profile']=='legacy' and (a['scene'],a['physics'],a['repeat'],a['ordinal'])==(e['scene'],e['physics'],e['repeat'],e['ordinal'])),None)
        pairs.append({'slow_event_id':e['event_id'],'normal_event_id':n['event_id'],'legacy_event_id':old['event_id'] if old else None,
            'normal_same_run':n['run_id']==e['run_id'],'direction_fallback':fallback,'normal_pool_median_recovery_s':median})
    write(out/'events.json',all_events);table(out/'events.csv',all_events);write(out/'event-pairs.json',pairs);write(out/'runs.json',all_runs)
    chosen={p[k] for p in pairs for k in ('slow_event_id','normal_event_id','legacy_event_id') if p[k]}
    for i,e in enumerate(e for e in all_events if e['event_id'] in chosen):
        plot_event(out,e)
        if media:clip_event(root,out,e)
        print(f"Media {i+1}/{len(chosen)} {e['event_id']}",flush=True)
    # Leave all events, not just selected tails, available in the report.
    grouped=[]
    for scene in ('move_ref','line','ego_sweep','ego_sweep_reverse','spin'):
        for physics in ('no-drag','drag'):
            selected=[r for r in all_runs if r['algorithm_profile']=='tracking_v2' and r['scene']==scene and r['physics']==physics]
            ss=[r['shadow'] for r in selected];n=sum(s['frames'] for s in ss);seconds=sum(r['duration_s'] for r in selected)
            blockers=collections.Counter();sole=collections.Counter()
            for s in ss:blockers.update(s['blocked_reason_counts_overlapping']);sole.update(s['sole_blocker_counts'])
            g={'scene':scene,'physics':physics,'runs':len(ss),'frames':n,'eligible_fraction':sum(s['eligible_frames'] for s in ss)/n,
                'candidate_requests':sum(s['variants']['receipt']['candidate_requests'] for s in ss),
                'candidate_request_hz':sum(s['variants']['receipt']['candidate_requests'] for s in ss)/seconds,
                'historical_shots':sum(s['historical_shots'] for s in ss),'historical_shot_hz':sum(s['historical_shots'] for s in ss)/seconds,
                'blocked_reasons_overlapping':dict(blockers),'sole_blocker_counts':dict(sole),
                'timing_sensitive_frames':sum(s['timing_sensitivity_changed_frames'] for s in ss),
                'longest_no_qualification_s':max(s['longest_no_qualification_s'] for s in ss)}
            for key in ('matched_historical_shots','removed_historical_shots','removed_historical_front_hits','removed_historical_front_misses','retained_historical_front_hits','retained_historical_front_misses'):
                g[key]=sum(s[key] for s in ss)
            grouped.append(g)
    tags=collections.Counter(tag for e in slow for tag in e['explanatory_tags'])
    shifts=[{'event_id':e['event_id'],'old':e['recovery_s'],'continuous_same_identity':e['continuous_velocity_recovery_s']} for e in events if e['recovery_s']!=e['continuous_velocity_recovery_s']]
    result={'schema_version':1,'diagnosis_only':True,'algorithm_fixes_applied':False,'new_closed_loop_runs':0,
        'v2_reversal_events':48,'slow_events':11,'normal_events':37,'slow_event_tags_overlapping':dict(tags),
        'sampling_identity_metric_changes':shifts,'gate_groups':grouped,'pairs':pairs,'media_events':len(chosen),
        'gate_timing':load(out/'shadow-provenance.json'),'live_gate_reproduction':load(out/'live-pilot-replay-check.json'),
        'notes':['Historical velocity metric is preserved. New metric breaks continuity at >100 ms sample gaps and model identity changes.',
            'Model plate IDs are not GT slot IDs. Nearest GT association is accepted only within 0.25 m and with >0.03 m margin.',
            'Prediction point error uses recorded horizon and future own-origin displacement; it does not include drag compensation or predict a new bullet path.',
            'Gate requests are hypothetical opportunities on frozen observations, not simulator launches or a new hit rate.',
            'Overlapping reasons do not partition causal delay. No candidate-level association logs exist for all rejected detections. Unknown causes remain unknown.']}
    result['interpretation']=synthesize(out,all_events,all_runs,result)
    write(out/'summary.json',result);table(out/'gate-summary.csv',grouped);render_report(out,all_events,result)
    before=load(out/'protected-before.json');after=protected(root);same=before==after
    current=evidence(root,batch);old=load(out/'evidence-before.json')
    checks={'protected_sources_configs_binary_unchanged':same,'old_evidence_and_reports_unchanged':current==old,
        'events_48_slow_11':len(events)==48 and len(slow)==11,'original_metrics_reproduced':all(r['original_metric_reproduced'] for r in all_runs),
        'every_historical_v2_launch_matched':all(r['shadow']['matched_historical_shots']==r['shadow']['historical_shots'] for r in all_runs if r['shadow']),
        'pair_count_11':len(pairs)==11}
    write(out/'preservation-and-completeness.json',checks)
    if not all(checks.values()):raise RuntimeError(checks)
    print(json.dumps({'complete':True,'slow_tags':dict(tags),'gate_requests':sum(g['candidate_requests'] for g in grouped),'historical_shots':sum(g['historical_shots'] for g in grouped)},ensure_ascii=False),flush=True)

def render_report(out,events,result):
    esc=lambda s:html.escape(str(s),quote=True)
    fmt=lambda x:'—' if x is None else f'{x:.3f}'
    by={e['event_id']:e for e in events};cards=[]
    for pair in result['pairs']:
        e=by[pair['slow_event_id']];n=by[pair['normal_event_id']];old=by.get(pair['legacy_event_id'])
        def media(a,label):
            if a is None:return ''
            base='events/'+a['event_id']+'/'
            return f'<div><b>{label}：速度 {fmt(a["recovery_s"])} s / 命中 {fmt(a["first_front_hit_s"])} s</b><p><a href="{base}frames.csv">逐帧 CSV</a> · <a href="{base}event.json">事件数据</a> · <a href="../{a["run_id"]}/report.html">原始报告</a></p><video controls preload="none" src="{base}clip.mp4"></video><a href="{base}timeline.png"><img loading="lazy" src="{base}timeline.png"></a></div>'
        cards.append(f'<details data-scene="{e["scene"]}" data-physics="{e["physics"]}"><summary>{esc(e["scene"])} / {e["physics"]} / repeat {e["repeat"]} / 换向 {e["ordinal"]}：{fmt(e["recovery_s"])} s</summary><p>{esc("；".join(e["explanatory_tags"]))}</p><p>首次可信更新 {fmt(e["first_trusted_update_s"])} s；方向首次正确 {fmt(e["first_correct_direction_s"])} s；连续达标起点 {fmt(e["continuous_velocity_recovery_s"])} s；确认时刻 {fmt(e["velocity_confirmation_s"])} s。命中前漏失 {e["missed_shots_before_first_front"]} 发。</p><div class="pair">'+media(e,'慢事件')+media(n,'正常对照')+media(old,'同重复旧算法')+'</div></details>')
    grows=[]
    for g in result['gate_groups']:
        grows.append('<tr>'+''.join('<td>'+esc(v)+'</td>' for v in [g['scene']+'/'+g['physics'],f"{g['eligible_fraction']*100:.1f}%",fmt(g['historical_shot_hz']),fmt(g['candidate_request_hz']),g['removed_historical_front_hits'] if g['scene']!='spin' else '旋转不按正面评价',g['removed_historical_front_misses'] if g['scene']!='spin' else '—',fmt(g['longest_no_qualification_s']),g['timing_sensitive_frames']])+'</tr>')
    interpretation=result.get('interpretation',{});gate_totals=interpretation.get('moving_gate_totals',{})
    equal=interpretation.get('equal_window_comparison',[]);slow_gap=next((e['mean_duration_first_300ms_s'] for e in equal if e['group']=='slow' and e['phenomenon']=='no_enemy_detection'),0);normal_gap=next((e['mean_duration_first_300ms_s'] for e in equal if e['group']=='normal' and e['phenomenon']=='no_enemy_detection'),0);worst=interpretation.get('worst_dropout',{})
    headline=f"慢事件前 300 ms 平均无检测 {slow_gap*1000:.1f} ms，正常事件 {normal_gap*1000:.1f} ms。最慢事件方向在 {worst.get('first_correct_direction_s',0):.3f} s 转正确、首次正面命中 {worst.get('first_front_hit_s',0):.3f} s、速度幅值达标 {worst.get('continuous_velocity_recovery_s',0):.3f} s。移动场景离线候选请求 {gate_totals.get('candidate_requests_per_s',0):.2f}/s，历史实际发射 {gate_totals.get('historical_shots_per_s',0):.2f}/s。"
    notes=''.join('<li>'+esc(x)+'</li>' for x in result['notes']);tags=''.join('<li>'+esc(k)+': '+str(v)+' / 11</li>' for k,v in result['slow_event_tags_overlapping'].items())
    page=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>换向与开火门控离线诊断</title><style>body{{font:16px/1.6 system-ui;background:#f5f7f6;color:#182b28;margin:0}}main{{max-width:1450px;margin:auto;padding:28px}}section,details{{background:white;border:1px solid #dce3df;border-radius:10px;padding:20px;margin:15px 0}}summary{{cursor:pointer;font-weight:650}}.pair{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:15px}}img,video{{width:100%}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{border-bottom:1px solid #dde4e0;padding:8px;text-align:right}}td:first-child,th:first-child{{text-align:left}}a{{color:#116f61}}.scroll{{overflow:auto}}select{{padding:8px}}@media(max-width:900px){{.pair{{grid-template-columns:1fr}}}}</style><main>
<h1>换向长尾与开火门控：离线诊断</h1><p>48 次换向 · 11 次慢恢复 · 30 次新算法记录的开火资格重放。仅分析，未实施算法修复。</p>
<section><h2>主要发现</h2><p>{headline}</p><p>上述数据用于诊断。未修改算法，也不将历史发射筛选结果当作新策略命中率。</p><img src="diagnosis-overview.png" alt="Diagnostic comparison"></section><section><h2>慢事件中观察到的现象</h2><ul>{tags}</ul><p>这些现象允许重叠，表示证据关联，不代表已证明的因果分摊。原始速度恢复与首次正面命中指标均保留。</p><p><a href="findings.html">诊断结论与后续建议</a> · <a href="summary.json">汇总 JSON</a> · <a href="events.csv">全部事件 CSV</a> · <a href="event-pairs.json">对照选择规则结果</a></p></section>
<section><h2>离线门控机会</h2><p>直接调用冻结版本 C++ Shooter。以接收时刻估算姿态年龄，主要敏感性检查提前 1/2 ms；另存更宽的时间压力情景。候选请求不是实际发射；历史命中被拦截数量不等于改变策略后的损失或收益。</p><div class="scroll"><table><tr><th>条件</th><th>资格帧占比</th><th>历史发射/s</th><th>候选请求/s</th><th>拦截历史正面命中</th><th>拦截历史正面未命中</th><th>最长无资格/s</th><th>时刻敏感帧</th></tr>{''.join(grows)}</table></div><p><a href="gate-summary.csv">门控汇总 CSV</a> · <a href="live-pilot-replay-check.json">与实际新门控记录核对</a> · <a href="boundary-checks.json">门控边界检查</a></p></section>
<section><h2>原始输入图像线索</h2><p>下图裁剪自原始 JPEG，绿色细线为已输出检测的角点；绿色实心物体在原图中存在。最慢事件 8 帧漏检中，只有前 3 帧的装甲近似 ROI 内有绿色像素重叠，不足以解释全部漏检。</p><img loading="lazy" src="worst-event-input-contact-sheet.png"><p><a href="raw-image-overlap.json">图像相关性检查与限制</a></p></section><section><h2>逐事件证据</h2><label>场景 <select id="scene"><option value="">全部</option><option>line</option><option>ego_sweep</option><option>ego_sweep_reverse</option></select></label> <label>弹道 <select id="physics"><option value="">全部</option><option>no-drag</option><option>drag</option></select></label><p>横轴以真值速度过零为 0；视频使用名义 30 FPS，精确时刻和原视频帧号见 CSV。原始片段包含换向前 0.75 s 到后 2 s。</p></section>{''.join(cards)}
<section><h2>检查与解释边界</h2><ul>{notes}</ul><p><a href="preservation-and-completeness.json">源码、原始报告和证据未变检查</a> · <a href="analysis-boundary-checks.json">连续恢复检查</a> · <a href="shadow-provenance.json">重放来源</a></p></section>
<script>function filter(){{document.querySelectorAll('details[data-scene]').forEach(d=>d.hidden=(scene.value&&d.dataset.scene!==scene.value)||(physics.value&&d.dataset.physics!==physics.value));}}const scene=document.getElementById('scene'),physics=document.getElementById('physics');scene.onchange=physics.onchange=filter;</script></main></html>'''
    (out/'report.html').write_text(page)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('stage',choices=['shadow','analyze','report']);p.add_argument('--root',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--no-media',action='store_true');a=p.parse_args()
    if a.stage=='shadow':shadow_stage(a.root,a.output)
    elif a.stage=='analyze':analyze_stage(a.root,a.output,not a.no_media)
    else:report_stage(a.root,a.output)

def synthesize(out,all_events,all_runs,result):
    events=[e for e in all_events if e['algorithm_profile']=='tracking_v2'];slow=[e for e in events if e['slow']]
    keys=('no_enemy_detection','selected_plate_unobserved','innovation_rejected','association_unresolved','not_aligned','maneuver')
    equal=[]
    for is_slow in (True,False):
        chosen=[e for e in events if e['slow']==is_slow]
        for key in keys:
            seconds=[e['first_300ms_durations'][key]['seconds'] for e in chosen]
            equal.append({'group':'slow' if is_slow else 'normal','events':len(chosen),'phenomenon':key,
                'mean_duration_first_300ms_s':statistics.mean(seconds),'events_with_60ms_or_more':sum(s>=.06 for s in seconds)})
    table(out/'equal-window-comparison.csv',equal)
    radial=[];plate_velocity_checks=[];shot_lag=[]
    for e in events:
        rows=list(jsonl(out/'runs'/e['run_id']/f"event-{e['ordinal']}.jsonl"));samples=[]
        for r in rows:
            if r['selected_gt_velocity_y'] is not None and r['gt_vy'] is not None:
                plate_velocity_checks.append(abs(r['selected_gt_velocity_y']-r['gt_vy']))
            if not e['slow'] or r['event_dt_s']<.2 or r['event_dt_s']>e['recovery_s'] or r['tracker']!='tracking' or r['estimated_vy'] is None or r['gt_vy'] is None or abs(r['estimated_vy']-r['gt_vy'])<=.15:continue
            x=r['filter_state'];bearing=math.atan2(x[2],x[0]);vr=r['radial_velocity_error_mps']*math.sin(bearing);vl=r['lateral_velocity_error_mps']*math.cos(bearing)
            samples.append({'seq':r['seq'],'event_dt_s':r['event_dt_s'],'vy_error':r['estimated_vy']-r['gt_vy'],
                'radial_contribution_to_vy_error':vr,'lateral_contribution_to_vy_error':vl,'radial_dominates':abs(vr)>abs(vl)})
        if e['slow']:
            radial.append({'event_id':e['event_id'],'tracking_error_frames':len(samples),'radial_dominant_frames':sum(r['radial_dominates'] for r in samples),'samples':samples})
        if e['first_front_hit_s'] is not None:shot_lag.append(e['first_front_hit_s'])
    write(out/'velocity-error-decomposition.json',radial)
    coverage={'matched_plate_velocity_samples':len(plate_velocity_checks),'plate_vs_center_relative_vy_abs_p95_mps':q(plate_velocity_checks,.95),
        'note':'Only confidently associated visible plates in the three non-spinning movement scenes. Unmatched observations excluded; not a universal plate-center equivalence.'}
    write(out/'velocity-reference-check.json',coverage)
    moving=[g for g in result['gate_groups'] if g['scene'] in MOVING];counts=collections.Counter();sole=collections.Counter()
    for g in moving:counts.update(g['blocked_reasons_overlapping']);sole.update(g['sole_blocker_counts'])
    frames=sum(g['frames'] for g in moving);candidate=sum(g['candidate_requests'] for g in moving);shots=sum(g['historical_shots'] for g in moving)
    gates={'frames':frames,'candidate_requests':candidate,'historical_shots':shots,'seconds':324,
        'candidate_requests_per_s':candidate/324,'historical_shots_per_s':shots/324,
        'qualified_frame_fraction':sum(g['eligible_fraction']*g['frames'] for g in moving)/frames,
        'removed_historical_front_hits':sum(g['removed_historical_front_hits'] for g in moving),
        'removed_historical_front_misses':sum(g['removed_historical_front_misses'] for g in moving),
        'retained_historical_front_hits':sum(g['retained_historical_front_hits'] for g in moving),
        'retained_historical_front_misses':sum(g['retained_historical_front_misses'] for g in moving),
        'overlapping_reason_counts':dict(counts),'sole_blocker_counts':dict(sole),
        'timing_sensitivity_1_2ms_changed_frames':sum(g['timing_sensitive_frames'] for g in result['gate_groups']),
        'all_replayed_frames':sum(g['frames'] for g in result['gate_groups'])}
    worst=max(slow,key=lambda e:e['recovery_s']);worst_rows=list(jsonl(out/'runs'/worst['run_id']/f"event-{worst['ordinal']}.jsonl"))
    longest=[];current=[]
    for r in worst_rows:
        if r['raw_detection_count']==0:current.append(r)
        else:
            if len(current)>len(longest):longest=current
            current=[]
    if len(current)>len(longest):longest=current
    dropout={'event_id':worst['event_id'],'consecutive_zero_detection_frames':len(longest),
        'seq_first':longest[0]['seq'],'seq_last':longest[-1]['seq'],'start_dt_s':longest[0]['event_dt_s'],'end_dt_s':longest[-1]['event_dt_s'],
        'gt_visibility_positive_frames':sum((r['truth_visible_plates'] or 0)>0 for r in longest),
        'first_correct_direction_s':worst['first_correct_direction_s'],'first_trusted_update_s':worst['first_trusted_update_s'],
        'continuous_velocity_recovery_s':worst['continuous_velocity_recovery_s'],'first_front_hit_s':worst['first_front_hit_s']}
    risks=[e for e in events if e['first_front_hit_s'] is None or e['first_front_hit_s']>.8]
    output={'equal_window_comparison':equal,'radial_dominant_tracking_error_frames':sum(r['radial_dominant_frames'] for r in radial),
        'total_tracking_error_frames':sum(r['tracking_error_frames'] for r in radial),'worst_dropout':dropout,'velocity_reference':coverage,
        'moving_gate_totals':gates,'first_front_hit_over_800ms':[{'event_id':e['event_id'],'seconds':e['first_front_hit_s'],'missed_shots':e['missed_shots_before_first_front']} for e in risks],
        'ranked_followups':[{'priority':1,'subject':'短时无检测与选中装甲观测连续性','implemented':False},
            {'priority':2,'subject':'径向速度偏差和重建后的初始化','implemented':False},
            {'priority':3,'subject':'三稳定帧及跟踪状态对开火资格的影响','implemented':False}]}
    write(out/'interpretation.json',output)
    f=lambda x:'—' if x is None else f'{x:.3f}'
    lines_text=['# 换向长尾与开火门控离线诊断','',
        '**本轮仅诊断，没有实施算法修复，也没有启动新的闭环仿真。** 原算法、配置、验收阈值及旧报告保持不变。','',
        '读取 60 次有效对照；新算法移动场景共 48 次换向，11 次原速度恢复超过 0.3 秒，37 次正常。全部原换向指标已复算一致。',
        '## 观察与解释','',
        '慢事件完整窗口内：8/11 伴随累计至少 60 ms 无敌方检测，9/11 有模型装甲选择变化，1/11 重建轨迹。所有 11 次均有双轴未对齐，但正常事件也有，因此不能将未对齐直接当作慢恢复根因。','',
        '| 同样的换向后 300 ms 窗口 | 慢事件 11 次 | 正常事件 37 次 |','|---|---:|---:|']
    for key,label in [('no_enemy_detection','平均无敌方检测时长'),('selected_plate_unobserved','平均选中装甲未直接更新时长'),('not_aligned','平均双轴未对齐时长')]:
        a=next(x for x in equal if x['group']=='slow' and x['phenomenon']==key);b=next(x for x in equal if x['group']=='normal' and x['phenomenon']==key)
        lines_text.append(f"| {label} | {a['mean_duration_first_300ms_s']*1000:.1f} ms | {b['mean_duration_first_300ms_s']*1000:.1f} ms |")
    lines_text.extend(['',f"最慢事件连续 {len(longest)} 帧无任何检测（原视频对应 seq {longest[0]['seq']}–{longest[-1]['seq']}），这 {len(longest)} 帧的真值可见性均为正。随后 trusted_update_timeout 重建轨迹：首次可信更新 {f(worst['first_trusted_update_s'])} s，方向首次正确 {f(worst['first_correct_direction_s'])} s，首次正面命中 {f(worst['first_front_hit_s'])} s，速度幅值持续达标 {f(worst['recovery_s'])} s。",
        '真值可见性是几何检查，不能证明图像质量或检测器必然应该检出；检测中断的图像原因仍需检查原始角点/置信度和输入帧，现有日志不能给出所有被拒候选。','',
        f"在慢事件换向后 0.2 s 至原恢复时刻、且 tracker=tracking 但 vy 仍超差的 {output['total_tracking_error_frames']} 帧中，{output['radial_dominant_tracking_error_frames']} 帧的 vy 误差主要由沿视线方向的速度误差贡献。最慢事件重新跟踪后的横向误差较小，径向误差仍持续；因此不能把整段长尾解释为方向跟随迟缓。",
        f"对已可靠关联的非旋转装甲，装甲真值相对 vy 与车体真值相对 vy 的绝对差 P95 为 {f(coverage['plate_vs_center_relative_vy_abs_p95_mps'])} m/s。仅对这些关联成功的样本成立。",
        '',f"加入 >100 ms 采样缺口和装甲/轨迹身份变化断开连续性后，仅 {len(result['sampling_identity_metric_changes'])} 个新算法事件的达标时间变化；详情见 summary.json。旧指标保留，没有放宽 0.3 s 门槛。",
        '',f"48 次换向中有 {len(risks)} 次首次正面命中超过 0.8 s；其余事件不能据此宣称全部动态行为都合格。",'',
        '## 新开火门控的离线代价','',
        f"移动记录中，现有发射 {shots} 次（{shots/324:.2f}/s）；新门控在固定的观测和云台轨迹上产生 {candidate} 次限频后的候选请求（{candidate/324:.2f}/s），资格帧占比 {gates['qualified_frame_fraction']*100:.1f}%。候选请求不是实际发射。",
        f"{sole['three_stable_frames']} 帧仅因尚未累积三个稳定帧而被拦截，占移动帧的 {sole['three_stable_frames']/frames*100:.1f}%；另有 {sole['tracker_not_tracking']} 帧只有 tracking 状态条件失败，{sole['discontinuity']} 帧只有连续性条件失败。条件重叠计数另见 gate-summary.csv。",
        f"在已发生的历史发射请求上，新门控会拦截 {gates['removed_historical_front_hits']} 发历史正面命中及 {gates['removed_historical_front_misses']} 发历史正面未命中；保留 {gates['retained_historical_front_hits']} 发正面命中和 {gates['retained_historical_front_misses']} 发正面未命中。这个筛选不能用来计算新策略命中率，新的发射时间会改变弹道。",
        f"与真实新门控试验的 {result['live_gate_reproduction']['compared_frames']} 帧核对通过。接收时刻相对真实调用时刻的姿态年龄偏差 P95 约 {result['live_gate_reproduction']['receipt_proxy_minus_actual_pose_age_ms_p95']:.2f} ms；在全部 {gates['all_replayed_frames']} 帧上提前 1/2 ms 的敏感性检查改变资格的帧数为 {gates['timing_sensitivity_1_2ms_changed_frames']}。这不是所有时间误差的严格上界。",
        '另存“减去整段 pipeline 时长”和“再提前 5 ms”的宽范围压力情景。它们可能早于姿态快照，产生负年龄，因此不能与更接近实际调用的主结果等权解释。','',
        '## 待讨论的下一轮修复建议（本轮未实施）','',
        '1. **先定位无检测与选中装甲更新中断。** 以最慢事件和 11 组正常对照中的原始画面、角点、类别、置信度为依据，检查检测丢失与模型装甲切换。预期减少超时重建与无效旧速度延续。风险是简单延长保持时间会延长错误预测；最小验证先回放这些异常及匹配正常事件，再做目标平移和两种己方移动各一次闭环。',
        '2. **再检查径向速度与重建初始化。** 优先处理已经方向正确、横向响应正常但径向偏差持续的事件，保留径向/横向分量和实际首次命中双重评价。预期减少深度噪声引起的速度长尾。风险是强制压低径向运动会损伤真实接近/远离运动；最小验证必须加入真实径向加减速和斜向运动的合成观测，并重放本轮异常和正常样本。',
        '3. **开火门控暂不直接作为默认。** 先讨论连续三稳定帧与瞬时 tracking 状态的资格损失，保留当前双轴对齐和姿态新鲜度的证据。任何后续策略变更需单独比较有效命中/秒、首次命中和误发；最小验证为静止、目标平移、己方往返和旋转的独立门控对照。','',
        '## 产物与复现','',
        '- `report.html`：11 组慢事件、正常对照、同条件旧算法，交互筛选和视频。',
        '- `events.json/csv`：新旧算法所有换向事件；`runs/*/frames.jsonl`：逐帧证据及完整观测。',
        '- `shadow/*/frame-audit.csv`、`historical-shot-audit.csv`：门控原因和历史发射匹配。',
        '- `velocity-error-decomposition.json`、`equal-window-comparison.csv`：等长窗口及速度分量分析。',
        '- `preservation-and-completeness.json`：原始证据、报告、参数与源码未变检查。','',
        '所有实际命中统计沿用原记录。未标记人工视觉验收通过，未升级基线，未做任何算法修复。'])
    if (out/'raw-image-overlap.json').exists():
        image_check=load(out/'raw-image-overlap.json');output['raw_image_association']=image_check['groups']
        overlap=sum(r['green_overlap'] for r in image_check['worst_dropout_frames']);n=len(image_check['worst_dropout_frames'])
        extra=['## 原始图像补充检查','',f'最慢事件的 {n} 帧连续漏检中，{overlap} 帧的正面装甲近似 ROI 内有绿色像素重叠，其余帧没有。原始图像中可见绿色弹丸；源码使用不透明绿色发光材质。',
            f"全部可投影的移动帧中，绿色重叠时无敌方检测比例为 {image_check['groups']['True']['no_enemy_detection_fraction']*100:.1f}%，无重叠时为 {image_check['groups']['False']['no_enemy_detection_fraction']*100:.1f}%。分场景并不一致，射击、视角和场景相位有混杂，不能据此认定弹丸是根因。",
            '这只是离线图像相关性检查，没有重新渲染、修改检测器或试验关闭弹丸显示。后续第一项建议应把弹丸遮挡、视角变化和检测候选筛除链一起核对。','','[输入图像对照](worst-event-input-contact-sheet.png) · [逐帧图像检查](raw-image-overlap.csv)','']
        pos=lines_text.index('## 待讨论的下一轮修复建议（本轮未实施）');lines_text[pos:pos]=extra
        write(out/'interpretation.json',output)
    lines_text.insert(lines_text.index('## 新开火门控的离线代价'), '两次首次正面命中超过 0.8 s 的事件，其早期请求到出膛约 0.74–3.59 ms；未发现长时间排队等待出膛。第一段延迟主要包含之前弹丸未命中、重新跟踪与瞄准过程，不能把它全部归给开火等待。\n')
    (out/'findings.md').write_text('\n'.join(lines_text)+'\n')
    (out/'findings.html').write_text('<!doctype html><meta charset="utf-8"><title>诊断结论</title><style>body{font:16px/1.7 system-ui;max-width:1100px;margin:2em auto;padding:1em}pre{white-space:pre-wrap}</style><p><a href="report.html">返回交互报告</a></p><pre>'+html.escape('\n'.join(lines_text))+'</pre>')
    return output


def report_stage(root,out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    events=load(out/'events.json');runs=load(out/'runs.json');result=load(out/'summary.json')
    result['interpretation']=synthesize(out,events,runs,result);write(out/'summary.json',result)
    fig,axes=plt.subplots(2,2,figsize=(13,9),layout='constrained');equal=result['interpretation']['equal_window_comparison']
    names=['no_enemy_detection','selected_plate_unobserved','not_aligned'];x=np.arange(3)
    for i,group in enumerate(['normal','slow']):
        values=[next(e['mean_duration_first_300ms_s']*1000 for e in equal if e['group']==group and e['phenomenon']==name) for name in names]
        axes[0,0].bar(x+(i-.5)*.35,values,.35,label=group,color=['#19796e','#cb7431'][i])
    axes[0,0].set_xticks(x,['No detection','Selected plate not updated','Not aligned'],rotation=15);axes[0,0].set_ylabel('Mean time / ms');axes[0,0].set_title('Same first 300 ms after reversal');axes[0,0].legend()
    ev=[e for e in events if e['algorithm_profile']=='tracking_v2'];axes[0,1].scatter([e['recovery_s'] or 2 for e in ev],[e['first_front_hit_s'] or 2 for e in ev],c=['#cb7431' if e['slow'] else '#19796e' for e in ev]);axes[0,1].axvline(.3,color='gray',linestyle='--');axes[0,1].axhline(.8,color='gray',linestyle='--');axes[0,1].set_xlabel('Velocity recovery / s');axes[0,1].set_ylabel('First front hit / s');axes[0,1].set_title('48 events; orange = velocity recovery > 0.3 s')
    gs=[g for g in result['gate_groups'] if g['scene'] in MOVING];x=np.arange(len(gs))
    axes[1,0].bar(x-.18,[g['historical_shot_hz'] for g in gs],.36,label='historical launches',color='#6b7280');axes[1,0].bar(x+.18,[g['candidate_request_hz'] for g in gs],.36,label='shadow candidate requests',color='#19796e');axes[1,0].set_xticks(x,[g['scene']+'/'+g['physics'] for g in gs],rotation=25,ha='right',fontsize=8);axes[1,0].set_ylabel('Count / second');axes[1,0].legend(fontsize=8);axes[1,0].set_title('Candidates are not new shots or hits')
    solo=result['interpretation']['moving_gate_totals']['sole_blocker_counts'];labels=['three_stable_frames','discontinuity','tracker_not_tracking','yaw_misaligned','pitch_misaligned'];axes[1,1].barh(labels,[solo.get(k,0) for k in labels],color='#19796e');axes[1,1].invert_yaxis();axes[1,1].set_xlabel('Frames (only this condition blocks)');axes[1,1].set_title('Moving scenes: sole blockers')
    for ax in axes.flat:ax.grid(alpha=.2);ax.set_axisbelow(True)
    fig.savefig(out/'diagnosis-overview.png',dpi=160);fig.savefig(out/'diagnosis-overview.svg');plt.close(fig)
    render_report(out,events,result)
    exp,study,batch=paths(root);checks=load(out/'preservation-and-completeness.json')
    checks['protected_sources_configs_binary_unchanged']=protected(root)==load(out/'protected-before.json')
    checks['old_evidence_and_reports_unchanged']=evidence(root,batch)==load(out/'evidence-before.json')
    write(out/'preservation-and-completeness.json',checks);assert all(checks.values())
    print('Final report rendered; original evidence and algorithm remain unchanged',flush=True)

if __name__=='__main__':main()
