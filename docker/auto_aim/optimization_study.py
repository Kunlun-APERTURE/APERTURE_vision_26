#!/usr/bin/env python3
"""Frozen staged v4 comparisons. Failed stages never enter the next baseline."""
import argparse,bisect,contextlib,fcntl,hashlib,html,json,math,shutil,time
from pathlib import Path
import yaml
import experiments,manage
from highspeed_study import signature
from highspeed_report import motion_analysis
from focus_diagnosis import analyze
from impact_quality import summarize,quantile
from report import load,write

ROOT=manage.ROOT;OPS=manage.OPS
OUTPUT=OPS/'experiments/optimization-v4-20261021'
CONFIG=ROOT/'sp_vision_25/configs/simulator.yaml'
SCENES=['hf_static_5','hf_static_8','hf_uniform_3','hf_reverse_3','hf_turn90_3','hf_stop_start_v4','hf_spin_pos','hf_spin_neg']
PROFILES={'m':'tracking_fire_v4m','mt':'tracking_fire_v4mt','mtr':'tracking_fire_v4mtr'}

def frozen():
    result=signature()
    paths=list((ROOT/'sp_vision_25/tasks/auto_aim').rglob('*.cpp'))+list((ROOT/'sp_vision_25/tasks/auto_aim').rglob('*.hpp'))
    paths += [ROOT/'sp_vision_25/src/sim_auto_aim.cpp',ROOT/'vision_simulator_26/src/systems/gimbal_pid.rs',ROOT/'vision_simulator_26/src/ros2/plugin.rs',ROOT/'vision_simulator_26/rm_interfaces/msg/GimbalCmd.msg']
    result['sources']={str(p.relative_to(ROOT)):experiments.digest(p) for p in paths if p.is_file()}
    return result

def configure(options):
    base=yaml.safe_load((OUTPUT/'baseline/sp_vision_25/configs/simulator.yaml').read_text())
    base['optimization_v4']=options
    CONFIG.write_text(yaml.safe_dump(base,sort_keys=False))

def start(profile,physics):
    manage.stop()
    manage.start(argparse.Namespace(terrain='flat_range',scene='hs_static',control='off',view='off',replace=False,
        fire='auto',physics=physics,bullet_speed=25.,reported_bullet_speed=25.,aim_origin='muzzle',gimbal_profile='feedforward',
        fire_policy='advice',fire_rate_hz=5.,algorithm_profile=profile,projectile_visible='on',command_script=''))

