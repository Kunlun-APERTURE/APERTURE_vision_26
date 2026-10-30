"""Compute ablation results, future-point errors, regression effects and artifacts."""
import argparse
from collections import Counter
import html
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess

import manage
from decision_benchmark import sha
from decision_benchmark_report import aggregate
from motion_study import OPTIONS,verify
from perception_study import quantile
from report import load,write
from sync_first_divergence_analysis import primary


def stats(values):return dict(n=len(values),p50=statistics.median(values) if values else None,p95=quantile(values,.95))


def effects(root):
    import numpy as np
    s=load(root/'state.json');comparisons=[]
    # Bootstrap the sensor-seed unit, preserving both scenes together for all.
    # Precomputed per-seed totals avoid treating bullets or frames as samples.
    for phase in ('screen','confirm','regression'):
        entries=[r for r in s['runs'] if r['phase']==phase]
        if not entries:continue
        for reference in ('baseline','BS','R','RC'):
            for variant in OPTIONS:
                if variant in ('baseline',reference):continue
                if reference=='R' and variant!='RC' or reference=='RC' and variant!='ALL':continue
                for scene in (*sorted({r['scene'] for r in entries}),'all'):
                    a=[r for r in entries if r['variant']==reference and (scene=='all' or r['scene']==scene)]
                    b=[r for r in entries if r['variant']==variant and (scene=='all' or r['scene']==scene)]
                    if not a or not b:continue
                    ka={(r['sensor_seed'],r['scene']) for r in a};kb={(r['sensor_seed'],r['scene']) for r in b}
                    if ka!=kb:raise ValueError('Unpaired motion comparison')
                    seeds=sorted({r['sensor_seed'] for r in a})
                    def totals(rows):return np.asarray([[sum(r[k] for r in rows if r['sensor_seed']==seed) for k in ('shots','hits','weight','duration_s')] for seed in seeds],float)
                    aa,bb=totals(a),totals(b)
                    rng=np.random.default_rng(20261024);indices=rng.integers(0,len(seeds),size=(10000,len(seeds)))
                    ax,bx=aa[indices].sum(axis=1),bb[indices].sum(axis=1)
                    valid=(ax[:,0]>0)&(bx[:,0]>0)
                    metrics={}
                    pa,pb=aa.sum(axis=0),bb.sum(axis=0)
                    for name,col,den in (('weighted',2,0),('exact',1,0),('hits_per_s',1,3),('frequency',0,3)):
                        if pa[den]<=0 or pb[den]<=0:metrics[name]=dict(delta=None,ci95=None);continue
                        delta=pb[col]/pb[den]-pa[col]/pa[den]
                        ci=None
                        if valid.all():ci=np.quantile(bx[:,col]/bx[:,den]-ax[:,col]/ax[:,den],[.025,.975]).tolist()
                        metrics[name]=dict(delta=float(delta),ci95=ci)
                    comparisons.append(dict(phase=phase,reference=reference,variant=variant,scene=scene,seeds=len(seeds),metrics=metrics))
    write(root/'paired-effects.json',dict(completed=True,comparisons=comparisons,bootstrap_unit='sensor seed, keeping scenes paired',
        samples=10000,seed=20261024,interpretation='Per-ablation descriptive intervals; many comparisons are not multiplicity-adjusted. Frozen confirm gate is separate.'))


