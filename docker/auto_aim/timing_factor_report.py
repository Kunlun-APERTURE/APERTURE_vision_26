"""Report the frozen 2x2 timing/model experiment without acquiring new trials."""
import argparse
from collections import Counter
import html
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

import background_jobs as jobs
import manage
from decision_benchmark import sha,verify_bundle
from decision_benchmark_report import aggregate,unified_score,behavior
from maneuver_fire_study import compare,regression_gate
from perception_study import protected,quantile
from report import load,write
from timing_factor_study import initialize,VARIANTS,BASE_VARIANT,DELAY_VARIANTS,PARENT

METRICS=(('weighted',2,0),('exact',1,0),('hits_per_s',1,3),('frequency',0,3))
CONTRASTS={
 'BS_delay':{'BS4':1,'BS15':-1},
 'I_delay':{'I4':1,'I15':-1},
 'model_at_15':{'I15':1,'BS15':-1},
 'model_at_4':{'I4':1,'BS4':-1},
 'interaction':{'I4':1,'I15':-1,'BS4':-1,'BS15':1},
 'BS4_vs_v3':{'BS4':1,'baseline':-1},
 'I4_vs_v3':{'I4':1,'baseline':-1},
 'I4_vs_BS15':{'I4':1,'BS15':-1}}


def stats(values):
    return dict(n=len(values),p50=quantile(values,.5),p95=quantile(values,.95),
      mean=sum(values)/len(values) if values else None)


def analyze(root):
    import numpy as np
    c=initialize(root);s=load(root/'state.json');results=[]
    for phase in ('screen','regression','confirm'):
        rows=[r for r in s['runs'] if r['phase']==phase]
        for scene in ('all',*sorted({r['scene'] for r in rows})):
            for name,weights in CONTRASTS.items():
                rr={v:[r for r in rows if r['variant']==v and (scene=='all' or r['scene']==scene)] for v in weights}
                if any(not rs for rs in rr.values()):continue
                keys={v:{(r['sensor_seed'],r['scene']) for r in rs} for v,rs in rr.items()}
                assert len({tuple(sorted(x)) for x in keys.values()})==1,(phase,scene,name)
                seeds=sorted({r['sensor_seed'] for r in next(iter(rr.values()))})
                totals={v:np.asarray([[sum(r[k] for r in rs if r['sensor_seed']==seed) for k in ('shots','hits','weight','duration_s')] for seed in seeds],float) for v,rs in rr.items()}
                rng=np.random.default_rng(c['bootstrap']['random_seed'])
                draw=rng.integers(0,len(seeds),size=(c['bootstrap']['samples'],len(seeds)))
                boot={v:x[draw].sum(axis=1) for v,x in totals.items()};point={v:x.sum(axis=0) for v,x in totals.items()};metrics={}
                for metric,num,den in METRICS:
                    valid=all(np.all(x[:,den]>0) for x in boot.values())
                    delta=sum(weights[v]*x[num]/x[den] for v,x in point.items()) if all(x[den]>0 for x in point.values()) else None
                    interval=np.quantile(sum(weights[v]*x[:,num]/x[:,den] for v,x in boot.items()),[.025,.975]).tolist() if valid else None
                    metrics[metric]=dict(delta=float(delta) if delta is not None else None,ci95=interval)
                results.append(dict(phase=phase,scene=scene,contrast=name,weights=weights,seeds=len(seeds),metrics=metrics))
    write(root/'factorial-effects.json',dict(contrasts=results,bootstrap=c['bootstrap'],
      estimand='Difference of aggregate per-shot rates; interaction=(I4-I15)-(BS4-BS15). Resample paired sensor seeds jointly across variants and scenes.',
      limitations='Development contrasts are descriptive, correlated and not multiplicity-adjusted. Three-seed regression is exploratory. Independent confirmation was conditional on frozen selection gates.'))
    plots(root)


