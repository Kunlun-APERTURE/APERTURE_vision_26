"""Read-only error-budget review on archived BS validation paths."""
import argparse
import bisect
from collections import Counter,defaultdict
import hashlib
import html
import json
import math
import os
from pathlib import Path
import statistics
import subprocess

import manage
from decision_benchmark import sha
from perception_study import protected,quantile
from report import load,write

PARENT=manage.OPS/'experiments/perception-association-20261023'
HISTORY=('ROBUSTNESS_V3.md','OPTIMIZATION_V4.md','DECISION_BENCHMARK.md',
    'PERCEPTION_OPTIMIZATION.md','PERCEPTION_REPAIR.md','PERCEPTION_HANDOFF.md','PERCEPTION_ASSOCIATION.md')


def freeze(root):
    root.mkdir(parents=True,exist_ok=True)
    if (root/'contract.json').exists():return load(root/'contract.json')
    parent=load(PARENT/'state.json');c=load(PARENT/'contract.json')
    if parent['state']!='completed':raise ValueError('Complete source study first')
    sources=[]
    for entry in parent['runs']:
        if entry['phase']!='confirm' or entry['variant']!='BS':continue
        f=manage.OPS/'experiments'/entry['run_id']
        sources.append(dict(entry,source_hashes={n:sha(f/n) for n in ('manifest.json','summary.json','run-state.json',
            'frames.jsonl','telemetry.jsonl','session-evidence/events.jsonl')}))
    if len(sources)!=20:raise ValueError('Expected all 20 frozen BS validation paths')
    result=dict(version=1,parent=str(PARENT),parent_contract_sha256=sha(PARENT/'contract.json'),sources=sources,
        protected_before=protected(),analysis_script_sha256=sha(Path(__file__)),
        history={name:sha(manage.OPS/name) for name in HISTORY},
        conditions=dict(future_must_precede_measurement_end=True,physical_identity='Image-matched selected observation; must agree with scorer nearest-plate id',
            native_cv_error_max_m=.001,truth_difference_s=.01,image_match_max_px=12),
        interventions=['native','true_position','true_velocity','true_radial_velocity','true_lateral_velocity','true_position_and_velocity'],
        purpose='Post-inference component ceilings and review; no new inference, simulator, shot, tuning, capacity or deployment.',
        evaluator_image=load(Path(c['bundles']['BS'])/'bundle.json')['images']['evaluator'])
    write(root/'contract.json',result);return result


def phase(scene,t,h):
    if t<1:return 'stationary' if t+h<1 else 'cross_start'
    if t<1.5:return 'start_acceleration'
    if scene=='hf_turn90_3':
        if t<3.2:return 'steady' if t+h<3.2 else 'cross_unseen_turn'
        return 'post_turn_0_500ms' if t<3.7 else 'post_turn_later'
    return 'steady'


def stats(values):
    return dict(n=len(values),p50=statistics.median(values) if values else None,p95=quantile(values,.95))


