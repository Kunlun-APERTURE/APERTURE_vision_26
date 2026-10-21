#!/usr/bin/env python3
"""Advice-gated 5 Hz firing stability and aim-point step audit (offline only)."""
import argparse
import bisect
import collections
import csv
import hashlib
import html
import math
from pathlib import Path
import subprocess

import numpy as np
from diagnostics import analyze, jsonl, load, quantile, table, wrap, write
from gimbal_compare import controller_metrics
from moving_fire import firing_timing, miss_geometry, stability, statistics

SCENES=('move_ref','line','ego_sweep','ego_sweep_reverse')


def point_steps(folder,shots,output):
    observations=[p['data'] for p in jsonl(folder/'telemetry.jsonl') if p['kind']=='observation']
    observations.sort(key=lambda r:r['frame']['stamp_ns'])
    rows=[]; previous=None; last_valid=None
    with (output/'frames.csv').open() as stream:
        plate_by_seq={int(r['seq']):r.get('selected_plate_index') for r in csv.DictReader(stream)}
    start=load(folder/'run-state.json')['measurement_start_stamp_ns']
    for obs in observations:
        f,r=obs['frame'],obs['result']
        if not obs.get('command_emitted') or not r.get('valid') or r.get('aim_point') is None:continue
        aim=np.array(r['aim_point'])+np.array(f['gimbal_position_map'])
        row={'seq':f['seq'],'stamp_ns':f['stamp_ns'],'receipt_ns':obs['receipt_ns'],
            'time_s':(f['stamp_ns']-start)/1e9,'yaw_rad':r['yaw'],'pitch_rad':r['pitch'],
            'tracker':r.get('tracker'),'fire_advice':r.get('fire_advice'),
            'aim_world':aim.tolist(),'point_jump':False,'reacquisition':False,
            'selected_plate_index':plate_by_seq.get(f['seq']),
            'estimated_yaw_rate':(r.get('track_estimate') or {}).get('yaw_rate')}
        if previous:
            row['plate_index_changed']=(row['selected_plate_index'] is not None and previous['selected_plate_index'] is not None
                and row['selected_plate_index']!=previous['selected_plate_index'])
            row['tracker_state_changed']=row['tracker']!=previous['tracker']
            dt=(f['stamp_ns']-previous['stamp_ns'])/1e9
            row['dt_s']=dt
            if .015<=dt<=.1:
                dy=wrap(r['yaw']-previous['yaw_rad']);dp=r['pitch']-previous['pitch_rad']
                displacement=aim-np.array(previous['aim_world'])
                row['angular_step_deg']=math.degrees(math.hypot(dy,dp))
                row['point_xy_step_cm']=float(np.linalg.norm(displacement[:2])*100)
                if previous.get('_velocity') is not None:
                    residual=displacement-previous['_velocity']*dt
                    angular_residual=math.hypot(wrap(dy-previous['_yaw_rate']*dt),dp-previous['_pitch_rate']*dt)
                    row['angular_step_residual_deg']=math.degrees(angular_residual)
                    row['point_xy_step_residual_cm']=float(np.linalg.norm(residual[:2])*100)
                    row['point_lateral_residual_cm']=float(np.dot(residual[:2],[-math.sin(r['yaw']),math.cos(r['yaw'])])*100)
                    row['point_radial_residual_cm']=float(np.dot(residual[:2],[math.cos(r['yaw']),math.sin(r['yaw'])])*100)
                    row['point_jump']=row['angular_step_residual_deg']>.5 or row['point_xy_step_residual_cm']>5
                row['_velocity']=displacement/dt;row['_yaw_rate']=dy/dt;row['_pitch_rate']=dp/dt
            elif dt>.1:row['reacquisition']=True
        rows.append(row);previous=row
    rows.sort(key=lambda r:r['receipt_ns'])
    receipts=[r['receipt_ns'] for r in rows]
    for shot in shots:
        stamp=shot['fire_stamp_ns']
        lo=bisect.bisect_left(receipts,stamp-300_000_000);hi=bisect.bisect_right(receipts,stamp)
        window=rows[lo:hi]
        shot['point_jump_prev_300ms']=any(r['point_jump'] for r in window)
        shot['angular_jump_prev_300ms']=any(r.get('angular_step_residual_deg',0)>.5 for r in window)
        shot['lateral_point_jump_prev_300ms']=any(abs(r.get('point_lateral_residual_cm',0))>5 for r in window)
        for threshold in (.5,1,2):
            shot[f'angular_jump_gt_{threshold:g}_deg_prev_300ms']=any(r.get('angular_step_residual_deg',0)>threshold for r in window)
        shot['reacquisition_prev_300ms']=any(r['reacquisition'] for r in window)
        shot['max_angular_step_residual_prev_300ms_deg']=max((r.get('angular_step_residual_deg',0) for r in window),default=None)
        shot['max_point_xy_residual_prev_300ms_cm']=max((r.get('point_xy_step_residual_cm',0) for r in window),default=None)
        if hi:
            shot['last_valid_point_age_ms']=(stamp-receipts[hi-1])/1e6
    clean=[{k:v for k,v in r.items() if not k.startswith('_')} for r in rows]
    table(output/'point-steps.csv',clean)
    measured=[r for r in rows if 0<=r['time_s']<=load(folder/'manifest.json')['duration_s']]
    angular_large=[r for r in measured if r.get('angular_step_residual_deg',0)>1]
    return {'valid_command_frames':len(measured),'point_jump_frames':sum(r['point_jump'] for r in measured),
        'reacquisitions':sum(r['reacquisition'] for r in measured),
        'angular_gt_1deg_frames':len(angular_large),
        'angular_gt_1deg_with_model_index_change':sum(r.get('plate_index_changed',False) for r in angular_large),
        'angular_gt_1deg_with_tracker_state_change':sum(r.get('tracker_state_changed',False) for r in angular_large),
        'angular_residual_p95_deg':quantile([r.get('angular_step_residual_deg') for r in measured],.95)}