def detail(folder):
    motion_analysis(folder)
    diagnostic=analyze(folder)
    motion=load(folder/'motion-analysis.json');manifest=load(folder/'manifest.json');state=load(folder/'run-state.json')
    plan=load(folder/'configuration/scenario-plan.json');samples=motion['samples'];times=[r['t'] for r in samples]
    shots=[json.loads(l) for l in (folder/'quality-shots.jsonl').open()]
    changes=plan.get('motion_change_times_s',[])
    rows=[]
    for l in (folder/'telemetry.jsonl').open():
        packet=json.loads(l)
        if packet['kind']!='observation':continue
        d=packet['data'];r=d['result'];stamp=d['frame']['stamp_ns'];t=(stamp-state['measurement_start_stamp_ns'])*1e-9
        if not 0<=t<=manifest['duration_s']:continue
        i=bisect.bisect_left(times,t);near=samples[max(0,i-1):i+1]
        truth=min(near,key=lambda x:abs(x['t']-t)) if near else None
        if truth and abs(truth['t']-t)>.1:truth=None
        estimate=r.get('filter_state');fire=r.get('fire_check') or {}
        rows.append(dict(t=t,velocity_error=math.hypot(estimate[1]-truth['vx'],estimate[3]-truth['vy']) if estimate and truth else None,
            tracking=r.get('tracker')=='tracking',yaw_violation=abs(fire.get('yaw_error_rad',0))>fire.get('yaw_tolerance_rad',1),
            yaw_qualified=bool(fire.get('valid') and fire.get('pose_valid')),pose_valid=fire.get('pose_valid'),
            pose_extrapolation_s=r.get('pose_extrapolation_s'),circular=r.get('circular_prediction',False),
            suspect=r.get('prediction_suspect',False),sigma=r.get('prediction_yaw_sigma_rad'),blocked=fire.get('blocked_reason')))
    events=[]
    starts=changes or ([1.] if 'spin' in manifest['scene'] else [])
    for change in starts:
        origin=None;recovery=None
        maneuver=next((m for m in diagnostic.get('maneuvers',[]) if m['command_change_s']==change),{})
        departure=maneuver.get('physical_velocity_departure_s')
        for r in rows:
            if r['t']<(departure if departure is not None else change):continue
            if r['t']>change+2:break
            good=r['tracking'] and r['velocity_error'] is not None and r['velocity_error']<=.3
            if good:
                if origin is None:origin=r['t']
                if r['t']-origin>=.1:recovery=origin-change;break
            else:origin=None
        impacts=[(s['terminal_stamp_ns']-state['measurement_start_stamp_ns'])*1e-9-change for s in shots if s.get('useful_hit') and s.get('terminal_stamp_ns') and change<=s['fire_time_s']<change+2 and (s['terminal_stamp_ns']-state['measurement_start_stamp_ns'])*1e-9<change+2]
        events.append(dict(change_s=change,physical_departure_s=departure,recovery_s=recovery,first_impact_s=min(impacts) if impacts else None,
            waste_first_half_second=sum(not s.get('useful_hit') for s in shots if change<=s['fire_time_s']<change+.5)))
    def score(a,b,near=False):
        ss=[s for s in shots if a<=s['fire_time_s']<b and (not near or s.get('launch_range_m') is not None and 5<=s['launch_range_m']<=8)]
        return dict(shots=len(ss),duration_s=b-a,**summarize(ss))
    windows=[(t,min(t+2,manifest['duration_s'])) for t in changes] if changes else [(2.,manifest['duration_s'])]
    target=[score(a,b,True) for a,b in windows]
    steady=[r for r in rows if r['t']>=2 and not any(t<=r['t']<t+.5 for t in changes) and r['yaw_qualified']]
    maneuver_quality=diagnostic.get('maneuvers',[])
    result=dict(scene=manifest['scene'],profile=manifest['algorithm_profile'],physics=manifest['shooting']['physics'],seed=manifest['random_seed'],
        all=score(0,manifest['duration_s']),target=target,events=events,
        yaw_steady_samples=len(steady),yaw_steady_violations=sum(r['yaw_violation'] for r in steady),
        intensity_achieved=motion['intensity_achieved'],maneuvers=maneuver_quality,
        circular_fraction=sum(r['circular'] for r in rows)/len(rows) if rows else None,
        suspect_frames=sum(r['suspect'] for r in rows),prediction_blocked_frames=sum(r['blocked']=='prediction' for r in rows),
        run_id=folder.name,wall_s=load(folder/'summary.json',{}).get('metrics',{}).get('total_wall_s'))
    write(folder/'optimization-metrics.json',result)
    return result

def aggregate(rows,window='target'):
    scores=[s for r in rows for s in (r['target'] if window=='target' else [r['all']])]
    shots=sum(s['shots'] for s in scores);hits=sum(s['useful_armor_hits'] for s in scores);duration=sum(s['duration_s'] for s in scores)
    weights=[s['weighted_hit_score_sum'] for s in scores]
    events=[e for r in rows for e in r['events']]
    yaw_samples=sum(r['yaw_steady_samples'] for r in rows)
    return dict(runs=len(rows),shots=shots,hits=hits,hit_rate=hits/shots if shots else None,
        weighted_hit_rate=sum(weights)/shots if shots and all(v is not None for v in weights) else None,
        useful_hits_per_s=hits/duration if duration else None,
        first_impact_p90_s=quantile([e['first_impact_s'] if e['first_impact_s'] is not None else 2. for e in events],.9),
        first_impact_censor_fraction=sum(e['first_impact_s'] is None for e in events)/len(events) if events else None,
        recovery_p90_s=quantile([e['recovery_s'] if e['recovery_s'] is not None else 2. for e in events],.9),
        recovery_censor_fraction=sum(e['recovery_s'] is None for e in events)/len(events) if events else None,
        waste_first_half_second=sum(e['waste_first_half_second'] for e in events),
        yaw_violation_fraction=sum(r['yaw_steady_violations'] for r in rows)/yaw_samples if yaw_samples else None,
        intensity_achieved=all(r['intensity_achieved'] for r in rows))