def analyze(root,require_agreement=True):
    import numpy as np
    from perception_attribution_analysis import timeline,match_truth
    c=freeze(root);rows=[];timing=[];counts=Counter();per_run=[]
    prefix='' if require_agreement else 'sensitivity-'
    if not require_agreement:
        write(root/'sensitivity-contract.json',dict(parent_contract_sha256=sha(root/'contract.json'),
            difference='Keep independent image identity matches even if estimated 3D nearest-plate identity disagrees; label that disagreement explicitly.',
            rationale='The frozen strict filter excludes large depth errors. This predeclared sensitivity does not replace strict results or enter acceptance.',
            source_sha256=sha(Path(__file__))))
    for entry in c['sources']:
        source=manage.OPS/'experiments'/entry['run_id'];state=load(source/'run-state.json');truth=timeline(source)
        frames={r['stamp_ns']:r for r in (json.loads(l) for l in (source/'frames.jsonl').open())}
        results=[p['data'] for p in (json.loads(l) for l in (source/'session-evidence/events.jsonl').open()) if p['kind']=='result']
        accepted=[];run_counts=Counter()
        for packet in results:
            f,r=packet['frame'],packet['result'];stamp=f['stamp_ns'];fr=frames.get(stamp)
            if fr is None:continue
            counts['measured_frames']+=1;run_counts['measured']+=1
            h=r.get('prediction_horizon_s');sid=r.get('selected_plate_id',-1)
            if not r.get('valid') or sid<0 or not isinstance(h,(int,float)) or not 0<h<1:
                counts['invalid_forecast']+=1;continue
            future=stamp+round(h*1e9)
            if future>state['measurement_end_stamp_ns']:counts['future_beyond_measurement']+=1;continue
            links=r.get('continuity',{}).get('image_association') or {}
            chosen=next((x for x in links.get('observations',[]) if x['plate_id']==sid),None)
            if chosen is None:counts['selected_not_observed']+=1;continue
            observations=r.get('observations',[])
            if not observations:counts['no_observations']+=1;continue
            distances=[float(np.linalg.norm(np.mean(a['corners'],axis=0)-chosen['image_center'])) for a in observations]
            oi=int(np.argmin(distances))
            matched=match_truth(f,observations,truth(stamp))
            if distances[oi]>4 or oi not in matched:counts['no_unique_image_truth_match']+=1;continue
            identity=matched[oi]['plate']['id']
            agrees=identity==fr.get('selected_gt_armor_id')
            if not agrees:
                counts['physical_identity_disagreement']+=1
                if require_agreement:continue
            def position(t):
                a=next((a for a in truth(t) if a['id']==identity),None)
                return np.asarray(a['position'],float) if a else None
            now=position(stamp);end=position(future);a=position(stamp-10_000_000);b=position(stamp+10_000_000)
            if any(x is None for x in (now,end,a,b)):counts['missing_truth_brackets']+=1;continue
            velocity=(b-a)/.02
            pos=np.asarray(r['track_estimate']['plates'][sid],float)+f['gimbal_position_map']
            vel=np.asarray(r['plate_velocities'][sid],float)
            native=np.asarray(r['aim_point'],float)+f['gimbal_position_map']
            if np.linalg.norm(native-(pos+vel*h))>.001:counts['native_not_cv']+=1;continue
            radial=pos-np.asarray(f['camera_in_map']['position']);radial[2]=0
            if np.linalg.norm(radial)<1:counts['invalid_range']+=1;continue
            radial/=np.linalg.norm(radial);lateral=np.array([-radial[1],radial[0],0])
            dv=velocity-vel;errors={}
            cases=dict(native=native,true_position=native+now-pos,true_velocity=native+dv*h,
                true_radial_velocity=native+radial*np.dot(dv,radial)*h,
                true_lateral_velocity=native+lateral*np.dot(dv,lateral)*h,
                true_position_and_velocity=now+velocity*h)
            for name,point in cases.items():errors[name]=float(np.linalg.norm((point-end)[:2]))
            t=(stamp-state['measurement_start_stamp_ns'])*1e-9
            row=dict(run_id=entry['run_id'],scene=entry['scene'],sensor_seed=entry['sensor_seed'],seq=f['seq'],time_s=t,
                phase=phase(entry['scene'],t,h),horizon_s=h,physical_id=identity,errors_m=errors,
                nearest_3d_identity_agrees=agrees,
                current_position_error_m=float(np.linalg.norm((pos-now)[:2])),
                radial_velocity_error_mps=float(np.dot(vel-velocity,radial)),lateral_velocity_error_mps=float(np.dot(vel-velocity,lateral)),
                radial_sign_wrong=abs(float(np.dot(velocity,radial)))>.2 and float(np.dot(velocity,radial))*float(np.dot(vel,radial))<0)
            rows.append(row);accepted.append(row);counts['accepted']+=1;run_counts['accepted']+=1
        # The last received valid advice is an attribution proxy, not a measured
        # independent hardware actuator delay. All matching failures are kept.
        receipts=sorted((p['frame']['logical_receipt_ns'],p) for p in results if 'logical_receipt_ns' in p['frame'])
        stamps=[x[0] for x in receipts]
        shots=[json.loads(l) for l in (PARENT/'normalized-scores'/source.name/'shots.jsonl').open()]
        for shot in shots:
            index=bisect.bisect_right(stamps,shot['fire_stamp_ns'])-1
            if index<0:counts['unattributed_shot']+=1;continue
            receipt,p=receipts[index];r=p['result'];f=p['frame']
            if not r.get('valid') or not r.get('fire_advice'):counts['last_command_not_valid_advice']+=1;continue
            decision=r['evaluation_clocks']['aim_now_ns']
            timing.append(dict(run_id=source.name,scene=entry['scene'],shot_seq=shot['shot_seq'],
                exposure_to_launch_s=(shot['fire_stamp_ns']-f['stamp_ns'])*1e-9,
                decision_to_launch_s=(shot['fire_stamp_ns']-decision)*1e-9,
                receipt_to_launch_s=(shot['fire_stamp_ns']-receipt)*1e-9))
        per_run.append(dict(run_id=source.name,scene=entry['scene'],sensor_seed=entry['sensor_seed'],counts=dict(run_counts),
            phase_metrics={p:{name:stats([r['errors_m'][name] for r in accepted if r['phase']==p]) for name in c['interventions']}
                for p in sorted({r['phase'] for r in accepted})}))
    groups=[]
    for scene in ('hf_uniform_3','hf_turn90_3'):
        for p in sorted({r['phase'] for r in rows if r['scene']==scene}):
            rr=[r for r in rows if r['scene']==scene and r['phase']==p]
            groups.append(dict(scene=scene,phase=p,frames=len(rr),paths=len({r['run_id'] for r in rr}),
                horizon_s=stats([r['horizon_s'] for r in rr]),radial_sign_wrong=sum(r['radial_sign_wrong'] for r in rr),
                errors={name:stats([r['errors_m'][name] for r in rr]) for name in c['interventions']}))
    write(root/(prefix+'result.json'),dict(completed=True,counts=dict(counts),groups=groups,per_run=per_run,
        timing={key:stats([r[key] for r in timing]) for key in ('exposure_to_launch_s','decision_to_launch_s','receipt_to_launch_s')},
        shots_attributed=len(timing),new_closed_loop_trials=0,algorithm_modified=False,
        limitations=['Truth interventions are unattainable component ceilings, not algorithm proposals or new hit rates.',
            'Frames are correlated; pooled P95 is descriptive. Per-run phase values and match coverage are retained.',
            'Identity filtering excludes ambiguous or unobserved targets; the error budget is conditional on matched frames.',
            'The original forecast horizon is fixed for each intervention; ballistic re-solving and closed-loop feedback are absent.',
            'Future turning within the horizon may be unobservable at exposure even with perfect current position and velocity.',
            'Latest-received-command timing is not independently traced physical actuator latency.']))
    (root/(prefix+'frame-budget.jsonl')).write_text(''.join(json.dumps(r)+'\n' for r in rows))
    (root/(prefix+'shot-timing.jsonl')).write_text(''.join(json.dumps(r)+'\n' for r in timing))
    if protected()!=c['protected_before']:raise ValueError('Production artifacts changed during review')
    for entry in c['sources']:
        f=manage.OPS/'experiments'/entry['run_id']
        if any(sha(f/n)!=h for n,h in entry['source_hashes'].items()):raise ValueError('Original archived evidence changed')
    write(root/(prefix+'validation.json'),dict(passed=True,sources=len(c['sources']),production_unchanged=True,source_evidence_unchanged=True,
        new_closed_loop_trials=0,algorithm_modified=False,source_sha256=sha(Path(__file__))))


