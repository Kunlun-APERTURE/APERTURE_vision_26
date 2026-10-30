"""Seal replay attribution without changing algorithms, recordings or capacity."""
import argparse
from collections import Counter
import html
import json
import os
from pathlib import Path
import re
import subprocess

import manage
from decision_benchmark import sha,verify_bundle
from perception_study import protected
from report import load,write


def records(path):return [json.loads(l) for l in Path(path).open()]


def evidence(root):
    roots=[root,root/'longest-A'];replays=0;frames=0;native=0;decisions=0
    sealed=[]
    for folder in roots:
        c=load(folder/'contract.json');s=load(folder/'state.json');result=load(folder/'result.json')
        if s['state']!='completed' or not result['completed']:raise ValueError('Incomplete attribution')
        if sha(folder/'contract.json')!=s['contract_sha256']:raise ValueError('Definition changed')
        for source in c['sources']:
            original=manage.OPS/'experiments'/source['run_id']
            if any(sha(original/name)!=value for name,value in source['source_hashes'].items()):raise ValueError('Original evidence changed')
            cases=[r for r in s['replays'] if r['source_run']==source['run_id']]
            if len(cases)!=3 or len({r['input_sha256'] for r in cases})!=1:raise ValueError('Input pairing changed')
            for r in cases:
                path=Path(r['output'])
                if any(sha(path/name)!=value for name,value in r['output_hashes'].items()):raise ValueError('Replay evidence changed')
                data=records(path/'events.jsonl')
                if len(data)!=r['frames'] or any(p['stage']!='primary' for p in data):raise ValueError('Unexpected replay history')
                if any(not p['frame'].get('replay_clocks') for p in data):raise ValueError('Recorded evaluation clocks missing')
                if r['native_configuration'] and any(x['different_frames'] for x in r['source_reproduction'].values()):
                    raise ValueError('Native source was not reproduced')
                replays+=1;frames+=r['frames'];native+=r['native_configuration']
        for name in ('initial','fallbacks','traces','divergences'):
            p=folder/'analysis'/(name+'.jsonl');sealed.append(dict(path=str(p.relative_to(root)),sha256=sha(p)))
        initial=records(folder/'analysis/initial.jsonl')
        if any(not x['frozen_decision_reproduced'] for x in initial):raise ValueError('Geometry mirror failed')
        decisions+=len(initial)
        if protected()!=c['protected']:raise ValueError('Protected algorithm/runtime changed')
    for path in load(root/'contract.json')['bundles'].values():verify_bundle(Path(path))
    log=(root/'analysis-checks.log').read_text()
    if not log.rstrip().endswith('OK'):raise ValueError('Geometry checks failed')
    result=dict(passed=True,archived_sources=26,replays=replays,replayed_frames=frames,
        same_configuration_exact_reproductions=native,independent_initial_decisions_reproduced=decisions,
        geometry_checks=int(re.search(r'Ran (\d+) tests',log)[1]),
        same_images_own_pose_and_recorded_clocks_verified=True,original_recordings_unchanged=True,
        production_binary_config_and_test_unchanged=True,new_closed_loop_trials=0,
        algorithm_modified=False,algorithm_promoted=False,capacity_registration=False,
        analysis_outputs=sealed,
        diagnostic_sources={p.name:sha(p) for p in manage.OPS.glob('perception_attribution*.py')})
    write(root/'validation.json',result)
    return result