def prepare(folder,output,reuse=False):
    cache=output/folder.name
    if reuse and (cache/'summary.json').exists():
        result,shots=load(cache/'summary.json'),load(cache/'shots.json')
        for name,expected in result['source_sha256'].items():
            if hashlib.sha256((folder/name).read_bytes()).hexdigest()!=expected:raise RuntimeError('Changed evidence '+str(folder/name))
    else:result,_,shots=analyze(folder,cache)
    result['controller']=controller_metrics(folder,shots)
    result['timing_audit']=firing_timing(folder,shots)
    result['point_steps']=point_steps(folder,shots,cache)
    miss_geometry(folder,shots)
    plot_steps(cache,shots,result['duration_s'])
    result['stability']=stability(shots,result['duration_s'])
    extra=jump_clip(folder,cache,shots)
    result['clips']=[c for c in result['clips'] if c['name']!='jump_near_miss']+extra
    summary=load(folder/'summary.json')
    result['tracking_fraction']=summary['metrics']['correct_tracking_fraction']
    result['all_rules_passed']=all(r['passed'] for r in summary['rules'])
    write(cache/'summary.json',result);write(cache/'shots.json',shots);table(cache/'shots.csv',shots)
    return result,shots


def jump_clip(folder,output,shots):
    candidates=[s for s in shots if not s['front_hit'] and s['angular_jump_prev_300ms']]
    if not candidates:return []
    shot=max(candidates,key=lambda s:(not s['target_hit'],s['max_angular_step_residual_prev_300ms_deg'] or 0))
    indices=list(jsonl(folder/'frames.jsonl'));stamps=[r['stamp_ns'] for r in indices]
    first=max(0,bisect.bisect_left(stamps,shot['fire_stamp_ns']-800_000_000))
    last=min(len(indices)-1,bisect.bisect_right(stamps,shot['fire_stamp_ns']+1_200_000_000))
    if first>=last:return []
    name=f"jump_near_miss_shot_{shot['shot_seq']}.mp4"
    a,b=indices[first]['video_index'],indices[last]['video_index']
    if not (output/name).exists():
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-ss',str(a/30),'-i',str(folder/'annotated.avi'),
            '-t',str((b-a+1)/30),'-an','-c:v','libx264','-threads','2','-preset','ultrafast','-crf','24','-pix_fmt','yuv420p',str(output/name)],check=True)
    result={'name':'jump_near_miss','file':name,'shot_seq':shot['shot_seq'],'fire_time_s':shot['fire_time_s'],
        'first_stamp_ns':stamps[first],'last_stamp_ns':stamps[last],
        'first_video_frame':a,'last_video_frame':b,'source_fps':30}
    write(output/'jump-clip.json',result)
    return [result]