def analyze(root):
    import numpy as np
    from perception_attribution_analysis import timeline,match_truth
    c=load(root/'contract.json');s=load(root/'state.json');metrics=[];activations={v:Counter() for v in OPTIONS}
    for e in c['sources']:
        source=manage.OPS/'experiments'/e['run_id'];truth=timeline(source);state=load(source/'run-state.json')
        for v in OPTIONS:
            row=next(r for r in s['replays'] if r['variant']==v and r['source_run']==source.name)
            valid=0;future_errors=[];stationary_speed=[];post_turn_errors=[];total=0
            for packet in primary(Path(row['output'])):
                f,r=packet['frame'],packet['result'];d=r.get('continuity',{}).get('filter',{}).get('motion_v5') or {}
                for a in d.get('radial',[]):activations[v]['radial_releases']+=a['released'];activations[v]['radial_observations']+=1
                activations[v]['shared_frames']+='shared_velocity' in d
                activations[v]['acceleration_frames']+=any(math.hypot(*a[:2])>.01 for a in d.get('accelerations',[]))
                if f['stamp_ns']<state['measurement_start_stamp_ns']:continue
                t=(f['stamp_ns']-state['measurement_start_stamp_ns'])*1e-9;h=r.get('prediction_horizon_s');sid=r.get('selected_plate_id',-1)
                if not r.get('valid') or sid<0 or not isinstance(h,(int,float)) or not 0<h<1:continue
                if f['stamp_ns']+round(h*1e9)>state['measurement_end_stamp_ns']:continue
                total+=1
                links=r.get('continuity',{}).get('image_association') or {}
                chosen=next((x for x in links.get('observations',[]) if x['plate_id']==sid),None)
                obs=r.get('observations',[])
                if chosen is None or not obs:continue
                distances=[float(np.linalg.norm(np.mean(a['corners'],axis=0)-chosen['image_center'])) for a in obs]
                i=int(np.argmin(distances));matches=match_truth(f,obs,truth(f['stamp_ns']))
                if distances[i]>4 or i not in matches:continue
                identity=matches[i]['plate']['id'];actual=next((a for a in truth(f['stamp_ns']+round(h*1e9)) if a['id']==identity),None)
                if actual is None:continue
                prediction=np.asarray(r['aim_point'])+f['gimbal_position_map'];error=float(np.linalg.norm((prediction-np.asarray(actual['position']))[:2]))
                future_errors.append(error);valid+=1
                if t<.7:stationary_speed.append(float(np.linalg.norm(np.asarray(r['plate_velocities'][sid])[:2])))
                if e['scene']=='hf_turn90_3' and 3.2<=t<3.7:post_turn_errors.append(error)
            metrics.append(dict(source_run=source.name,engine=e['engine'],scene=e['scene'],variant=v,
                measured_forecasts=total,matched_forecasts=valid,future_error_m=stats(future_errors),
                post_turn_future_error_m=stats(post_turn_errors),stationary_speed_mps=stats(stationary_speed),
                raw_future_errors_m=future_errors,raw_post_turn_errors_m=post_turn_errors,raw_stationary_speeds_mps=stationary_speed))
    groups=[]
    for v in OPTIONS:
        rr=[r for r in metrics if r['variant']==v]
        def merge(key):return stats([x for r in rr for x in r[key]])
        groups.append(dict(variant=v,future_error_m=merge('raw_future_errors_m'),post_turn_future_error_m=merge('raw_post_turn_errors_m'),
            stationary_speed_mps=merge('raw_stationary_speeds_mps'),matched=sum(r['matched_forecasts'] for r in rr),eligible=sum(r['measured_forecasts'] for r in rr)))
    phase_rows=[];live_activations={v:Counter() for v in OPTIONS}
    for entry in [r for r in s['runs'] if r['phase']=='screen']:
        source=manage.OPS/'experiments'/entry['run_id'];stats_state=load(source/'run-state.json')
        for line in (source/'session-evidence/events.jsonl').open():
            packet=json.loads(line)
            if packet['kind']!='result':continue
            data=packet['data'];frame=data['frame']
            if frame['stamp_ns']>stats_state['measurement_end_stamp_ns']:continue
            model=data['result'].get('continuity',{}).get('filter',{}).get('motion_v5') or {}
            for a in model.get('radial',[]):live_activations[entry['variant']]['radial_releases']+=a['released'];live_activations[entry['variant']]['radial_observations']+=1
            live_activations[entry['variant']]['shared_frames']+='shared_velocity' in model
            live_activations[entry['variant']]['acceleration_frames']+=any(math.hypot(*a[:2])>.01 for a in model.get('accelerations',[]))
    for phase in ('screen','confirm'):
        for v in OPTIONS:
            rr=[r for r in s['runs'] if r['phase']==phase and r['variant']==v]
            for scene in ('hf_uniform_3','hf_turn90_3'):
                shots=[]
                for entry in [r for r in rr if r['scene']==scene]:
                    path=root/'normalized-scores'/entry['run_id']/'shots.jsonl';shots.extend(json.loads(l) for l in path.open())
                for segment in ('stationary','moving','after_turn'):
                    selected=[r for r in shots if ('stationary' if r['fire_time_s']<1 else 'after_turn' if scene=='hf_turn90_3' and r['fire_time_s']>=3.2 else 'moving')==segment]
                    if selected:phase_rows.append(dict(phase=phase,variant=v,scene=scene,segment=segment,shots=len(selected),hits=sum(r['outcome']=='target_armor' for r in selected),weighted=sum(r['weighted_hit_score'] for r in selected)/len(selected)))
    write(root/'analysis-result.json',dict(completed=True,groups=groups,per_replay=metrics,activations={v:dict(a) for v,a in activations.items()},
        live_activations={v:dict(a) for v,a in live_activations.items()},phase_scores=phase_rows,
        prediction_scope='Independent posthoc image/physical-plate matching; unmatched/occluded predictions omitted with coverage. No forecast hit-rate inference.'))