def acceptance(batch):
    definition=batch['definition'];base=definition['baseline'];candidate=definition['candidate'];stage=definition['stage']
    rows=[r['metrics'] for r in batch['runs']]
    targets=['hf_reverse_3','hf_turn90_3','hf_stop_start_v4'] if stage=='m' else ['hf_uniform_3','hf_reverse_3','hf_turn90_3'] if stage=='mt' else [s for s in definition['scenes'] if 'spin' in s]
    results={p:aggregate([r for r in rows if r['profile']==p and r['scene'] in targets]) for p in (base,candidate)}
    b,c=results[base],results[candidate];checks=[]
    def check(name,ok,baseline=None,value=None):checks.append(dict(name=name,passed=bool(ok),baseline=baseline,value=value))
    bw,cw=b['weighted_hit_rate'],c['weighted_hit_rate']
    check('target_weighted_relative_10pct_and_absolute_3pp',bw is not None and cw is not None and cw>=bw*1.1 and cw>=bw+.03,bw,cw)
    def ratio(name,key,fraction,less=False):
        bv,cv=b[key],c[key];ok=bv is not None and cv is not None and (cv<=bv*fraction if less else cv>=bv*fraction)
        check(name,ok,bv,cv)
    ratio('target_useful_hits_per_s_95pct','useful_hits_per_s',.95)
    bv,cv=b['first_impact_p90_s'],c['first_impact_p90_s']
    check('first_useful_impact_p90_plus_100ms',bv is not None and cv is not None and cv<=bv+.1,bv,cv)
    bv,cv=b['first_impact_censor_fraction'],c['first_impact_censor_fraction']
    check('first_useful_impact_censor_nongrowth',bv is not None and cv is not None and cv<=bv,bv,cv)
    if stage=='m':
        ratio('first_500ms_waste_down_30pct','waste_first_half_second',.7,True)
        ratio('2d_velocity_recovery_p90_down_30pct','recovery_p90_s',.7,True)
        ratio('2d_velocity_recovery_censor_nongrowth','recovery_censor_fraction',1.,True)
    if stage=='mt':ratio('steady_yaw_violation_down_30pct','yaw_violation_fraction',.7,True)
    per_scene={}
    for scene in definition['scenes']:
        bs=aggregate([r for r in rows if r['profile']==base and r['scene']==scene],'all')
        cs=aggregate([r for r in rows if r['profile']==candidate and r['scene']==scene],'all')
        per_scene[scene]={base:bs,candidate:cs}
        if 'static' in scene:
            for key,margin in [('hit_rate',.02),('weighted_hit_rate',.03)]:
                bv,cv=bs[key],cs[key];check(scene+'_'+key,bv is not None and cv is not None and cv>=bv-margin,bv,cv)
        elif scene not in targets or stage=='mtr':
            bv,cv=bs['useful_hits_per_s'],cs['useful_hits_per_s'];check(scene+'_useful_hits_95pct',bv is not None and cv is not None and cv>=bv*.95,bv,cv)
        if stage=='mtr' and scene in targets:
            bv,cv=bs['weighted_hit_rate'],cs['weighted_hit_rate'];check(scene+'_weighted_gain',bv is not None and cv is not None and cv>=bv*1.1 and cv>=bv+.03,bv,cv)
    quality=[]
    for r in rows:
        for m in r['maneuvers']:
            quality.append(m.get('near_range_qualified',False) and m.get('pre_speed_qualified') is not False)
    check('actual_motion_intensity',all(r['intensity_achieved'] for r in rows))
    check('maneuver_near_range_and_prehold',bool(quality) and all(quality))
    expected=len(definition['scenes'])*len(definition['seeds'])*len(definition['physics'])*2
    check('complete_matrix',batch.get('complete') and len(rows)==expected,expected,len(rows))
    result=dict(stage=stage,passed=all(r['passed'] for r in checks),checks=checks,target=results,per_scene=per_scene,
        visual_review='pending',promoted=False,primary_score='Actual near-range launches in maneuver 0–2s, or steady t>=2s. Overlapping stop/restart windows are counted as declared event windows.',
        censored_at_s=2.,quantile='nearest_rank')
    return result