def plot_steps(output,shots,duration):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    with (output/'point-steps.csv').open() as stream:
        rows=[r for r in csv.DictReader(stream) if 0<=float(r['time_s'])<=duration]
    fig,axes=plt.subplots(3,1,figsize=(12,8),sharex=True)
    for axis,field,label,threshold in [(axes[0],'angular_step_residual_deg','Command angle residual (deg)',.5),
                                            (axes[1],'point_xy_step_residual_cm','Aim point XY residual (cm)',5)]:
        selected=[r for r in rows if r.get(field)]
        axis.plot([float(r['time_s']) for r in selected],[float(r[field]) for r in selected],linewidth=.8)
        axis.axhline(threshold,color='orange',linestyle='--');axis.set_ylabel(label);axis.grid(alpha=.2)
    for field,label,color in [('point_radial_residual_cm','Radial absolute','grey'),('point_lateral_residual_cm','Lateral absolute','purple')]:
        selected=[r for r in rows if r.get(field)]
        axes[1].plot([float(r['time_s']) for r in selected],[abs(float(r[field])) for r in selected],linewidth=.6,alpha=.65,color=color,label=label)
    axes[1].legend(fontsize=8)
    for s in shots:
        color='green' if s['front_hit'] else 'orange' if s['target_hit'] else 'red'
        axes[2].scatter(s['fire_time_s'],1 if s['target_hit'] else 0,color=color,marker='x' if s['point_jump_prev_300ms'] else 'o',s=30)
    axes[2].set_yticks([0,1],['Miss','Target hit']);axes[2].set_xlabel('Scenario time (s); x = point jump in previous 300 ms; orange = non-front target hit')
    axes[2].grid(alpha=.2);axes[2].set_xlim(0,duration)
    fig.suptitle(output.name,fontsize=9);fig.tight_layout();fig.savefig(output/'point-steps.png',dpi=140);plt.close(fig)