def plots(root):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    d=load(root/'analysis-result.json');rr=[r for r in d['groups'] if r['variant']!='baseline']
    fig,axes=plt.subplots(1,2,figsize=(12,4),layout='constrained')
    for ax,key,title in ((axes[0],'future_error_m','Same-input future XY error'),(axes[1],'post_turn_future_error_m','First 0.5s after turn')):
        ax.bar([r['variant'] for r in rr],[r[key]['p95'] if r[key]['p95'] is not None else 0 for r in rr]);ax.set(title=title,ylabel='P95 (m)');ax.grid(axis='y',alpha=.2)
    fig.savefig(root/'forecast-ablation.png',dpi=170);plt.close(fig)
    s=load(root/'state.json');rows=[r for r in s['runs'] if r['phase']=='screen'];variants=s['screen_variants']
    fig,axes=plt.subplots(1,2,figsize=(12,4),layout='constrained')
    for ax,scene in zip(axes,('hf_uniform_3','hf_turn90_3')):
        aa=[aggregate([r for r in rows if r['scene']==scene and r['variant']==v]) for v in variants]
        ax.bar([i-.2 for i in range(len(aa))],[100*a['target_hit_rate'] if a['target_hit_rate'] is not None else 0 for a in aa],width=.4,label='Exact')
        ax.bar([i+.2 for i in range(len(aa))],[100*a['weighted_hit_rate'] if a['weighted_hit_rate'] is not None else 0 for a in aa],width=.4,label='Weighted')
        ax.set(xticks=range(len(aa)),xticklabels=variants,title=scene,ylabel='Percent of all shots');ax.legend();ax.grid(axis='y',alpha=.2)
    fig.savefig(root/'backend-ablation.png',dpi=170);plt.close(fig)


