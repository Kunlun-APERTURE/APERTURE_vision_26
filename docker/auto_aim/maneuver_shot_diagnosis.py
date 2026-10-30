"""Read-only per-shot causal ledger and error decomposition for frozen S/I runs."""
import argparse
import bisect
from collections import Counter
import html
import json
import math
from pathlib import Path
import re

import manage
from decision_benchmark import sha,verify_bundle
from perception_study import protected,quantile
from report import load,write

PARENT=manage.OPS/'experiments/maneuver-fire-v6-20261026'
ROOT=manage.OPS/'experiments/maneuver-shot-diagnosis-20261026'
VARIANTS=('baseline','BS','P','S','I')
SOURCE_FILES=('docker/auto_aim/bridge.py','docker/auto_aim/sync_bridge.py',
 'vision_simulator_26/src/ros2/plugin.rs','vision_simulator_26/src/systems/gimbal_pid.rs',
 'vision_simulator_26/src/bench/events.rs','vision_simulator_26/src/systems/projectile.rs',
 'sp_vision_25/tools/trajectory.cpp','sp_vision_25/tasks/auto_aim/shooter.cpp')


def angle(x):return math.remainder(x,2*math.pi)
def stats(values):
    values=[float(x) for x in values if x is not None and math.isfinite(x)]
    return dict(n=len(values),p50=quantile(values,.5),p95=quantile(values,.95),mean=sum(values)/len(values) if values else None)


def freeze(root):
    root.mkdir(parents=True,exist_ok=True)
    if (root/'contract.json').exists():return load(root/'contract.json')
    c=load(PARENT/'contract.json');s=load(PARENT/'state.json');assert s['state']=='completed'
    files={str(PARENT/'report-seal.json'):sha(PARENT/'report-seal.json')}
    for name,h in load(PARENT/'report-seal.json').items():
        assert sha(PARENT/name)==h
        files[str(PARENT/name)]=h
    sources=[]
    for row in [x for x in s['runs'] if x['phase']=='screen']:
        source=manage.OPS/'experiments'/row['run_id'];score=PARENT/'normalized-scores'/row['run_id']/'shots.jsonl'
        paths=[source/name for name in ('manifest.json','run-state.json','telemetry.jsonl','session-evidence/events.jsonl')]+[score]
        sources.append(dict(row,files={str(p):sha(p) for p in paths}))
    bundle=Path(c['bundles']['S']);meta=verify_bundle(bundle)
    code={str(bundle/p):meta['files'][p] for p in SOURCE_FILES}
    contract=dict(version=1,parent=str(PARENT),source_runs=sources,parent_files=files,protected=c['protected'],
      code_files=code,scoring='No new shots; exact/weighted scores imported without changes',
      ledger='Unique forwarded pulse before launch within 250ms; command capture timestamp checked against gimbal accepted timestamp at launch and observable command topic. No latest-advice proxy.',
      timing='Use recorded stamp_ns for target lookup. Infer physical flight duration from actual horizontal displacement/initial horizontal velocity; do not assume recorded stamp and physics elapsed durations are interchangeable.',
      closest='Piecewise-linear closest approach of observed projectile centre and selected plate centre in the common recorded clock; no collision reclassification or ballistic reintegration.',
      counterfactual='At the same observed closest-approach instant, replace only initial launch velocity with the command direction. Diagnostic linear velocity-difference displacement, not a new hit simulation.',
      no_algorithm_changes=True,no_new_live_trials=True)
    write(root/'contract.json',contract);return contract