def plots(root):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    d=load(root/'result.json')
    fig,ax=plt.subplots(figsize=(10,4),layout='constrained');x=np.arange(4)
    for i,(field,label,color) in enumerate((('improved_over_5cm','Fallback improved pose >5cm','tab:green'),
                                          ('negligible_within_5cm','Within 5cm','gray'),
                                          ('worsened_over_5cm','Fallback worsened pose >5cm','tab:red'))):
        ax.bar(x+(i-1)*.25,[r[field] for r in d['fallback_summary']],width=.25,label=label,color=color)
    ax.set(xticks=x,xticklabels=[r['source_engine']+' '+r['scene'].removeprefix('hf_') for r in d['fallback_summary']],
           ylabel='Direct same-candidate paired observations',title='A35 hard rejection sometimes discards a useful refinement')
    ax.legend(fontsize=8);ax.grid(axis='y',alpha=.2)
    fig.savefig(root/'fallback-effects.png',dpi=170);fig.savefig(root/'fallback-effects.svg');plt.close(fig)
    initial=records(root/'analysis/initial.jsonl')
    long=[r for r in initial if r['source_engine']=='ui' and r['source_variant']=='B35' and r['scene']=='hf_turn90_3' and r['sensor_seed']==1901]
    fig,axes=plt.subplots(1,2,figsize=(12,4),layout='constrained')
    pending=[r for r in long if r['status']=='pending_inconsistent']
    axes[0].plot([r['wait_s'] for r in pending],[r['original']['best_cross'] for r in pending],label='Original joint match')
    axes[0].plot([r['wait_s'] for r in pending],[r['truth_position_only']['best_cross'] for r in pending],label='Posthoc true anchor position only')
    axes[0].axhline(.35,color='black',ls='--',label='Frozen gate')
    axes[0].set(xlabel='Time since first recorded input (s)',ylabel='Normalized cross-plate endpoint residual',title='B35: native UI turn / seed1901')
    axes[0].legend(fontsize=8);axes[0].grid(alpha=.2)
    sr=root/'longest-A';sc=load(sr/'contract.json');candidate=next(e for e in sc['sources'] if e['variant']=='A35')
    traces=records(sr/'analysis/traces.jsonl')
    for variant,color in (('baseline','tab:blue'),('A35','tab:orange')):
        rr=[r for r in traces if r['source_run']==candidate['run_id'] and r['variant']==variant and r['center_error_m'] is not None]
        axes[1].plot([r['time_s'] for r in rr],[r['center_error_m'] for r in rr],label=variant,color=color)
    axes[1].axvline(0,color='gray',ls=':');axes[1].axhline(.5,color='gray',ls='--')
    axes[1].set(xlabel='Recorded scenario time (s); negative = warmup',ylabel='Center XY error (m)',
                title='A35 longest first-hit source: SAME recorded input')
    axes[1].legend();axes[1].grid(alpha=.2)
    fig.savefig(root/'mechanism-timelines.png',dpi=170);fig.savefig(root/'mechanism-timelines.svg');plt.close(fig)
    # Show the actual first rejection of the selected longest-A failure.
    from sync_timing_alignment import first
    source=manage.OPS/'experiments'/candidate['run_id'];_,_,jpeg=first(source)
    import cv2
    img=cv2.imdecode(np.frombuffer(jpeg,np.uint8),cv2.IMREAD_COLOR)
    replay=next(p for p in records(sr/'replays'/candidate['run_id']/'A35/events.jsonl') if p['stage']=='primary')
    obs=replay['result']['observations'][0];guard=obs['refinement']
    fig,ax=plt.subplots(figsize=(8,4),layout='constrained');ax.imshow(cv2.cvtColor(img,cv2.COLOR_BGR2RGB),interpolation='nearest')
    for name,color in (('raw','tab:orange'),('candidate','lime')):
        xy=np.asarray(guard[name]);xy=np.vstack((xy,xy[0]));ax.plot(xy[:,0],xy[:,1],'.-',color=color,label=name)
    ax.set(xlim=(727,773),ylim=(603,569),title='First warmup image: side_ratio_change rejects the useful candidate')
    ax.legend();fig.savefig(root/'first-rejection-corners.png',dpi=190);plt.close(fig)


