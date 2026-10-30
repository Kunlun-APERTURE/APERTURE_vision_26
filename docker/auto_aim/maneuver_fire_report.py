"""Independent scoring, paired uncertainty and evidence audit for S/I shooting."""
import argparse
from collections import Counter
import html
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

import manage
from background_capacity import assess
from decision_benchmark import sha,verify_bundle
from decision_benchmark_report import aggregate,behavior
from perception_study import protected,quantile
from report import load,write
from maneuver_fire_study import initialize,VARIANTS,compare,regression_gate


def paired(root):
    import numpy as np
    c=load(root/'contract.json');s=load(root/'state.json');out=[]
    for phase in ('screen','regression','confirm'):
        rows=[r for r in s['runs'] if r['phase']==phase]
        for ref in ('baseline','BS','P'):
            for variant in ('S','I'):
                for scene in ('all',*sorted({r['scene'] for r in rows})):
                    a=[r for r in rows if r['variant']==ref and (scene=='all' or r['scene']==scene)]
                    b=[r for r in rows if r['variant']==variant and (scene=='all' or r['scene']==scene)]
                    if not a or not b:continue
                    keys=lambda rr:{(r['sensor_seed'],r['scene']) for r in rr}
                    if keys(a)!=keys(b):
                        # The static five-way reference is intentionally smaller
                        # than the three-way spin/ego regression. Do not pool it.
                        if scene=='all':continue
                        raise ValueError('Unpaired candidate/reference')
                    seeds=sorted({r['sensor_seed'] for r in a})
                    totals=lambda rr:np.asarray([[sum(r[k] for r in rr if r['sensor_seed']==seed) for k in ('shots','hits','weight','duration_s')] for seed in seeds])
                    aa,bb=totals(a),totals(b);rng=np.random.default_rng(c['bootstrap']['random_seed'])
                    draws=rng.integers(0,len(seeds),size=(10000,len(seeds)));ax,bx=aa[draws].sum(axis=1),bb[draws].sum(axis=1)
                    original_a,original_b=aa.sum(axis=0),bb.sum(axis=0);metrics={}
                    for name,num,den in (('weighted',2,0),('exact',1,0),('hits_per_s',1,3),('frequency',0,3)):
                        valid=(ax[:,den]>0)&(bx[:,den]>0)
                        delta=float(original_b[num]/original_b[den]-original_a[num]/original_a[den]) if original_a[den]>0 and original_b[den]>0 else None
                        ci=np.quantile(bx[:,num]/bx[:,den]-ax[:,num]/ax[:,den],[.025,.975]).tolist() if valid.all() else None
                        metrics[name]=dict(delta=delta,ci95=ci)
                    out.append(dict(phase=phase,reference=ref,variant=variant,scene=scene,seeds=len(seeds),metrics=metrics))
    write(root/'paired-effects.json',dict(comparisons=out,unit='paired sensor seed; scenes kept together when pooled',
      samples=10000,multiple_comparisons='Development intervals descriptive, not multiplicity adjusted; independent confirmation has its own frozen gate'))
    forecast_quality(root)
    plots(root)


def plots(root):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    s=load(root/'state.json');fig,axes=plt.subplots(2,3,figsize=(15,8),layout='constrained')
    for ax,(phase,scene,title) in zip(axes.flat,[('screen','hf_uniform_3','Uniform 3 m/s'),('screen','hf_turn90_3','90-degree turns'),
        ('regression','hf_static_5','Static 5 m'),('regression','hf_spin_pos','Positive spin'),('regression','hf_spin_neg','Negative spin'),
        ('regression','ego_right_centered','Ego motion')]):
        rows=[r for r in s['runs'] if r['phase']==phase and r['scene']==scene]
        variants=[v for v in VARIANTS if any(r['variant']==v for r in rows)]
        values=[aggregate([r for r in rows if r['variant']==v]) for v in variants]
        ax.bar([i-.18 for i in range(len(values))],[100*r['target_hit_rate'] if r['target_hit_rate'] is not None else 0 for r in values],width=.36,label='Exact hit %',color='#3568a8')
        ax.bar([i+.18 for i in range(len(values))],[100*r['weighted_hit_rate'] if r['weighted_hit_rate'] is not None else 0 for r in values],width=.36,label='Weighted hit %',color='#2d9a89')
        ax.set(xticks=range(len(values)),xticklabels=[v+'\nn='+str(r['shots']) for v,r in zip(variants,values)],title=title,ylim=(0,105),ylabel='Percent of shots')
        ax.grid(axis='y',alpha=.2);ax.set_axisbelow(True)
    axes.flat[0].legend(loc='lower left',fontsize=8)
    fig.savefig(root/'shooting-comparison.png',dpi=150);plt.close(fig)