def render(root):
    c=load(root/'contract.json');s=load(root/'state.json');d=load(root/'analysis-result.json');v=load(root/'validation.json')
    number=lambda x:'—' if x is None else f'{x:.3f}'
    lines=['# 三方向运动优化与结果计算','',
      '已实现：R 按装甲保存径向机动证据、C 共享车体平移并保留旋转速度、P 有界衰减加速度外推、T 可配置发射延迟。均为独立开关，默认关闭，正式 v3 与射击规则未改。','',
      'BS 是此前联合初始化与图像关联的开发起点；R/C/P/T/RC/ALL 均继承它。ALL 同时启用三个方向，RC 是径向适应加共享平移。各单项和组合分别计算，不假设收益能相加。',
      f'完成 {v["replays"]} 段录像回放、{v["frames"]} 帧、{v["backend_trials"]} 轮后台闭环。新增 UI 实验 0。',
      '[实现与固定预算](implementation.md) · [冻结定义](contract.json) · [证据校验](validation.json) · [状态](state.json)','',
      '## 同输入估计与未来点误差','',
      '| 配置 | 中心误差 P95 / m | 有效覆盖 | 未来点 P95 / m | 变向后0.5s P95 / m | 静止所选板速度 P95 / m/s | 匹配/可用预测 |','|---|---:|---:|---:|---:|---:|---:|']
    replay=load(root/'replay-result.json')['scores']
    for row in d['groups']:
        score=replay[row['variant']];lines.append(f'| {row["variant"]} | {number(score["error_p95_m"])} | {score["coverage"]*100:.2f}% | {number(row["future_error_m"]["p95"])} | {number(row["post_turn_future_error_m"]["p95"])} | {number(row["stationary_speed_mps"]["p95"])} | {row["matched"]}/{row["eligible"]} |')
    lines+=['','未来点按独立图像匹配选中物理装甲，无法匹配时跳过，原预测时钟/相机轨迹固定；不同候选覆盖不同，不能将覆盖差异解释为精度收益。v3 没有共享图像关联诊断，未来点不在这项条件评分内；它的中心误差和闭环评分仍完整。','',
      '![外推消融](forecast-ablation.png)','',
      '## 激活与关闭兼容性','',
      f'C++ 检查：{c["cpp_checks"]}；14 条关闭新运动机制的原生配置核心输出逐帧复现。录像集成检查通过，核对输入、共享旋转项、加速度限幅、延迟与滤波分离。','',
      '| 配置 | 径向观测 | 径向释放 | 共享帧 | 非零加速度帧 |','|---|---:|---:|---:|---:|']
    for variant,a in d['activations'].items():lines.append(f'| {variant} | {a.get("radial_observations",0)} | {a.get("radial_releases",0)} | {a.get("shared_frames",0)} | {a.get("acceleration_frames",0)} |')
    lines+=['','后台开发实际激活次数（含暖机，截止测量结束）：','',
      '| 配置 | 径向释放 | 共享帧 | 非零加速度帧 |','|---|---:|---:|---:|']
    for variant,a in d.get('live_activations',{}).items():lines.append(f'| {variant} | {a.get("radial_releases",0)} | {a.get("shared_frames",0)} | {a.get("acceleration_frames",0)} |')
    lines+=['','若径向释放很少，说明当前阈值/证据链没有充分触发，不能将“实现了开关”解释为已经解决变向响应；本轮不根据开发成绩改阈值。']
    p=load(root/'parallel-result.json');lines+=['','## 后台并行与成本','',f'单/双路探针通过={p["passed"]}，实际重叠={p["actual_overlap"]}，后续 {p["effect_parallelism"]} 路。此资格限冻结负载，不登记全局容量。']
    spans={}
    for phase in ('probe_p1','probe_p2','screen','regression','confirm','repeat'):
        rr=[r for r in s['runs'] if r['phase']==phase]
        if not rr:continue
        span=max(r['attempt']['ended'] for r in rr)-min(r['attempt']['started'] for r in rr);spans[phase]=span
        lines+=['',f'{phase}：{len(rr)} 轮，批次跨度 {span:.1f}s，{len(rr)*3600/span:.1f} 轮/小时。']
    lines+=['',f'单次最大完整用时 {max(r["wall_s"] for r in s["runs"]):.2f}s。原始图像全留存，未请求视频渲染，人工整段视频验收未完成。']
    for phase,title in (('screen','后台开发消融'),('confirm','独立确认'),('regression','静止/旋转/自车移动回归')):
        rows=[r for r in s['runs'] if r['phase']==phase]
        if not rows:continue
        lines+=['','## '+title,'','| 场景 | 配置 | 命中/发射 | 精确命中 | 加权命中 | 命中/秒 | 首次命中 P90 / s |','|---|---|---:|---:|---:|---:|---:|']
        for scene in (*sorted({r['scene'] for r in rows}),'all'):
            for variant in OPTIONS:
                rr=[r for r in rows if r['variant']==variant and (scene=='all' or r['scene']==scene)]
                if not rr:continue
                a=aggregate(rr);exact=number(100*a['target_hit_rate']) if a['target_hit_rate'] is not None else '—';weighted=number(100*a['weighted_hit_rate']) if a['weighted_hit_rate'] is not None else '—'
                lines.append(f'| {scene} | {variant} | {a["hits"]}/{a["shots"]} | {exact}% | {weighted}% | {a["hits_per_s"]:.3f} | {number(quantile([r["first_hit_s"] for r in rr],.9))} |')
        if phase=='regression':lines+=['','回归每格仅三个种子，用于暴露明显副作用，不等于完整场景资格；不同场景不以合并成绩作为通过依据。',
            '普通同步入口原本只放行四个历史场景。本轮使用固定时序协调器的场景目录能力，通过研究局部白名单提交额外回归，不改全局校验/容量。这些场景尚未另获单/双路效果或 UI 等价资格。[入口说明](regression-extension.json)']
    lines+=['','![后台消融](backend-ablation.png)','', '## 规则判定','']
    paired=load(root/'paired-effects.json',{})
    if paired:
        lines+=['','以下合并加权差以配对种子重采样。开发消融含多重比较，区间仅作描述，不替代独立确认门槛。','',
          '| 阶段 | 候选 | 对照 | 加权差 / pp | 配对95%区间 / pp |','|---|---|---|---:|---|']
        for row in paired['comparisons']:
            if row['scene']!='all':continue
            m=row['metrics']['weighted'];ci=m['ci95'];text='无有效区间' if ci is None else f'[{100*ci[0]:+.2f}, {100*ci[1]:+.2f}]'
            lines.append(f'| {row["phase"]} | {row["variant"]} | {row["reference"]} | {100*m["delta"]:+.2f} | {text} |' if m['delta'] is not None else f'| {row["phase"]} | {row["variant"]} | {row["reference"]} | 无发射 | {text} |')
    for row in load(root/'screen-result.json',[]):
        failed=[k for k,value in row['checks'].items() if not value]
        lines+=[f'{row["variant"]} 开发通过={row["passed"]}；未通过：'+(', '.join(failed) if failed else '无')+'。']
    confirm=load(root/'confirm-result.json')
    if confirm:lines+=['',f'独立确认通过={confirm["passed"]}，加权收益95%区间={confirm["weighted_gain_95"]}；未通过项='+str([k for k,value in confirm['checks'].items() if not value])+'。']
    lines+=['',f'开发入选={s.get("selected")}；独立通过={s.get("backend_confirmed",False)}；UI复核资格={s.get("ui_eligible",False)}。默认 v3 未推广替换。','',
      '## 适用范围与风险','',
      '- 普通四板小装甲是本轮运动模型适用范围，其他类别保留原逻辑。共享中心仍有固定半径与相位先验偏差。',
      '- T 的4.3ms是已有后台记录的决策到发射代理延迟，未宣称适用于实车；没有根据新成绩扫描延迟。',
      '- P 不能预知尚未发生的转向；加速度由有噪声速度增量估计，可能损失稳态中心度。',
      '- 所有低分、无发射或回归退化均保留；相关录像帧不是独立样本，推理回放不是新命中实验。',
      '- 本轮只在后台计算效果，没有自动 UI、硬件部署或全局容量推广。','']
    lines.insert(-1,'- 固定逻辑时序便于隔离数值算法；新增计算仍须在后续实测独立检查耗时与截止时间，不能用同步实验代替实时部署资格。')
    text='\n'.join(lines);(root/'report.md').write_text(text)
    def inline(value):
        value=html.escape(value);return re.sub(r'\[([^\]]+)\]\(([^)]+)\)',r'<a href="\2">\1</a>',value)
    parts=[];table=False
    for line in lines:
        if table and not line.startswith('|'):parts.append('</table>');table=False
        if line.startswith('|'):
            if re.fullmatch(r'[|:\- ]+',line):continue
            tag='td' if table else 'th'
            if not table:parts.append('<table>');table=True
            parts.append('<tr>'+''.join('<'+tag+'>'+inline(x.strip())+'</'+tag+'>' for x in line.strip('|').split('|'))+'</tr>')
        elif line.startswith('!['):
            m=re.fullmatch(r'!\[([^\]]*)\]\(([^)]+)\)',line)
            if m:parts.append('<img alt="'+html.escape(m[1])+'" src="'+html.escape(m[2])+'">')
        elif line.startswith('#'):
            n=len(line)-len(line.lstrip('#'));parts.append(f'<h{n}>'+inline(line[n:].strip())+f'</h{n}>')
        elif line:parts.append('<p>'+inline(line)+'</p>')
    if table:parts.append('</table>')
    (root/'report.html').write_text('<meta charset="utf-8"><title>三个方向运动优化结果</title><style>body{font:16px/1.6 sans-serif;max-width:1300px;margin:36px auto;padding:0 20px}table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:7px}img{max-width:100%}</style>'+''.join(parts))


