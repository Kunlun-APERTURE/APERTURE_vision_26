"""Frozen same-observation Singer/IMM experiment, before closed-loop acquisition."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import time

import background_jobs as jobs
import manage
from decision_benchmark import sha, verify_bundle
from perception_repair import FIELDS
from perception_study import protected, quantile
from report import load, write
from sync_first_divergence import run_one
from sync_first_divergence_analysis import primary

PARENT=manage.OPS/'experiments/motion-v5-20261024'
DEFAULT=manage.OPS/'experiments/maneuver-v6-20261026-r2'
SOURCE_FILES=('CMakeLists.txt','tasks/auto_aim/maneuver_filter.hpp','tasks/auto_aim/motion_model.hpp',
              'tasks/auto_aim/target.cpp','tasks/auto_aim/target.hpp','tests/maneuver_filter_test.cpp')
CORE_FIELDS=tuple(dict.fromkeys((*FIELDS,'aim_point','distance','plate_velocities','track_estimate',
                                'fire_check','prediction_horizon_s')))


def freeze(root):
    if (root/'contract.json').exists():
        c=load(root/'contract.json')
        if sha(root/'contract.json')!=load(root/'state.json')['contract_sha256'] or protected()!=c['protected']:
            raise ValueError('Frozen contract or protected artifacts changed')
        for path in c['bundles'].values():verify_bundle(Path(path))
        return c
    prior=load(PARENT/'contract.json');ps=load(PARENT/'state.json')
    binary=root/'build-source/sp_vision_25/build/motion/sim_auto_aim'
    if not binary.exists() or 'PASSED' not in (root/'maneuver-checks.log').read_text():
        raise ValueError('Build and run mechanism checks before freezing')
    sources=[]
    for e in prior['sources']:
        references={v:next(r for r in ps['replays'] if r['source_run']==e['run_id'] and r['variant']==v) for v in ('BS','baseline')}
        sources.append(dict(e,references={v:dict(output=r['output'],events_sha256=sha(Path(r['output'])/'events.jsonl'),
                                  input_sha256=r['input_sha256']) for v,r in references.items()}))
    c=dict(version=1,created=time.time(),parent=str(PARENT),parent_sha256=sha(PARENT/'contract.json'),
      protected=protected(),sources=sources,bundles={},workers=2,maximum_replays=28,
      candidate_binary_sha256=sha(binary),source_sha256={p:sha(root/'build-source/sp_vision_25'/p) for p in SOURCE_FILES},
      scoring_horizons_s=[.1,.2,.4],include_native_shooting_horizon=True,
      same_input_variants=['BS','P','S','I'],
      model_parameters=dict(single=dict(tau_s=.15,acceleration_sigma_mps2=3.),
        imm=[dict(tau_s=.05,acceleration_sigma_mps2=.5),dict(tau_s=.35,acceleration_sigma_mps2=4.5)],
        calm_to_maneuver_per_s=.8,maneuver_to_calm_per_s=3.,cv_variance_reference_interval_s=.03),
      replay_gate=dict(overall_future_p95_ratio=.9,post_turn_0_500ms_p95_ratio=.9,
        scene_future_p95_ratio=1.05,stationary_speed_p95_ratio=1.1,stationary_future_p95_ratio=1.1,
        minimum_post_turn_samples=30,minimum_forecast_samples=300),
      selection_horizon='native shooting horizon, same selected physical plate and matched frames for all models',
      backend_policy='Only after the frozen replay gate passes; freeze new backend seeds and qualification before acquisition.',
      no_ground_truth_input=True,automatic_promotion=False,ui_trials=0,maximum_complete_backend_trial_s=40,
      comparison='BS accepted observations and identity frozen by shadow execution; S/I are not fed back to association, aim or fire. '
                 'P is a same-input forecast diagnostic, not a new closed-loop P trial.',
      historical_evidence={str(p):sha(p) for p in (PARENT/'report.md',PARENT/'validation.json',PARENT/'contract.json')})
    for name in ('OFF','shadow'):
        base=Path(prior['bundles']['baseline' if name=='OFF' else 'BS'])
        target=jobs.STATE/'bundles'/('maneuver_v6_'+name+'_'+sha(binary)[:12])
        if target.exists():raise ValueError('Refuse to overwrite existing bundle')
        shutil.copytree(base,target,copy_function=os.link)
        for p in SOURCE_FILES:
            out=target/'sp_vision_25'/p;out.parent.mkdir(parents=True,exist_ok=True);out.unlink(missing_ok=True)
            shutil.copy2(root/'build-source/sp_vision_25'/p,out)
        out=target/'sp_vision_25/build/simulator/sim_auto_aim';out.unlink();shutil.copy2(binary,out)
        if name=='shadow':
            out=target/'sp_vision_25/configs/simulator.yaml';text=out.read_text();out.unlink()
            out.write_text(text+'\nmotion_v6:\n  shadow: true\n')
        metadata=load(base/'bundle.json');metadata.update(bundle_id=target.name,created=time.time(),parent_bundle=str(base),
          maneuver_mode=name,truth_input=False)
        for p in (*SOURCE_FILES,'build/simulator/sim_auto_aim','configs/simulator.yaml'):
            metadata['files']['sp_vision_25/'+p]=sha(target/'sp_vision_25'/p)
        (target/'bundle.json').unlink();write(target/'bundle.json',metadata);verify_bundle(target)
        c['bundles'][name]=str(target)
    write(root/'contract.json',c)
    write(root/'state.json',dict(state='frozen',contract_sha256=sha(root/'contract.json'),replays=[]))
    return c


def one(root,c,entry,variant):
    source=manage.OPS/'experiments'/entry['run_id'];out=root/'replays'/source.name/variant
    result=load(out/'result.json') if (out/'result.json').exists() else run_one(source,out,Path(c['bundles'][variant]),
        seconds=entry['duration_s'],detector_history='session',diagnosis=True)
    reference=entry['references']['baseline' if variant=='OFF' else 'BS'];refpath=Path(reference['output'])
    if not result['complete'] or result['primary_input_sha256']!=reference['input_sha256']:
        raise ValueError('Replay input/completion mismatch')
    if sha(refpath/'events.jsonl')!=reference['events_sha256']:raise ValueError('Historical replay changed')
    original=primary(refpath);current=primary(out);different={k:0 for k in CORE_FIELDS}
    if variant=='shadow' and not any('innovation_variance' in u for p in current
        for u in (p['result'].get('continuity',{}).get('filter') or {}).get('updates',[])):
        raise ValueError('Binary lacks required innovation diagnostic: inspect build provenance')
    if len(current)!=len(original):raise ValueError('Frame count mismatch')
    for a,b in zip(original,current):
        if a['frame']!=b['frame'] or a['jpeg_sha256']!=b['jpeg_sha256']:raise ValueError('Frame pairing mismatch')
        for k in CORE_FIELDS:different[k]+=a['result'].get(k)!=b['result'].get(k)
    if any(different.values()):raise ValueError('Default/shadow affected core behavior: '+str(different))
    return dict(source_run=source.name,variant=variant,engine=entry['engine'],scene=entry['scene'],
      output=str(out),frames=result['frames'],wall_s=result['wall_s'],differences=different,
      input_sha256=result['primary_input_sha256'],output_sha256=sha(out/'events.jsonl'))


def replay(root):
    c=freeze(root);s=load(root/'state.json');s['state']='replaying';write(root/'state.json',s)
    pending=[(entry,v) for entry in c['sources'] for v in ('OFF','shadow') if
             not any(r['source_run']==entry['run_id'] and r['variant']==v for r in s['replays'])]
    with ThreadPoolExecutor(max_workers=c['workers']) as pool:
        for row in pool.map(lambda p:one(root,c,*p),pending):
            s['replays'].append(row);write(root/'state.json',s)
            print(json.dumps(dict(stage='same-input',completed=len(s['replays']),variant=row['variant'],source=row['source_run'])),flush=True)
    s['state']='replays_complete';write(root/'state.json',s)


def stats(values):
    return dict(n=len(values),p50=quantile(values,.5),p95=quantile(values,.95),mean=statistics.mean(values) if values else None)


def analyze(root):
    # Import truth-dependent modules only here, after every algorithm replay is done.
    import numpy as np
    from perception_attribution_analysis import timeline,match_truth
    c=freeze(root);s=load(root/'state.json')
    if len(s['replays'])!=c['maximum_replays']:raise ValueError('Complete all replays first')
    def position(state,t,tau):
        x=np.asarray(state).reshape(3,3);u=-math.expm1(-t/tau);g=tau*(t-tau*u)
        return x[:,0]+t*x[:,1]+g*x[:,2]
    traces=[];innovations=[];covariance_count=0;covariance_min=1.;mode=[];cost=[];coverage={};native_max_difference=0.
    branch={k:[] for k in ('BS','S','I','interacted_calm','interacted_maneuver','selected_probability')}
    for entry in c['sources']:
        source=manage.OPS/'experiments'/entry['run_id'];run=load(source/'run-state.json');truth=timeline(source)
        path=next(Path(r['output']) for r in s['replays'] if r['source_run']==source.name and r['variant']=='shadow')
        for packet in primary(path):
            f,r=packet['frame'],packet['result'];filter=r.get('continuity',{}).get('filter') or {}
            if not filter.get('motion_v6'):continue
            if f['stamp_ns']<run['measurement_start_stamp_ns'] or f['stamp_ns']>run['measurement_end_stamp_ns']:continue
            t=(f['stamp_ns']-run['measurement_start_stamp_ns'])*1e-9
            phase='stationary' if t<.7 else 'post_turn_0_500ms' if entry['scene']=='hf_turn90_3' and 3.2<=t<3.7 else 'post_turn_later' if entry['scene']=='hf_turn90_3' and t>=3.7 else 'moving'
            cost.append(r.get('pipeline_ms'))
            for update in filter.get('updates',[]):
                if update.get('accepted') and update.get('innovation') and update.get('innovation_variance'):
                    z=[a/math.sqrt(max(b,1e-12)) for a,b in zip(update['innovation'],update['innovation_variance'])]
                    innovations.append(dict(source=source.name,engine=entry['engine'],scene=entry['scene'],phase=phase,id=update['plate_id'],t=t,z=z))
            banks=filter['motion_v6']['plates'];plates=filter.get('plates',[])
            for bank in banks:
                if not bank.get('initialized'):continue
                probs=bank['probability'];assert abs(sum(probs)-1)<1e-10 and min(probs)>=0
                for key in ('single_covariance','imm_covariance'):
                    P=np.asarray(bank[key]).reshape((9,9),order='F')
                    assert np.isfinite(P).all() and np.max(np.abs(P-P.T))<1e-8
                    mineig=float(np.linalg.eigvalsh(P).min());covariance_min=min(covariance_min,mineig);covariance_count+=1
                    assert mineig>=-1e-8
                mode.append(dict(engine=entry['engine'],scene=entry['scene'],phase=phase,probability=probs[1]))
            sid=r.get('selected_plate_id',-1);h=r.get('prediction_horizon_s')
            if not r.get('valid') or not 0<=sid<len(banks) or sid>=len(plates) or not isinstance(h,(int,float)) or not 0<h<1:continue
            bank=banks[sid];cv=plates[sid]
            if not bank.get('initialized') or not cv.get('initialized'):continue
            for label,dt in [('native',h),*((str(x),x) for x in c['scoring_horizons_s'])]:
                if f['stamp_ns']+round(dt*1e9)<=run['measurement_end_stamp_ns']:
                    key=entry['engine']+'/'+label
                    coverage.setdefault(key,dict(eligible=0,matched=0));coverage[key]['eligible']+=1
            links=r.get('continuity',{}).get('image_association') or {};obs=r.get('observations',[])
            chosen=next((a for a in links.get('observations',[]) if a['plate_id']==sid),None)
            if not chosen or not obs:continue
            distances=[float(np.linalg.norm(np.mean(a['corners'],axis=0)-chosen['image_center'])) for a in obs]
            idx=int(np.argmin(distances));matched=match_truth(f,obs,truth(f['stamp_ns']))
            if distances[idx]>4 or idx not in matched:continue
            identity=matched[idx]['plate']['id'];state=np.asarray(cv['state']).reshape(3,2);origin=np.asarray(f['gimbal_position_map'])
            velocities=dict(BS=state[:,1],P=state[:,1],S=np.asarray(bank['single_state']).reshape(3,3)[:,1],I=np.asarray(bank['imm_state']).reshape(3,3)[:,1])
            for label,dt in [('native',h),*((str(x),x) for x in c['scoring_horizons_s'])]:
                stamp=f['stamp_ns']+round(dt*1e9)
                if stamp>run['measurement_end_stamp_ns']:continue
                actual=next((a for a in truth(stamp) if a['id']==identity),None)
                if not actual:continue
                cvp=state[:,0]+dt*state[:,1]
                if label=='native' and r.get('aim_point'):
                    native_max_difference=max(native_max_difference,float(np.linalg.norm(cvp-np.asarray(r['aim_point']))))
                predictions=dict(BS=cvp,P=cvp+np.asarray(bank['old_acceleration'])*.15*(dt+.15*math.expm1(-dt/.15)),
                  S=position(bank['single_state'],dt,.15),
                  I=sum(p*position(m['state'],dt,m['tau']) for p,m in zip(bank['probability'],bank['models'])))
                errors={v:float(np.linalg.norm((p+origin-np.asarray(actual['position']))[:2])) for v,p in predictions.items()}
                if entry['engine']=='backend' and phase=='stationary' and label=='native':
                    for v in ('BS','S','I'):branch[v].append(float(np.linalg.norm(velocities[v][:2])))
                    for key,model in zip(('interacted_calm','interacted_maneuver'),bank['models']):
                        branch[key].append(math.hypot(model['state'][1],model['state'][4]))
                    branch['selected_probability'].append(bank['probability'][1])
                traces.append(dict(source=source.name,scene=entry['scene'],engine=entry['engine'],phase=phase,t=t,
                  seq=f['seq'],horizon=label,dt=dt,physical_plate=identity,algorithm_plate=sid,errors=errors,
                  speeds={v:float(np.linalg.norm(a[:2])) for v,a in velocities.items()},mode_probability=bank['probability'][1]))
                coverage[entry['engine']+'/'+label]['matched']+=1
    # A single common frame/identity/horizon set serves every model. UI sensitivity is separate.
    groups=[]
    for engine in ('backend','ui'):
        for horizon in ('native',*[str(x) for x in c['scoring_horizons_s']]):
            for scene in ('all','hf_uniform_3','hf_turn90_3'):
                for phase in ('all','stationary','moving','post_turn_0_500ms','post_turn_later'):
                    rows=[r for r in traces if r['engine']==engine and r['horizon']==horizon and (scene=='all' or r['scene']==scene) and (phase=='all' or r['phase']==phase)]
                    if rows:groups.append(dict(engine=engine,horizon=horizon,scene=scene,phase=phase,
                        variants={v:dict(error_m=stats([r['errors'][v] for r in rows]),speed_mps=stats([r['speeds'][v] for r in rows])) for v in c['same_input_variants']}))
    def group(scene='all',phase='all'):
        return next(g for g in groups if g['engine']=='backend' and g['horizon']=='native' and g['scene']==scene and g['phase']==phase)
    gate=c['replay_gate'];decisions=[]
    for v in ('S','I'):
        allg=group()['variants'];post=group('hf_turn90_3','post_turn_0_500ms')['variants'];static=group(phase='stationary')['variants']
        checks=dict(minimum_forecasts=allg[v]['error_m']['n']>=gate['minimum_forecast_samples'],
          minimum_post_turn=post[v]['error_m']['n']>=gate['minimum_post_turn_samples'],
          overall_future=allg[v]['error_m']['p95']<=gate['overall_future_p95_ratio']*allg['BS']['error_m']['p95'],
          post_turn=post[v]['error_m']['p95']<=gate['post_turn_0_500ms_p95_ratio']*post['BS']['error_m']['p95'],
          stationary_speed=static[v]['speed_mps']['p95']<=gate['stationary_speed_p95_ratio']*static['BS']['speed_mps']['p95'],
          stationary_future=static[v]['error_m']['p95']<=gate['stationary_future_p95_ratio']*static['BS']['error_m']['p95'])
        for scene in ('hf_uniform_3','hf_turn90_3'):
            g=group(scene)['variants'];checks[scene]=g[v]['error_m']['p95']<=gate['scene_future_p95_ratio']*g['BS']['error_m']['p95']
        decisions.append(dict(variant=v,checks=checks,passed=all(checks.values())))
    innovation_groups=[]
    for engine,scene in ((e,s) for e in ('backend','ui') for s in ('hf_uniform_3','hf_turn90_3')):
        for phase in ('stationary','moving','post_turn_0_500ms','post_turn_later'):
            rows=[r for r in innovations if r['engine']==engine and r['scene']==scene and r['phase']==phase]
            if not rows:continue
            values=np.asarray([r['z'] for r in rows]);pairs=[];previous={}
            for b in rows:
                key=(b['source'],b['id']);a=previous.get(key)
                if a and 0<b['t']-a['t']<=.1:pairs.append((a['z'][2],b['z'][2]))
                previous[key]=b
            corr=float(np.corrcoef(np.asarray(pairs).T)[0,1]) if len(pairs)>3 else None
            innovation_groups.append(dict(engine=engine,scene=scene,phase=phase,n=len(rows),mean_z=values.mean(axis=0).tolist(),
              rms_z=np.sqrt((values**2).mean(axis=0)).tolist(),radial_lag1_correlation=corr,pairs=len(pairs)))
    write(root/'forecast-traces.json',traces)
    write(root/'branch-diagnosis.json',dict(scope='Posthoc inspection of already-interacted branches, not independent branch-only candidates',
      stationary_common_frames=len(branch['BS']),stats={k:stats(v) for k,v in branch.items()}))
    per_source=[]
    for entry in c['sources']:
        rows=[r for r in traces if r['source']==entry['run_id'] and r['horizon']=='native']
        if not rows:continue
        stationary=[r for r in rows if r['phase']=='stationary']
        per_source.append(dict(source=entry['run_id'],engine=entry['engine'],scene=entry['scene'],
          sensor_seed=entry.get('sensor_seed'),variants={v:dict(future=stats([r['errors'][v] for r in rows]),
            stationary_speed=stats([r['speeds'][v] for r in stationary])) for v in c['same_input_variants']}))
    write(root/'analysis-result.json',dict(groups=groups,decisions=decisions,innovations=innovation_groups,
      covariance_checks=covariance_count,minimum_covariance_eigenvalue=covariance_min,
      forecast_coverage=coverage,native_forecast_max_difference_m=native_max_difference,
      per_source=per_source,
      mode_probability=[dict(engine=engine,scene=scene,phase=phase,stats=stats([r['probability'] for r in mode if r['engine']==engine and r['scene']==scene and r['phase']==phase])) for engine in ('backend','ui') for scene in ('hf_uniform_3','hf_turn90_3') for phase in ('stationary','moving','post_turn_0_500ms','post_turn_later')],
      runtime_samples=len(cost),truth_used_only_after_replay=True))
    s.update(state='analyzed',eligible=[r['variant'] for r in decisions if r['passed']]);write(root/'state.json',s)
    render(root)


def render(root):
    c=load(root/'contract.json');s=load(root/'state.json');a=load(root/'analysis-result.json')
    def num(x):return '—' if x is None else f'{x:.3f}'
    lines=['# 加速度状态与双模型同观测实验','',
      '同一 BS 接受的量测和板号分别驱动原 CV、旧 P、单模型 S、双模型 I。新模型仅旁路估计，不改变关联、瞄准或开火；因此本报告没有新增命中率。',
      '',f'完成 {len(s["replays"])} 段推理回放，{sum(r["frames"] for r in s["replays"])} 帧；默认关闭与旁路模式的核心输出逐帧复现历史参考。',
      '[冻结参数和门槛](contract.json) · [逐帧误差](forecast-traces.json) · [分析原始结果](analysis-result.json)','',
      '## 后台来源，同一物理板与原射击时域','',
      '| 场景/阶段 | 共同帧数 | BS P95/m | P P95/m | S P95/m | I P95/m |',
      '|---|---:|---:|---:|---:|---:|']
    for g in a['groups']:
        if g['engine']!='backend' or g['horizon']!='native' or (g['scene']=='all' and g['phase']!='all'):continue
        v=g['variants'];lines.append('| '+g['scene']+'/'+g['phase']+' | '+str(v['BS']['error_m']['n'])+' | '+' | '.join(num(v[k]['error_m']['p95']) for k in c['same_input_variants'])+' |')
    lines+=['','## 稳态速度与固定时域','', '| 来源/时域 | BS P95/m | P P95/m | S P95/m | I P95/m |','|---|---:|---:|---:|---:|']
    for g in a['groups']:
        if g['scene']=='all' and g['phase']=='all':
            lines.append('| '+g['engine']+'/'+g['horizon']+' | '+' | '.join(num(g['variants'][k]['error_m']['p95']) for k in c['same_input_variants'])+' |')
    for g in a['groups']:
        if g['scene']=='all' and g['phase']=='stationary' and g['horizon']=='native':
            lines+=['',g['engine']+' 静止所选板速度 P95(m/s)：'+', '.join(k+'='+num(g['variants'][k]['speed_mps']['p95']) for k in c['same_input_variants'])+'。']
    lines+=['','## 冻结回放门槛','',
      '后台共同帧：总体及变向后 0–0.5s 的未来点 P95 均改善至少 10%；各场景 P95 最多退化 5%；静止速度与未来点 P95 最多退化 10%。门槛用于控制下一阶段成本，不代表统计显著性或实车资格。']
    for d in a['decisions']:lines+=['',d['variant']+' 通过='+str(d['passed'])+'；未通过：'+str([k for k,v in d['checks'].items() if not v])+'。']
    lines+=['','独立匹配覆盖：'+', '.join(k+'='+str(v['matched'])+'/'+str(v['eligible']) for k,v in a['forecast_coverage'].items())+'。',
      'BS 原射击时域重建与实际 aim_point 最大差(m)：'+num(a['native_forecast_max_difference_m'])+'。']
    lines+=['','## 创新诊断','', '| 场景/阶段 | 样本数 | 径向创新均值/σ | 径向创新 RMS/σ | 同板径向相邻相关 |','|---|---:|---:|---:|---:|']
    for g in a['innovations']:lines.append('| '+g['engine']+'/'+g['scene']+'/'+g['phase']+' | '+str(g['n'])+' | '+num(g['mean_z'][2])+' | '+num(g['rms_z'][2])+' | '+num(g['radial_lag1_correlation'])+' |')
    lines+=['','## 双模型机动概率','', '| 后台场景/阶段 | 样本数（板×帧） | 中位数 | P95 |','|---|---:|---:|---:|']
    for g in a['mode_probability']:
        if g['engine']=='backend' and g['stats']['n']:
            lines.append('| '+g['scene']+'/'+g['phase']+' | '+str(g['stats']['n'])+' | '+num(g['stats']['p50'])+' | '+num(g['stats']['p95'])+' |')
    lines+=['','## 分轨迹复核','', '| 后台来源 | BS未来点P95/m | S未来点P95/m | I未来点P95/m | BS静止速度P95 | S静止速度P95 | I静止速度P95 |','|---|---:|---:|---:|---:|---:|---:|']
    for g in a['per_source']:
        if g['engine']!='backend':continue
        v=g['variants'];label=str(g['sensor_seed'])+'/'+g['scene']
        lines.append('| '+label+' | '+' | '.join(num(v[k]['future']['p95']) for k in ('BS','S','I'))+' | '+' | '.join(num(v[k]['stationary_speed']['p95']) for k in ('BS','S','I'))+' |')
    branch=load(root/'branch-diagnosis.json')['stats']
    lines+=['','## 静止退化的进一步定位','',
      '在同一180帧后台静止匹配集合中，所选板机动模型概率中位数为 '+num(branch['selected_probability']['p50'])+'、P95为 '+num(branch['selected_probability']['p95'])+'；没有看到静止时机动模型占主导。',
      '已交互的稳态分支速度 P95='+num(branch['interacted_calm']['p95'])+'m/s，仍高于原 CV 的 '+num(branch['BS']['p95'])+'m/s；已交互的机动分支为 '+num(branch['interacted_maneuver']['p95'])+'m/s。',
      '因此不宜只调切换阈值。下一步应分别隔离连续噪声换算、加速度初始不确定度和交互注入，对比未经交互的稳态分支。上述分支已经接收模型交互，不能当作单独运行的稳态模型成绩，也尚不能认定具体哪一项造成退化。[分支诊断](branch-diagnosis.json)']
    lines+=['','## 限制与下一阶段','',
      'S 的 τ=0.15s、加速度标准差3m/s²；I 的稳态 τ=0.05s/0.5m/s²、机动 τ=0.35s/4.5m/s²；模式转移采用连续时间率，参数在评分前冻结。所有模型保持各自协方差，模式融合包含均值分歧项。',
      '旧 P 此处使用相同 CV 速度差分；固定时间内同板未来点是诊断指标，不能直接换算为命中率。只对可独立图像匹配的物理板评分，所有模型共享完全相同的评分集合；UI 来源单列，不能与后台合并宣称迁移。',
      '旁路模型继承 BS 的实际接受量测，不能评估自身接入关联后是否改变拒绝、换板或开火。这一步通过后仍需完整闭环；丢失的真值匹配不作零误差。创新采用边际标准化，方向相关性和帧相关性未被消除。',
      '场景阶段按观测时刻划分，未来时域可能跨越运动边界；静止速度指标不受未来运动影响。门槛使用的帧不是独立统计样本。',
      '本轮主要误差指标为平面XY，不能据此获得垂向预测或pitch弹道资格；未来候选接入前还需独立核对这两项。',
      '后台固定时序不证明实时计算截止时间；新增推理成本需在候选接入后实测。正式版本、物理、评分、advice 和5Hz上限未改变。']
    if s['eligible']:lines+=['', '具备下一阶段候选：'+str(s['eligible'])+'，尚需单独冻结接入与后台预算。']
    else:lines+=['','本轮 S/I 均未通过冻结回放门槛，不进入新增射击实验。保留失败结果，不基于这些成绩扫描参数。']
    if (root/'rebuild-reason.json').exists():
        lines+=['','## 构建来源与总成本','',
          '第一批 maneuver-v6-20261026 的增量构建复用了时间戳更新前的目标文件，新增创新方差诊断没有进入二进制；该批28段/5104帧全部保留，标为构建来源不一致，不用于验收。',
          '本批重新触发相关源码和头文件依赖编译，要求实际输出包含新增诊断，模型参数与门槛完全未改。有效28段/5104帧，本次累计56段/10208帧，无新增后台射击或UI采集。',
          '[构建来源](build-provenance.json) · [重编原因](rebuild-reason.json) · [验收校验](validation.json)']
    text='\n'.join(lines)+'\n';(root/'report.md').write_text(text)
    def inline(value):
        value=html.escape(value)
        return re.sub(r'\[([^\]]+)\]\(([^)]+)\)',r'<a href="\2">\1</a>',value)
    parts=[];table=False
    for line in lines:
        if table and not line.startswith('|'):parts.append('</table>');table=False
        if line.startswith('|'):
            if re.fullmatch(r'[|:\- ]+',line):continue
            tag='td' if table else 'th'
            if not table:parts.append('<table>');table=True
            parts.append('<tr>'+''.join('<'+tag+'>'+inline(cell.strip())+'</'+tag+'>' for cell in line.strip('|').split('|'))+'</tr>')
        elif line.startswith('#'):
            level=len(line)-len(line.lstrip('#'));parts.append(f'<h{level}>'+inline(line[level:].strip())+f'</h{level}>')
        elif line:parts.append('<p>'+inline(line)+'</p>')
    if table:parts.append('</table>')
    (root/'report.html').write_text('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>加速度与双模型实验</title><style>body{max-width:1200px;margin:32px auto;padding:0 20px;font:16px/1.65 sans-serif;color:#18212d}table{border-collapse:collapse;font-size:14px;width:100%;margin:20px 0}td,th{border:1px solid #d5dce5;padding:7px;text-align:right}th{background:#edf2f8}td:first-child,th:first-child{text-align:left}a{color:#175cb5}h2{margin-top:36px}</style>'+''.join(parts))


def verify(root):
    c=freeze(root);s=load(root/'state.json');a=load(root/'analysis-result.json')
    assert protected()==c['protected']
    assert c['protected']==load(PARENT/'contract.json')['protected']
    provenance=load(root/'build-provenance.json');source=root/'build-source/sp_vision_25'
    assert sha(source/'build/motion/sim_auto_aim')==provenance['binary_sha256']==c['candidate_binary_sha256']
    for p,h in provenance['source_tree_sha256'].items():assert sha(source/p)==h,p
    for p,h in c['source_sha256'].items():assert sha(source/p)==h,p
    for p,h in c['historical_evidence'].items():assert sha(Path(p))==h,p
    assert len(s['replays'])==c['maximum_replays']
    for r in s['replays']:
        assert not any(r['differences'].values())
        assert sha(Path(r['output'])/'events.jsonl')==r['output_sha256']
    assert a['covariance_checks']>0 and a['minimum_covariance_eigenvalue']>=-1e-8
    assert len(a['innovations'])>0 and a['native_forecast_max_difference_m']<1e-4
    s['state']='ready_for_backend' if s['eligible'] else 'completed_replay_rejected'
    write(root/'state.json',s)
    write(root/'validation.json',dict(passed=True,replays=len(s['replays']),frames=sum(r['frames'] for r in s['replays']),
      same_accepted_measurements=True,protected_unchanged=True,historical_evidence_unchanged=True,
      covariance_checks=a['covariance_checks'],new_closed_loop_trials=0,ui_acquisition=0,automatic_promotion=False,
      build_source_files_verified=provenance['source_count'],required_runtime_diagnostic_verified=True,
      study_source_sha256=sha(Path(__file__))))
    seal={p.name:sha(p) for p in root.iterdir() if p.is_file() and p.name not in ('report-seal.json','controller.log')}
    write(root/'report-seal.json',seal)
    print(json.dumps(dict(verified=True,eligible=s['eligible'])),flush=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('freeze','replay','analyze','verify'))
    parser.add_argument('--output',type=Path,default=DEFAULT);args=parser.parse_args();root=args.output.resolve()
    {'freeze':freeze,'replay':replay,'analyze':analyze,'verify':verify}[args.action](root)


if __name__=='__main__':main()