def report(output,batch):
    result=acceptance(batch);write(output/'acceptance.json',result)
    tr=''.join('<tr><td>'+html.escape(c['name'])+'</td><td>'+str(c['passed'])+'</td><td>'+str(c['baseline'])+'</td><td>'+str(c['value'])+'</td></tr>' for c in result['checks'])
    links=''.join('<li>'+html.escape(str(r['key']))+': <a href="../../'+r['run_id']+'/report.html">完整视频与报告</a></li>' for r in batch['runs'])
    (output/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>v4 分阶段验收</title><style>body{font:16px system-ui;margin:2em}td{border:1px solid #aaa;padding:.4em}table{border-collapse:collapse}</style><h1>'+result['stage']+' 分阶段验收</h1><p>完成 '+str(len(batch['runs']))+' 次；规则通过：'+str(result['passed'])+'；人工视觉待复核，默认算法未升级。</p><table><tr><th>规则</th><th>通过</th><th>基线</th><th>候选</th></tr>'+tr+'</table><ul>'+links+'</ul>')
    publish_index()
    return result

def publish_index():
    links=''.join('<li><a href="stage-'+stage+'/report.html">阶段 '+stage+'</a></li>' for stage in ('m','mt','mtr') if (OUTPUT/('stage-'+stage)/'report.html').exists())
    (OUTPUT/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>v4 分阶段优化实验</title><h1>v4 分阶段优化实验</h1><p>各阶段冻结候选，独立对照。未通过规则的版本不进入后续基线。人工视觉待复核，默认算法未升级。</p><ul>'+links+'</ul><p><a href="development/selection.json">历史回放、参数筛选与 v3 回归</a> · <a href="conclusions.json">阶段结论</a></p>')
    experiments.publish_desktop(manage,OUTPUT)

def formal(stage,baseline,options):
    output=OUTPUT/('stage-'+stage);output.mkdir(parents=True,exist_ok=True)
    configure(options);candidate=PROFILES[stage];index=['m','mt','mtr'].index(stage)
    definition=dict(stage=stage,baseline=baseline,candidate=candidate,options=options,scenes=SCENES+(['hf_spin_move_pos','hf_spin_move_neg'] if stage=='mtr' else []),
        seeds=[2026102108+2*index,2026102109+2*index],physics=['no-drag','drag'])
    path=output/'batch.json';batch=load(path);sig=frozen()
    if batch and (batch['definition']!=definition or batch['signature']!=sig):raise RuntimeError('Frozen stage changed')
    batch=batch or dict(definition=definition,signature=sig,runs=[],invalid_attempts=[],complete=False)
    write(path,batch);write(output/'frozen.json',sig);shutil.copy2(CONFIG,output/'algorithm.yaml')
    began=time.monotonic();previous=batch.get('active_wall_s',0.)
    for repeat,seed in enumerate(definition['seeds']):
        profiles=[baseline,candidate] if repeat==0 else [candidate,baseline]
        scenes=definition['scenes'] if repeat==0 else list(reversed(definition['scenes']))
        for physics in definition['physics']:
            for profile in profiles:
                pending=[s for s in scenes if not any(r['key']==[seed,physics,profile,s] for r in batch['runs'])]
                if not pending:continue
                start(profile,physics)
                for scene in pending:
                    if frozen()!=sig:raise RuntimeError('Frozen inputs changed')
                    for attempt in range(2):
                        folder=experiments.run_one(manage,scene,argparse.Namespace(mode='shoot',duration=None,seed=seed,label='optimization-v4-'+stage))
                        summary=load(folder/'summary.json',{});row=dict(key=[seed,physics,profile,scene],run_id=folder.name,verdict=summary.get('verdict'))
                        if row['verdict']=='invalid':
                            batch['invalid_attempts'].append(row);write(path,batch)
                            if attempt:raise RuntimeError('Repeated invalid evidence')
                            continue
                        row['metrics']=detail(folder);batch['runs'].append(row);break
                    batch['active_wall_s']=previous+time.monotonic()-began
                    write(path,batch);report(output,batch)
                    print(json.dumps(dict(stage=stage,progress=len(batch['runs']),expected=len(scenes)*8,key=row['key'],run_id=folder.name)),flush=True)
    batch['complete']=True;batch['final_signature']=frozen();batch['active_wall_s']=previous+time.monotonic()-began
    write(path,batch);return report(output,batch)

def development(stage,baseline,options):
    output=OUTPUT/('ablation-'+stage);output.mkdir(parents=True,exist_ok=True)
    variants=([('estimator',dict(prediction_gate_v4=False)),('gate',dict(maneuver_v4=False)),('combined',{})] if stage=='m' else
        [('pose',dict(continuous_control_v4=False)),('continuous',dict(pose_alignment_v4=False)),('combined',{})] if stage=='mt' else
        [('estimator',dict(circular_prediction_v4=False)),('prediction',dict(rotation_estimation_v4=False)),('combined',{})])
    scenes=['hf_static_5','hf_turn90_3'] if stage=='m' else ['hf_uniform_3','hf_reverse_3'] if stage=='mt' else ['hf_spin_pos','hf_spin_move_neg']
    rows=[]
    for name,extra in [('baseline',None)]+variants:
        configure(dict(options,**(extra or {})))
        profile=baseline if extra is None else PROFILES[stage]
        start(profile,'no-drag')
        for scene in scenes:
            folder=experiments.run_one(manage,scene,argparse.Namespace(mode='shoot',duration=None,seed=2026102195,label='v4-development-'+stage+'-'+name))
            if load(folder/'summary.json',{}).get('verdict')=='invalid':raise RuntimeError('Development recording invalid')
            rows.append(dict(variant=name,metrics=detail(folder)))
            write(output/'results.json',dict(stage=stage,runs=rows,role='Development ablation, excluded from formal acceptance'))
            print(json.dumps(dict(development=stage,variant=name,scene=scene,shots=rows[-1]['metrics']['all']['shots'])),flush=True)
    publish_index()

def run():
    OUTPUT.mkdir(parents=True,exist_ok=True)
    original=CONFIG.read_bytes();selection=load(OUTPUT/'development/selection.json')['selected']
    latency=load(OUTPUT/'development/latency.json')
    options=dict(maneuver_sigma=selection['sigma'],maneuver_frames=selection['frames'],mt_use_m=False,r_use_m=False,r_use_t=False,launch_delay_s=latency['selected_launch_delay_s'])
    conclusions=[];baseline='tracking_fire_v3'
    with (manage.RUNTIME/'operation.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        try:
            for stage in ('m','mt','mtr'):
                development(stage,baseline,options)
                result=formal(stage,baseline,options);conclusions.append(result)
                if result['passed']:
                    baseline=PROFILES[stage]
                    if stage=='m':options.update(mt_use_m=True,r_use_m=True)
                    elif stage=='mt':options['r_use_t']=True
                write(OUTPUT/'conclusions.json',dict(stages=conclusions,last_rules_passed=baseline,default_promoted=False,visual_review='pending'))
                print(json.dumps(dict(stage_finished=stage,passed=result['passed'],next_baseline=baseline)),flush=True)
        finally:
            manage.stop();CONFIG.write_bytes(original)
            saved=manage.profile();saved.update(scene='static',terrain='arena',fire='off',control='off',algorithm_profile='legacy');manage.save_profile(saved)
            experiments.publish_desktop(manage,OUTPUT)

if __name__=='__main__':run()