def analyze(root):
    import numpy as np
    from perception_attribution_analysis import timeline,match_truth
    c=freeze(root);ledger=[];orphans=[];counts=Counter();errors=[]
    for row in c['source_runs']:
        source=manage.OPS/'experiments'/row['run_id'];run=load(source/'run-state.json')
        normalized=[json.loads(l) for l in (PARENT/'normalized-scores'/row['run_id']/'shots.jsonl').open()]
        shots=sorted((x for x in run['shot_events'] if x['kind']==0),key=lambda x:x['stamp_ns'])
        obs=[json.loads(l)['data'] for l in (source/'session-evidence/events.jsonl').open() if json.loads(l)['kind']=='result']
        pulses=[x for x in obs if x.get('fire_forwarded')];used=set();traces={};gimbal=[];commands=[]
        for line in (source/'telemetry.jsonl').open():
            p=json.loads(line);d=p['data']
            if p['kind']=='projectile_trace':
                for x in d['projectiles']:traces.setdefault(x['shot_seq'],[]).append((d['stamp_ns'],x['position']))
            elif p['kind']=='gimbal_trace':gimbal.append(d)
            elif p['kind']=='command' and d['fire_advice']:commands.append(d)
        truth=timeline(source)
        for shot in shots:
            counts['shots']+=1
            candidates=[(i,x) for i,x in enumerate(pulses) if i not in used and 0<=shot['stamp_ns']-x['receipt_ns']<=250_000_000]
            assert len(candidates)==1,(source.name,shot['shot_seq'],'ambiguous pulse',len(candidates))
            i,p=candidates[0];used.add(i);f,r=p['frame'],p['result'];stamp=f['stamp_ns']
            captures=[x for x in gimbal if x['command_stamp_ns']==stamp]
            accepted=sorted({x['accepted_wall_ns'] for x in captures})
            assert len(accepted)==1 and accepted[0]==shot['stamp_ns'],(source.name,shot['shot_seq'],'execution command mismatch')
            observed=[x for x in commands if x['stamp_ns']==stamp]
            assert len(observed)<=1
            if observed:
                assert abs(angle(math.radians(observed[0]['yaw_deg'])-r['yaw']))<1e-12
                counts['command_topic_confirmed']+=1
            else:counts['missing_command_topic']+=1
            assert r['fire_advice'] and p['command_emitted']
            counts['unique_pulse_gimbal_confirmed']+=1
            score=next(x for x in normalized if x['shot_seq']==shot['shot_seq'])
            launch=np.asarray(shot['world_point']);v=np.asarray(shot['velocity']);yaw=math.atan2(v[1],v[0]);pitch=math.atan2(v[2],np.linalg.norm(v[:2]))
            prediction_stamp=stamp+round(r['prediction_horizon_s']*1e9)
            t=(shot['stamp_ns']-run['measurement_start_stamp_ns'])*1e-9
            phase='early' if t<1 else 'after_turn' if row['scene']=='hf_turn90_3' and t>=3.2 else 'moving'
            item=dict(run_id=source.name,variant=row['variant'],scene=row['scene'],sensor_seed=row['sensor_seed'],
              shot_seq=shot['shot_seq'],outcome=score['outcome'],weighted_hit_score=score['weighted_hit_score'],phase=phase,
              frame_seq=f['seq'],capture_stamp_ns=stamp,decision_stamp_ns=r['evaluation_clocks']['aim_now_ns'],
              receipt_stamp_ns=p['receipt_ns'],launch_stamp_ns=shot['stamp_ns'],gimbal_accepted_stamp_ns=accepted[0],
              command_topic_confirmed=bool(observed),selected_plate_id=r['selected_plate_id'],actual_hit_plate_id=score['armor_id'],
              capture_to_launch_ms=(shot['stamp_ns']-stamp)*1e-6,decision_to_launch_ms=(shot['stamp_ns']-r['evaluation_clocks']['aim_now_ns'])*1e-6,
              receipt_to_launch_ms=(shot['stamp_ns']-p['receipt_ns'])*1e-6,
              command_yaw_rad=r['yaw'],launch_yaw_rad=yaw,command_elevation_rad=-r['pitch'],launch_elevation_rad=pitch,
              launch_yaw_error_rad=angle(yaw-r['yaw']),launch_pitch_error_rad=pitch+r['pitch'],
              checked_yaw_error_rad=r['fire_check']['yaw_error_rad'],checked_yaw_tolerance_rad=r['fire_check']['yaw_tolerance_rad'],
              prediction_horizon_s=r['prediction_horizon_s'],predicted_arrival_stamp_ns=prediction_stamp,physical_plate_id=None)
            timing=(r.get('diagnosis') or {}).get('aimer') or {}
            if 'processing_age_s' in timing and 'launch_delay_s' in timing:
                item.update(assumed_launch_delay_ms=timing['launch_delay_s']*1e3,
                  launch_offset_mismatch_ms=timing['launch_delay_s']*1e3-item['decision_to_launch_ms'],
                  solver_flight_s=r['prediction_horizon_s']-timing['processing_age_s']-timing['launch_delay_s'])
            link=next((x for x in (r.get('continuity',{}).get('image_association') or {}).get('observations',[]) if x['plate_id']==r['selected_plate_id']),None)
            observations=r.get('observations',[]);identity=None
            if link and observations:
                ds=[float(np.linalg.norm(np.mean(x['corners'],axis=0)-link['image_center'])) for x in observations]
                idx=int(np.argmin(ds));matches=match_truth(f,observations,truth(stamp))
                if ds[idx]<=4 and idx in matches:identity=matches[idx]['plate']['id']
            if identity is not None:
                counts['selected_plate_matched']+=1;item['physical_plate_id']=identity
                item['hit_same_selected_plate']=score['armor_id']==identity if score['outcome']=='target_armor' else None
                target=next((x for x in truth(prediction_stamp) if x['id']==identity),None)
                if target:
                    gt=np.asarray(target['position']);aim=np.asarray(r['aim_point'])+f['gimbal_position_map'];desired=math.atan2(gt[1]-launch[1],gt[0]-launch[0])
                    item.update(nominal_xy_error_m=float(np.linalg.norm((aim-gt)[:2])),nominal_z_error_m=abs(float(aim[2]-gt[2])),
                      nominal_prediction_yaw_error_rad=angle(r['yaw']-desired),nominal_total_yaw_error_rad=angle(yaw-desired))
                points=[(shot['stamp_ns'],shot['world_point'])]+traces.get(shot['shot_seq'],[])
                points=sorted({int(t):p for t,p in points}.items());best=None
                for (ta,pa),(tb,pb) in zip(points,points[1:]):
                    ga=next((x for x in truth(ta) if x['id']==identity),None);gb=next((x for x in truth(tb) if x['id']==identity),None)
                    if ga is None or gb is None or tb-ta>100_000_000:continue
                    pa,pb=np.asarray(pa),np.asarray(pb);ca,cb=np.asarray(ga['position']),np.asarray(gb['position'])
                    a=pa-ca;change=(pb-cb)-a;u=float(np.clip(-a@change/max(float(change@change),1e-30),0,1))
                    diff=a+u*change;distance=float(np.linalg.norm(diff))
                    if best is None or distance<best[0]:
                        best=(distance,ta+round(u*(tb-ta)),pa+u*(pb-pa),ca+u*(cb-ca),np.asarray(ga['normal'])+u*(np.asarray(gb['normal'])-ga['normal']))
                if best:
                    distance,closest_stamp,bullet,center,normal=best;normal/=np.linalg.norm(normal)
                    right=np.cross([0,0,1],normal);right/=np.linalg.norm(right);up=np.cross(normal,right)
                    ray=center-launch;radial=ray[:2]/np.linalg.norm(ray[:2]);lateral=np.array([-radial[1],radial[0]])
                    physics_t=float((bullet[:2]-launch[:2])@v[:2]/(v[:2]@v[:2]))
                    horizontal_residual=float(np.linalg.norm(bullet[:2]-launch[:2]-v[:2]*physics_t))
                    assert horizontal_residual<.002 and physics_t>=0
                    elevation=-r['pitch'];ideal_velocity=25*np.array([math.cos(elevation)*math.cos(r['yaw']),math.cos(elevation)*math.sin(r['yaw']),math.sin(elevation)])
                    execution=(v-ideal_velocity)*physics_t;ideal=bullet-execution;total=bullet-center;ideal_error=ideal-center
                    assert np.linalg.norm(total-ideal_error-execution)<1e-9
                    target_yaw=math.atan2(ray[1],ray[0]);pred_yaw=angle(r['yaw']-target_yaw);exec_yaw=angle(yaw-r['yaw']);total_yaw=angle(yaw-target_yaw)
                    assert abs(angle(total_yaw-pred_yaw-exec_yaw))<1e-10
                    item.update(closest_stamp_ns=closest_stamp,closest_recorded_distance_m=distance,
                      predicted_minus_closest_arrival_ms=(prediction_stamp-closest_stamp)*1e-6,
                      observed_stamp_flight_s=(closest_stamp-shot['stamp_ns'])*1e-9,physical_displacement_flight_s=physics_t,
                      horizontal_trace_reconstruction_residual_m=horizontal_residual,
                      prediction_yaw_error_rad=pred_yaw,total_yaw_error_rad=total_yaw,
                      execution_reduces_abs_yaw_error=abs(total_yaw)<abs(pred_yaw),
                      closest_total_lateral_m=float(total[:2]@lateral),closest_ideal_lateral_m=float(ideal_error[:2]@lateral),
                      closest_execution_lateral_m=float(execution[:2]@lateral),
                      closest_total_up_m=float(total@up),closest_ideal_up_m=float(ideal_error@up),closest_execution_up_m=float(execution@up),
                      closest_plate_horizontal_m=float(total@right),closest_plate_normal_m=float(total@normal),
                      closest_point=bullet.tolist(),closest_plate_center=center.tolist())
                    if 'solver_flight_s' in item:
                        item['flight_horizon_minus_observed_ms']=(item['solver_flight_s']-item['observed_stamp_flight_s'])*1e3
                        assert abs(item['predicted_minus_closest_arrival_ms']-item['launch_offset_mismatch_ms']-item['flight_horizon_minus_observed_ms'])<.002
                    filter=r.get('continuity',{}).get('filter') or {};plates=filter.get('plates',[])
                    sid=r['selected_plate_id'];origin=np.asarray(f['gimbal_position_map'])
                    if sid<len(plates) and plates[sid].get('initialized'):
                        state=plates[sid]['state'];h=r['prediction_horizon_s']
                        cv=origin+np.asarray([state[2*j]+h*state[2*j+1] for j in range(3)])
                        aim=origin+np.asarray(r['aim_point']);normal_ref=next((a for a in truth(prediction_stamp) if a['id']==identity),None)
                        cvyaw=math.atan2(cv[1]-launch[1],cv[0]-launch[0]);ideal_actual_yaw=math.atan2(center[1]-launch[1],center[0]-launch[0])
                        item['own_CV_yaw_error_at_closest_rad']=angle(cvyaw-ideal_actual_yaw)
                        if 'launch_offset_mismatch_ms' in item:
                            corrected_h=max(0,h-item['launch_offset_mismatch_ms']*1e-3)
                            corrected_cv=origin+np.asarray([state[2*j]+corrected_h*state[2*j+1] for j in range(3)])
                            item['own_CV_launch_offset_only_yaw_error_rad']=angle(math.atan2(corrected_cv[1]-launch[1],corrected_cv[0]-launch[0])-ideal_actual_yaw)
                        if normal_ref:
                            actual=np.asarray(normal_ref['position']);radial_ref=(actual-launch)[:2];radial_ref/=np.linalg.norm(radial_ref)
                            tangent_ref=np.array([-radial_ref[1],radial_ref[0]])
                            for label,point in (('own_CV',cv),('candidate',aim)):
                                diff=point-actual
                                item[label+'_nominal_radial_error_m']=float(diff[:2]@radial_ref)
                                item[label+'_nominal_lateral_error_m']=float(diff[:2]@tangent_ref)
                                item[label+'_nominal_z_error_m']=float(diff[2])
                                item[label+'_nominal_xy_error_m']=float(np.linalg.norm(diff[:2]))
                        banks=filter.get('motion_v6',{}).get('plates',[])
                        if sid<len(banks) and banks[sid].get('initialized'):
                            b=banks[sid];actual_h=(closest_stamp-stamp)*1e-9
                            def projected(state,tau,h):
                                g=tau*(h+tau*math.expm1(-h/tau))
                                return np.asarray([state[3*j]+h*state[3*j+1]+g*state[3*j+2] for j in range(3)])
                            if row['variant']=='S':retimed=projected(b['single_state'],.15,actual_h)+origin
                            else:retimed=sum(p*projected(m['state'],m['tau'],actual_h) for p,m in zip(b['probability'],b['models']))+origin
                            rt_yaw=math.atan2(retimed[1]-launch[1],retimed[0]-launch[0])
                            item['candidate_retimed_yaw_error_rad']=angle(rt_yaw-ideal_actual_yaw)
                            item['candidate_retimed_xy_error_m']=float(np.linalg.norm((retimed-center)[:2]))
                            if 'launch_offset_mismatch_ms' in item:
                                corrected_h=max(0,h-item['launch_offset_mismatch_ms']*1e-3)
                                if row['variant']=='S':corrected=projected(b['single_state'],.15,corrected_h)+origin
                                else:corrected=sum(p*projected(m['state'],m['tau'],corrected_h) for p,m in zip(b['probability'],b['models']))+origin
                                item['candidate_launch_offset_only_yaw_error_rad']=angle(math.atan2(corrected[1]-launch[1],corrected[0]-launch[0])-ideal_actual_yaw)
            ledger.append(item)
        for i,p in enumerate(pulses):
            if i not in used:
                orphans.append(dict(run_id=source.name,variant=row['variant'],scene=row['scene'],frame_seq=p['frame']['seq'],
                  receipt_minus_end_ms=(p['receipt_ns']-run['measurement_end_stamp_ns'])*1e-6,
                  boundary_exclusion=p['receipt_ns']>run['measurement_end_stamp_ns']))
    assert len(ledger)==461 and counts['unique_pulse_gimbal_confirmed']==461
    assert len(orphans)==2 and all(x['boundary_exclusion'] for x in orphans)
    write(root/'shot-ledger.json',ledger);write(root/'unfired-pulses.json',orphans)
    groups=[]
    metrics=('capture_to_launch_ms','decision_to_launch_ms','receipt_to_launch_ms','launch_yaw_error_rad','launch_pitch_error_rad',
      'nominal_xy_error_m','nominal_prediction_yaw_error_rad','nominal_total_yaw_error_rad','prediction_yaw_error_rad',
      'total_yaw_error_rad','predicted_minus_closest_arrival_ms','closest_recorded_distance_m','closest_total_lateral_m',
      'closest_ideal_lateral_m','closest_execution_lateral_m','closest_total_up_m','closest_ideal_up_m','closest_execution_up_m',
      'assumed_launch_delay_ms','launch_offset_mismatch_ms','solver_flight_s','flight_horizon_minus_observed_ms',
      'own_CV_yaw_error_at_closest_rad','own_CV_nominal_radial_error_m','own_CV_nominal_lateral_error_m','own_CV_nominal_z_error_m',
      'own_CV_nominal_xy_error_m','candidate_nominal_radial_error_m','candidate_nominal_lateral_error_m','candidate_nominal_z_error_m',
      'candidate_nominal_xy_error_m','candidate_retimed_yaw_error_rad','candidate_retimed_xy_error_m',
      'own_CV_launch_offset_only_yaw_error_rad','candidate_launch_offset_only_yaw_error_rad')
    for variant in VARIANTS:
        for scene in ('all','hf_uniform_3','hf_turn90_3'):
            for phase in ('all','early','moving','after_turn'):
                rr=[x for x in ledger if x['variant']==variant and (scene=='all' or x['scene']==scene) and (phase=='all' or x['phase']==phase)]
                if not rr:continue
                scored=[x for x in rr if 'closest_recorded_distance_m' in x];g=dict(variant=variant,scene=scene,phase=phase,shots=len(rr),
                  hits=sum(x['outcome']=='target_armor' for x in rr),matched=len(scored),
                  weighted=sum(x['weighted_hit_score'] for x in rr)/len(rr),
                  different_plate_hits=sum(x.get('hit_same_selected_plate') is False for x in rr),
                  execution_cancels_yaw=sum(x['execution_reduces_abs_yaw_error'] for x in scored),
                  metrics={k:stats([abs(x[k]) for x in rr if k in x]) for k in metrics},
                  signed_means={k:sum(x[k] for x in rr if k in x)/sum(k in x for x in rr) if any(k in x for x in rr) else None for k in metrics})
                groups.append(g)
    write(root/'analysis-result.json',dict(counts=dict(counts),groups=groups,unfired_pulses=len(orphans),
      source_runs=len(c['source_runs']),no_new_live_trials=True))
    plots(root);render(root)