def plots(root):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    s=load(root/'state.json');fig,axes=plt.subplots(2,3,figsize=(15,8),layout='constrained')
    settings=[('screen','hf_uniform_3','Uniform 3 m/s'),('screen','hf_turn90_3','90-degree turns'),
      ('regression','hf_static_5','Static 5 m'),('regression','hf_spin_pos','Positive spin'),
      ('regression','hf_spin_neg','Negative spin'),('regression','ego_right_centered','Ego motion')]
    for ax,(phase,scene,title) in zip(axes.flat,settings):
        values=[aggregate([r for r in s['runs'] if r['phase']==phase and r['scene']==scene and r['variant']==v]) for v in VARIANTS]
        ax.bar([i-.18 for i in range(5)],[100*r['target_hit_rate'] if r['target_hit_rate'] is not None else 0 for r in values],width=.36,color='#3568a8',label='Exact hit %')
        ax.bar([i+.18 for i in range(5)],[100*r['weighted_hit_rate'] if r['weighted_hit_rate'] is not None else 0 for r in values],width=.36,color='#2d9a89',label='Weighted hit %')
        ax.set(xticks=range(5),xticklabels=[v+'\nn='+str(r['shots']) for v,r in zip(VARIANTS,values)],ylim=(0,105),title=title,ylabel='Percent of all shots')
        ax.grid(axis='y',alpha=.2);ax.set_axisbelow(True)
    axes.flat[0].legend(fontsize=8,loc='lower left')
    fig.savefig(root/'shooting-comparison.png',dpi=160);plt.close(fig)
    results=load(root/'factorial-effects.json')['contrasts'];fig,axes=plt.subplots(1,2,figsize=(11,4.5),layout='constrained')
    for ax,scene in zip(axes,('hf_uniform_3','hf_turn90_3')):
        labels=('BS_delay','I_delay','model_at_4','interaction')
        rr=[next(r for r in results if r['phase']=='screen' and r['scene']==scene and r['contrast']==name) for name in labels]
        for i,r in enumerate(rr):
            m=r['metrics']['weighted'];ci=m['ci95'];delta=100*m['delta']
            if ci:ax.plot([100*ci[0],100*ci[1]],[i,i],color='#3568a8',linewidth=3)
            ax.scatter([delta],[i],color='#2d9a89',s=55,zorder=3)
        ax.axvline(0,color='#6c7785',linestyle='--');ax.set(yticks=range(4),yticklabels=('BS: short - 15ms','IMM: short - 15ms','IMM - BS at short','Interaction'),title=scene,xlabel='Weighted hit rate difference (pp)')
        ax.invert_yaxis();ax.grid(axis='x',alpha=.2)
    fig.suptitle('Paired-seed development intervals; no independent confirmation',fontsize=11)
    fig.savefig(root/'timing-effects.png',dpi=160);plt.close(fig)


