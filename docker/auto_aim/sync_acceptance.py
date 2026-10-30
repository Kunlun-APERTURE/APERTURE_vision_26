#!/usr/bin/env python3
"""Summarize a frozen synchronous study without modifying its evidence."""
import argparse
import html
import json
from pathlib import Path
import re
import statistics

from report import load, write


LABELS = {'passed': '通过', 'different': '差异明确',
          'insufficient_samples': '样本不足', 'invalid': '无效'}


def render_html(lines):
    def inline(value):
        value=html.escape(value)
        value=re.sub(r'`([^`]+)`',r'<code>\1</code>',value)
        return re.sub(r'\[([^]]+)\]\(([^)]+)\)',r'<a href="\2">\1</a>',value)
    blocks=[];index=0
    while index<len(lines):
        line=lines[index]
        if line.startswith('|'):
            rows=[]
            while index<len(lines) and lines[index].startswith('|'):
                rows.append(lines[index].strip('|').split('|'));index+=1
            header='<tr>'+''.join('<th>'+inline(cell.strip())+'</th>' for cell in rows[0])+'</tr>'
            body=''.join('<tr>'+''.join('<td>'+inline(cell.strip())+'</td>' for cell in row)+'</tr>' for row in rows[2:])
            blocks.append('<div class="table"><table><thead>'+header+'</thead><tbody>'+body+'</tbody></table></div>')
            continue
        if line.startswith('#'):
            count=len(line)-len(line.lstrip('#'));blocks.append(f'<h{count}>'+inline(line[count:].strip())+f'</h{count}>')
        elif line.startswith('- '):
            items=[]
            while index<len(lines) and lines[index].startswith('- '):
                items.append('<li>'+inline(lines[index][2:])+'</li>');index+=1
            blocks.append('<ul>'+''.join(items)+'</ul>');continue
        elif line:blocks.append('<p>'+inline(line)+'</p>')
        index+=1
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        '<title>同步实验验收</title><style>body{font:15px system-ui;max-width:1450px;margin:2em auto;padding:0 1em;line-height:1.6}'
        '.table{overflow:auto}table{border-collapse:collapse;font-size:13px}th,td{border:1px solid #bbb;padding:.4em .6em;white-space:nowrap}'
        'th{background:#edf1f5}code{background:#f3f3f3;padding:.1em .3em}a{color:#1765a8}</style>'
        +''.join(blocks)+'<p><a href="study.json">完整统计与冻结版本记录</a></p></html>')


def interval(result, name):
    bounds = result.get('intervals_95', {}).get(name)
    return '—' if bounds is None else f'[{100*bounds[0]:+.2f}, {100*bounds[1]:+.2f}]'


def gate_notes(result):
    labels=dict(target_hit_rate='精确命中',weighted_hit_rate='加权命中',
        fire_frequency='发射频率',correct_tracking_fraction='正确跟踪',longest_loss_s='最长丢失')
    notes=[]
    for name,bounds in result.get('intervals_95',{}).items():
        limit=result['margins'][name]
        outside=bounds[1]<-limit or bounds[0]>limit
        unresolved=not (-limit<=bounds[0] and bounds[1]<=limit)
        if outside:notes.append(labels[name]+'超界')
        elif unresolved and result['conclusion']=='insufficient_samples':notes.append(labels[name]+'待定')
    return '；'.join(notes) or result.get('reason','—')