def spacings(shots):
    by_run=collections.defaultdict(list)
    for s in shots:by_run[s['run_id']].append(s['fire_time_s'])
    return [b-a for values in by_run.values() for a,b in zip(sorted(values),sorted(values)[1:])]


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--batch',type=Path,nargs='+',required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--reuse',action='store_true')
    args=p.parse_args();args.output.mkdir(exist_ok=True,parents=True)
    records=[]
    for path in args.batch:
        batch=load(path)
        if not batch['complete']:raise RuntimeError('Incomplete batch '+str(path))
        records.extend(path.parent/r['run_id'] for r in batch['runs'])
    counts=collections.Counter((load(f/'manifest.json')['scene'],load(f/'manifest.json')['shooting']['physics']) for f in records)
    if counts!=collections.Counter({(s,physics):3 for s in SCENES for physics in ('no-drag','drag')}):
        raise RuntimeError('Expected three independent runs per scene/physics')
    runs=[];shots=[];signatures=collections.defaultdict(set)
    for folder in records:
        manifest=load(folder/'manifest.json');setting=manifest['shooting']
        if setting['fire_policy']!='advice' or setting['fire_rate_limit_hz']!=5 or manifest['gimbal_profile']!='feedforward':
            raise RuntimeError('Unexpected experiment condition')
        for key,value in [('algorithm_binary',manifest['algorithm_binary_sha256']),
                          ('algorithm_yaml',manifest['configuration_sha256']['algorithm.yaml']),
                          ('weights',str(sorted(manifest['algorithm_weights_sha256'].items()))),
                          ('simulator_image',manifest['containers']['simulator_image_id'])]:signatures[key].add(value)
        r,s=prepare(folder,args.output,args.reuse)
        if not r['all_rules_passed']:raise RuntimeError('Invalid/failed run '+folder.name)
        runs.append(r);shots.extend(s);print('Analyzed',folder.name,flush=True)
    if any(len(v)!=1 for v in signatures.values()):raise RuntimeError('Uncontrolled algorithm/image difference')
    groups={}
    for scene in SCENES:
        for physics in ('no-drag','drag'):
            rr=[r for r in runs if r['scene']==scene and r['physics']==physics]
            ss=[s for s in shots if s['scene']==scene and s['physics']==physics]
            seconds=sum(r['duration_s'] for r in rr)
            steady=[s for s in ss if s['phase']=='steady']
            groups[scene+'/'+physics]={**statistics(ss,seconds),
                'runs':len(rr),'longest_miss_streak':max(r['stability']['longest_target_miss_streak'] for r in rr),
                'longest_front_miss_streak':max(r['stability']['longest_front_miss_streak'] for r in rr),
                'minimum_launch_spacing_s':min(spacings(ss),default=None),
                'median_launch_spacing_s':quantile(spacings(ss)),
                'accepted_angle_samples':sum(s.get('accepted_command_yaw_error_deg') is not None for s in ss),
                'request_angle_samples':sum(s.get('request_yaw_error_deg') is not None for s in ss),
                'request_yaw_abs_median_deg':quantile([abs(s['request_yaw_error_deg']) for s in ss if s.get('request_yaw_error_deg') is not None]),
                'request_yaw_abs_p95_deg':quantile([abs(s['request_yaw_error_deg']) for s in ss if s.get('request_yaw_error_deg') is not None],.95),
                'request_pitch_abs_p95_deg':quantile([abs(s['request_pitch_error_deg']) for s in ss if s.get('request_pitch_error_deg') is not None],.95),
                'tracking_fraction_median':quantile([r['tracking_fraction'] for r in rr]),
                'front_hit_rate_run_range':[min(r['front_hits']/r['shots'] for r in rr if r['shots']),max(r['front_hits']/r['shots'] for r in rr if r['shots'])],
                'steady':statistics(steady,(13 if scene=='line' else 7.5)*len(rr)) if scene!='move_ref' else None,
                'near_point_jump':statistics([s for s in ss if s['point_jump_prev_300ms']],None),
                'without_recent_point_jump':statistics([s for s in ss if not s['point_jump_prev_300ms']],None),
                'steady_near_jump':statistics([s for s in steady if s['point_jump_prev_300ms']],None),
                'steady_without_jump':statistics([s for s in steady if not s['point_jump_prev_300ms']],None),
                'angular_jump_sensitivity':{str(t):{
                    'near':statistics([s for s in ss if s[f'angular_jump_gt_{t:g}_deg_prev_300ms']],None),
                    'away':statistics([s for s in ss if not s[f'angular_jump_gt_{t:g}_deg_prev_300ms']],None),
                    'steady_near':statistics([s for s in steady if s[f'angular_jump_gt_{t:g}_deg_prev_300ms']],None),
                    'steady_away':statistics([s for s in steady if not s[f'angular_jump_gt_{t:g}_deg_prev_300ms']],None)} for t in (.5,1,2)}}
    result={'runs':runs,'groups':groups,'signatures':{k:list(v) for k,v in signatures.items()},
        'policy':'advice; bridge request spacing >=0.2 s; simulator independently enforces >=0.2 s launch spacing',
        'limits':['Point-step correlation does not prove a causal mechanism.',
            'Selected plate indices belong to the estimated model; an index change alone does not prove a physical armor switch.',
            'Jump definition: residual relative to the preceding command rate exceeds 0.5 deg or 5 cm in world XY; valid adjacent frames 15..100 ms; 300 ms pre-launch exposure.',
            'Missing tracking remains in overall results. Steady motion is reported separately. No new stability gate added.',
            'Visual review pending; no formal baseline selected.']}
    low_error_misses=[s for s in shots if not s['target_hit'] and s.get('request_yaw_error_deg') is not None
        and s.get('request_pitch_error_deg') is not None and abs(s['request_yaw_error_deg'])<.1 and abs(s['request_pitch_error_deg'])<.1]
    result['low_execution_error_miss_examples']=[{k:s.get(k) for k in ('run_id','scene','physics','shot_seq','fire_time_s','outcome',
        'request_yaw_error_deg','request_pitch_error_deg','matched_pulse_to_launch_ms','latest_raw_fire_advice',
        'max_angular_step_residual_prev_300ms_deg','miss_plane_lateral_cm','miss_plane_vertical_cm')} for s in sorted(low_error_misses,key=lambda s:(s['scene']!='move_ref',s['run_id'],s['shot_seq']))[:12]]
    write(args.output/'comparison.json',result);table(args.output/'all-shots.csv',shots)
    render(args.output,result)