def live_audit(root):
    import yaml
    c=load(root/'contract.json');s=load(root/'state.json');groups=[];per_run=[];orphans=[];gate_rows=[];startup=[]
    for entry in s['runs']:
        folder=manage.OPS/'experiments'/entry['run_id'];state=load(folder/'run-state.json')
        config=yaml.safe_load((Path(c['bundles'][entry['variant']])/'sp_vision_25/configs/simulator.yaml').read_text())
        rows=[json.loads(line)['data'] for line in (folder/'session-evidence/events.jsonl').open() if json.loads(line)['kind']=='result']
        delays=[];times=[];reconstructed=0;marker=0;max_error=0.;valid=0;gates=Counter()
        for p in rows:
            f,r=p['frame'],p['result']
            if not state['measurement_start_stamp_ns']<=f['stamp_ns']<=state['measurement_end_stamp_ns']:continue
            valid+=bool(r.get('valid'));times.append(r['pipeline_ms'])
            check=r.get('fire_check') or {};gates['capture_frames']+=1
            for label,key in (('tracking_measurement_valid','valid'),('aligned','aligned'),('discontinuity','discontinuity'),('plate_changed','plate_changed')):
                gates[label]+=bool(check.get(key))
            gates['advice']+=bool(r.get('fire_advice'));gates['forwarded_with_capture_in_measurement']+=bool(p.get('fire_forwarded'))
            diag=(r.get('diagnosis') or {}).get('aimer') or {}
            if 'launch_delay_s' in diag:
                expected=c['short_delay_s'] if entry['variant'] in DELAY_VARIANTS else (
                  config['high_speed_delay_time'] if abs(r['filter_state'][7])>config['decision_speed'] else config['low_speed_delay_time'])
                assert abs(diag['launch_delay_s']-expected)<1e-12,(entry['run_id'],diag['launch_delay_s'],expected)
                delays.append(diag['launch_delay_s'])
            if entry['variant'].startswith('I') and r.get('valid'):
                marker+=diag.get('state_motion_predictor')=='I'
                banks=r.get('continuity',{}).get('filter',{}).get('motion_v6',{}).get('plates',[]);sid=r['selected_plate_id'];h=r['prediction_horizon_s']
                if 0<=sid<len(banks) and banks[sid].get('initialized'):
                    bank=banks[sid]
                    def point(m,j):
                        x=m['state'];tau=m['tau'];g=tau*(h+tau*math.expm1(-h/tau));return x[3*j]+h*x[3*j+1]+g*x[3*j+2]
                    predicted=[sum(p*point(m,j) for p,m in zip(bank['probability'],bank['models'])) for j in range(3)]
                    max_error=max(max_error,math.dist(predicted,r['aim_point']));reconstructed+=1
        assert delays
        if entry['phase']=='regression' and entry['scene'] in ('hf_spin_pos','hf_spin_neg'):
            gate_rows.append(dict(run_id=entry['run_id'],variant=entry['variant'],scene=entry['scene'],counts=dict(gates)))
        if entry['variant'].startswith('I'):assert valid==marker and reconstructed>0 and max_error<1e-4
        # Keep an actual-pulse execution ledger on the 50 main runs. No latest-advice proxy.
        execution=[]
        if entry['phase']=='screen':
            pulses=[p for p in rows if p.get('fire_forwarded')];used=set();acceptance={}
            for line in (folder/'telemetry.jsonl').open():
                packet=json.loads(line)
                if packet['kind']=='gimbal_trace':
                    x=packet['data'];acceptance.setdefault(x['command_stamp_ns'],set()).add(x['accepted_wall_ns'])
            shots=sorted((x for x in state['shot_events'] if x['kind']==0),key=lambda x:x['stamp_ns'])
            for shot in shots:
                candidates=[(i,p) for i,p in enumerate(pulses) if i not in used and 0<=shot['stamp_ns']-p['receipt_ns']<=250_000_000]
                assert len(candidates)==1,(entry['run_id'],shot['shot_seq'],'pulse ambiguity')
                i,p=candidates[0];used.add(i);r=p['result'];stamp=p['frame']['stamp_ns']
                assert acceptance.get(stamp)=={shot['stamp_ns']}
                actual=(shot['stamp_ns']-r['evaluation_clocks']['aim_now_ns'])*1e-9
                expected=r['diagnosis']['aimer']['launch_delay_s']
                execution.append(dict(shot_seq=shot['shot_seq'],frame_seq=p['frame']['seq'],capture_stamp_ns=stamp,
                  receipt_stamp_ns=p['receipt_ns'],launch_stamp_ns=shot['stamp_ns'],decision_to_launch_s=actual,
                  assumed_launch_delay_s=expected,offset_mismatch_s=expected-actual))
            for i,p in enumerate(pulses):
                if i not in used:
                    orphans.append(dict(run_id=entry['run_id'],variant=entry['variant'],frame_seq=p['frame']['seq'],
                      receipt_minus_measurement_end_ms=(p['receipt_ns']-state['measurement_end_stamp_ns'])*1e-6))
            assert len(execution)==entry['shots']
            first_shot=shots[0] if shots else None
            score_rows=[json.loads(line) for line in (root/'normalized-scores'/entry['run_id']/'shots.jsonl').open()]
            first_score=score_rows[0] if score_rows else None
            if first_shot:
                before=[p for p in rows if state['measurement_start_stamp_ns']<=p['receipt_ns']<first_shot['stamp_ns'] and not p.get('fire_forwarded')]
                startup.append(dict(run_id=entry['run_id'],variant=entry['variant'],scene=entry['scene'],sensor_seed=entry['sensor_seed'],
                  first_fire_s=(first_shot['stamp_ns']-state['measurement_start_stamp_ns'])*1e-9,
                  first_shot_outcome=first_score['outcome'],first_hit_s=entry['first_hit_s'],
                  pre_first_fire_rows=[dict(frame_seq=p['frame']['seq'],receipt_s=(p['receipt_ns']-state['measurement_start_stamp_ns'])*1e-9,
                    valid=p['result']['fire_check'].get('valid'),aligned=p['result']['fire_check'].get('aligned'),
                    discontinuity=p['result']['fire_check'].get('discontinuity'),yaw_error_rad=p['result']['fire_check'].get('yaw_error_rad'),
                    yaw_tolerance_rad=p['result']['fire_check'].get('yaw_tolerance_rad'),advice=p['result']['fire_advice']) for p in before]))
        per_run.append(dict(run_id=entry['run_id'],variant=entry['variant'],phase=entry['phase'],scene=entry['scene'],
          valid_frames=valid,verified_delay_frames=len(delays),delay_counts={str(k):v for k,v in Counter(delays).items()},imm_marker_frames=marker,
          reconstructed_forecasts=reconstructed,forecast_max_error_m=max_error,pipeline_ms=stats(times),
          execution=execution))
    for phase in ('screen','regression'):
        for variant in VARIANTS:
            rows=[r for r in per_run if r['phase']==phase and r['variant']==variant]
            execution=[x for r in rows for x in r['execution']]
            groups.append(dict(phase=phase,variant=variant,valid_frames=sum(r['valid_frames'] for r in rows),
              verified_delay_frames=sum(r['verified_delay_frames'] for r in rows),
              delay_counts=dict(sum((Counter(r['delay_counts']) for r in rows),Counter())),
              imm_marker_frames=sum(r['imm_marker_frames'] for r in rows),
              reconstructed_forecasts=sum(r['reconstructed_forecasts'] for r in rows),
              forecast_max_error_m=max((r['forecast_max_error_m'] for r in rows),default=0),
              decision_to_launch_ms=stats([1000*x['decision_to_launch_s'] for x in execution]),
              offset_mismatch_ms=stats([1000*x['offset_mismatch_s'] for x in execution])))
    write(root/'live-audit.json',dict(groups=groups,per_run=per_run,unfired_pulses=orphans,
      method='Actual forwarded pulse matched uniquely to launch, confirmed by gimbal capture/acceptance stamp. Model/offset checked in actual algorithm diagnostics. No truth input or recomputed outcome.'))
    cases=[]
    for row in startup:
        if row['variant'] not in DELAY_VARIANTS:continue
        base=next(r for r in startup if r['variant']=='baseline' and r['scene']==row['scene'] and r['sensor_seed']==row['sensor_seed'])
        if row['first_hit_s']>base['first_hit_s']+.1:
            cases.append(dict(row,baseline_first_hit_s=base['first_hit_s'],baseline_first_fire_s=base['first_fire_s']))
    gate_groups=[]
    for scene in ('hf_spin_pos','hf_spin_neg'):
        for variant in VARIANTS:
            summed=sum((Counter(r['counts']) for r in gate_rows if r['scene']==scene and r['variant']==variant),Counter())
            gate_groups.append(dict(scene=scene,variant=variant,counts=dict(summed)))
    write(root/'case-diagnostics.json',dict(startup_cases=cases,all_startup=startup,spin_gate_groups=gate_groups,
      interpretation='Posthoc failure localization, no gate changes. Startup cases have paired first-hit increase >0.1s vs v3. Spin rows use capture within measurement; warmup-captured commands can launch after measurement begins and are not counted as captured rows. Gates overlap and do not form exclusive causes.'))


