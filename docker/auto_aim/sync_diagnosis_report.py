#!/usr/bin/env python3
"""Summarize all diagnostic candidates, including regressions and invalid attempts."""
import argparse
from collections import Counter,defaultdict
import hashlib
import html
import json
from pathlib import Path
import statistics
import subprocess

import background_jobs as jobs
import manage
from report import load,write


def percent(value):return '—' if value is None else f'{100*value:.2f}%'


def interval(row,name):
    bounds=row.get('intervals_95',{}).get(name)
    return '—' if bounds is None else f'[{bounds[0]*100:+.2f}, {bounds[1]*100:+.2f}]'


def summarize(root):
    candidates=[];runs=[];attempts=[];branches=[]
    for label,folder in [('阶段边界串行化',root),('完整延迟链',root/'timing-candidate'),
            ('复位后固定静置起点',root/'reset-candidate'),('整数纳秒开火限频',root/'cadence-candidate')]:
        state=load(folder/'diagnosis-state.json')
        if not state or state['state']!='completed':raise ValueError('Complete diagnostic candidate first: '+str(folder))
        entry=dict(label=label,folder=str(folder),state=state);candidates.append(entry)
        for mode,value in state['modes'].items():
            groups=defaultdict(list)
            for r in value['runs']:
                path=manage.OPS/'experiments'/r['run_id'];metrics=load(path/'summary.json')['metrics']
                history=load(path/'synchronization.json')['phase_history']
                origin=next(h['stamp_ns'] for h in history if h['mode']=='settle_setup')
                warm=next(h['stamp_ns'] for h in history if h['mode']=='warm')
                measure=next(h['stamp_ns'] for h in history if h['mode']=='measure')
                row=dict(r,stage=label,mode=mode,metrics=metrics,phase_history=history,
                    warm_relative_to_settle_ns=warm-origin,measure_relative_to_settle_ns=measure-origin)
                runs.append(row);groups[r['scene']].append(row)
            for scene,rr in groups.items():
                counts=Counter((r['metrics']['shots_fired'],round(r['metrics']['target_hit_rate'],10),
                    round(r['metrics']['weighted_hit_rate'],10),r['warm_relative_to_settle_ns'],r['measure_relative_to_settle_ns']) for r in rr)
                branches.append(dict(stage=label,mode=mode,scene=scene,runs=len(rr),signatures=[dict(count=count,
                    shots=k[0],hit_rate=k[1],weighted_rate=k[2],warm_ns=k[3],measure_ns=k[4]) for k,count in counts.items()]))
            with jobs.connect() as db:
                for job_id in value['jobs']:
                    attempts.extend(dict(a,job_id=job_id,stage=label) for a in jobs.status(db,job_id)['attempts'])
    result=dict(version=1,candidates=candidates,runs=runs,attempts=attempts,startup_signatures=branches,
        certified=False,capacity_registered=False,algorithm_changed=False,
        ui_replacement_approved=False,default_execution_mode='realtime',
        raw_trials=len(runs),invalid_attempts=sum(a.get('verdict')=='invalid' for a in attempts),
        interpretation='Timing correctness, deterministic initialization, and UI equivalence are separate checks. No diagnostic stage grants capacity.')
    write(root/'result.json',result)
    lines=['# 后台模式差异诊断与修复','',
        '**本轮未授予 UI 接近或新并发资格。自瞄算法 v3、物理参数和评分公式没有调整。**','',
        '## 已定位的问题','',
        '1. RPC 在控制步确认返回前切换阶段，可能用上一个逻辑时间建立期限。阶段切换已改为由步进线程在确认边界执行。',
        '2. ROS 场景就绪消息的到达时刻仍能移动静置起点。新增可选、带确认的物理场景复位，并从该边界重置控制时序后执行固定静置。',
        '3. `pipeline_ms` 从 JPEG 解码之后开始，不能作为发送至决策的全部延迟。完整校准补入解码等开销、决策后回传，以及自身姿态的采样年龄。','',
        '4. 0.2 秒开火限频使用浮点减法。在严格间隔 200,000,000 纳秒时，不同启动偏移可得到 0.1999999999999993 或 0.20000000000000018 秒，导致放行结果不同。同步分支已改为整数纳秒，实时分支保留原来的 monotonic 秒。','',
        '逐帧证据中，开火脉冲先于算法输出分叉：对照组分别在第 109 / 65 帧出现放行差异，算法观测/输出到第 116 / 68 帧才不同。该先后关系可将这两个样本的首次分叉定位到桥接器限频。',
        '这证明了这些样本的分叉起点；不能把全部 UI 差异都归因于限频。','',
        '延迟参数仅由原 12 轮 UI 校准的时间数据生成，没有用命中成绩挑参数；独立 20 轮旧 UI 参考保持不变。','',
        '| 时序项 | 原固定配置 | 完整校准配置 |','|---|---:|---:|',
        '| 图像发送前延迟 | 38.456297 ms | 38.456297 ms |',
        '| 发送至算法决策 | 6.735815 ms | 9.537230 ms |',
        '| 决策至桥接器接收 | 未单列 | 0.433691 ms |',
        '| 自身姿态在发送时的年龄 | 0 ms | 1.070773 ms |','',
        '这些是分阶段中位数；完整校准中 `pipeline_ms` 漏计的部分中位数为 2.730857 ms。','',
        '## 对原结果的纠正','',
        '原固定时序转向场景的加权提高，主要发生在转向前：静止段旧 UI / 后台约为 53.8% / 85.9%，直行段为 37.8% / 70.6%。',
        '实际转向窗口 3.2–3.7 秒双方加权成绩均为零。发射时间分段不等于撞击时段：飞行中的弹丸可能跨越运动阶段。',
        '原记录时序匀速场景前两秒，跟踪器多数仍处于 tracking，但中心估计偏差约 1.1 米，超过正确跟踪的 0.5 米门槛。',
        '因此正确跟踪比例下降、最长正确跟踪中断增加，不能直接解释为完全没有识别或完全没有目标跟踪。','',
        '## 各候选与旧 UI 的比较','',
        '每格比较使用预先固定的前 5 对独立试次；百分比为汇总值，差区间以百分点计。所有阶段均完整保留，不能混合后挑选成绩。','',
        '| 阶段 | 后台时序 | 场景 | 发数 UI/后台 | 命中率 UI/后台 | 加权 UI/后台 | 加权差 95% 区间 | 总判定 |',
        '|---|---|---|---:|---|---|---|---|']
    for stage in candidates:
        for profile,comparisons in stage['state']['comparisons'].items():
            for scene,r in comparisons['ui'].items():
                a=r.get('baseline',{});b=r.get('candidate',{})
                lines.append(f"| {stage['label']} | {profile} | {scene} | {a.get('shots','—')}/{b.get('shots','—')} | {percent(a.get('target_hit_rate'))}/{percent(b.get('target_hit_rate'))} | {percent(a.get('weighted_hit_rate'))}/{percent(b.get('weighted_hit_rate'))} | {interval(r,'weighted_hit_rate')} | {r['conclusion']} |")
    lines+=['','## 重复与双路对照','',
        '以下只描述已覆盖条件；通过局部重复门槛不代表完整 UI 接近认证。','',
        '| 阶段 | 后台时序 | 比较 | 场景 | 对数 | 精确差区间 | 加权差区间 | 判定 |',
        '|---|---|---|---|---:|---|---|---|']
    for stage in candidates:
        for profile,comparisons in stage['state']['comparisons'].items():
            for kind in ('independent_repeats','one_vs_two'):
                for scene,r in comparisons[kind].items():
                    lines.append(f"| {stage['label']} | {profile} | {kind} | {scene} | {r['pairs']} | {interval(r,'target_hit_rate')} | {interval(r,'weighted_hit_rate')} | {r['conclusion']} |")
    lines+=['','## 启动与结果分支','',
        '| 阶段 | 时序/路数 | 场景 | 轮数 | 结果和相对阶段时间的签名数 |','|---|---|---|---:|---:|']
    for r in branches:lines.append(f"| {r['stage']} | {r['mode']} | {r['scene']} | {r['runs']} | {len(r['signatures'])} |")
    final=candidates[-1]['state']
    stable=[r for c in final['comparisons'].values() for kind in ('independent_repeats','one_vs_two') for r in c[kind].values()]
    result['covered_score_stability_passed']=bool(stable) and all(r['conclusion']=='passed' for r in stable)
    result['covered_score_comparisons']=len(stable)
    write(root/'result.json',result)
    lines[4:4]=[f"已覆盖的 {len(stable)} 项独立重复/单路双路评分比较：{'全部通过原定 ±2 个百分点门槛' if result['covered_score_stability_passed'] else '尚未全部通过'}。UI 接近比较另行判定。",'']
    lines+=['','## 尚未对齐的测量边界','',
        '最终候选已覆盖的独立重复和单路/双路命中评分比较均通过；仍未通过与旧 UI 的接近门槛。',
        '已有证据显示测量启动时的管线历史不同：旧 UI 在图像/推理持续运行时开始测量，部分首发使用测量开始前采集的图像；',
        '当前同步模式清空暖机采样队列并重启采样时序。转向场景 UI 首发平均约 54 ms，同步固定配置为 125 ms。',
        '这个启动边界差异已被记录，其对全部剩余命中偏差的贡献尚未通过单因素闭环实验隔离。下一步应优先对齐该边界，保留独立 UI 参考。',
        'UI 的传感器噪声相位及实际暖机历史也与固定同步条件不同；当前暖机原图未完整归档，不能用测量视频精确重建初始化过程。']
    durations=[r['metrics'].get('total_wall_s') for r in runs];durations=[d for d in durations if d is not None]
    lines+=['','## 验证与范围','',f"- 共 {len(runs)} 轮有效诊断结果；无效尝试 {result['invalid_attempts']} 次，完整保留，不替换得分。",
        f"- 基础结果耗时中位数 {statistics.median(durations):.2f} 秒，最大 {max(durations):.2f} 秒。",
        '- 原 UI、阶段串行化、完整延迟链、显式复位和整数限频分别冻结；不同候选不混算。',
        '- 未放宽 ±2 / ±5 个百分点门槛；未注册同步并发容量；未扩展四路或八路。',
        '- 同步算法帧的自身姿态只来自历史自身 TF，不包含目标真值。真值仅用于事后评分与分段诊断。',
        '- 固定重复验证的是同条件可重现性，不能代替 UI 噪声、时序分布或实机泛化验证。',
        '- 人工完整视频验收尚未完成；图像解码与少量抽帧检查不能代替人工验收。','',
        '## 证据','',
        '- [基线分段表](baseline/diagnosis.md) · [时间线图](baseline/timelines.png) · [原图裁剪](baseline/original-input-crops.png)',
        '- [阶段串行化详细数据](diagnosis.json) · [完整延迟链数据](timing-candidate/diagnosis.json) · [最终候选分段表](cadence-candidate/diagnosis.md)',
        '- [开火浮点边界与首次分叉证据](fire-cadence-diagnosis.json)',
        '- [测量启动边界与首发时间](measurement-boundary.json) · [最终候选时间线](cadence-candidate/timelines.png) · [最终原图裁剪](cadence-candidate/original-input-crops.png)',
        '- [单元检查](unit-checks.json) · [最终协议检查](cadence-candidate/protocol-checks.json) · [证据审计](evidence-audit.json) · [完整结构化汇总](result.json)','',
        '## 录像与原始实验','']
    validation=load(root/'validation.json')
    if validation:
        position=lines.index('## 验证与范围')+2
        lines[position:position]=[
            f"- 最终 {validation['unit_checks']['count']} 项单元检查、{validation['protocol_checks']['count']} 项真实协议检查通过。",
            f"- 独立实时双路回归 {validation['realtime_regression_trials']} 轮通过；默认镜像和自动双路容量保持原配置。",
            f"- 全部 {validation['evidence_audit']['runs']} 轮证据审计通过，解码 {validation['evidence_audit']['decoded_jpegs']} 帧原图、{validation['evidence_audit']['decoded_mp4_frames']} 帧录像。",
            '- [最终验证汇总](validation.json) · [实时回归](realtime-regression.json) · [实时证据审计](realtime-evidence-audit.json)']
    for r in runs:
        lines.append(f"- {r['stage']} / {r['mode']} / {r['scene']} / repeat {r['repeat_id']}：[报告](../{r['run_id']}/report.html)")
    text='\n'.join(lines)+'\n';(root/'report.md').write_text(text)
    # Keep a compact HTML table alongside the complete machine-readable evidence.
    body=[];in_table=False;in_list=False
    def inline(line):
        import re
        value=html.escape(line)
        value=re.sub(r'\[([^]]+)\]\(([^)]+)\)',r'<a href="\2">\1</a>',value)
        value=re.sub(r'\*\*([^*]+)\*\*',r'<strong>\1</strong>',value)
        value=re.sub(r'`([^`]+)`',r'<code>\1</code>',value)
        return value
    for line in lines:
        if line.startswith('|'):
            if line.startswith('|---'):continue
            if not in_table:body.append('<table>');in_table=True
            body.append('<tr>'+''.join('<td>'+inline(c.strip())+'</td>' for c in line.strip('|').split('|'))+'</tr>');continue
        if in_table:body.append('</table>');in_table=False
        if line.startswith('- '):
            if not in_list:body.append('<ul>');in_list=True
            body.append('<li>'+inline(line[2:])+'</li>');continue
        if in_list:body.append('</ul>');in_list=False
        if line.startswith('# '):body.append('<h1>'+inline(line[2:])+'</h1>')
        elif line.startswith('## '):body.append('<h2>'+inline(line[3:])+'</h2>')
        elif line:body.append('<p>'+inline(line)+'</p>')
    if in_table:body.append('</table>')
    if in_list:body.append('</ul>')
    (root/'report.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>后台模式差异诊断</title>'
        '<style>body{font:15px system-ui;max-width:1500px;margin:2em auto;padding:0 1em;color:#172336}table{border-collapse:collapse;font-size:13px;width:100%}td{border:1px solid #dbe2eb;padding:7px}tr:first-child{background:#eaf0f7;font-weight:bold}a{color:#1457a5}h2{margin-top:2em}</style>'+
        ''.join(body)+'</html>')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('root',type=Path);args=p.parse_args()
    result=summarize(args.root.resolve());print(json.dumps(dict(runs=result['raw_trials'],certified=result['certified'])))