def f(v):return 'n/a' if v is None else f'{v:.3f}'


def render(output,data):
    all_shots=[s for r in data['runs'] for s in load(output/r['run_id']/'shots.json')]
    outside=[s for s in all_shots if abs(s.get('request_yaw_error_deg',0))>2]
    data['firing_gate_audit']={'request_yaw_error_gt_2deg':len(outside),
        'target_misses':sum(not s['target_hit'] for s in outside),
        'front_misses':sum(not s['front_hit'] for s in outside),
        'source':'sp_vision_25/tasks/auto_aim/shooter.cpp:32-34',
        'condition':'image-time yaw compared to previous command; new command step allowed up to twice configured tolerance; no pitch-error condition'}
    write(output/'comparison.json',data)
    lines=['# Advice 5 Hz 射击稳定性与打击点突变','',
        '最新用户要求：保留 advice 开火，上限 5 Hz，即请求间隔和实际出膛间隔均不小于 0.2 秒。advice 变为 false 时不新增请求，已有挂起请求仍沿用原执行语义。',
        '算法二进制、模型、YAML、仅前馈云台参数、弹道及 v3 命中判定保持原样。模拟器只新增可配置发射限频，机械冷却按实际时间持续计时。',
        '24 次正式实验：静止 6 秒、目标平移 18 秒、己方底盘往返平移 18 秒及反向起步 18 秒；各三次、分别有阻力和无阻力。单次完整流程限时 40 秒。',
        '己方移动 2–6 / 6–10 / 10–14 秒以名义 0.5 m/s 往返；主要稳定窗口 3–5.5 / 7–9.5 / 11–13.5 秒。反向起步采用已知初始己方朝向 −156°。','',
        '## 实际结果','',
        '| 场景 | 阻力 | 目标任意装甲 | 正面装甲 | 发射 Hz | 最小间隔 s | 最长连续未命中目标 | 稳定阶段正面 |',
        '|---|---|---:|---:|---:|---:|---:|---:|']
    for key,g in data['groups'].items():
        scene,physics=key.split('/');steady=g['steady'] or {}
        lines.append(f"| {scene} | {physics} | {g['target_hits']}/{g['shots']} | {g['front_hits']}/{g['shots']} | {f(g['firing_hz'])} | {f(g['minimum_launch_spacing_s'])} | {g['longest_miss_streak']} | {steady.get('front_hits',0)}/{steady.get('shots',0)} |")
    lines+=['','## 发射时的指令执行','',
        '按发射前最近且不重复的原始开火请求匹配角度。己方移动时先减去弹丸继承的底盘速度，再计算实际枪口方向。此误差描述触发请求与实际枪口之间的差，不是目标真值误差。',
        '出膛距请求约 1–2 ms，而控制遥测间隔约 10 ms；遥测可能仍显示上一条指令，因此不将最近遥测的角度当作本发当前指令。相关遥测字段保留在 CSV，供复核。','',
        '| 场景/阻力 | 请求匹配发数 | yaw 绝对误差中位数/P95 | pitch 绝对误差 P95 | 请求到出膛中位数 ms | 最新建议 false |',
        '|---|---:|---:|---:|---:|---:|']
    for key,g in data['groups'].items():
        lines.append(f"| {key} | {g['request_angle_samples']}/{g['shots']} | {f(g['request_yaw_abs_median_deg'])}/{f(g['request_yaw_abs_p95_deg'])}° | {f(g['request_pitch_abs_p95_deg'])}° | {f(g['pulse_to_launch_median_ms'])} | {g['latest_advice_false']} |")
    gate=data['firing_gate_audit']
    lines+=['','### 开火门限源码复核','',
        '`Shooter::shoot` 当前比较图像时刻 yaw 与 `last_command_.yaw`，并允许本次与上次 yaw 指令变化小于两倍容差；没有 pitch 误差检查，yaw 差值也未做环绕归一化。当前远距离容差为 2°。这是现有逻辑，本轮未修改。',
        f"本轮实际枪口相对触发请求 yaw 误差 >2° 的弹丸有 {gate['request_yaw_error_gt_2deg']} 发，其中 {gate['front_misses']} 发未命中正面装甲、{gate['target_misses']} 发未命中任意目标装甲；这支持下一步单独评估对当前指令与双轴误差的开火门限，不等于新门限已通过闭环实验。"]
    forensic_path=output/'static-miss-forensic.json'
    if forensic_path.exists():
        example=load(forensic_path)
        lines += [f"静止有阻力样本 #{example['shot_seq']}：请求射线在当前正面装甲 X 截面的横偏约 {example['request_ray_y_error_at_current_front_x_cm']:.2f} cm，实际枪口射线约 {example['actual_bore_ray_y_error_at_current_front_x_cm']:.2f} cm；实际飞行穿越装甲平面横偏 {example['observed_miss_plane_lateral_cm']:.2f} cm。出膛在请求后 {example['pulse_to_launch_ms']:.2f} ms，当前请求与枪口相差约 {abs(example['request_yaw_error_deg']):.2f}°。这是已发生样本的几何诊断，不是等待后重新发射的命中结果。"]
    lines+=['','### 相对触发请求角度误差较小的未命中样本','',
        '以下样本在 yaw、pitch 上均相对原始开火请求小于 0.1°。枪口相对请求误差小，也不能保证请求指向正确打击点。','',
        '| 场景/物理/发号 | 出膛时刻 | yaw/pitch 误差 ° | 请求距出膛 ms | 正面平面横/竖偏差 cm |',
        '|---|---:|---:|---:|---:|']
    for s in data['low_execution_error_miss_examples']:
        lines.append(f"| {s['scene']}/{s['physics']}/#{s['shot_seq']} | {f(s['fire_time_s'])} | {f(s['request_yaw_error_deg'])}/{f(s['request_pitch_error_deg'])} | {f(s['matched_pulse_to_launch_ms'])} | {f(s['miss_plane_lateral_cm'])}/{f(s['miss_plane_vertical_cm'])} |")
    lines+=['','## 打击点突变与命中相关性','',
        '以连续有效指令的前一帧变化率外推下一帧，残差超过 0.5° 或世界 XY 平面 5 cm 记为突变；间隔超过 100 ms 单列为重捕获，不混作连续突变。按发射前 300 ms 是否出现突变分组。',
        '此处仅分析关联，未修改开火门限；同一次突变可能覆盖多发，弹丸之间并非独立样本。','',
        '| 场景/阻力 | 近突变目标命中 | 无近期突变目标命中 | 稳定移动近突变正面 | 稳定移动无突变正面 |',
        '|---|---:|---:|---:|---:|']
    for key,g in data['groups'].items():
        a,b,c,d=(g[k] for k in ('near_point_jump','without_recent_point_jump','steady_near_jump','steady_without_jump'))
        lines.append(f"| {key} | {a['target_hits']}/{a['shots']} | {b['target_hits']}/{b['shots']} | {c['front_hits']}/{c['shots']} | {d['front_hits']}/{d['shots']} |")
    lines+=['','### 指令角突变的独立统计','',
        '世界点 XY 残差另拆分为视线径向/横向残差，见 point-steps.csv。下表只按指令角残差判断，避免径向估计抖动主导分类；三种阈值均完整报告，不事后选择最有利阈值。','',
        '| 场景/阻力 | 角残差阈值 | 附近目标命中 | 无近期角突变目标命中 | 稳定移动附近正面 | 稳定移动无近期角突变正面 |',
        '|---|---:|---:|---:|---:|---:|']
    for key,g in data['groups'].items():
        for threshold,stats in g['angular_jump_sensitivity'].items():
            a,b,c,d=(stats[k] for k in ('near','away','steady_near','steady_away'))
            lines.append(f"| {key} | {threshold}° | {a['target_hits']}/{a['shots']} | {b['target_hits']}/{b['shots']} | {c['front_hits']}/{c['shots']} | {d['front_hits']}/{d['shots']} |")
    lines+=['','## 每轮逐发序列','', 'H=目标任意装甲命中，M=未命中。逐秒分布、实际间隔、角误差、原始建议及突变窗口见 JSON/CSV。','']
    for r in data['runs']:
        lines.append(f"- {r['run_id']}：`{r['stability']['sequence_target']}`；[逐发诊断]({r['run_id']}/shots.csv) · [打击点突变]({r['run_id']}/point-steps.csv) · [原始完整录像](../{r['run_id']}/report.html)")
    lines+=['','所有结果仍待人工视觉验收；未选择正式基线。此前独立 2 Hz 连发记录保留在原目录，不混入本轮。']
    (output/'report.md').write_text('\n'.join(lines)+'\n')
    brief=['# Advice 5 Hz 实验摘要','',
        '发射仍受 advice 控制；请求与实际出膛均限制至少间隔 0.2 秒。24 次正式重复，结果尚待人工视觉验收。','',
        '| 场景 | 无阻力稳定段正面 | 有阻力稳定段正面 |','|---|---:|---:|']
    for scene in SCENES:
        cells=[]
        for physics in ('no-drag','drag'):
            g=data['groups'][scene+'/'+physics];s=g['steady'] or g
            cells.append(f"{s['front_hits']}/{s['shots']} ({s['front_hits']/s['shots']:.1%})" if s['shots'] else '无样本')
        brief.append(f'| {scene} | '+ ' | '.join(cells)+' |')
    near=[g['angular_jump_sensitivity']['1']['steady_near'] for g in data['groups'].values()]
    away=[g['angular_jump_sensitivity']['1']['steady_away'] for g in data['groups'].values()]
    def ratio(rows):
        n=sum(r['shots'] for r in rows);h=sum(r['front_hits'] for r in rows)
        return f'{h}/{n} ({h/n:.1%})' if n else '无样本'
    brief+=['',f'稳定移动样本中，发射前 300 ms 出现 >1° 指令角残差时正面命中 {ratio(near)}；无近期该突变时为 {ratio(away)}。这是相关性证据，不证明唯一原因。',
        f"本轮还有 {gate['request_yaw_error_gt_2deg']} 发相对触发请求的实际 yaw 误差 >2°，其中 {gate['front_misses']} 发未命中正面装甲。源码用上一条 yaw 指令判断跟随，且没有 pitch 误差检查。", 
        '下一步优先单独评估当前指令与双轴误差的开火门限，同时复核打击点/位姿估计连续性与跟踪重置；本轮未修改该 advice 门限。','',
        '[完整报告](report.html) · [逐发数据](all-shots.csv)']
    (output/'summary.md').write_text('\n'.join(brief)+'\n')
    body='<html lang="zh-CN"><meta charset="UTF-8"><title>Advice 5 Hz 稳定性</title><style>body{font:16px sans-serif;max-width:1200px;margin:30px auto}pre{white-space:pre-wrap}img,video{max-width:100%}</style><h1>Advice 5 Hz 稳定性</h1><p><a href="report.md">完整报告</a> · <a href="all-shots.csv">所有逐发数据</a></p><pre>'+html.escape('\n'.join(lines))+'</pre>'
    for r in data['runs']:
        name=r['run_id'];body+=f'<h2>{html.escape(name)}</h2><a href="../{name}/report.html">原始完整录像</a><p><img loading="lazy" src="{name}/point-steps.png"></p><p><img loading="lazy" src="{name}/diagnosis.png"></p>'
        for clip in r['clips']:
            body+=f'<p>{html.escape(clip["name"])} · shot {clip["shot_seq"]}</p><video controls preload="none" src="{name}/{clip["file"]}"></video>'
    (output/'report.html').write_text(body+'</html>')


if __name__=='__main__':main()