def verify(root):
    c=initialize(root);s=load(root/'state.json');counts=Counter(r['phase'] for r in s['runs'])
    assert counts['probe_p1']==counts['probe_p2']==10 and counts['screen']==50 and counts['regression']==60
    assert counts['confirm']<=60 and counts['repeat']<=6 and len(s['replays'])==10
    assert protected()==c['protected']
    for path,h in c['historical_files'].items():assert sha(Path(path))==h,path
    parent=load(PARENT/'contract.json')
    for variant,path in c['bundles'].items():
        bundle=Path(path);meta=verify_bundle(bundle);base=load(Path(parent['bundles'][BASE_VARIANT[variant]])/'bundle.json')
        changes=[p for p,h in base['files'].items() if meta['files'][p]!=h]
        assert changes==(['sp_vision_25/configs/simulator.yaml'] if variant in DELAY_VARIANTS else [])
        assert sha(bundle/'sp_vision_25/build/simulator/sim_auto_aim')==c['candidate_binary_sha256']
    for row in s['replays']:
        assert sha(Path(row['output'])/'events.jsonl')==row['output_sha256'] and not any(row['differences'].values())
    unique=set();evidence={}
    for r in s['runs']:
        key=(r['phase'],r['variant'],r['scene'],r['sensor_seed'],r['repeat_id']);assert key not in unique;unique.add(key)
        folder=manage.OPS/'experiments'/r['run_id'];m=load(folder/'manifest.json');meta=load(Path(c['bundles'][r['variant']])/'bundle.json')
        for name,h in r['source_hashes'].items():assert sha(folder/name)==h
        assert load(folder/'session-evidence-audit.json')['passed'] and r['health']['healthy'] and r['wall_s']<=40
        assert m['algorithm_binary_sha256']==c['candidate_binary_sha256']
        assert m['configuration_sha256']['algorithm.yaml']==meta['files']['sp_vision_25/configs/simulator.yaml']
        assert all(m.get(k)==v for k,v in dict(scene=r['scene'],sensor_seed=r['sensor_seed'],terrain='flat_range',input_contract='aligned-v1',warmup_frames=30).items())
        assert all(m['execution'].get(k)==v for k,v in dict(mode='lockstep',timing_profile=c['backend_timing_profile'],detector_history='session',clock_domain='simulation').items())
        assert all(m['shooting'].get(k)==v for k,v in dict(bullet_speed=25.,reported_bullet_speed=25.,physics='no-drag',aim_origin='muzzle',
          gimbal_profile='feedforward',fire_policy='advice',fire_rate_limit_hz=5.,algorithm_profile='tracking_fire_v3').items())
        for role in ('algorithm','simulator','bridge','evaluator'):
            assert m['containers'][role+'_image_id']==meta['images']['simulator' if role=='bridge' else role]
        with jobs.connect() as db:
            attempts=[dict(a) for a in db.execute('SELECT a.* FROM attempts a JOIN items i ON a.item_id=i.id WHERE i.job_id=?',(r['job_id'],))]
        assert len(attempts)==1 and attempts[0]['state']=='finished' and attempts[0]['verdict']!='invalid'
        assert attempts[0]['run_id']==r['run_id']
        scored=unified_score(folder,root/'normalized-scores'/r['run_id'],c)['metrics']
        assert scored['shots_fired']==r['shots'] and scored['target_armor_hits']==r['hits'] and abs(scored['weighted_hit_score_sum']-r['weight'])<1e-10
        evidence[r['run_id']]={name:sha(folder/name) for name in ('manifest.json','run-state.json','summary.json','telemetry.jsonl','session-evidence/events.jsonl','session-evidence-audit.json')}
        evidence[r['run_id']]['normalized_shots.jsonl']=sha(root/'normalized-scores'/r['run_id']/'shots.jsonl')
    expected_cells={(v,scene,seed) for v in VARIANTS for scene in ('hf_uniform_3','hf_turn90_3') for seed in c['screen_seeds']}
    assert {(r['variant'],r['scene'],r['sensor_seed']) for r in s['runs'] if r['phase']=='screen'}==expected_cells
    assert {(r['variant'],r['scene'],r['sensor_seed']) for r in s['runs'] if r['phase']=='regression'}=={(v,sc,seed) for v in VARIANTS for sc in c['regression_scenes'] for seed in c['regression_seeds']}
    rows=[r for r in s['runs'] if r['phase']=='screen'];reg=[r for r in s['runs'] if r['phase']=='regression']
    effects=[compare(rows,v,c,ref) for v in DELAY_VARIANTS for ref in ('baseline','BS15')]
    regressions=[regression_gate(reg,v) for v in DELAY_VARIANTS]
    assert effects==load(root/'screen-result.json') and regressions==load(root/'regression-result.json')
    eligible=[v for v in DELAY_VARIANTS if all(x['passed'] for x in effects if x['variant']==v) and next(x['passed'] for x in regressions if x['variant']==v)]
    assert eligible==[] and s['selected'] is None and counts['confirm']==counts['repeat']==0
    assert load(root/'parallel-result.json')['passed'] and s['parallelism']==2
    write(root/'run-evidence-hashes.json',evidence)
    live=load(root/'live-audit.json')
    write(root/'validation.json',dict(passed=True,acquisition_complete=True,backend_trials=len(s['runs']),trial_counts=dict(counts),
      replays=len(s['replays']),replay_frames=sum(r['frames'] for r in s['replays']),
      same_algorithm_binary=True,only_two_short_delay_configs_changed=True,protected_unchanged=True,
      historical_evidence_unchanged=True,raw_scores_recomputed=True,frozen_selection_recomputed=True,
      actual_model_and_delay_verified=True,main_shots_uniquely_associated=sum(len(r['execution']) for r in live['per_run']),
      maximum_complete_trial_s=max(r['wall_s'] for r in s['runs']),ui_acquisition=0,automatic_promotion=False,
      source_sha256={p.name:sha(p) for p in (manage.OPS/'timing_factor_study.py',Path(__file__))}))


