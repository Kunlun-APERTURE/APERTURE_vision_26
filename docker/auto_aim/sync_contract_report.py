"""Report mechanism acceptance separately from UI aiming equivalence."""
import argparse
import hashlib
import html
import json
import math
import os
from pathlib import Path
import statistics

import manage
import experiments
from report import load,write
from sync_first_divergence import STATE_FIELDS,sha


def lines(path):return [json.loads(l) for l in path.open()]


def normalized(value):
    if isinstance(value,dict):return {k:normalized(v) for k,v in value.items() if k not in ('session','reset_id')}
    if isinstance(value,list):return [normalized(v) for v in value]
    return value


def records(folder):
    end=load(folder/'run-state.json')['measurement_end_stamp_ns']
    events=lines(folder/'session-evidence/events.jsonl')
    inputs={p['data']['seq']:p for p in events if p['kind']=='input'}
    return [dict(frame=p['data']['frame'],result=p['data']['result'],jpeg_sha256=inputs[p['data']['frame']['seq']]['jpeg_sha256'])
        for p in events if p['kind']=='result' and p['data']['frame']['stamp_ns']<=end]


def relative_frame(frame,origin,step_origin):
    value=normalized(frame)
    def shift(item):
        if not isinstance(item,dict):return item
        for key,data in item.items():
            if isinstance(data,dict):shift(data)
            elif key in ('stamp_ns','sent_ns','logical_now_ns','logical_receipt_ns'):item[key]=data-origin
            elif key=='step_id':item[key]=data-step_origin
        return item
    return shift(value)


def analyze(root):
    state=load(root/'contract-study.json');assert state['state']=='completed'
    out=[]
    for mode,value in state['modes'].items():
        for e in value['runs']:
            folder=manage.OPS/'experiments'/e['run_id'];manifest=load(folder/'manifest.json')
            run=load(folder/'run-state.json');start,end=run['measurement_start_stamp_ns'],run['measurement_end_stamp_ns']
            history=load(folder/'session-evidence-audit.json');assert history['passed'],(folder,history['errors'])
            events=lines(folder/'session-evidence/events.jsonl')
            packets=[p['data'] for p in events if p['kind']=='result']
            measured=[p for p in packets if start<=p['frame']['stamp_ns']<=end]
            scores=[p for p in lines(folder/'frames.jsonl') if p.get('measurement')]
            assert {p['frame']['stamp_ns'] for p in measured}=={p['stamp_ns'] for p in scores}
            warm=next(p['data'] for p in events if p['kind']=='warmup_contract')
            prefix=[p['sensor']['index'] for p in warm['inputs']]
            assert prefix==list(range(30))
            seq=[p['frame']['sensor']['index'] for p in packets]
            gaps=[dict(previous=a,next=b) for a,b in zip(seq,seq[1:]) if b!=a+1]
            pulse=next((p for p in packets if p['fire_forwarded']),None)
            capture=[p['frame']['stamp_ns'] for p in measured]
            interval=[(b-a)*1e-6 for a,b in zip(capture,capture[1:])]
            age=[(p['receipt_ns']-p['frame']['stamp_ns'])*1e-6 for p in measured]
            ack=next((p['data']['stamp_ns'] for p in events if p['kind']=='measurement_ack'),None)
            if ack is not None:assert ack==start,'Recorder must use the acknowledged physical begin'
            out.append(dict(e,mode=mode,sensor_seed=manifest['sensor_seed'],warmup_frames=len(prefix),
                warmup_sensor_indices=prefix,scored_frames=len(scores),measured_inputs=len(measured),
                missing_scored_frames=0,physical_start_ns=start,physical_end_ns=end,
                begin_ack_equals_physical_start=ack==start if ack is not None else None,
                first_pulse_ms=(pulse['receipt_ns']-start)*1e-6 if pulse else None,
                sensor_gaps=gaps,capture_interval_p50_ms=statistics.median(interval),
                capture_to_result_p50_ms=statistics.median(age),
                metrics=load(folder/'summary.json')['metrics'],timing=load(folder/'timing.json'),
                source_hashes={name:sha(folder/name) for name in ('manifest.json','run-state.json','frames.jsonl','session-evidence/events.jsonl')}))
    repeats=[]
    for scene in ('hf_uniform_3','hf_turn90_3'):
        a=next(r for r in out if r['mode']=='backend' and r['scene']==scene and r['repeat_id']==0)
        b=next(r for r in out if r['mode']=='backend' and r['scene']==scene and r['repeat_id']==3)
        aa,bb=[records(manage.OPS/'experiments'/r['run_id']) for r in (a,b)]
        assert len(aa)==len(bb)
        origins=[rr[0]['frame']['stamp_ns'] for rr in (aa,bb)]
        steps=[rr[0]['frame']['step_id'] for rr in (aa,bb)]
        assert all(relative_frame(x['frame'],origins[0],steps[0])==relative_frame(y['frame'],origins[1],steps[1])
            and x['jpeg_sha256']==y['jpeg_sha256'] for x,y in zip(aa,bb))
        changes={k:sum(x['result'].get(k)!=y['result'].get(k) for x,y in zip(aa,bb)) for k in STATE_FIELDS}
        assert not any(v for k,v in changes.items() if k!='fire_check')
        ignored=[]
        for x,y in zip(aa,bb):
            p,q=x['result']['fire_check'],y['result']['fire_check']
            if p==q:continue
            assert {k for k in p if p.get(k)!=q.get(k)}=={'last_good_age_s'} and min(p['last_good_age_s'],q['last_good_age_s'])>3600
            ignored.append(dict(seq=x['frame']['seq'],left=p['last_good_age_s'],right=q['last_good_age_s']))
        repeats.append(dict(scene=scene,runs=[a['run_id'],b['run_id']],frames=len(aa),
            same_primary_images_relative_timing=True,absolute_origin_shift_ns=origins[1]-origins[0],
            first_step_shift=steps[1]-steps[0],raw_algorithm_differences=changes,
            uninitialized_last_good_clock_origin_differences=ignored,
            weighted_hit_rate=[a['metrics']['weighted_hit_rate'],b['metrics']['weighted_hit_rate']]))
    groups=[]
    for mode in ('ui','backend'):
        for scene in ('hf_uniform_3','hf_turn90_3'):
            rr=[r for r in out if r['mode']==mode and r['scene']==scene and r['repeat_id']<3]
            n=sum(r['metrics']['shots_fired'] for r in rr);hit=sum(r['metrics']['target_armor_hits'] for r in rr)
            groups.append(dict(mode=mode,scene=scene,runs=len(rr),shots=n,hits=hit,target_hit_rate=hit/n,
                weighted_hit_rate=sum(r['metrics']['weighted_hit_score_sum'] for r in rr)/n,
                correct_tracking=statistics.mean(r['metrics']['correct_tracking_fraction'] for r in rr),
                first_pulse_ms=statistics.mean(r['first_pulse_ms'] for r in rr)))
    result=dict(passed=True,runs=out,aggregates=groups,repeatability=repeats,
        maximum_wall_s=max(r['timing']['wall_seconds'] for r in out),
        all_contiguous_warmup=True,missing_scored_frames=0,
        ui_equivalence_certified=False,capacity_registered=False,
        qualification_note='Three paired sensor seeds are mechanism validation; minimum five pairs and confidence gates remain unchanged.')
    write(root/'mechanism-result.json',result)
    return result