def sensitivity(root):analyze(root,False)


def structure(root):
    """Test an unweighted shared velocity as an explicit diagnostic ablation."""
    c=load(root/'contract.json');budget={(r['run_id'],r['seq']):r for r in
        (json.loads(l) for l in (root/'sensitivity-frame-budget.jsonl').open())}
    rows=[];switches=[]
    for entry in c['sources']:
        source=manage.OPS/'experiments'/entry['run_id'];previous=None;state=load(source/'run-state.json')
        for line in (source/'session-evidence/events.jsonl').open():
            packet=json.loads(line)
            if packet['kind']!='result':continue
            frame=packet['data']['frame'];result=packet['data']['result'];key=(source.name,frame['seq'])
            if not state['measurement_start_stamp_ns']<=frame['stamp_ns']<=state['measurement_end_stamp_ns']:continue
            velocities=result.get('plate_velocities',[]);sid=result.get('selected_plate_id',-1)
            if sid<0 or sid>=len(velocities):continue
            generation=result.get('track_generation');current=(frame['stamp_ns'],sid,velocities[sid],generation)
            if previous and generation==previous[3] and sid!=previous[1] and 0<frame['stamp_ns']-previous[0]<100_000_000:
                switches.append(dict(run_id=source.name,scene=entry['scene'],velocity_jump_mps=math.dist(velocities[sid],previous[2])))
            previous=current
            b=budget.get(key)
            if b is None:continue
            plates=(result.get('continuity',{}).get('filter') or {}).get('plates',[])
            fresh=[velocities[i] for i,p in enumerate(plates) if p.get('initialized') and p.get('age_s',99)<.05 and i<len(velocities)]
            if len(fresh)<2:continue
            spread=max(math.dist(a[:2],z[:2]) for i,a in enumerate(fresh) for z in fresh[i+1:])
            shared=[sum(v[i] for v in fresh)/len(fresh) for i in range(3)]
            # Recover native error vector using analysis-only logged oracle
            # coordinates is deliberately avoided; recompute only a predicted
            # displacement scale, and do not infer new hit rates.
            correction=[(x-y)*b['horizon_s'] for x,y in zip(shared,velocities[sid])]
            rows.append(dict(run_id=source.name,scene=entry['scene'],phase=b['phase'],spread_mps=spread,
                average_velocity_change_mps=math.dist(shared[:2],velocities[sid][:2]),
                average_future_point_change_m=math.hypot(*correction[:2])))
    groups=[]
    for scene in ('hf_uniform_3','hf_turn90_3'):
        for p in sorted({r['phase'] for r in rows if r['scene']==scene}):
            rr=[r for r in rows if r['scene']==scene and r['phase']==p]
            groups.append(dict(scene=scene,phase=p,multiple_fresh_frames=len(rr),
                spread_mps=stats([r['spread_mps'] for r in rr]),
                average_future_point_change_m=stats([r['average_future_point_change_m'] for r in rr])))
    write(root/'structure-result.json',dict(completed=True,groups=groups,selected_plate_switches=len(switches),
        switch_velocity_jump_mps=stats([r['velocity_jump_mps'] for r in switches]),
        limitations='Fresh independent plate filters on a translating rigid vehicle should agree, but shared averaging is not verified as a repair and ignores correlated depth noise, covariance and rotation. No counterfactual accuracy/hit rate is claimed.'))