def render(root):
    c=load(root/'contract.json');s=load(root/'state.json');validation=load(root/'validation.json');factors=load(root/'factorial-effects.json');live=load(root/'live-audit.json')
    pct=lambda x:'—' if x is None else f'{100*x:.3f}%'
    num=lambda x:'—' if x is None else f'{x:.3f}'
    lines=['# BS/I × 15ms/实测发射补偿射击对照','',
      '已完成冻结的130轮后台射击：20轮单/双路探针、50轮匀速与90°变向主对照、60轮静止/旋转/自车回归。全部使用同一算法二进制，模型参数、射击规则、物理与评分未改；只有BS4、I4把原有补偿策略改为固定4.329533ms。',
      '结论：缩短补偿在本批平移开发集出现较大收益，特别是变向场景；BS4与I4的总体加权几乎相同，没有证据说明IMM带来额外总体收益。候选仍未通过首次命中及旋转回归门槛，因此未进入独立确认或替换正式v3。','',
      '[冻结合同](contract.json) · [配对效应与交互](factorial-effects.json) · [实际启用与逐发时间核查](live-audit.json) · [证据校验](validation.json)','',
      '![按场景射击结果](shooting-comparison.png)','',
      '## 配置含义','',
      '| 名称 | 状态与预测 | 额外补偿 |','|---|---|---:|',
      '| baseline | 正式v3对照 | 原配置15/30ms分支 |','| BS15 | BS CV预测 | 原配置15/30ms分支 |','| BS4 | BS CV预测 | 固定4.329533ms |',
      '| I15 | BS接受量测＋IMM未来点 | 原配置15/30ms分支 |','| I4 | BS接受量测＋IMM未来点 | 固定4.329533ms |',
      '', '4.329533ms取自上一批461发唯一脉冲关联的决策→出膛时长中位数，冻结前取得，没有扫描命中分数。它仅是当前后台时序的标定，不是实车通用常数。', '']
    lines+=['原有策略在估计角速度绝对值超过8rad/s时使用30ms，否则15ms；BS15/I15是批次名称，并非全场景强制15ms。逐帧核查确认本批BS15/I15始终15ms，所有主场景对照也均15ms；只有v3旋转回归的4帧触发30ms。因此主场景是完整的2×2，旋转对v3的比较额外包含该分支差异。','']
    for phase,title in (('screen','主场景开发结果'),('regression','回归结果')):
        rows=[r for r in s['runs'] if r['phase']==phase]
        lines+=['## '+title,'','| 场景 | 配置 | 命中/发射 | 精确命中 | 加权命中 | 命中/秒 | 首次命中P90/s |','|---|---|---:|---:|---:|---:|---:|']
        for scene in [*sorted({r['scene'] for r in rows}),*(['all'] if phase=='screen' else [])]:
            for variant in VARIANTS:
                rr=[r for r in rows if r['variant']==variant and (scene=='all' or r['scene']==scene)];a=aggregate(rr)
                lines.append(f'| {scene} | {variant} | {a["hits"]}/{a["shots"]} | {pct(a["target_hit_rate"])} | {pct(a["weighted_hit_rate"])} | {num(a["hits_per_s"])} | {num(quantile([r["first_hit_s"] for r in rr],.9))} |')
        if phase=='regression':lines+=['','每格仅三个种子，回归是额外目录场景探索，不继承主场景的单/双路效果或UI等价资格。各场景分开验收，不用合并成绩掩盖旋转退化。']
    lines+=['','## 因子效应与模型交互','',
      '交互=(I4−I15)−(BS4−BS15)。采用相同种子同时重采样所有配置；合并主场景时保留两个场景为同一配对单位。以下区间是开发集描述，未校正多重比较，不能代替独立确认。',
      '','![时域效应区间](timing-effects.png)','',
      '| 场景 | 比较 | 加权差/pp [95%区间] | 精确差/pp [95%区间] | 命中/秒差 [95%区间] |','|---|---|---|---|---|']
    labels={'BS_delay':'BS4−BS15','I_delay':'I4−I15','model_at_15':'I15−BS15','model_at_4':'I4−BS4','interaction':'时域×模型交互','BS4_vs_v3':'BS4−v3','I4_vs_v3':'I4−v3','I4_vs_BS15':'I4−BS15'}
    def interval(m,scale):
        if m['delta'] is None:return '—'
        value=f'{scale*m["delta"]:+.3f}';ci=m['ci95']
        return value+(' ['+f'{scale*ci[0]:+.3f}, {scale*ci[1]:+.3f}'+']' if ci else ' [无有效区间]')
    for row in factors['contrasts']:
        if row['phase']!='screen':continue
        if row['scene']!='all' and row['contrast'] not in ('BS_delay','I_delay','model_at_4','interaction'):continue
        m=row['metrics'];lines.append('| '+row['scene']+' | '+labels[row['contrast']]+' | '+interval(m['weighted'],100)+' | '+interval(m['exact'],100)+' | '+interval(m['hits_per_s'],1)+' |')
    lines+=['','## 验收与代价','']
    for row in load(root/'screen-result.json'):
        lines+=['',row['variant']+'/'+row['reference']+' 主场景通过='+str(row['passed'])+'；未通过：'+str([k for k,v in row['checks'].items() if not v])+'。']
    for row in load(root/'regression-result.json'):
        lines+=['',row['variant']+' 回归通过='+str(row['passed'])+'；未通过：'+str([k for k,v in row['checks'].items() if not v])+'。']
    lines+=['','首次命中按每轨迹第一次实际命中计算，包括开局静止阶段，不能把它表述成“变向后的恢复时间”。旋转的最低30发门槛与分数退化分开报告，样本不足不等于分数一定下降。',
      '','候选没有通过完整门槛，条件独立确认与重复性未启动。保留未使用确认种子，不依据本批结果扩充样本、修改门槛或自动部署。','',
      '## 首次命中与旋转退化的现有记录定位','',
      '| 相对同种子v3首次命中增加>0.1s的轨迹 | v3首发/s | 候选首发/s | v3首次命中/s | 候选首次命中/s | 候选第一发结果 |','|---|---:|---:|---:|---:|---|']
    cases=load(root/'case-diagnostics.json')
    for row in cases['startup_cases']:
        lines.append('| '+row['variant']+'/'+row['scene']+'/'+str(row['sensor_seed'])+' | '+' | '.join(num(row[k]) for k in ('baseline_first_fire_s','first_fire_s','baseline_first_hit_s','first_hit_s'))+' | '+row['first_shot_outcome']+' |')
    lines+=['','转向场景BS4/3402与I4/3405的第一发都命中；延迟发生在首发之前。保留的早期帧显示跟踪/量测有效，但yaw未进入原有对齐窗口，I4/3405中途另有命令突变重置。它们发生在0–1s开局静止阶段，与3.2s的目标转向尚无时间重叠。不能据此认定变向恢复变慢，也不能用放宽开火窗直接绕过。',
      '', '| 旋转场景/配置 | 捕获帧数 | 跟踪/量测有效帧 | 对齐帧 | advice帧 | 换板事件帧 |','|---|---:|---:|---:|---:|---:|']
    for row in cases['spin_gate_groups']:
        counts=row['counts'];lines.append('| '+row['scene']+'/'+row['variant']+' | '+' | '.join(str(counts.get(k,0)) for k in ('capture_frames','tracking_measurement_valid','aligned','advice','plate_changed'))+' |')
    lines+=['','旋转中BS15/I15在原补偿下也已有较少的有效帧和advice，说明问题并非全部由缩短延迟产生；但这些帧属于各自闭环输入，不能作为单一因果归因。以上门控条件可重叠，warmup捕获但测量期出膛的命令不在捕获帧计数中。[完整逐轨迹诊断](case-diagnostics.json)','',
      '## 实际执行与计算成本','',
      '| 主场景配置 | 实际关联发射数 | 决策→出膛P50/ms | 补偿−出膛P50/ms | 核对补偿帧数 | IMM已重建预测帧数 |','|---|---:|---:|---:|---:|---:|']
    for row in live['groups']:
        if row['phase']!='screen':continue
        lines.append('| '+row['variant']+' | '+str(row['decision_to_launch_ms']['n'])+' | '+num(row['decision_to_launch_ms']['p50'])+' | '+num(row['offset_mismatch_ms']['p50'])+' | '+str(row['verified_delay_frames'])+' | '+str(row['reconstructed_forecasts'])+' |')
    lines+=['','| 阶段/配置 | 实际补偿秒值→帧数 |','|---|---|']
    for row in live['groups']:lines.append('| '+row['phase']+'/'+row['variant']+' | '+str(row['delay_counts'])+' |')
    parallel=load(root/'parallel-result.json');lines+=['',f'单/双路探针通过={parallel["passed"]}；实际重叠={parallel["actual_overlap"]}；后续并行度={s["parallelism"]}。单次完整最长{validation["maximum_complete_trial_s"]:.2f}s，满足40s预算。',
      '', '| 阶段 | 轮数 | 批次跨度/s | 吞吐/小时 |','|---|---:|---:|---:|']
    for phase in ('probe_p1','probe_p2','screen','regression'):
        rr=[r for r in s['runs'] if r['phase']==phase];span=max(r['attempt']['ended'] for r in rr)-min(r['attempt']['started'] for r in rr)
        lines.append(f'| {phase} | {len(rr)} | {span:.1f} | {len(rr)*3600/span:.1f} |')
    lines+=['','并发探针资格仅限本批固定负载，不登记全局容量。固定逻辑时序不能证明实车计算截止时间；回归仍缺少独立UI资格。新增UI采集0，原始输入与低分记录保留，未补做人工整段视频验收。','',
      '## 报告恢复与证据范围','',
      '130轮采集已正常结束，控制器在导入尚未就绪的报告模块时退出。本次只补报告，保留原失败状态和日志，没有删除或重新采集任何试验。模型、延迟、效果门槛和原始评分未改。',
      '[报告恢复记录](report-recovery.json) · [逐轨迹证据哈希](run-evidence-hashes.json) · [报告产物哈希](report-seal.json)','',
      '## 下一步建议','',
      '优先保留简单的BS4作为非旋转平移候选，单独核查首次命中变慢与旋转退化的具体发射；当前数据没有支持全局替换的充分证据。若做场景自适应，必须依赖可观测运动证据，不能读取仿真场景名或真值。',
      'IMM暂不与时域补偿捆绑推广：I4与BS4总体加权几乎相同，额外模型的收益需要在相同延迟、独立种子下证明。先定位旋转的预测/换板与开火窗口，再决定是否设计新的策略。']
    (root/'report.md').write_text('\n'.join(lines)+'\n')
    def inline(value):return re.sub(r'\[([^\]]+)\]\(([^)]+)\)',r'<a href="\2">\1</a>',html.escape(value))
    parts=[];table=False
    for line in lines:
        if table and not line.startswith('|'):parts.append('</table>');table=False
        if line.startswith('|'):
            if re.fullmatch(r'[|:\- ]+',line):continue
            tag='td' if table else 'th'
            if not table:parts.append('<table>');table=True
            parts.append('<tr>'+''.join('<'+tag+'>'+inline(cell.strip())+'</'+tag+'>' for cell in line.strip('|').split('|'))+'</tr>')
        elif line.startswith('!['):
            m=re.fullmatch(r'!\[([^\]]*)\]\(([^)]+)\)',line)
            if m:parts.append('<img style="max-width:100%" alt="'+html.escape(m[1])+'" src="'+html.escape(m[2])+'">')
        elif line.startswith('#'):
            n=len(line)-len(line.lstrip('#'));parts.append(f'<h{n}>'+inline(line[n:].strip())+f'</h{n}>')
        elif line:parts.append('<p>'+inline(line)+'</p>')
    if table:parts.append('</table>')
    (root/'report.html').write_text('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BS/I时域因子实验</title><style>body{max-width:1350px;margin:32px auto;padding:0 20px;font:16px/1.65 sans-serif;color:#18212d}table{border-collapse:collapse;width:100%;font-size:14px;margin:20px 0}td,th{border:1px solid #ccd5df;padding:7px;text-align:right}th{background:#edf2f8}td:first-child,th:first-child{text-align:left}h2{margin-top:36px}a{color:#175cb5}</style>'+''.join(parts))