def figures(root):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    r=load(root/'mechanism-result.json');fig,axes=plt.subplots(1,2,figsize=(12,4.3),layout='constrained')
    for ax,scene in zip(axes,('hf_uniform_3','hf_turn90_3')):
        for mode,color in [('ui','tab:blue'),('backend','tab:orange')]:
            rr=sorted([p for p in r['runs'] if p['mode']==mode and p['scene']==scene and p['repeat_id']<3],key=lambda p:p['sensor_seed'])
            ax.plot([p['sensor_seed'] for p in rr],[100*p['metrics']['weighted_hit_rate'] for p in rr],marker='o',color=color,label=mode)
        ax.set(title=scene,xlabel='Predeclared native sensor seed',ylabel='Weighted hit rate (%)',xticks=[1701,1702,1703])
        ax.grid(alpha=.2);ax.legend()
    for ext in ('png','svg'):fig.savefig(root/('paired-seeds.'+ext),dpi=170)
    plt.close(fig)
    fig,axes=plt.subplots(1,2,figsize=(12,4),layout='constrained')
    x=np.arange(16);axes[0].bar(x,[p['warmup_frames'] for p in r['runs']]);axes[0].axhline(30,color='gray',ls='--')
    axes[0].set(xlabel='All predeclared trials',ylabel='Processed warmup frames',title='Native sensor prefix 0..29 verified in every trial',ylim=(0,35))
    for scene,color in [('hf_uniform_3','tab:blue'),('hf_turn90_3','tab:orange')]:
        rr=[p for p in r['runs'] if p['mode'] in ('ui','backend') and p['scene']==scene and p['repeat_id']<3]
        axes[1].scatter(range(len(rr)),[p['first_pulse_ms'] for p in rr],color=color,label=scene)
    axes[1].set(xlabel='Six main trials per scene',ylabel='First fire pulse after physical start (ms)',title='No 10 Hz phase-status gate; algorithm advice still controls firing')
    axes[1].legend();axes[1].grid(alpha=.2)
    for ext in ('png','svg'):fig.savefig(root/('mechanism-checks.'+ext),dpi=170)
    plt.close(fig)