def plots(root):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    rows=load(root/'shot-ledger.json')
    fig,axes=plt.subplots(1,2,figsize=(12,4.5),layout='constrained')
    for ax,variant in zip(axes,('S','I')):
        selected=[x for x in rows if x['variant']==variant and x['scene']=='hf_turn90_3' and 'candidate_launch_offset_only_yaw_error_rad' in x]
        for index,(phase,label) in enumerate((('early','Early'),('moving','Moving before turn'),('after_turn','After turn'))):
            rr=[x for x in selected if x['phase']==phase]
            for shift,key,name,color in ((-.23,'prediction_yaw_error_rad','Recorded command','#3568a8'),
              (0,'candidate_launch_offset_only_yaw_error_rad','Launch offset only','#2d9a89'),
              (.23,'candidate_retimed_yaw_error_rad','Oracle recorded horizon','#d99439')):
                value=quantile([abs(x[key]) for x in rr],.5)
                ax.bar(index+shift,0 if value is None else 1000*value,width=.22,color=color,label=name if index==0 else None)
        ax.set(xticks=range(3),xticklabels=[label+'\nn='+str(sum(x['phase']==phase for x in selected)) for phase,label in
          (('early','Early'),('moving','Before turn'),('after_turn','After turn'))],title=variant+' / 90-degree turn shots',ylabel='Median absolute command yaw error (mrad)')
        ax.grid(axis='y',alpha=.2);ax.set_axisbelow(True)
    axes[0].legend(fontsize=8)
    fig.suptitle('Posthoc sensitivity only: same launched shots, no new hit-rate experiment',fontsize=11)
    fig.savefig(root/'timing-sensitivity.png',dpi=160);plt.close(fig)