def forecast_quality(root):
    import numpy as np
    from perception_attribution_analysis import timeline,match_truth
    s=load(root/'state.json');samples=[];coverage=Counter()
    for row in [r for r in s['runs'] if r['phase']=='screen']:
        source=manage.OPS/'experiments'/row['run_id'];truth=timeline(source);run=load(source/'run-state.json')
        for line in (source/'session-evidence/events.jsonl').open():
            p=json.loads(line)
            if p['kind']!='result':continue
            f,r=p['data']['frame'],p['data']['result'];h=r.get('prediction_horizon_s');sid=r.get('selected_plate_id',-1)
            if not run['measurement_start_stamp_ns']<=f['stamp_ns']<=run['measurement_end_stamp_ns']:continue
            if not r.get('valid') or sid<0 or not isinstance(h,(float,int)) or not 0<h<1:continue
            future=f['stamp_ns']+round(h*1e9)
            if future>run['measurement_end_stamp_ns']:continue
            coverage[row['variant']+'_eligible']+=1
            link=next((a for a in (r.get('continuity',{}).get('image_association') or {}).get('observations',[]) if a['plate_id']==sid),None)
            obs=r.get('observations',[])
            if link is None or not obs:continue
            distances=[float(np.linalg.norm(np.mean(a['corners'],axis=0)-link['image_center'])) for a in obs]
            idx=int(np.argmin(distances));matched=match_truth(f,obs,truth(f['stamp_ns']))
            if distances[idx]>4 or idx not in matched:continue
            actual=next((a for a in truth(future) if a['id']==matched[idx]['plate']['id']),None)
            if actual is None:continue
            coverage[row['variant']+'_matched']+=1
            predicted=np.asarray(r['aim_point'])+f['gimbal_position_map'];actual=np.asarray(actual['position'])
            muzzle=np.asarray(f['gimbal_position_map'])+r.get('muzzle_offset_world',[0,0,0]);ray=actual[:2]-muzzle[:2];ray/=np.linalg.norm(ray)
            error=predicted-actual;lateral=np.array([-ray[1],ray[0]])
            angle=math.remainder(math.atan2(predicted[1]-muzzle[1],predicted[0]-muzzle[0])-math.atan2(actual[1]-muzzle[1],actual[0]-muzzle[0]),2*math.pi)
            samples.append(dict(variant=row['variant'],scene=row['scene'],advice=bool(r['fire_advice']),
              xy=float(np.linalg.norm(error[:2])),radial=abs(float(error[:2]@ray)),lateral=abs(float(error[:2]@lateral)),
              z=abs(float(error[2])),yaw=abs(angle)))
    groups=[]
    for variant in VARIANTS:
        for scene in ('all','hf_uniform_3','hf_turn90_3'):
            for advice in ('all','advice_true','advice_false'):
                rr=[r for r in samples if r['variant']==variant and (scene=='all' or r['scene']==scene) and (advice=='all' or r['advice']==(advice=='advice_true'))]
                if not rr:continue
                groups.append(dict(variant=variant,scene=scene,advice=advice,n=len(rr),
                  metrics={k:dict(p50=quantile([r[k] for r in rr],.5),p95=quantile([r[k] for r in rr],.95)) for k in ('xy','radial','lateral','z','yaw')}))
    write(root/'forecast-quality.json',dict(groups=groups,coverage=dict(coverage),
      scope='Posthoc image-matched selected physical plate at each closed-loop decision horizon. Inputs and cameras differ across variants. Advice is not an actual-shot command association. Baseline lacks the shared image identity diagnostic and is omitted.'))