def plots(root):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    r=load(root/'sensitivity-result.json');keys=('native','true_position','true_radial_velocity','true_lateral_velocity','true_position_and_velocity')
    labels=('Native','True current position','True radial velocity','True lateral velocity','True position + velocity')
    fig,axes=plt.subplots(1,2,figsize=(12,4),layout='constrained')
    for ax,p,title in ((axes[0],'post_turn_0_500ms','First 0.5s after turn'),(axes[1],'post_turn_later','Later after turn')):
        g=next(g for g in r['groups'] if g['scene']=='hf_turn90_3' and g['phase']==p)
        ax.barh(labels,[g['errors'][key]['p95'] for key in keys]);ax.invert_yaxis()
        ax.set(title=title+' (posthoc image identity)',xlabel='Future point XY error P95 (m)');ax.grid(axis='x',alpha=.2)
    fig.savefig(root/'component-ceilings.png',dpi=170);plt.close(fig)


def report(root):
    c=load(root/'contract.json');r=load(root/'result.json');s=load(root/'sensitivity-result.json');struct=load(root/'structure-result.json')
    history=load(PARENT/'confirm-result.json');metrics=history['groups']['all']
    lines=['# 开发历史复盘与下一轮突破口','',
      '本轮只读取已有二十条 BS 独立验证记录和冻结源码，未新增闭环、推理或射击，未修改算法、物理和正式配置。已分析的种子 2601–2610 今后属于开发/诊断数据；下一候选须使用新的独立确认种子。','',
      '## 从开发历史能确认什么','',
      '| 阶段 | 有价值的证据 | 不能据此认定的结论 |','|---|---|---|',
      '| v3 | 独立板平移、径向噪声分离改善稳定性 | 低径向过程项能快速跟踪变向 |',
      '| v4 机动/时序/旋转 | 时序与部分运动窗口出现收益；原协方差门控曾导致完全不发射 | 整包失败不等于机动识别没有价值，不能叠加后直接推广 |',
      '| A/Bfix/H3 | 分别消除具体角点误拒绝、长初始化等待、错误交接 | 修好一个失败例就能稳定提高整体命中 |',
      '| S/BS | 提示板号与更新板号冲突从原 v3 的 203 次实际接受降为零；BS 通过开发门槛 | BS 已通过独立加权收益和变向场景验收 |','',
      f'BS 独立确认：精确命中 {metrics["baseline"]["target_hit_rate"]*100:.2f}% → {metrics["candidate"]["target_hit_rate"]*100:.2f}%，加权 {metrics["baseline"]["weighted_hit_rate"]*100:.2f}% → {metrics["candidate"]["weighted_hit_rate"]*100:.2f}%；加权收益 95% 区间 −3.69 至 +9.84pp。默认 v3 保留，BS 值得作为隔离开发起点。','',
      '历史批次使用不同种子与部分不同评分定义，不以跨批绝对分数做排名。当前后台支持固定负载双路高吞吐；其确定性不代表 UI/实车效果等价。','',
      '## 当前位置、速度和未来机动的误差预算','',
      f'源数据测量帧 {r["counts"]["measured_frames"]}。严格要求图像身份与三维最近板身份一致，仅保留 {r["counts"]["accepted"]} 帧；额外图像身份敏感性检查保留 {s["counts"]["accepted"]} 帧。后者覆盖更广，但包含三维最近板身份分歧，二者分开保存。','',
      '下表使用较广的独立图像匹配集合。每次只在分析器内替换一个误差分量，保留原预测时域，不进行新的弹道或闭环求解。这是可改善空间，不是可部署算法或新命中率。','',
      '| 场景/阶段 | 帧数/轨迹数 | 原 P95 / m | 真值位置 | 真值径向速度 | 真值横向速度 | 真值位置+速度 |','|---|---:|---:|---:|---:|---:|---:|']
    for g in s['groups']:
        e=g['errors'];lines.append(f'| {g["scene"]}/{g["phase"]} | {g["frames"]}/{g["paths"]} | '+ ' | '.join(f'{e[key]["p95"]:.3f}' for key in ('native','true_position','true_radial_velocity','true_lateral_velocity','true_position_and_velocity'))+' |')
    lines+=['','![误差分量上限](component-ceilings.png)','',
      '跨尚未观测的变向窗口，即使当前状态完全正确，匀速外推仍可能出错；这部分不能通过消除当前测量误差全部解决。变向后已观测窗口中径向速度贡献明显，后段位置偏差也很大。各干预有耦合、可能抵消误差，不能将 P95 降幅相加成归因百分比。','',
      '## 可能的突破口与优先级','',
      '1. **方向自适应的速度估计。** 冻结 BS 径向过程参数为 3、横向为 30。原 v3 机动主要释放横向速度；全局机动证据只跟随当帧第一块观测，换板会打断连续性。优先试验按物理板持续积累径向/横向创新，只对可靠方向释放速度协方差，保持原开火规则。验收同时约束静止径向抖动、变向速度恢复和未来点误差。',
      '2. **共享车体平移速度。** 普通非旋转车辆的可见装甲共享车体平移；当前每块板独立 CV，切板可能切换到另一份速度。可考虑共享平移状态，保留板偏移，旋转时显式加角速度叉乘项。不能直接平均相关噪声或把旋转视为平移。',
      '3. **预测感知的弹道/时序。** 分清捕获年龄、实际发射延迟和飞行时间，避免统一加减一个延迟。超过观测支持的变向窗口，未来预测应表达机动不确定性；先改预测或有限自适应，谨慎使用停火门控，防止重复旧 v4 的吞吐损失。',
      '4. **深度观测联合更新。** 稳态也存在可观的当前位置偏差。多板端点联合解算与带不确定度的共同深度约束有长期价值，但应与速度模型分开消融，防止几何先验有偏却被过度信任。','',
      '不优先继续调角点拒绝阈值、延长三帧保护、扩大确认样本或追求 UI 像素一致。当前更值得花成本的是分离状态估计与未来预测问题。','',
      '## 时序证据与限制','',
      f'匹配 {r["shots_attributed"]} 次实际发射：捕获到发射中位 {1000*r["timing"]["exposure_to_launch_s"]["p50"]:.2f}ms，决策到发射 {1000*r["timing"]["decision_to_launch_s"]["p50"]:.2f}ms，记录接收边界到发射 {1000*r["timing"]["receipt_to_launch_s"]["p50"]:.2f}ms。当前低角速预测附加延迟为 15ms。',
      '这里是以发射前最新已接收有效 advice 作为关联代理，并非独立追踪到物理执行器的硬件延迟；不能直接宣称把 15ms 改成 4ms 就能改善。以 3m/s 估算，11ms 约 3.3cm，通常小于已观测到的米级变向外推错误，但足以影响加权中心分数。','',
      '## 结构性波动','',
      f'测量内同代轨迹切换优先板 {struct["selected_plate_switches"]} 次，相邻帧所选速度差 P95 {struct["switch_velocity_jump_mps"]["p95"]:.3f}m/s。该差值也包含真实加速，不能全部归为错误切板。','',
      '| 场景/阶段 | 多块新鲜板帧数 | 板间速度差 P95 / m/s | 简单平均对未来点改变量 P95 / m |','|---|---:|---:|---:|']
    for g in struct['groups']:lines.append(f'| {g["scene"]}/{g["phase"]} | {g["multiple_fresh_frames"]} | {g["spread_mps"]["p95"]:.3f} | {g["average_future_point_change_m"]["p95"]:.3f} |')
    lines+=['','简单平均只是显示共享状态的影响尺度，尚未评估精度收益。帧有相关性；当前场景只有匀速与 90° 变向，不能据此声称高速旋转、车体移动和实车都已覆盖。','',
      '## 建议下一批实施','',
      '先保留 v3 正式对照、BS 隔离开发基线。只加一个径向机动适应候选；先用现有录像/观测对照误差和静止抖动，进入闭环前冻结新种子。后台双路先做匀速与变向，再加入车体移动和正负旋转的低成本回归。明显改善且独立确认通过之后，才做 UI 特征性复核。','',
      '[严格分析](result.json) · [敏感性分析](sensitivity-result.json) · [结构分析](structure-result.json) · [冻结来源](contract.json) · [证据校验](validation.json)','']
    text='\n'.join(lines);(root/'report.md').write_text(text)
    def inline(value):
        value=html.escape(value);value=re.sub(r'\*\*([^*]+)\*\*',r'<strong>\1</strong>',value)
        return re.sub(r'\[([^\]]+)\]\(([^)]+)\)',r'<a href="\2">\1</a>',value)
    import re
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
    (root/'report.html').write_text('<meta charset="utf-8"><title>开发历史与突破口</title><style>body{font:16px/1.6 sans-serif;max-width:1250px;margin:36px auto;padding:0 20px}table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:7px}img{max-width:100%}</style>'+''.join(parts))