def render(root):
    a=load(root/'analysis-result.json');num=lambda v:'—' if v is None else f'{v:.4f}'
    lines=['# S/I 实际发射逐弹诊断','',
      '仅分析已完成的50轮主场景射击，未新增射击、修改算法、物理或评分。461发弹丸都由唯一实际转发脉冲关联，另以云台接受的捕获时间戳和出膛时刻核对；460条还有控制命令遥测，1条该话题缺失但脉冲与云台证据保留。',
      '两个未发射脉冲的接收时刻均已越过测量结束边界，单列保留，不把它们解释为测量内漏射。',
      '[逐弹账本](shot-ledger.json) · [分析结果](analysis-result.json) · [来源与方法](contract.json) · [未发射脉冲](unfired-pulses.json) · [证据校验](validation.json)','',
      '结论：未发现S/I因额外等待导致发射延迟增加；相同发射中，预测方向、执行跟随与未来时刻存在不同程度的耦合。总体XY尾部误差下降不足以说明实际射击窗口的典型方向误差下降。',
      '本批精确关联的决策→出膛为4.3295ms，而原补偿15ms。仅修正这项差值的事后敏感性在转向场景有信号，但匀速S反而退化，且变向后即使用实际时域仍残留较大方向误差。不能把一个延迟参数当作全部根因。','',
      '![同一实际发射的时域敏感性](timing-sensitivity.png)','',
      '## 实际发射的时序与跟随误差','',
      '| 配置 | 发射数 | 捕获→出膛P50/ms | 决策→出膛P50/ms | 接收→出膛P50/ms | 出膛相对命令yaw误差P50/rad | P95/rad |','|---|---:|---:|---:|---:|---:|---:|']
    for g in a['groups']:
        if g['scene']!='all' or g['phase']!='all':continue
        m=g['metrics'];lines.append('| '+g['variant']+' | '+str(g['shots'])+' | '+' | '.join(num(m[k]['p50']) for k in ('capture_to_launch_ms','decision_to_launch_ms','receipt_to_launch_ms','launch_yaw_error_rad'))+' | '+num(m['launch_yaw_error_rad']['p95'])+' |')
    lines+=['','## 所选物理板的最近接记录点','',
      '| 配置/场景 | 发射/匹配 | 命中其他板数 | 预测yaw误差P50 | 实际总yaw误差P50 | 预测yaw误差P95 | 实际总yaw误差P95 | 预测时刻−最近接时刻均值/ms |','|---|---:|---:|---:|---:|---:|---:|']
    for g in a['groups']:
        if g['phase']!='all' or g['variant']=='baseline':continue
        m=g['metrics'];lines.append('| '+g['variant']+'/'+g['scene']+' | '+str(g['shots'])+'/'+str(g['matched'])+' | '+str(g['different_plate_hits'])+' | '+' | '.join(num(m[k][q]) for k,q in [('prediction_yaw_error_rad','p50'),('total_yaw_error_rad','p50'),('prediction_yaw_error_rad','p95'),('total_yaw_error_rad','p95')])+' | '+num(g['signed_means']['predicted_minus_closest_arrival_ms'])+' |')
    lines+=['','预测yaw误差=命令方向−最近接时刻真实板方向；执行误差=出膛方向−命令方向；两者之和等于实际总误差。最近接点在已记录弹丸与板中心之间作分段线性求解，不是新碰撞判定。表中末列为带符号均值，正值表示预测所指时刻较晚。','',
      '## 同一最近接时刻的横向和垂向分解','',
      '| 配置/场景 | 匹配数 | 理想命令横向绝对误差P50/m | 执行横向P50/m | 总横向P50/m | 理想命令up绝对误差P50/m | 执行upP50/m | 总upP50/m |','|---|---:|---:|---:|---:|---:|---:|---:|']
    for g in a['groups']:
        if g['phase']!='all' or g['variant']=='baseline':continue
        m=g['metrics'];lines.append('| '+g['variant']+'/'+g['scene']+' | '+str(g['matched'])+' | '+' | '.join(num(m[k]['p50']) for k in ('closest_ideal_lateral_m','closest_execution_lateral_m','closest_total_lateral_m','closest_ideal_up_m','closest_execution_up_m','closest_total_up_m'))+' |')
    lines+=['','理想命令点只用实际弹丸位置减去初速度差×物理飞行时间，复用同一重力轨迹；没有重算相交时刻、目标响应或新命中率。主场景自车静止，未把这一分解推广至自车速度未知的情形。','',
      '## 同一实际发射、同一输入的CV与候选预测','',
      '| 候选/场景 | 匹配数 | 原CV径向误差P50/m | 候选径向误差P50/m | 原CV横向误差P50/m | 候选横向误差P50/m | 原CV瞄准yaw误差P50/rad | 候选瞄准yaw误差P50/rad |','|---|---:|---:|---:|---:|---:|---:|---:|']
    for g in a['groups']:
        if g['phase']!='all' or g['variant'] not in ('S','I'):continue
        m=g['metrics'];lines.append('| '+g['variant']+'/'+g['scene']+' | '+str(m['own_CV_nominal_radial_error_m']['n'])+' | '+' | '.join(num(m[k]['p50']) for k in ('own_CV_nominal_radial_error_m','candidate_nominal_radial_error_m','own_CV_nominal_lateral_error_m','candidate_nominal_lateral_error_m','own_CV_yaw_error_at_closest_rad','prediction_yaw_error_rad'))+' |')
    lines+=['','前四项对原预测所指时刻的真值计算；最后两项对实际最近接时刻方向计算。CV是该候选在自身闭环图像上继续维护的同一CV状态，并非另一组的轨迹。未重新运行CV闭环，不能把这项当作候选相对BS的因果命中收益。','',
      '## 预测时刻与实际最近接时刻的差','',
      '| 配置 | 名义发射补偿P50/ms | 补偿−实际决策出膛P50/ms | 名义飞行−记录飞行均值/ms | 总时刻差均值/ms | 候选按实际时域重算yaw误差P50/rad |','|---|---:|---:|---:|---:|---:|']
    for g in a['groups']:
        if g['scene']!='all' or g['phase']!='all':continue
        m=g['metrics'];lines.append('| '+g['variant']+' | '+num(m['assumed_launch_delay_ms']['p50'])+' | '+num(m['launch_offset_mismatch_ms']['p50'])+' | '+num(g['signed_means']['flight_horizon_minus_observed_ms'])+' | '+num(g['signed_means']['predicted_minus_closest_arrival_ms'])+' | '+num(m['candidate_retimed_yaw_error_rad']['p50'])+' |')
    lines+=['','按真实最近接时域重算只作事后敏感性分析，不输入算法、不重选板、不重算云台或弹道；实际最近接时刻本身依赖当前弹道。不能将其作为可提前获得的发射时域。','',
      '## 只更正已测得的发射补偿差：单点敏感性','',
      '| 配置/场景 | 原命令yaw误差P50/rad | 仅减发射补偿差P50/rad | 完整实际时域事后重算P50/rad |','|---|---:|---:|---:|']
    for g in a['groups']:
        if g['phase']!='all' or g['variant'] not in ('S','I'):continue
        m=g['metrics'];lines.append('| '+g['variant']+'/'+g['scene']+' | '+' | '.join(num(m[k]['p50']) for k in ('prediction_yaw_error_rad','candidate_launch_offset_only_yaw_error_rad','candidate_retimed_yaw_error_rad'))+' |')
    lines+=['','此处仅按该发射的实测决策→出膛时长，将名义15ms减少约10.67ms；没有搜索延迟、重算飞行时间或生成新命中率。先前T单项射击未通过，不能据这一事后改善直接推广4.33ms。连续云台跟踪与离散发射的延迟语义仍须分开验证。','',
      '## 下一轮可证伪的对照','',
      '优先做独立时域对照：BS/I分别保留15ms与采用本批直接测得的4.3295ms，形成2×2，另保留正式v3。冻结参数后用新种子、同样射击规则和评分验证，先测匀速与转向，再检查静止、自车和旋转代价。它用于判断时域与模型是否交互，不是对4.33ms收益的承诺。',
      '随后再评估径向与横向分开建模：在不同时改时序的条件下，保留可靠的CV方向或垂向，只单独检验机动模型的距离/速度贡献。候选应先用同输入离线比较典型误差与尾部误差，不能只优化XY P95。',
      '开火窗口本轮不放宽；后续评分应增加实际发射关联后的横向/角度典型误差及命中/秒约束，避免降低少量极端误差却损伤大量可命中的普通发射。以上为下一轮建议，本诊断没有提交新射击任务。','',
      '## 适用范围','',
      '物理板身份先按捕获图像独立匹配，未匹配发射保留在账本但不进入所选板误差。v3缺少共享图像身份，仍有全部发射与跟随误差，不能与其他组的条件板误差强行合并。',
      '真实弹丸可命中另一块板；所选板最近接距离不能替代任意目标装甲的命中判定。该项是逐弹诊断，不是新独立样本、随机干预或可部署真值输入。',
      'ROS记录时钟与物理积分时长不完全相同：目标匹配使用记录时间戳，物理飞行时长由实际水平位移/初速度反推，两者分别保存。不能把二者差直接解释成硬件延迟。',
      '不根据本诊断扫描延迟、扩大射击窗口或修改评分。下一候选需要另行冻结，并用新射击种子验证。']
    text='\n'.join(lines)+'\n';(root/'report.md').write_text(text)
    parts=[];table=False
    def inline(x):return re.sub(r'\[([^\]]+)\]\(([^)]+)\)',r'<a href="\2">\1</a>',html.escape(x))
    for line in lines:
        if table and not line.startswith('|'):parts.append('</table>');table=False
        if line.startswith('|'):
            if re.fullmatch(r'[|:\- ]+',line):continue
            tag='td' if table else 'th'
            if not table:parts.append('<table>');table=True
            parts.append('<tr>'+''.join('<'+tag+'>'+inline(x.strip())+'</'+tag+'>' for x in line.strip('|').split('|'))+'</tr>')
        elif line.startswith('!['):
            match=re.fullmatch(r'!\[([^\]]*)\]\(([^)]+)\)',line)
            if match:parts.append('<img style="max-width:100%" alt="'+html.escape(match[1])+'" src="'+html.escape(match[2])+'">')
        elif line.startswith('#'):
            n=len(line)-len(line.lstrip('#'));parts.append(f'<h{n}>'+inline(line[n:].strip())+f'</h{n}>')
        elif line:parts.append('<p>'+inline(line)+'</p>')
    if table:parts.append('</table>')
    (root/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>实际发射逐弹诊断</title><style>body{max-width:1300px;margin:32px auto;padding:0 16px;font:16px/1.6 sans-serif}table{border-collapse:collapse;font-size:13px;width:100%;margin:20px 0}td,th{border:1px solid #ccd5df;padding:7px;text-align:right}th{background:#edf2f8}td:first-child,th:first-child{text-align:left}a{color:#175cb5}</style>'+''.join(parts))


def verify(root):
    c=load(root/'contract.json');assert protected()==c['protected']
    for p,h in c['parent_files'].items():assert sha(Path(p))==h,p
    for e in c['source_runs']:
        for p,h in e['files'].items():assert sha(Path(p))==h,p
    for p,h in c['code_files'].items():assert sha(Path(p))==h,p
    rows=load(root/'shot-ledger.json');assert len(rows)==461
    assert len({(x['run_id'],x['shot_seq']) for x in rows})==461
    for x in rows:assert x['launch_stamp_ns']==x['gimbal_accepted_stamp_ns'] and x['receipt_stamp_ns']<=x['launch_stamp_ns']
    assert sum(x.get('physical_plate_id') is not None for x in rows)==351
    reconstructed=[x for x in rows if 'horizontal_trace_reconstruction_residual_m' in x]
    assert len(reconstructed)==351 and max(x['horizontal_trace_reconstruction_residual_m'] for x in reconstructed)<.002
    for x in reconstructed:
        assert abs(angle(x['total_yaw_error_rad']-x['prediction_yaw_error_rad']-x['launch_yaw_error_rad']))<1e-10
        for axis in ('lateral','up'):
            assert abs(x['closest_total_'+axis+'_m']-x['closest_ideal_'+axis+'_m']-x['closest_execution_'+axis+'_m'])<1e-9
    for e in c['source_runs']:
        rr=[x for x in rows if x['run_id']==e['run_id']]
        assert len(rr)==e['shots'] and sum(x['outcome']=='target_armor' for x in rr)==e['hits']
        assert abs(sum(x['weighted_hit_score'] for x in rr)-e['weight'])<1e-10
    assert all(x['boundary_exclusion'] for x in load(root/'unfired-pulses.json'))
    write(root/'validation.json',dict(passed=True,shots=461,runs=50,unique_forwarded_pulses_and_gimbal_acceptance_verified=True,
      command_topic_confirmed=460,command_topic_missing=1,boundary_unfired_pulses=2,
      selected_physical_plate_matches=351,per_run_original_scores_reproduced=True,vector_and_angle_decomposition_verified=True,
      maximum_horizontal_trace_reconstruction_error_m=max(x['horizontal_trace_reconstruction_residual_m'] for x in reconstructed),
      protected_unchanged=True,historical_evidence_unchanged=True,no_new_live_trials=True,
      source_sha256=sha(Path(__file__))))
    write(root/'report-seal.json',{p.name:sha(p) for p in root.iterdir() if p.is_file() and p.name!='report-seal.json'})


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('freeze','analyze','verify','render'));parser.add_argument('--output',type=Path,default=ROOT)
    args=parser.parse_args();root=args.output.resolve();{'freeze':freeze,'analyze':analyze,'verify':verify,'render':render}[args.action](root)