def live_diagnostics(root):
    c=load(root/'contract.json');s=load(root/'state.json');groups=[]
    for phase in ('screen','regression','confirm'):
        for variant in VARIANTS:
            samples=[];probability=[];counts=Counter();max_error=0.;deltas=[];delta_xy=[];delta_z=[];phase_scores=[]
            outcomes=Counter();impact_x=[];impact_y=[];hit_proximity=[]
            for row in [r for r in s['runs'] if r['phase']==phase and r['variant']==variant]:
                source=manage.OPS/'experiments'/row['run_id'];run=load(source/'run-state.json')
                for line in (source/'session-evidence/events.jsonl').open():
                    p=json.loads(line)
                    if p['kind']!='result':continue
                    f=p['data']['frame'];r=p['data']['result'];stamp=f['stamp_ns']
                    if not run['measurement_start_stamp_ns']<=stamp<=run['measurement_end_stamp_ns']:continue
                    counts['frames']+=1;counts['valid']+=bool(r.get('valid'));samples.append(r['pipeline_ms'])
                    if variant not in ('S','I') or not r.get('valid'):continue
                    marker=(r.get('diagnosis') or {}).get('aimer',{}).get('state_motion_predictor')
                    counts['aim_marker']+=marker==variant
                    filter=r.get('continuity',{}).get('filter') or {};banks=filter.get('motion_v6',{}).get('plates',[])
                    sid=r.get('selected_plate_id',-1);h=r.get('prediction_horizon_s')
                    if not 0<=sid<len(banks) or not banks[sid].get('initialized') or not isinstance(h,(int,float)):continue
                    b=banks[sid]
                    def forecast(state,tau):
                        g=tau*(h+tau*math.expm1(-h/tau))
                        return [state[3*j]+h*state[3*j+1]+g*state[3*j+2] for j in range(3)]
                    expected=forecast(b['single_state'],.15) if variant=='S' else [sum(p*forecast(m['state'],m['tau'])[j] for p,m in zip(b['probability'],b['models'])) for j in range(3)]
                    max_error=max(max_error,math.dist(expected,r['aim_point']));counts['reconstructed']+=1
                    cv=filter['plates'][sid]['state'];cvfuture=[cv[2*j]+h*cv[2*j+1] for j in range(3)]
                    deltas.append(math.dist(cvfuture,r['aim_point']));probability.append(b['probability'][1])
                    delta_xy.append(math.hypot(cvfuture[0]-r['aim_point'][0],cvfuture[1]-r['aim_point'][1]))
                    delta_z.append(abs(cvfuture[2]-r['aim_point'][2]))
                shots=root/'normalized-scores'/row['run_id']/'shots.jsonl'
                if shots.exists():
                    selected=[json.loads(line) for line in shots.open()]
                    for shot in selected:
                        outcomes[shot['outcome']]+=1
                        if shot['outcome']=='target_armor':
                            if shot.get('impact_x_fraction') is not None:impact_x.append(abs(shot['impact_x_fraction']))
                            if shot.get('impact_y_fraction') is not None:impact_y.append(abs(shot['impact_y_fraction']))
                            if shot.get('impact_center_proximity') is not None:hit_proximity.append(shot['impact_center_proximity'])
                    for label in ('early','moving','after_turn'):
                        rr=[x for x in selected if ('early' if x['fire_time_s']<1 else 'after_turn' if row['scene']=='hf_turn90_3' and x['fire_time_s']>=3.2 else 'moving')==label]
                        if rr:phase_scores.append(dict(run_id=row['run_id'],scene=row['scene'],segment=label,shots=len(rr),
                          hits=sum(x['outcome']=='target_armor' for x in rr),weight=sum(x['weighted_hit_score'] for x in rr)))
            if counts['frames']:
                groups.append(dict(phase=phase,variant=variant,counts=dict(counts),
                  pipeline_ms_p50=quantile(samples,.5),pipeline_ms_p95=quantile(samples,.95),
                  forecast_reconstruction_max_error_m=max_error,forecast_delta_from_CV_p95_m=quantile(deltas,.95),
                  forecast_delta_XY_p95_m=quantile(delta_xy,.95),forecast_delta_Z_p95_m=quantile(delta_z,.95),
                  outcomes=dict(outcomes),hit_impact_X_fraction_p95=quantile(impact_x,.95),
                  hit_impact_Y_fraction_p95=quantile(impact_y,.95),
                  mean_proximity_on_hits=sum(hit_proximity)/len(hit_proximity) if hit_proximity else None,
                  maneuver_probability_p50=quantile(probability,.5),phase_scores=phase_scores))
    write(root/'live-diagnostics.json',dict(groups=groups,
      note='Wall-clock pipeline durations are observed under fixed logical timing and do not qualify real-time deployment. Early/moving/after_turn groups refer to firing time, not guaranteed constant motion over bullet flight.'))