def summarize(folder):
    state = load(folder/'study.json')
    if not state:
        raise ValueError('Missing study.json')
    certification='通过' if state.get('certified') else '未通过' if state['state']=='completed' else '尚未完成'
    lines = ['# 非实时同步实验验收', '',
             f"状态：{state['state']}；默认执行模式：`realtime`。", '',
             f"UI 接近认证：{certification}。"
             f"推荐时序：`{state.get('recommended_profile') or '无'}`。", '',
             '## 比较结果', '',
             '区间为候选减基线的 95% 配对 bootstrap 区间，单位为百分点。单路及并发稳定性门槛为 ±2，UI 接近门槛为 ±5。'
             'UI 比较还要求发射频率相对差 ±5%、正确跟踪比例 ±5 个百分点、平均最长丢失时间差 ±0.2 秒；总判定包含这些门槛。', '',
             '| 时序 | 比较 | 场景 | 对数 | 基线/候选发数 | 精确命中率差区间 | 加权命中率差区间 | 判定 | 涉及的门槛 |',
             '|---|---|---|---:|---:|---|---|---|---|']
    comparisons = []
    for profile, checks in state.get('qualification', {}).items():
        groups = [('旧 UI → 单路', checks.get('ui_equivalence', {}))]
        groups.extend((('单路独立重复' if level == '1' else f'单路 → {level} 路'), results)
                      for level, results in checks.get('repeatability', {}).items())
        for comparison, scenes in groups:
            for scene, result in scenes.items():
                shots = '/'.join(str(result.get(side, {}).get('shots', '—'))
                                 for side in ('baseline', 'candidate'))
                conclusion = LABELS.get(result['conclusion'], result['conclusion'])
                lines.append(f"| {profile} | {comparison} | {scene} | {result.get('pairs', 0)} | "
                             f"{shots} | {interval(result, 'target_hit_rate')} | "
                             f"{interval(result, 'weighted_hit_rate')} | {conclusion} | {gate_notes(result)} |")
                comparisons.append(dict(profile=profile, comparison=comparison, scene=scene, **result))
    lines.extend(['', '## 容量', '',
                  '| 时序 | 稳定性通过的路数 | 有效实验/小时 | 推荐路数 |', '|---|---|---|---:|'])
    for profile, result in state.get('capacity', {}).items():
        rates = '，'.join(f'{level} 路 {rate:.1f}' for level, rate in result.get('valid_runs_per_hour', {}).items())
        levels = ', '.join(str(n) for n in result['accepted_levels']) or '无'
        lines.append(f"| {profile} | {levels} | {rates or '—'} | {result.get('recommended_parallelism') or '—'} |")
    lines.extend(['', '### 相同任务清单的实测吞吐量', '',
                  '仅比较各路数的首组四场景平衡批次；补样场景比例不同，不用于选择并发。实测效率与统计认证分别记录。', '',
                  '| 时序与路数 | 有效轮数 | 总秒数 | 有效实验/小时 |', '|---|---:|---:|---:|'])
    for key,value in state.get('observed_performance',{}).items():
        if value:lines.append(f"| {key} | {value['valid_runs']} | {value['wall_seconds']:.1f} | {value['valid_runs_per_hour']:.1f} |")
    finish=load(folder/'finish-state.json',{})
    realtime=load(Path(finish['benchmark']),{}) if finish.get('benchmark') else {}
    if realtime.get('state')=='completed':
        lines.extend(['', '### 默认实时模式的容量复核', '',
            '| 路数 | 健康检查 | 有效轮数 | 有效实验/小时 |', '|---:|---|---:|---:|'])
        for row in realtime['results']:
            lines.append(f"| {row['level']} | {'通过' if row['healthy'] else '未通过'} | {row['valid_runs']} | {row['valid_runs_per_hour']:.1f} |")
        lines.extend(['',f"实时推荐并发：{realtime['recommended_parallelism']}；[完整容量记录](../{realtime['id']}/report.html)。"])
    costs=[]
    lines.extend(['', '## 基础结果耗时', '',
                  '包含每轮环境创建、固定暖机与证据封存；媒体生成另行排队，不计入基础结果边界。', '',
                  '| 时序 | 路数 | 有效轮数 | 现实耗时中位数（秒） | P95（秒） |', '|---|---:|---:|---:|---:|'])
    for entry in state.get('modes',{}).values():
        seconds=[]
        for run in entry['runs']:
            timing=load(folder.parent/run['run_id']/'timing.json',{})
            if isinstance(timing.get('wall_seconds'),(int,float)):seconds.append(timing['wall_seconds'])
        if seconds:
            median=statistics.median(seconds);p95=sorted(seconds)[min(len(seconds)-1,int(.95*len(seconds)))]
            costs.append(dict(profile=entry['profile'],parallelism=entry['level'],valid_runs=len(seconds),
                median_basic_wall_s=median,p95_basic_wall_s=p95))
            lines.append(f"| {entry['profile']} | {entry['level']} | {len(seconds)} | {median:.2f} | {p95:.2f} |")
    runtime_checks={}
    lines.extend(['', '## 独立运行检查', '',
                  '这些检查与统计认证分开；四路功能成功不表示命中效果已经等价。', ''])
    for filename,label in (
        ('protocol-checks.json','真实 Unix 步进协议'),
        ('algorithm-compatibility.json','旧新程序同图数值比较'),
        ('operational-checks.json','预览、推理暂停与调度重启恢复'),
        ('parallel-functional.json','四路同时测量与实例隔离'),
        ('evidence-audit.json','正式实验原图、视频与哈希审计'),
        ('operational-evidence-audit.json','独立运行检查证据审计')):
        check=load(folder/filename,{})
        if not check:continue
        runtime_checks[filename]=dict(passed=check['passed'],evidence=filename)
        verdict='通过' if check['passed'] else '未通过'
        lines.append(f'- {label}：{verdict}；[证据]({filename})。')
    lines.extend(['', '## 证据与范围', '',
                  f"- 冻结候选：`{state.get('bundle', '—')}`。",
                  f"- 独立 UI 参考 {len(state.get('reference', []))} 轮；校准 {len(state.get('calibration', []))} 轮。",
                  '- 场景：静止 8 米、匀速 3 米、高速转弯、反向旋转；v3、advice、无阻力。',
                  '- 旧数据原件保留；统一 v2 重算位于 `rescore/`。',
                  '- 环境无效尝试不替代有效得分；不同候选版本保存在 `candidate_history`，不混合计算。',
                  '- 未通过与样本不足均不视为认证。8 路仅在 4 路通过后测试。',
                  '- 数值验收与人工视觉复核分别记录；同步结果不能替代最终实时部署验证。', ''])
    result = dict(study=state['name'], state=state['state'],
                  certified=state.get('certified', False),
                  default_execution_mode='realtime',
                  recommended_profile=state.get('recommended_profile'),
                  frozen_bundle=state.get('bundle'),
                  capacity=state.get('capacity', {}), comparisons=comparisons,costs=costs,
                  observed_performance=state.get('observed_performance',{}),runtime_checks=runtime_checks)
    if realtime:result['realtime_capacity']=dict(benchmark=realtime.get('id'),state=realtime.get('state'),
        recommended_parallelism=realtime.get('recommended_parallelism'),results=realtime.get('results',[]))
    write(folder/'acceptance.json', result)
    (folder/'acceptance.md').write_text('\n'.join(lines))
    page=render_html(lines)
    (folder/'acceptance.html').write_text(page)
    (folder/'report.html').write_text(page)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('folder', type=Path)
    args = parser.parse_args()
    result = summarize(args.folder)
    print(json.dumps({k: result[k] for k in ('study', 'state', 'certified', 'recommended_profile')}, ensure_ascii=False))
