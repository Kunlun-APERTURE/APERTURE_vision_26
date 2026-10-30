"""Keep all candidate stages and failures in the final fidelity report."""
import argparse
import html
import json
from pathlib import Path
import re
import statistics

import experiments
import manage
from report import load,write
from sync_continuity import rows


def document(lines):
    def inline(text):
        text=html.escape(text)
        text=re.sub(r'\[([^]]+)\]\(([^)]+)\)',r'<a href="\2">\1</a>',text)
        return re.sub(r'`([^`]+)`',r'<code>\1</code>',text)
    parts=[];table=False
    for line in lines:
        if line.startswith('|'):
            if line.startswith('|---'):continue
            if not table:parts.append('<table>');table=True
            parts.append('<tr>'+''.join('<td>'+inline(c.strip())+'</td>' for c in line.strip('|').split('|'))+'</tr>')
            continue
        if table:parts.append('</table>');table=False
        if line.startswith('# '):parts.append('<h1>'+inline(line[2:])+'</h1>')
        elif line.startswith('## '):parts.append('<h2>'+inline(line[3:])+'</h2>')
        elif line:parts.append('<p>'+inline(line)+'</p>')
    if table:parts.append('</table>')
    return '<!doctype html><meta charset="utf-8"><title>仿真实验可信度修复</title><style>body{font:15px system-ui;max-width:1250px;margin:2em auto;line-height:1.6}td{border:1px solid #aaa;padding:.4em}table{border-collapse:collapse}img{max-width:100%}</style>'+''.join(parts)