def render(root):
    d=load(root/'result.json');v=load(root/'validation.json');fallback=d['fallback_summary'];supp=root/'longest-A'
    sc=load(supp/'contract.json');candidate=next(e for e in sc['sources'] if e['variant']=='A35')
    first=next(p for p in records(supp/'replays'/candidate['run_id']/'A35/events.jsonl') if p['stage']=='primary')
    guard=first['result']['observations'][0]['refinement']
    tr=records(supp/'analysis/traces.jsonl')
    first_errors={name:next(p['center_error_m'] for p in tr if p['source_run']==candidate['run_id'] and p['variant']==name) for name in ('baseline','A35')}
    total=sum(r['matched_direct_events'] for r in fallback);worse=sum(r['worsened_over_5cm'] for r in fallback);better=sum(r['improved_over_5cm'] for r in fallback)
    native=records(root/'analysis/initial.jsonl')
    long=[r for r in native if r['source_engine']=='ui' and r['source_variant']=='B35' and r['scene']=='hf_turn90_3' and r['sensor_seed']==1901]
    longest=max(long,key=lambda p:p['wait_s'])
    lines=['# 感知退化的同输入归因','','本轮没有修改算法，没有新增 UI／后台闭环或射击实验。', '',
        f"完成 24 条原轨迹 × 三个冻结配置的 72 段主回放，并补充最长 A35 未命中录像及其配对基线共 6 段。"
        f"合计 {v['replayed_frames']} 帧推理，26 条原配置全部复现观测、滤波、选板、角度命令和开火建议。", '',
        '[冻结定义](contract.json) · [主分析](result.json) · [最长 A35 补充](longest-A/contract.json) · [证据审计](validation.json)', '',
        '## 结论一：A35 将网络角点当作过强的参考，拒绝了部分有益矫正', '',
        f"{total} 个可直接配对事件中，回退使装甲位置误差增加超过 5cm 的有 {worse} 个，减少超过 5cm 的有 {better} 个。"
        '这些是相关帧级诊断计数，不是独立样本数或命中率。仅当 A35 拒绝的候选角点与基线实际采用角点一致、且匹配到同一真实装甲时才比较。', '',
        '| 输入来源 | 场景 | 直接配对数 | 回退改善 >5cm | 回退恶化 >5cm | 中位位置误差变化 |',
        '|---|---|---:|---:|---:|---:|']
    for r in fallback:
        lines.append(f"| {r['source_engine']} | {r['scene']} | {r['matched_direct_events']} | {r['improved_over_5cm']} | {r['worsened_over_5cm']} | {r['delta']['p50']:+.3f}m |")
    events=records(root/'analysis/fallbacks.jsonl');bad=[r for r in events if r['error_delta_m'] is not None and r['error_delta_m']>.05]
    smaller=sum(r['fallback_reprojection_px']<r['baseline_reprojection_px'] for r in bad)
    lines+=['',f"在 {len(bad)} 个明显恶化事件中，{smaller} 个回退后的重投影 RMS 反而更小。"
            '因此下一轮不能仅按重投影 RMS 更低来决定采用原始还是矫正角点。', '',
            '### 最长未命中记录的第一处分叉', '',
            f"选取全部既有后台 A35 匀速记录中首次命中最晚的一条，实际首次命中 {sc['selected_delay_s']:.3f}s，"
            '并保留其同种子基线作为对照。此补充用于解释失败机制，不并入主样本统计。', '',
            f"同一暖机第一张 JPEG 上：端点改动 {guard['endpoint_fraction']:.3f}、灯条长度改动 {guard['length_fraction']:.3f}，"
            f"均低于 0.35；左右长度比改动 {guard['ratio_fraction']:.3f} 超过 0.35，单独触发拒绝。"
            f"中心 XY 误差由基线 {first_errors['baseline']:.3f}m 变为 A35 {first_errors['A35']:.3f}m。"
            '观察／初始化在暖机第一帧分叉，之后才出现跟踪状态和开火建议分叉。', '',
            '固定相机轨迹回放不能证明这一个拒绝独自造成全部后续漏弹；原录像中早期已发射但未命中，'
            '并非简单的“门控一直没开火”。[完整补充分析](longest-A/result.json)', '',
            '## 结论二：B35 把有偏单板位姿当成硬几何约束，等待到观测变化才初始化', '',
            '| 来源 | 场景 | 等待帧 | 仅放宽高度可恢复 | 扩大半径范围可恢复 | 仅替换真实位置可恢复 | 仅替换真实朝向可恢复 |',
            '|---|---|---:|---:|---:|---:|---:|']
    for r in d['initial_summary']:
        q=r['rescued'];lines.append(f"| {r['source_engine']} | {r['scene']} | {r['pending_frames']} | {q['height_free']} | {q['radius_free']} | {q['truth_position_only']} | {q['truth_yaw_only']} |")
    lines+=['','各项干预单独进行，计数不能相加。真实位置／朝向仅在分析器内替换投影锚点，绝未进入算法或回放输入。', '',
            f"UI 转向 seed1901 的原生 B35 连续等待 60 帧，等待 {longest['wait_s']:.3f}s，"
            f"到场景时间 {longest['time_s']:.3f}s 才初始化，恰在目标从静止开始移动之后。"
            '此时图像观测发生变化，原本互相矛盾的车体假设才跨过硬门槛。', '',
            '主集合 UI 转向的 90 个等待帧：单独放宽高度恢复 0 帧，仅替换朝向恢复 0 帧，'
            '仅替换位置恢复 88 帧。这支持位置估计偏差是这组等待的主要阻塞，不能据此声称所有场景都是同一个原因。', '',
            '## 固定输入下的影响范围','','| 输入来源 | 场景阶段 | 配置 | 中心误差中位数 | P95 | 有效覆盖 | ≤0.5m 比例 |',
            '|---|---|---|---:|---:|---:|---:|']
    for r in d['groups']:
        if r['phase'] not in ('moving','after_turn'):continue
        err=r['error'];lines.append(f"| {r['source_engine']} | {r['scene']} / {r['phase']} | {r['variant']} | "
            f"{err['p50']:.3f}m | {err['p95']:.3f}m | {r['valid_fraction']:.2%} | {r['within_half_m_fraction']:.2%} |")
    lines+=['','同一条录像上的三个配置使用完全相同输入，表内比较估计误差而非射击成绩。'
            'UI／后台来源仍有不同的原始相机、控制和初始化历史，不能把来源间差异直接归罪于模拟器实现。', '',
            '## 建议的下一次修改','','- A：先撤除“左右灯条长度比改变即回退原始角点”的硬判断，作为单因素候选回放；'
            '把网络端点也视为有噪声观测，不以网络形状作为真值。其余门槛单独保留，避免同时改多个因素。',
            '- B：暂停继续调大一致性阈值。下一版应显式考虑单板位置不确定性，采用有界观测确认／多假设暂存，'
            '避免目标静止时无限等待；须同时验收误初始化和首次有效命中延迟。',
            '- 先用这批固定输入检验修订是否消除具体退化，随后后台独立验证；达到明显收益再做 UI 小批量核对。', '',
            '本轮只完成诊断，没有实施上述修改，也没有授予候选推广或新的并发资格。', '',
            '## 证据与局限','',
            f"独立投影分析复现 {v['independent_initial_decisions_reproduced']} 个冻结初始化判定；6 项投影／干预检查通过。"
            '所有源图像、记录时钟、模型、二进制及配置摘要均校验。', '',
            '帧级计数存在时序相关，长等待会贡献更多样本；主集合仅两个传感器种子。'
            '真实装甲按朝向、图像位置和一对一匹配，门槛 12px；直接回退比较同时要求同一个物理装甲。'
            '使用 135×56mm 灯条端点模型，不把物理装甲外边框当作灯条真值。', '',
            '![回退效果](fallback-effects.png)', '', '![机制时间线](mechanism-timelines.png)', '',
            '![第一帧拒绝](first-rejection-corners.png)', '']
    (root/'report.md').write_text('\n'.join(lines));body=[];table=False
    def inline(s):
        s=html.escape(s);s=re.sub(r'!\[([^\]]*)\]\(([^)]+)\)',r'<img alt="\1" src="\2">',s)
        return re.sub(r'\[([^\]]+)\]\(([^)]+)\)',r'<a href="\2">\1</a>',s)
    for line in lines:
        if line.startswith('|'):
            if not table:body.append('<table>');table=True
            cells=[v.strip() for v in line.strip('|').split('|')]
            if all(re.fullmatch('[-:]+',v) for v in cells):continue
            body.append('<tr>'+''.join('<td>'+inline(x)+'</td>' for x in cells)+'</tr>');continue
        if table:body.append('</table>');table=False
        if line.startswith('#'):
            n=len(line)-len(line.lstrip('#'));body.append(f'<h{n}>'+inline(line[n:].strip())+f'</h{n}>')
        elif line:body.append('<p>'+inline(line)+'</p>')
    if table:body.append('</table>')
    (root/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>同输入误差归因</title>'
        '<style>body{font:16px system-ui;max-width:1250px;margin:2em auto;line-height:1.6}table{border-collapse:collapse}'
        'td{border:1px solid #bbb;padding:.4em}img{max-width:100%}</style>'+''.join(body))


def finish(root):
    result=evidence(root);c=load(root/'contract.json');meta=verify_bundle(Path(c['bundles']['baseline']))
    subprocess.run(['docker','run','--rm','--network','none','--cpus','2','--user',f'{os.getuid()}:{os.getgid()}',
        '-e','MPLCONFIGDIR=/tmp/matplotlib','-e','OPENBLAS_NUM_THREADS=1',
        '-v',str(manage.ROOT)+':'+str(manage.ROOT)+':ro','-v',str(root)+':'+str(root),'-w',str(manage.ROOT),
        '--entrypoint','python3',meta['images']['evaluator'],'-B','docker/auto_aim/perception_attribution_report.py',
        'plots','--output',str(root)],check=True,timeout=120)
    render(root)
    import experiments
    experiments.publish_desktop(manage,root)
    print(json.dumps({k:v for k,v in result.items() if k not in ('diagnostic_sources','analysis_outputs')},indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['finish','plots']);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();root=a.output.resolve()
    if a.action=='plots':plots(root)
    else:finish(root)