def verify(root):
    c=initialize(root);s=load(root/'state.json');live=load(root/'live-diagnostics.json');checks={}
    assert s['state']=='completed' and len(s['replays'])==c['maximum_replays']
    assert protected()==c['protected']
    for p,h in c['historical_files'].items():assert sha(Path(p))==h,p
    for f,h in c['source_sha256'].items():assert sha(root/'build-source/sp_vision_25'/f)==h,f
    for p in c['bundles'].values():verify_bundle(Path(p))
    assert sha(root/'build-source/sp_vision_25/build/motion/sim_auto_aim')==c['candidate_binary_sha256']
    for row in s['replays']:
        assert sha(Path(row['output'])/'events.jsonl')==row['output_sha256']
        assert not any(row['differences'].values())
    counts=Counter(r['phase'] for r in s['runs']);assert counts['screen']==50 and counts['regression']==42 and counts['probe_p1']==counts['probe_p2']==10
    assert all(counts[k]<=v for k,v in [('screen',50),('regression',42),('confirm',60),('repeat',6)])
    seen=set()
    for r in s['runs']:
        key=(r['phase'],r['variant'],r['scene'],r['sensor_seed'],r['repeat_id']);assert key not in seen;seen.add(key)
        path=manage.OPS/'experiments'/r['run_id'];m=load(path/'manifest.json');meta=load(Path(c['bundles'][r['variant']])/'bundle.json')
        for f,h in r['source_hashes'].items():assert sha(path/f)==h,(r['run_id'],f)
        assert m['algorithm_binary_sha256']==c['candidate_binary_sha256']
        assert m['configuration_sha256']['algorithm.yaml']==meta['files']['sp_vision_25/configs/simulator.yaml']
        assert all(m.get(k)==v for k,v in dict(scene=r['scene'],sensor_seed=r['sensor_seed'],terrain='flat_range',input_contract='aligned-v1',warmup_frames=30).items())
        assert all(m['execution'].get(k)==v for k,v in dict(mode='lockstep',timing_profile=c['backend_timing_profile'],detector_history='session',clock_domain='simulation').items())
        assert all(m['shooting'].get(k)==v for k,v in dict(bullet_speed=25.,reported_bullet_speed=25.,physics='no-drag',aim_origin='muzzle',
          gimbal_profile='feedforward',fire_policy='advice',fire_rate_limit_hz=5.,algorithm_profile='tracking_fire_v3').items())
        for role in ('simulator','bridge','evaluator','algorithm'):
            assert m['containers'][role+'_image_id']==meta['images']['simulator' if role=='bridge' else role]
        assert r['health']['healthy'] and r['wall_s']<=40
        assert load(path/'session-evidence-audit.json')['passed']
        with jobs_db() as db:
            attempts=[dict(a) for a in db.execute('SELECT a.* FROM attempts a JOIN items i ON a.item_id=i.id WHERE i.job_id=?',(r['job_id'],))]
        assert len(attempts)==1 and attempts[0]['state']=='finished' and attempts[0]['verdict']!='invalid'
    for row in live['groups']:
        if row['variant'] in ('S','I'):
            assert row['counts']['aim_marker']==row['counts']['valid']
            assert row['counts']['reconstructed']>0 and row['forecast_reconstruction_max_error_m']<1e-4
            assert row['forecast_delta_from_CV_p95_m']>1e-4
    if s.get('backend_confirmed'):
        repeats=[]
        for r in [r for r in s['runs'] if r['phase']=='repeat']:
            a=next(x for x in s['runs'] if x['phase']=='confirm' and all(x[k]==r[k] for k in ('variant','scene','sensor_seed')))
            repeats.append(dict(variant=r['variant'],scene=r['scene'],same_scores=all(a[k]==r[k] for k in ('shots','hits','weight')),
              same_behavior=behavior(manage.OPS/'experiments'/a['run_id'])==behavior(manage.OPS/'experiments'/r['run_id'])))
        s['ui_eligible']=len(repeats)==6 and all(r['same_scores'] and r['same_behavior'] for r in repeats)
        write(root/'repeatability.json',dict(passed=s['ui_eligible'],pairs=repeats));write(root/'state.json',s)
    write(root/'validation.json',dict(passed=True,backend_trials=len(s['runs']),trial_counts=dict(counts),
      replays=len(s['replays']),replay_frames=sum(r['frames'] for r in s['replays']),
      protected_unchanged=True,historical_evidence_unchanged=True,algorithm_payloads_verified=True,
      fixed_physics_fire_and_scoring=True,live_predictor_activation_verified=True,
      maximum_complete_trial_wall_s=max(r['wall_s'] for r in s['runs']),
      ui_acquisition=0,automatic_promotion=False,capacity_registered=False,
      study_source_sha256={p.name:sha(p) for p in (manage.OPS/'maneuver_fire_study.py',manage.OPS/'maneuver_fire_report.py')}))