def finish(root):
    c=initialize(root);s=load(root/'state.json')
    expected=Counter(probe_p1=10,probe_p2=10,screen=50,regression=60)
    assert Counter(r['phase'] for r in s['runs'])==expected and len(s['replays'])==10
    if not (root/'report-recovery.json').exists():
        shutil.copy2(root/'state.json',root/'state-before-report-recovery.json')
        write(root/'report-recovery.json',dict(prior_state_sha256=sha(root/'state-before-report-recovery.json'),
          prior_state=s['state'],prior_error=s.get('error'),acquisition_completed_at=s.get('ended'),
          runs_reused=len(s['runs']),no_reacquisition=True,original_model_parameters_and_gate_unchanged=True))
    meta=load(Path(c['bundles']['baseline'])/'bundle.json')
    subprocess.run(['docker','run','--rm','--network','none','--cpus','2','--user',f'{os.getuid()}:{os.getgid()}',
      '-v',str(manage.ROOT)+':'+str(manage.ROOT)+':ro','-v',str(root)+':'+str(root),'-w',str(manage.ROOT),
      '--entrypoint','python3',meta['images']['evaluator'],'-B',str(Path(__file__)),'analyze','--output',str(root)],check=True,timeout=90)
    live_audit(root);verify(root)
    s=load(root/'state.json');s.pop('error',None);s.update(state='completed',report_completed=time.time(),report_recovery='report-recovery.json')
    write(root/'state.json',s);render(root)
    write(root/'report-seal.json',{p.name:sha(p) for p in root.iterdir() if p.is_file() and p.name not in ('report-seal.json','controller.log','controller.lock')})
    print(json.dumps(dict(completed=True,backend_trials=len(s['runs']),selected=s.get('selected'),report=str(root/'report.html'))),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('analyze','finish'));parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();root=args.output.resolve()
    if args.action=='analyze':analyze(root)
    else:finish(root)