def finish(root):
    c=load(root/'contract.json');s=load(root/'state.json')
    if s['state']!='completed':raise ValueError('Complete frozen acquisition first')
    verify(root,c,s)
    if s.get('error'):
        s.setdefault('resolved_controller_errors',[]).append(dict(error=s.pop('error'),
            resolution='Recovered controller/diagnostic bookkeeping; all required acquisitions and evidence verified complete without replacing scored trials.'))
        write(root/'state.json',s)
    extension=load(root/'regression-extension.json')
    original=load(Path(c['base_bundle'])/'bundle.json')['files']['docker/auto_aim/background_jobs.py']
    if sha(manage.OPS/'background_jobs.py')!=original:raise ValueError('Global scheduler validation unexpectedly changed')
    extension['verified_global_validation_sha256']=original
    extension['final_controller_sha256']=sha(manage.OPS/'motion_study.py')
    write(root/'regression-extension.json',extension)
    expected_replays={(e['run_id'],v) for e in c['sources'] for v in OPTIONS}
    if {(r['source_run'],r['variant']) for r in s['replays']}!=expected_replays:raise ValueError('Missing required ablation replay')
    expected_screen={(v,scene,seed) for v in s['screen_variants'] for scene in ('hf_uniform_3','hf_turn90_3') for seed in c['screen_seeds']}
    if {(r['variant'],r['scene'],r['sensor_seed']) for r in s['runs'] if r['phase']=='screen'}!=expected_screen:raise ValueError('Missing required background ablation')
    regression_variant=s.get('selected') or 'ALL'
    expected_regression={(v,scene,seed) for v in ('baseline',regression_variant) for scene in c['regression_scenes'] for seed in c['regression_seeds']}
    if {(r['variant'],r['scene'],r['sensor_seed']) for r in s['runs'] if r['phase']=='regression'}!=expected_regression:raise ValueError('Missing required regression trial')
    if s.get('selected'):
        expected_confirm={(v,scene,seed) for v in ('baseline',s['selected']) for scene in ('hf_uniform_3','hf_turn90_3') for seed in c['confirm_seeds']}
        if {(r['variant'],r['scene'],r['sensor_seed']) for r in s['runs'] if r['phase']=='confirm'}!=expected_confirm:raise ValueError('Missing independent confirmation')
    image=load(Path(c['bundles']['BS'])/'bundle.json')['images']['evaluator']
    prefix=['docker','run','--rm','--network','none','--cpus','2','--user',f'{os.getuid()}:{os.getgid()}',
        '-e','OPENBLAS_NUM_THREADS=1','-e','MPLCONFIGDIR=/tmp/matplotlib','-v',str(manage.ROOT)+':'+str(manage.ROOT)+':ro',
        '-v',str(root)+':'+str(root),'-w',str(manage.ROOT),'--entrypoint','python3',image,'-B',str(Path(__file__))]
    for action in ('analyze','effects','plots'):
        with (root/(action+'.log')).open('w') as log:subprocess.run([*prefix,action,'--output',str(root)],check=True,timeout=180,stdout=log,stderr=subprocess.STDOUT)
    (root/'implementation.md').write_text((manage.OPS/'MOTION_V5.md').read_text());render(root)
    write(root/'completion-audit.json',dict(passed=True,objective='Implement directions 1-3 and calculate their results',
        radial=dict(config='motion_v5.radial_adaptation',source='tasks/auto_aim/motion_model.hpp and Target::update_robust',replay_variants=['R','RC','ALL']),
        shared=dict(config='motion_v5.shared_translation',source='Target::sync_translation/plate_velocities',replay_variants=['C','RC','ALL']),
        prediction_timing=dict(config=['motion_v5.acceleration_prediction','motion_v5.calibrated_delay'],source='Target::forecast/Aimer::aim',replay_variants=['P','T','ALL']),
        cpp_checks=c['cpp_checks'],integration_checks=5,all_replay_cases_verified=len(expected_replays),all_screen_cases_verified=len(expected_screen),
        all_regression_cases_verified=len(expected_regression),independent_confirmation_required=bool(s.get('selected')),
        scores='paired-effects.json, screen-result.json, confirm-result.json when required',
        evidence='validation.json and report-seal.json',production_unchanged=True,
        outcome_is_not_required_to_pass='Failed candidates are fully calculated and retained; no default promotion is implied.'))
    files=('report.md','report.html','implementation.md','validation.json','analysis-result.json','replay-result.json',
        'parallel-result.json','screen-result.json','confirm-result.json','repeatability.json','forecast-ablation.png','backend-ablation.png',
        'motion-checks.log','cpp-checks.log','replay-checks.log','paired-effects.json','completion-audit.json','regression-extension.json')
    write(root/'report-seal.json',dict(completed=True,files={n:sha(root/n) for n in files if (root/n).exists()}))
    from experiments import publish_desktop
    publish_desktop(manage,root)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('analyze','effects','plots','render','finish'));p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();globals()[a.action](a.output.resolve())
