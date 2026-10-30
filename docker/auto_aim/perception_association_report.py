"""Summarize prior mechanisms and independently score persistent association."""
import argparse
import bisect
from collections import defaultdict
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
from perception_association import verify
from perception_study import quantile
from report import load,write
from sync_first_divergence_analysis import primary,truth_centers


def analyze(root):
    c=load(root/'contract.json');s=load(root/'state.json');traces=[];audits=[]
    for entry in s['replays']:
        source=manage.OPS/'experiments'/entry['source_run'];truth=truth_centers(source)
        start=load(source/'run-state.json')['measurement_start_stamp_ns']
        hints=mismatches=accepted_conflicts=selections=0
        for p in primary(Path(entry['output'])):
            frame=p['frame'];r=p['result'];d=r.get('continuity',{});filt=d.get('filter') or {}
            x=r.get('filter_state');gt=truth(frame['stamp_ns'])
            error=math.dist([x[0]+frame['gimbal_position_map'][0],x[2]+frame['gimbal_position_map'][1]],gt[:2]) if x is not None and gt is not None else None
            traces.append(dict(source=source.name,source_variant=entry['source_variant'],engine=entry['engine'],scene=entry['scene'],
                variant=entry['variant'],time_s=(frame['stamp_ns']-start)*1e-9,error_m=error))
            accepted={u['plate_id'] for u in filt.get('updates',[]) if u.get('accepted')}
            for row in d.get('selected_associations',[]):
                selections+=1;hint=row.get('supplied_hint')
                if hint is None or 'state_before' not in filt:continue
                hints+=1;expected=filt['state_before'][6]+row['plate_id']*math.pi/2
                if abs(math.remainder(hint-expected,2*math.pi))>1e-6:
                    mismatches+=1;accepted_conflicts+=row['plate_id'] in accepted
        audits.append(dict(source=source.name,engine=entry['engine'],scene=entry['scene'],variant=entry['variant'],
            selections=selections,with_hint=hints,hint_id_mismatches=mismatches,accepted_conflicts=accepted_conflicts))
    groups=[]
    for engine in ('ui','backend'):
        for scene in ('hf_uniform_3','hf_turn90_3'):
            for variant in ('baseline','S','BS'):
                rr=[r for r in traces if (r['engine'],r['scene'],r['variant'])==(engine,scene,variant)]
                values=[r['error_m'] for r in rr if r['error_m'] is not None]
                groups.append(dict(engine=engine,scene=scene,variant=variant,p50_m=statistics.median(values) if values else None,
                    p95_m=quantile(values,.95),coverage=len(values)/len(rr) if rr else None))
    coupling=[]
    for variant in ('baseline','H3','S','BS'):
        rr=[r for r in audits if r['variant']==variant]
        coupling.append(dict(variant=variant,paths=len(rr),**{key:sum(r[key] for r in rr) for key in
            ('selections','with_hint','hint_id_mismatches','accepted_conflicts')}))
    (root/'association-traces.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in traces))
    phases=defaultdict(list);example=None
    for entry in s['runs']:
        if entry['phase']!='confirm':continue
        source=manage.OPS/'experiments'/entry['run_id']
        shots=[json.loads(line) for line in (root/'normalized-scores'/source.name/'shots.jsonl').open()]
        if len(shots)!=entry['shots'] or abs(sum(r['weighted_hit_score'] for r in shots)-entry['weight'])>1e-8:
            raise ValueError('Shot diagnostic disagrees with frozen normalized score')
        for shot in shots:
            time=shot['fire_time_s'];phase='stationary' if time<1 else 'after_turn' if entry['scene']=='hf_turn90_3' and time>=3.2 else 'moving'
            phases[entry['scene'],entry['variant'],phase].append(shot)
        if entry['variant']!='BS' or entry['scene']!='hf_turn90_3' or entry['sensor_seed']!=2601:continue
        shot=next((r for r in shots if r['fire_time_s']>=3.2 and r['outcome']=='target_armor'),None)
        if shot is None:continue
        records=[json.loads(line)['data'] for line in (source/'session-evidence/events.jsonl').open() if json.loads(line)['kind']=='result']
        decision=max((r for r in records if r['frame'].get('logical_receipt_ns',10**30)<=shot['fire_stamp_ns']),key=lambda r:r['frame']['logical_receipt_ns'])
        matching_frame=next(json.loads(line) for line in (source/'frames.jsonl').open()
            if json.loads(line)['stamp_ns']==decision['frame']['stamp_ns'])
        selected_armor_id=matching_frame['selected_gt_armor_id']
        if selected_armor_id is None:raise ValueError('Selected physical plate was not matched by evaluator')
        packets=sorted([json.loads(line)['data'] for line in (source/'telemetry.jsonl').open() if json.loads(line)['kind']=='armors'],key=lambda r:r['stamp_ns'])
        stamps=[r['stamp_ns'] for r in packets]
        def at(stamp):
            i=bisect.bisect_left(stamps,stamp)
            if i==0 or i==len(stamps):raise ValueError('Missing armor truth brackets')
            a,b=packets[i-1:i+1]
            if b['stamp_ns']-a['stamp_ns']>100_000_000:raise ValueError('Armor truth gap too large')
            fraction=(stamp-a['stamp_ns'])/(b['stamp_ns']-a['stamp_ns'])
            aa,bb=[next(r for r in item['armors'] if r['robot_id']==1 and r['id']==selected_armor_id) for item in (a,b)]
            return [x+(y-x)*fraction for x,y in zip(aa['position'],bb['position'])]
        frame=decision['frame'];result=decision['result'];stamp=frame['stamp_ns'];future=stamp+round(result['prediction_horizon_s']*1e9)
        a,b=at(stamp-10_000_000),at(stamp+10_000_000)
        predicted=[x+y for x,y in zip(result['aim_point'],frame['gimbal_position_map'])];actual=at(future)
        example=dict(run_id=entry['run_id'],sensor_seed=2601,shot_seq=shot['shot_seq'],fire_time_s=shot['fire_time_s'],
            selected_physical_armor_id=selected_armor_id,hit_physical_armor_id=shot['armor_id'],
            selected_plate_position_error_m=matching_frame['selected_plate_position_error_m'],
            image_time_s=(stamp-load(source/'run-state.json')['measurement_start_stamp_ns'])*1e-9,
            command_to_launch_s=(shot['fire_stamp_ns']-frame['logical_receipt_ns'])*1e-9,
            alignment='Latest command whose recorded logical receipt precedes the actual launch; posthoc truth only.',
            estimated_velocity=result['plate_velocities'][result['selected_plate_id']],observed_true_velocity=[(y-x)/.02 for x,y in zip(a,b)],
            predicted_future_point=predicted,actual_same_plate_at_prediction_time=actual,prediction_horizon_s=result['prediction_horizon_s'],
            future_position_error_m=[x-y for x,y in zip(predicted,actual)],
            forecast_samples=[dict(offset_s=result['prediction_horizon_s']*i/20,
                predicted_x=result['track_estimate']['plates'][result['selected_plate_id']][0]+frame['gimbal_position_map'][0]
                    +result['plate_velocities'][result['selected_plate_id']][0]*result['prediction_horizon_s']*i/20,
                actual_x=at(stamp+round(result['prediction_horizon_s']*i/20*1e9))[0]) for i in range(21)],
            impact_x_fraction=shot['impact_x_fraction'],impact_y_fraction=shot['impact_y_fraction'],weighted_score=shot['weighted_hit_score'])
    phase_rows=[]
    for (scene,variant,phase),shots in phases.items():
        hits=[r for r in shots if r['outcome']=='target_armor'];weight=sum(r['weighted_hit_score'] for r in shots)
        phase_rows.append(dict(scene=scene,variant=variant,phase=phase,shots=len(shots),hits=len(hits),weighted_rate=weight/len(shots),
            mean_abs_x_fraction=statistics.mean(abs(r['impact_x_fraction']) for r in hits) if hits else None,
            mean_abs_y_fraction=statistics.mean(abs(r['impact_y_fraction']) for r in hits) if hits else None))
    write(root/'analysis-result.json',dict(completed=True,groups=groups,coupling=coupling,per_replay_coupling=audits,
        confirmation_phase_scores=phase_rows,turn_prediction_example=example,
        regression_source=next(e['run_id'] for e in c['sources'] if e['variant']=='H3' and e['sensor_seed']==2301 and e['scene']=='hf_uniform_3')))


def plots(root):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    d=load(root/'analysis-result.json');traces=[json.loads(l) for l in (root/'association-traces.jsonl').open()]
    fig,axes=plt.subplots(1,2,figsize=(12,4),layout='constrained')
    for ax,engine in zip(axes,('ui','backend')):
        for i,variant in enumerate(('baseline','S','BS')):
            values=[next(r['p95_m'] for r in d['groups'] if r['engine']==engine and r['scene']==scene and r['variant']==variant)
                for scene in ('hf_uniform_3','hf_turn90_3')]
            ax.bar([j+(i-1)*.24 for j in range(2)],values,width=.24,label=variant)
        ax.set(xticks=(0,1),xticklabels=('Uniform 3m/s','Turn 90deg'),title=engine+' recorded paths: SAME-input replay',ylabel='Center XY error P95 (m)')
        ax.legend();ax.grid(axis='y',alpha=.2)
    fig.savefig(root/'replay-errors.png',dpi=170);plt.close(fig)
    fig,ax=plt.subplots(figsize=(10,4),layout='constrained')
    for variant in ('baseline','H3','S','BS'):
        rr=[r for r in traces if r['source']==d['regression_source'] and r['variant']==variant and r['error_m'] is not None]
        ax.plot([r['time_s'] for r in rr],[r['error_m'] for r in rr],label=variant)
    ax.set(title='Prior H3 uniform seed2301 failure: fixed camera / pose / clocks',xlabel='Recorded scenario time (s); negative = warmup',ylabel='Center XY error (m)')
    ax.legend();ax.grid(alpha=.2);fig.savefig(root/'recorded-regression.png',dpi=170);plt.close(fig)
    example=d.get('turn_prediction_example')
    if example:
        samples=example['forecast_samples'];fig,ax=plt.subplots(figsize=(8,4),layout='constrained')
        ax.plot([r['offset_s'] for r in samples],[r['predicted_x'] for r in samples],label='Recorded CV forecast from exposure')
        ax.plot([r['offset_s'] for r in samples],[r['actual_x'] for r in samples],label='Posthoc actual same physical plate')
        ax.set(title='BS turn90 seed2601: velocity reversal lag at t=3.384s',xlabel='Time after exposure (s)',ylabel='Plate world X (m)')
        ax.legend();ax.grid(alpha=.2);fig.savefig(root/'turn-prediction.png',dpi=170);plt.close(fig)
    s=load(root/'state.json');screen=[r for r in s['runs'] if r['phase']=='screen']
    if not screen:return
    fig,axes=plt.subplots(1,2,figsize=(10,4),layout='constrained')
    for ax,scene in zip(axes,('hf_uniform_3','hf_turn90_3')):
        variants=s['screen_variants'];values=[aggregate([r for r in screen if r['variant']==variant and r['scene']==scene]) for variant in variants]
        for offset,key,label in ((-.2,'target_hit_rate','Exact hit rate'),(.2,'weighted_hit_rate','Weighted hit rate')):
            ax.bar([i+offset for i in range(len(values))],[100*a[key] if a[key] is not None else 0 for a in values],width=.4,label=label)
        ax.set(xticks=range(len(variants)),xticklabels=variants,title=scene,ylabel='Percent of all shots');ax.legend(fontsize=8);ax.grid(axis='y',alpha=.2)
    fig.savefig(root/'backend-screen.png',dpi=170);plt.close(fig)


def render(root):
    c=load(root/'contract.json');s=load(root/'state.json');d=load(root/'analysis-result.json');v=load(root/'validation.json');replay=load(root/'replay-result.json')
    number=lambda x:'—' if x is None else f'{x:.3f}'
    lines=['# 阶段总结与持续图像关联优化','',
      '此前 A、Bfix、H3 分别缓解角点误拒绝、初始化等待、早期错误板号交接，但均未通过默认 v3 的整体射击验收。H3 在上一轮自己的新种子上匀速加权 +5.10pp、变向 −5.32pp，说明短期保护不能替代持续关联。不同批次种子不同，不将跨批绝对成绩当排行榜。','',
      '本轮统一图像匹配、求解 yaw hint 与滤波更新所用板号。S 是 v3 加持续图像关联；BS 再加联合初始化；两者关闭 A 和 H3。默认关闭新开关，不自动推广。',
      f'完成 {v["replays"]} 段回放、{v["frames"]} 帧，{v["backend_trials"]} 轮后台闭环，新增 UI 实验 0。',
      '[实现与固定预算](implementation.md) · [冻结定义](contract.json) · [校验](validation.json) · [原始状态](state.json)','',
      '## 同输入回放：整体估计','',
      '| 配置 | 中心 XY P95 / m | 覆盖 | 初始化 P90 / s | 进入闭环 |','|---|---:|---:|---:|---|']
    b=replay['baseline'];lines.append(f'| baseline | {number(b["error_p95_m"])} | {100*b["coverage"]:.2f}% | {number(b["initial_latency_p90_s"])} | 对照 |')
    for variant,score in replay['candidates'].items():
        m=score['metrics'];lines.append(f'| {variant} | {number(m["error_p95_m"])} | {100*m["coverage"]:.2f}% | {number(m["initial_latency_p90_s"])} | {score["eligible"]} |')
    lines+=['','24 条录像每条均回放 v3/S/BS；额外十条 H3 用于原生复现。每条源录像固定原图、己方姿态与决策时钟，不由新命令改变相机轨迹。估计误差改善不能代替新闭环命中率。','',
      '![回放误差](replay-errors.png)','',
      '| 来源 | 场景 | 配置 | 中位误差 / m | P95 / m | 覆盖 |','|---|---|---|---:|---:|---:|']
    for row in d['groups']:
        lines.append(f'| {row["engine"]} | {row["scene"]} | {row["variant"]} | {number(row["p50_m"])} | {number(row["p95_m"])} | {100*row["coverage"]:.2f}% |')
    lines+=['','单独的 S 在部分 UI 来源轨迹中位误差变差，不能仅凭合并 P95 判断全面改善。每条源录像上的候选比较输入相同；不同来源相机轨迹的绝对误差不构成 UI/后台等价性检验。','',
      '## 两次板号决策是否确实冲突','',
      '对每次实际选择的观测，比较求解器 supplied_hint 与更新前车体相位及所分配板号对应的朝向。冲突阈值仅为数值容差；实际滤波接受与否另列。','',
      '| 配置 | 回放轨迹 | 有提示的已选观测 | 提示/更新板号冲突 | 实际接受冲突更新 |','|---|---:|---:|---:|---:|']
    for row in d['coupling']:lines.append('| '+' | '.join(str(row[key]) for key in ('variant','paths','with_hint','hint_id_mismatches','accepted_conflicts'))+' |')
    lines+=['','这些是相关帧/观测计数，不是独立实验概率；H3 仅十条原生轨迹，不能将它的计数与其他配置直接比较为相对错误率。','',
      '![固定失败录像](recorded-regression.png)','',
      '## 后台并行与成本','']
    p=load(root/'parallel-result.json')
    if p:
        lines+=[f'单/双路探针通过：{p["passed"]}；实际重叠：{p["actual_overlap"]}；效果使用 {p["effect_parallelism"]} 路。图像/算法行为和评分逐组一致，资源和 40 秒预算分别检查。资格限本轮负载，不写入全局容量登记。','',
          '| 配置 | 场景 | 图像/行为一致 | 评分一致 | 资源健康 | ≤40s |','|---|---|---|---|---|---|']
        for row in p['pairs']:lines.append('| '+' | '.join(str(row[key]) for key in ('variant','scene','same_behavior','same_scores','healthy','within_budget'))+' |')
        spans={}
        for phase in ('probe_p1','probe_p2','screen','confirm'):
            rr=[r for r in s['runs'] if r['phase']==phase]
            if not rr:continue
            span=max(r['attempt']['ended'] for r in rr)-min(r['attempt']['started'] for r in rr);spans[phase]=span
            lines+=['',f'{phase}：{len(rr)} 轮，批次跨度 {span:.1f}s，折算 {len(rr)*3600/span:.1f} 轮/小时。']
        if 'probe_p1' in spans and 'probe_p2' in spans:lines+=['',f'双路探针吞吐约为单路 {spans["probe_p1"]/spans["probe_p2"]:.2f} 倍；单次最长 {max(r["wall_s"] for r in s["runs"]):.2f}s。']
    else:lines+=['候选未通过回放护栏，没有启动新闭环。']
    for phase,title in (('screen','后台开发成绩'),('confirm','独立后台确认')):
        rows=[r for r in s['runs'] if r['phase']==phase]
        if not rows:continue
        lines+=['','## '+title,'',
          '| 场景 | 配置 | 命中/发射 | 精确命中 | 加权命中 | 命中/秒 | 首次命中 P90 / s |','|---|---|---:|---:|---:|---:|---:|']
        for scene in ('hf_uniform_3','hf_turn90_3','all'):
            for variant in ('baseline','S','BS'):
                rr=[r for r in rows if r['variant']==variant and (scene=='all' or r['scene']==scene)]
                if not rr:continue
                a=aggregate(rr);exact=number(100*a['target_hit_rate']) if a['target_hit_rate'] is not None else '—';weight=number(100*a['weighted_hit_rate']) if a['weighted_hit_rate'] is not None else '—'
                lines.append(f'| {scene} | {variant} | {a["hits"]}/{a["shots"]} | {exact}% | {weight}% | {a["hits_per_s"]:.3f} | {number(quantile([r["first_hit_s"] for r in rr],.9))} |')
        lines+=['','同场景各传感器种子的单次加权范围如下；只描述这批实验的波动，不把五次或十次试验当作已充分覆盖实车变化。','',
          '| 场景 | 配置 | 单次加权最小 | 单次加权中位 | 单次加权最大 |','|---|---|---:|---:|---:|']
        for scene in ('hf_uniform_3','hf_turn90_3'):
            for variant in ('baseline','S','BS'):
                rates=[100*r['weight']/r['shots'] for r in rows if r['scene']==scene and r['variant']==variant and r['shots']]
                if rates:lines.append(f'| {scene} | {variant} | {min(rates):.2f}% | {statistics.median(rates):.2f}% | {max(rates):.2f}% |')
    if (root/'backend-screen.png').exists():lines+=['','![后台开发成绩](backend-screen.png)']
    lines+=['','## 验收判定','']
    for filename,label in (('screen-result.json','开发'),('confirm-result.json','独立确认')):
        result=load(root/filename)
        if not result:continue
        for row in result if isinstance(result,list) else [result]:
            failures=[key for key,value in row['checks'].items() if not value]
            lines+=[f'{label} {row["variant"]}：通过={row["passed"]}；未通过项：'+(', '.join(failures) if failures else '无')+'。']
            if row.get('weighted_gain_95') is not None:lines+=['',f'独立配对加权收益 95% 区间：{row["weighted_gain_95"]}。']
    lines+=['',f'选入独立确认：{s.get("selected")}；独立后台通过：{s.get("backend_confirmed",False)}；UI 复核资格：{s.get("ui_eligible",False)}。默认 v3 未替换。',
      '新开发种子 2501–2505；独立确认仅使用 2601–2610；探针不混入效果统计。原始全图与数值封存，媒体未请求，尚未人工查看全部新视频。','',
      '## 剩余中心偏差在哪里','']
    if d.get('confirmation_phase_scores'):
        lines+=['按发射时刻分段：stationary 为 0–1s，moving 为 1s 后的匀速段，after_turn 为变向场景 3.2s 后。命中点坐标按板半宽/半高归一化，0 为中心，绝对值 1 为边缘。分段为本轮后的诊断，不能当作新的独立确认。','',
          '| 场景 | 配置 | 发射阶段 | 命中/发射 | 加权命中率 | 命中平均绝对 x | 命中平均绝对 y |','|---|---|---|---:|---:|---:|---:|']
        for row in d['confirmation_phase_scores']:
            lines.append(f'| {row["scene"]} | {row["variant"]} | {row["phase"]} | {row["hits"]}/{row["shots"]} | {100*row["weighted_rate"]:.2f}% | {number(row["mean_abs_x_fraction"])} | {number(row["mean_abs_y_fraction"])} |')
    example=d.get('turn_prediction_example')
    if example:
        est=example['estimated_velocity'];actual=example['observed_true_velocity'];error=example['future_position_error_m']
        lines+=['',
          f'具体例：BS 的独立 seed2601，图像时刻 {example["image_time_s"]:.3f}s、发射 {example["fire_time_s"]:.3f}s。根据发射前最后一条已接收命令关联，命令到发射间隔 {1000*example["command_to_launch_s"]:.2f}ms。',
          f'独立验收器匹配原瞄准物理装甲 ID={example["selected_physical_armor_id"]}，图像时刻的板位置误差仅 {example["selected_plate_position_error_m"]:.3f}m；实际命中 ID={example["hit_physical_armor_id"]}。下列外推误差使用原瞄准板作参照。',
          f'滤波板速度 x={est[0]:+.3f}m/s，同一物理装甲通过原始真值 ±10ms 差分测得 x={actual[0]:+.3f}m/s。未来点 x 误差 {error[0]:+.3f}m，而 z 误差约 {error[2]*1000:+.1f}mm；实际命中板上沿 y={example["impact_y_fraction"]:.3f}、加权得分 {example["weighted_score"]:.3f}。','',
          '![转向后的外推误差](turn-prediction.png)','',
          '这支持下一步优先检查变向后的径向速度收敛、距离外推与弹道补偿。尚未通过运动模型的单因素闭环干预证明全部高打由此引起；本轮没有修改这些参数。已分析的独立种子后续可作诊断/开发，下一次改动必须换用新的独立确认种子。']
    lines+=['','## 证据与适用范围','',
      f'- {c["geometry_checks"]} 项 C++ 检查、5 项录像集成检查通过；24 条原生配置逐帧复现核心输出。',
      '- 图像关联仅普通四板小装甲启用；单板深度噪声、预测器和射击时机没有在本轮修改。',
      '- 未匹配代价与图像门槛是本轮固定工程选择，没有按成绩扫描；没有给持续保护添加更长等待。',
      '- 固定录像中的 UI 来源不是新的 UI 闭环。后台通过也不证明 UI/实车算法排名或效果幅度一致。','']
    markdown='\n'.join(lines);(root/'report.md').write_text(markdown)
    def inline(value):
        value=html.escape(value);return re.sub(r'\[([^\]]+)\]\(([^)]+)\)',r'<a href="\2">\1</a>',value)
    parts=[];table=False
    for line in lines:
        if table and not line.startswith('|'):parts.append('</table>');table=False
        if line.startswith('|'):
            if re.fullmatch(r'[|:\- ]+',line):continue
            tag='td' if table else 'th'
            if not table:parts.append('<table>');table=True
            parts.append('<tr>'+''.join('<'+tag+'>'+inline(cell.strip())+'</'+tag+'>' for cell in line.strip('|').split('|'))+'</tr>')
        elif line.startswith('!['):
            match=re.fullmatch(r'!\[([^\]]*)\]\(([^)]+)\)',line)
            if match:parts.append('<img alt="'+html.escape(match[1])+'" src="'+html.escape(match[2])+'">')
        elif line.startswith('#'):
            level=len(line)-len(line.lstrip('#'));parts.append(f'<h{level}>'+inline(line[level:].strip())+f'</h{level}>')
        elif line:parts.append('<p>'+inline(line)+'</p>')
    if table:parts.append('</table>')
    (root/'report.html').write_text('<meta charset="utf-8"><title>持续图像关联优化</title><style>body{font:16px/1.6 sans-serif;max-width:1250px;margin:36px auto;padding:0 20px}table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:7px}img{max-width:100%}</style>'+''.join(parts))


def finish(root):
    c=load(root/'contract.json');s=load(root/'state.json')
    if s['state']!='completed':raise ValueError('Complete fixed experiment before final report')
    verify(root,c,s);analyze(root)
    meta=load(Path(c['bundles']['baseline'])/'bundle.json')
    command=['docker','run','--rm','--network','none','--cpus','2','--user',f'{os.getuid()}:{os.getgid()}',
        '-e','OPENBLAS_NUM_THREADS=1','-e','MPLCONFIGDIR=/tmp/matplotlib',
        '-v',str(manage.ROOT)+':'+str(manage.ROOT)+':ro','-v',str(root)+':'+str(root),'-w',str(manage.ROOT),
        '--entrypoint','python3',meta['images']['evaluator'],'-B',str(Path(__file__)),'plots','--output',str(root)]
    with (root/'plots.log').open('w') as log:subprocess.run(command,check=True,timeout=60,stdout=log,stderr=subprocess.STDOUT)
    (root/'implementation.md').write_text((manage.OPS/'PERCEPTION_ASSOCIATION.md').read_text());render(root)
    files=('report.md','report.html','validation.json','analysis-result.json','association-traces.jsonl',
        'replay-result.json','parallel-result.json','screen-result.json','confirm-result.json','repeatability.json',
        'implementation.md','replay-errors.png','recorded-regression.png','backend-screen.png','turn-prediction.png','cpp-checks.log','replay-checks.log')
    write(root/'report-seal.json',dict(completed=True,files={name:sha(root/name) for name in files if (root/name).exists()}))
    from experiments import publish_desktop
    publish_desktop(manage,root)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('analyze','plots','render','finish'));p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();globals()[a.action](a.output.resolve())