def finish(root):
    c=freeze(root)
    prefix=['docker','run','--rm','--network','none','--cpus','2','--user',f'{os.getuid()}:{os.getgid()}',
        '-e','OPENBLAS_NUM_THREADS=1','-e','MPLCONFIGDIR=/tmp/matplotlib',
        '-v',str(manage.ROOT)+':'+str(manage.ROOT)+':ro','-v',str(root)+':'+str(root),
        '-w',str(manage.ROOT),'--entrypoint','python3',c['evaluator_image'],'-B',str(Path(__file__))]
    for action in ('analyze','sensitivity'):
        with (root/(action+'-final.log')).open('w') as log:
            subprocess.run([*prefix,action,'--output',str(root)],check=True,timeout=60,stdout=log,stderr=subprocess.STDOUT)
    structure(root)
    with (root/'checks.log').open('w') as log:
        subprocess.run(['python3','-B',str(manage.OPS/'prediction_review_checks.py'),'--output',str(root)],
            check=True,timeout=30,stdout=log,stderr=subprocess.STDOUT)
    with (root/'plots.log').open('w') as log:
        subprocess.run([*prefix,'plots','--output',str(root)],check=True,timeout=60,stdout=log,stderr=subprocess.STDOUT)
    report(root)
    files=('contract.json','sensitivity-contract.json','result.json','sensitivity-result.json','structure-result.json',
        'validation.json','sensitivity-validation.json','frame-budget.jsonl','sensitivity-frame-budget.jsonl',
        'shot-timing.jsonl','sensitivity-shot-timing.jsonl','report.md','report.html','component-ceilings.png','checks.log')
    write(root/'report-seal.json',dict(passed=True,source_sha256={p.name:sha(p) for p in manage.OPS.glob('prediction_review*.py')},
        files={name:sha(root/name) for name in files},new_closed_loop_trials=0,algorithm_modified=False))
    from experiments import publish_desktop
    publish_desktop(manage,root)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('freeze','analyze','sensitivity','structure','plots','report','finish'));p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();globals()[a.action](a.output.resolve())