def jobs_db():
    import background_jobs
    return background_jobs.connect()


def render(root):
    c=load(root/'contract.json');s=load(root/'state.json');v=load(root/'validation.json');p=load(root/'paired-effects.json');live=load(root/'live-diagnostics.json')
    pct=lambda x:'—' if x is None else f'{100*x:.3f}%'
    num=lambda x:'—' if x is None else f'{x:.3f}'
    lines=['# S/I 预测器后台射击对照','',
      '本轮按用户要求进入真实后台闭环射击，核对旁路预测改善是否转化为命中收益；没有修调上一轮 S/I 的模型参数。正式 v3 保留，BS 和旧 P 为同批对照。',
      'S/I 仅替换 Aimer 目标副本中的未来位置、速度与协方差，当前状态和接受量测仍由 BS CV 决定。射击 advice、5Hz上限、25m/s无阻力、平坦靶场及评分未改。','',
      f'完成 {len(s["runs"])} 轮后台射击；单次最大完整用时 {v["maximum_complete_trial_wall_s"]:.2f}s。新增UI采集0。',
      '[冻结配置与预算](contract.json) · [证据校验](validation.json) · [配对区间](paired-effects.json) · [实际启用核查](live-diagnostics.json)','']
    main=[r for r in s['runs'] if r['phase']=='screen'];scores={v:aggregate([r for r in main if r['variant']==v]) for v in VARIANTS}
    lines+=['结论：本批主场景未验证出 S/I 的性能提升。相对 BS，加权命中率 '+pct(scores['BS']['weighted_hit_rate'])+'，S 为 '+pct(scores['S']['weighted_hit_rate'])+'，I 为 '+pct(scores['I']['weighted_hit_rate'])+'；精确命中和命中/秒也下降。主要退化在90°变向，未进入独立确认。','',
      '局部结果需要分别看：自车移动中 S/I 改善；旋转中加权比例提高，但命中/秒下降，不能解释为整体打击能力提高。静止 I 保持100%精确命中且加权仅下降约0.33pp，说明上一轮静止速度指标退化并未同等转化为射击退化；S静止加权下降约3.83pp。','',
      '闭环匹配分析还显示：未来点尾部P95改善，但典型误差并未一起改善。建议开火帧的XY误差中位数由BS的0.217m增至S的0.303m、I的0.298m；这些帧不是已关联的实际发射，仍须进一步核查命令到弹丸的时间与身份对应，不能仅凭此项断定脱靶原因。','']
    lines+=['![按场景的精确与加权命中率](shooting-comparison.png)','']
    for phase,title in (('screen','开发集：匀速与90°变向'),('regression','静止、旋转与自车移动回归'),('confirm','新种子独立确认')):
        rows=[r for r in s['runs'] if r['phase']==phase]
        if not rows:continue
        lines+=['## '+title,'','| 场景 | 配置 | 命中/发射 | 精确命中率 | 加权命中率 | 命中/秒 | 首次命中P90/s |','|---|---|---:|---:|---:|---:|---:|']
        scenes=sorted({r['scene'] for r in rows})+(['all'] if phase!='regression' else [])
        for scene in scenes:
            for variant in VARIANTS:
                rr=[r for r in rows if r['variant']==variant and (scene=='all' or r['scene']==scene)]
                if not rr:continue
                a=aggregate(rr);lines.append(f'| {scene} | {variant} | {a["hits"]}/{a["shots"]} | {pct(a["target_hit_rate"])} | {pct(a["weighted_hit_rate"])} | {a["hits_per_s"]:.3f} | {num(quantile([r["first_hit_s"] for r in rr],.9))} |')
        if phase=='regression':lines+=['','回归每格三个种子，属于额外目录场景的探索性回归，尚无独立单/双路效果与UI等价资格；各场景分别判定，不以合并成绩掩盖退化。BS/P只做静止回归，未参与旋转和自车移动比较。']
    lines+=['','## 配对收益与门槛','', '| 阶段/场景 | 候选/对照 | 加权差/pp | 配对95%区间/pp |','|---|---|---:|---|']
    for row in p['comparisons']:
        if row['phase']=='regression' or row['scene']!='all':continue
        m=row['metrics']['weighted'];ci=m['ci95'];interval='无有效区间' if ci is None else f'[{100*ci[0]:+.2f}, {100*ci[1]:+.2f}]'
        lines.append(f'| {row["phase"]}/{row["scene"]} | {row["variant"]}/{row["reference"]} | {num(None if m["delta"] is None else 100*m["delta"])} | {interval} |')
    lines+=['','开发区间按配对种子重采样，保留两个场景为同一单位；不将弹丸或视频帧当独立样本。多重开发比较仅作描述，独立确认另执行冻结门槛。']
    for row in load(root/'screen-result.json'):
        lines+=['',row['variant']+'/'+row['reference']+' 开发通过='+str(row['passed'])+'；未通过：'+str([k for k,val in row['checks'].items() if not val])+'。']
    for row in load(root/'regression-result.json'):
        lines+=['',row['variant']+' 回归通过='+str(row['passed'])+'；未通过：'+str([k for k,val in row['checks'].items() if not val])+'。']
    lines+=['','最低发射样本数与指标变化分别判定；旋转样本不足不等于其加权数值下降，但仍不能获得回归通过资格。']
    for row in load(root/'confirm-result.json',[]):lines+=['','独立 '+row['variant']+'/'+row['reference']+' 通过='+str(row['passed'])+'；区间='+str(row['weighted_gain_95'])+'。']
    lines+=['',f'开发入选={s.get("selected")}；独立确认通过={s.get("backend_confirmed",False)}；默认版本未替换。']
    if not s.get('selected'):lines+=['','没有候选同时通过主场景和回归门槛，因此按冻结预算未运行独立确认，没有将失败候选自动部署。']
    lines+=['','## 按发射时段分解主场景','',
      '| 场景/发射时段 | 配置 | 命中/发射 | 加权命中率 |','|---|---|---:|---:|']
    for scene in ('hf_uniform_3','hf_turn90_3'):
        for segment in ('early','moving','after_turn'):
            for variant in VARIANTS:
                rr=[p for row in live['groups'] if row['phase']=='screen' and row['variant']==variant
                    for p in row['phase_scores'] if p['scene']==scene and p['segment']==segment]
                shots=sum(x['shots'] for x in rr)
                if shots:lines.append(f'| {scene}/{segment} | {variant} | {sum(x["hits"] for x in rr)}/{shots} | {pct(sum(x["weight"] for x in rr)/shots)} |')
    lines+=['','early 为发射时刻<1s，after_turn 为变向场景发射时刻≥3.2s，其余为moving。该分组不保证弹丸飞行期间运动不变；静止靶场结果另列。']
    lines+=['','## 命中落点与预测修正方向','',
      '| 主场景配置 | 命中后平均中心分 | 命中横向归一化偏移P95 | 命中纵向归一化偏移P95 | 相对CV的XY修正P95/m | 相对CV的Z修正P95/m |',
      '|---|---:|---:|---:|---:|---:|']
    for row in live['groups']:
        if row['phase']=='screen':lines.append(f'| {row["variant"]} | {num(row["mean_proximity_on_hits"])} | {num(row["hit_impact_X_fraction_p95"])} | {num(row["hit_impact_Y_fraction_p95"])} | {num(row["forecast_delta_XY_p95_m"])} | {num(row["forecast_delta_Z_p95_m"])} |')
    lines+=['','落点统计只含命中弹，存在选择偏差，不能用于推断所有脱靶弹的误差方向。相对CV修正是在各自闭环输入上的诊断量，不是同输入误差收益或严格归因。']
    quality=load(root/'forecast-quality.json')
    lines+=['','## 闭环未来点：径向与横向分解','',
      '| 配置/建议状态 | 匹配帧数 | XY误差P50/m | XY误差P95/m | 径向误差P95/m | 横向误差P95/m | Z误差P95/m | yaw误差P95/rad |',
      '|---|---:|---:|---:|---:|---:|---:|---:|']
    for row in quality['groups']:
        if row['scene']!='all' or row['advice']=='advice_false':continue
        m=row['metrics'];lines.append(f'| {row["variant"]}/{row["advice"]} | {row["n"]} | {num(m["xy"]["p50"])} | '+' | '.join(num(m[k]['p95']) for k in ('xy','radial','lateral','z','yaw'))+' |')
    lines+=['','匹配覆盖：'+str(quality['coverage'])+'。该项真值只在射击完成后进入分析器，未进入算法。不同候选闭环的图像和相机轨迹不同，不能当作同输入严格归因；advice_true只表示建议开火，不等于已关联到实际弹丸。v3缺少共享图像身份诊断，因此不在此项条件评分中，射击评分仍完整。']
    parallel=load(root/'parallel-result.json')
    lines+=['','## 并行、计算成本与适用范围','',f'单/双路一致性通过={parallel["passed"]}，实际重叠={parallel["actual_overlap"]}，后续并行度={s["parallelism"]}。资格仅限本次冻结负载，不登记全局容量。',
      '| 阶段 | 轮数 | 批次跨度/s | 吞吐/小时 |','|---|---:|---:|---:|']
    for phase in ('probe_p1','probe_p2','screen','regression','confirm','repeat'):
        rr=[r for r in s['runs'] if r['phase']==phase]
        if rr:
            span=max(r['attempt']['ended'] for r in rr)-min(r['attempt']['started'] for r in rr)
            lines.append(f'| {phase} | {len(rr)} | {span:.1f} | {len(rr)*3600/span:.1f} |')
    lines+=['','| 主场景配置 | pipeline P50/ms | pipeline P95/ms | 有效帧/已核查实际预测 | 与CV未来点差P95/m |','|---|---:|---:|---:|---:|']
    for row in live['groups']:
        if row['phase']!='screen':continue
        counts=row['counts'];lines.append(f'| {row["variant"]} | {num(row["pipeline_ms_p50"])} | {num(row["pipeline_ms_p95"])} | {counts["valid"]}/{counts.get("reconstructed",0)} | {num(row["forecast_delta_from_CV_p95_m"])} |')
    lines+=['','固定逻辑时序保证对比控制，却不代替实车实时截止时间资格；表中的实测计算耗时也受当前负载影响。S/I的XYZ预测实际进入弹道求解，本轮射击评分已包含垂向偏差的后果，但未完成实车弹道标定。',
      '原始输入、状态、低分、无命中和全部发射记录保留，未请求额外视频渲染，尚无新增人工整段视频验收。旁路回放与闭环射击属于不同证据，不跨批次比较绝对成绩或宣称新命中提升。']
    text='\n'.join(lines)+'\n';(root/'report.md').write_text(text)
    def inline(x):return re.sub(r'\[([^\]]+)\]\(([^)]+)\)',r'<a href="\2">\1</a>',html.escape(x))
    parts=[];table=False
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
    (root/'report.html').write_text('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>S/I后台射击对照</title><style>body{max-width:1250px;margin:32px auto;padding:0 20px;font:16px/1.65 sans-serif;color:#18212d}table{width:100%;border-collapse:collapse;font-size:14px;margin:20px 0}td,th{border:1px solid #d5dce5;padding:7px;text-align:right}th{background:#edf2f8}td:first-child,th:first-child{text-align:left}a{color:#175cb5}h2{margin-top:36px}</style>'+''.join(parts))


def finish(root):
    c=initialize(root);meta=load(Path(c['bundles']['baseline'])/'bundle.json')
    command=['docker','run','--rm','--network','none','--cpus','2','--user',f'{os.getuid()}:{os.getgid()}',
      '-v',str(manage.ROOT)+':'+str(manage.ROOT)+':ro','-v',str(root)+':'+str(root),'-w',str(manage.ROOT),
      '--entrypoint','python3',meta['images']['evaluator'],'-B',str(Path(__file__)),'paired','--output',str(root)]
    subprocess.run(command,check=True,timeout=90)
    live_diagnostics(root);verify(root);render(root)
    write(root/'report-seal.json',{p.name:sha(p) for p in root.iterdir() if p.is_file() and p.name not in ('report-seal.json','controller.log','controller.lock')})


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('paired','finish'));parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();root=args.output.resolve()
    if args.action=='paired':paired(root)
    else:finish(root)