def report(root):
    primary=load(root/'result.json');receipt=load(root/'receipt-candidate/result.json')
    diagnostic=load(root/'final-review/diagnosis.json');validation=load(root/'validation.json')
    stages=[dict(stage='pipeline',**t) for t in primary['trials']]+[dict(stage='receipt',**t) for t in receipt['trials']]
    aggregates=primary['aggregates']+receipt['aggregates']
    look={(r['mode'],r['scene']):r for r in aggregates}
    delta=[]
    for scene in ('hf_uniform_3','hf_turn90_3'):
        before=look['ui-fixed-v4_p1',scene];after=look['ui-fixed-v6_p1',scene];ui=look['instrumented_ui',scene]
        delta.append(dict(scene=scene,before=before,after=after,ui=ui,
            weighted_delta=after['weighted']-before['weighted'],
            ui_residual={k:after[k]-ui[k] for k in ('exact','weighted','correct_tracking')},
            first_pulse_ui_gap_ms=after['first_pulse_ms']-ui['first_pulse_ms']))
    value=dict(version=1,algorithm_profile='tracking_fire_v3',final_timing_profile='ui-fixed-v6',
        stages=stages,aggregates=aggregates,differences=delta,validation=validation,
        certified=False,capacity_registered=False,human_visual_acceptance='not_completed',
        limitations=['Three paired runs diagnose direction; fewer than five pairs and fewer than 30 candidate shots per scene.',
            'Full bridge-session evidence does not export YOLO cache inherited from an earlier process session.',
            'Timing v6 is not algorithm v6. Algorithm, physics and scoring were not tuned.',
            'Only single-lane uniform/turn fidelity was evaluated; no new rotation, ego motion, 2/4/8-lane fidelity qualification.'])
    write(root/'final-result.json',value)
    lines=['# 仿真实验可信度修复与验收','',
        '结论：阶段切换与证据完整性已修复；当前后台候选仍不能作为 UI 等效的最终验收环境。最终候选是时序 ui-fixed-v6，自瞄算法仍为 tracking_fire_v3。','',
        '## 本轮修复','',
        '1. 暖机结束后保留待处理图像、控制/采样节拍和时序索引，正式测量不再清空管线。',
        '2. 在最后一帧暖机结果的逻辑接收与命令确认之后再停车，同时继续采样，避免把未来的结果按测量前状态判定。',
        '3. 完整归档新会话内每张原始 JPEG、发送头、算法结果、开火放行及阶段边界，暖机不计分。归档溢出、缺帧、哈希错误或阶段因果错误会明确判为无效。',
        '4. 实时实验支持 --session-evidence on；同步实验自动启用，不能关闭。实时全会话归档默认只分配单路，未继承普通采集任务的容量资格。','',
        '## 最终候选效果','',
        '各场景新 UI 和最终候选各 3 轮；旧后台对照本轮各 1 轮，分数与前轮冻结版本完全一致。此表只描述样本，不代表统计接近验收通过。','',
        '| 场景 | 模式 | 发数 | 精确命中 | 加权命中 | 正确跟踪 | 首开火脉冲 ms |',
        '|---|---|---:|---:|---:|---:|---:|']
    for scene in ('hf_uniform_3','hf_turn90_3'):
        for mode,title in (('instrumented_ui','原 UI＋被动归档'),('ui-fixed-v4_p1','旧后台 v4'),('ui-fixed-v6_p1','最终后台 v6')):
            r=look[mode,scene]
            lines.append(f"| {scene} | {title} | {r['shots']} | {r['exact']:.2%} | {r['weighted']:.2%} | {r['correct_tracking']:.2%} | {r['first_pulse_ms']:.2f} |")
    lines+=['',
        '匀速加权成绩由 41.11% 变为 31.48%，更靠近本轮 UI 的 29.10%；转向由 13.51% 提高至 21.11%，仍低于 UI 的 29.06%。',
        '转向正确跟踪比例从旧后台 34.76% 降至最终候选 26.22%，UI 为 61.64%；首开火脉冲仍明显晚于 UI。不能据加权成绩单项变化宣称仿真效果已经达标。',
        '正确跟踪要求 tracking、目标关联正确且中心 XY 误差不超过 0.5 米；它下降不等于每帧都未检测到目标。首脉冲也不同于弹丸实际出膛时刻。','',
        '## 可信度证据','',
        '两个场景最终候选各三次的精确/加权命中分数一致。所有候选前 30 张暖机图像与同场景对照逐字节相同；变化从阶段边界之后开始。',
        '最终候选的边界前后队列和时序索引一致；最后暖机逻辑接收不晚于停车，命令已确认。原 UI 暖机输入为 45～89 帧，后台为固定 30 帧处理，另有 1 张测量前采集、测量后处理的图像被保留。',
        '原 UI 六轮首帧均使用已有检测缓存；后台每次为新进程。会话归档可以重建本会话输入，不能声称导出了先前进程的全部内部缓存。',
        '原 UI 使用原冻结模拟器和算法，桥接器新增被动归档，单独标记 instrumented_ui；本轮 UI 与旧校准参考没有混池。','',
        '## 保留的中间结果与无效记录','',
        '仅保留队列的 v5 中间候选，转向加权达到 30.28%，但暖机停车早于最终逻辑接收。它作为诊断结果保留，未作为最终修复版本。后续 v6 修复的是时序因果，未按较高成绩挑选版本。',
        '首次 UI 准备尝试因容器未挂载新增归档模块而无效，未进入测量。修复挂载后重新冻结；原无效报告及此前两轮对照完整保留。',
        '[首次准备失败与两轮对照](../lockstep-continuity-20261023/continuity-state.json) · [v5 阶段](continuity-state.json) · [v6 阶段](receipt-candidate/continuity-state.json)','',
        '## 验证','']
    if validation:
        lines += [f"- {validation['unit_checks']['count']} 项最终单元检查、17 项真实步进协议检查通过。",
            f"- 全部 {validation['evidence_audit']['runs']} 轮有效实验的原图与测量证据审计通过，解码 {validation['evidence_audit']['full_history_decoded']} 张会话原图。",
            '- 最终代码额外进行了同步归档和实时归档各一轮冒烟检查，均不计入效果比较。',
            f"- 有效单轮耗时最大 {validation['maximum_trial_wall_s']:.2f} 秒。VT8 与 Sunshine 保持运行。",
            '- 算法源码、模型、配置及受保护测试文件哈希未变；没有调整物理参数与评分公式。']
    lines+=['',
        '[最终验证](validation.json) · [完整原图/视频审计](final-full-history-audit.json) · [分阶段数据](final-review/diagnosis.json) · [完整汇总](final-result.json)','',
        '## 图像与时间线','',
        '[时间线 PNG](final-review/timelines.png) · [时间线 SVG](final-review/timelines.svg) · [原始图像裁剪](final-review/original-input-crops.png) · [抽帧来源](final-review/figure-sources.json)',
        '展示固定的 repeat 0 与固定时刻；统计结论包含所有轮次。逐阶段命中按发射时间归组，零发数段的命中率未定义。完整人工视频验收尚未完成。','',
        '## 后续优先项','',
        '优先隔离 UI 与后台的进程生命周期、检测缓存和暖机长度差异，再检查相机噪声相位及联合时序分布。初始化和输入差异的贡献尚未隔离，不能直接认定它们解释全部残差。',
        '继续采用原有至少五对、每侧至少三十发及原置信区间门槛；通过单路效果与视觉验收后，再开展双路及更高并发。','',
        '## 每轮证据','']
    for t in stages:
        lines.append(f"- {t['mode']} / {t['scene']} / repeat {t['repeat_id']}：[报告](../{t['run_id']}/report.html) · [会话审计](../{t['run_id']}/session-evidence-audit.json)")
    text='\n'.join(lines)+'\n';(root/'report.md').write_text(text);(root/'report.html').write_text(document(lines))
    experiments.publish_desktop(manage,root)
    return value


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    report(args.output.resolve())