def finish(root):
    r=analyze(root);audit=load(root/'evidence-audit.json');replay=load(root/'clock-replay.json')
    assert audit['passed'] and audit['runs']==16 and replay['passed'] and replay['cases']==9
    registry=root.parent/'runtime-release.json'
    lines_out=['# 仿真实验输入修复与验收','',
        '最终候选完成 16 轮闭环验证：两个场景、三个预定传感器种子，两轮后台重复及 UI/后台各一轮管线检查。'
        '算法继续使用 tracking_fire_v3；本轮没有调整检测阈值、滤波、预测、弹道或命中公式。','',
        '## 已修复','',
        '- 阶段边界：模拟器在物理状态实际变化时记录 start/end；后台 ACK 携带边界，UI 使用可靠独立事件。开火不再等 10 Hz 展示状态。',
        '- 计分覆盖：记录器保留先到观测，并使用物理边界；16 轮测量输入与计分帧一一对应，漏计 0 帧。',
        '- 暖机：记录器先就绪，再处理恰好 30 个暖机输入；两侧都逐帧验证原生传感器序号 0–29。UI 移除了重复限频，暖机有界队列溢出明确判无效。'
        '这里的 30 指完成结果数；阶段切换时可能还有已采集的排队图像，仍保留并完整记录。',
        '- 噪声：保留 grain=0.05 与原生镜头 shader，显式 sensor_seed 驱动按采集帧推进的序列。每帧归档种子、序号、相位和重置 ID，场景 seed 独立。',
        '- 时钟：记录瞄准、开火判定、姿态年龄各自使用的采样时刻。7 段 UI 完整回放的观测、滤波、瞄准命令和开火建议精确复现；后台两段回放 14 项主要输出也全部一致。','',
        'UI 回放的原始差异另有完整保留：显示用预测时域出现浮点末位舍入，首次可信更新前的 last_good_age 使用不同零时钟原点。'
        '它们没有改变命令或开火判定；不宣称所有诊断字段逐位一致。','',
        '## 最终候选的三种子结果','',
        '| 模式 | 场景 | 发数/命中 | 精确命中率 | 加权命中率 | 正确跟踪率 | 首脉冲均值 ms |',
        '|---|---|---:|---:|---:|---:|---:|']
    for p in r['aggregates']:
        lines_out.append(f"| {p['mode']} | {p['scene']} | {p['shots']}/{p['hits']} | {p['target_hit_rate']:.2%} | {p['weighted_hit_rate']:.2%} | {p['correct_tracking']:.2%} | {p['first_pulse_ms']:.2f} |")
    lines_out += ['',
        '三组种子只是机制和敏感性检查，没有达到原定至少五对及置信区间门槛。UI/后台仍有捕获时序、真实处理延迟、物理准备轨迹等差别，'
        '同种子不会强制两条闭环相机轨迹逐像素相同。本轮不认证 UI 等效，不扩大同步并发。',
        '后台两场景同种子重复时，完整暖机和测量的 JPEG、相对时序、滤波、命令及开火判定均一致。'
        '绝对时钟起点相差一个控制步（约 2.08 ms），比较时显式平移时钟原点和步编号；'
        '首次可信更新前 last_good_age 的零原点诊断差异另有记录，不能宣称所有原始字段相同；'
        '多种子之间的分数仍会变化，应作为稳定性结果保留。','',
        '## 验证与版本','',
        f"最大单轮完整耗时 {r['maximum_wall_s']:.2f} 秒。完整会话 JPEG 解码 {audit['decoded_full_session_jpegs']} 张，"
        f"计分 JPEG {audit['decoded_measured_jpegs']} 张、MP4 {audit['decoded_mp4_frames']} 帧，证据核验通过。",
        '独立步进协议检查和主机单元检查见上层验证摘要；图表及固定图像已检查，完整人工逐视频验收仍未完成。',
        '首次管线版本有一轮 UI 在记录器就绪前停止；后续 r2 显示重复限频会跳暖机采集序号。它们的报告和全部分数仍保留在上层目录，'
        '最终 r3 完整重新冻结，未将前期较好的分数替换进最终统计。','',
        '[逐轮输入覆盖、指标及重复控制](mechanism-result.json) · [全会话及媒体审计](evidence-audit.json) · [实际时钟回放](clock-replay.json) · [冻结执行计划](contract-study.json)','',
        '新行为通过显式 `--input-contract aligned-v1 --sensor-seed N --detector-history session` 启用；暖机默认 30 帧。'
        '后台提交会选择已验证的专用模拟器/记录器镜像，默认实时镜像与旧 UI 基线未覆盖。新配置自动单路，普通实时容量单独重新验收。']
    text='\n'.join(lines_out)+'\n';(root/'report.md').write_text(text)
    from sync_acceptance import render_html
    body=render_html(text.splitlines()).replace('<h1>','<img style="max-width:100%" src="mechanism-checks.png"><img style="max-width:100%" src="paired-seeds.png"><h1>',1)
    (root/'report.html').write_text(body)
    experiments.publish_desktop(manage,root)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['analyze','figures','finish']);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();{'analyze':analyze,'figures':figures,'finish':finish}[args.action](args.output.resolve())
