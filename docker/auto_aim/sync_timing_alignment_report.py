"""Stage-by-stage acceptance: timing calibration is distinct from first-image equality."""
import argparse
import difflib
import hashlib
import json
import math
from pathlib import Path
import statistics

import manage
import experiments
from background_capacity import control_hz
from report import load,write
from sync_timing_alignment import observations,first,packets,stats
from sync_first_divergence import sha


def trial(mode,entry):
    folder=manage.OPS/'experiments'/entry['run_id'];manifest=load(folder/'manifest.json')
    state=load(folder/'run-state.json');start,end=state['measurement_start_stamp_ns'],state['measurement_end_stamp_ns']
    rr=observations(folder);measured=[r for r in rr if start<=r['frame']['stamp_ns']<=end]
    ip,fr,jpeg=first(folder);header=ip['data'];result=fr['result'];stamp=header['stamp_ns']
    traces=[p['data'] for p in packets(folder/'telemetry.jsonl') if p['kind']=='gimbal_trace']
    earlier=[p for p in traces if stamp-100_000_000<=p['stamp_ns']<=stamp]
    pulse=next((p for p in rr if p['fire_forwarded']),None)
    warm=next(p['data'] for p in packets(folder/'session-evidence/events.jsonl') if p['kind']=='warmup_contract')
    assert [p['sensor']['index'] for p in warm['inputs']]==list(range(30))
    score=load(folder/'summary.json');assert score['verdict']!='invalid'
    coverage={p['frame']['stamp_ns'] for p in measured}=={p['stamp_ns'] for p in packets(folder/'frames.jsonl') if p['measurement']}
    assert coverage
    values=dict(input_ns=[],processing_ns=[],return_ns=[],pose_ns=[])
    for p in measured:
        f=p['frame'];clocks=p['result'].get('evaluation_clocks',{})
        values['input_ns'].append(f['sent_ns']-f['stamp_ns'])
        values['pose_ns'].append(f['sent_ns']-f['latest_muzzle_pose']['stamp_ns'])
        if 'fire_now_ns' in clocks:
            values['processing_ns'].append(clocks['fire_now_ns']-f['sent_ns'])
            values['return_ns'].append(p['receipt_ns']-clocks['fire_now_ns'])
    synchronization=load(folder/'synchronization.json',{})
    release=[p for p in synchronization.get('phase_history',[]) if p['mode']=='release_home']
    return dict(entry,mode=mode,scene=manifest['scene'],sensor_seed=manifest['sensor_seed'],
        coverage_complete=coverage,warmup_frames=30,first_sensor_index=header['sensor']['index'],
        first_header=header,first_result=result,first_image_sha256=ip['jpeg_sha256'],
        first_capture_to_result_ms=(fr['receipt_ns']-stamp)*1e-6,
        first_capture_to_measurement_ms=(stamp-start)*1e-6,
        pre_first_exposure_pid_samples=earlier,preparation_release_phase=release,
        first_pulse_ms=(pulse['receipt_ns']-start)*1e-6 if pulse else None,
        control_hz=control_hz(folder),timing={k:stats(v) for k,v in values.items()},
        capture_interval_ns=stats([b['frame']['stamp_ns']-a['frame']['stamp_ns'] for a,b in zip(measured,measured[1:])]),
        metrics=score['metrics'],wall_s=load(folder/'timing.json')['wall_seconds'],
        source_hashes={name:sha(folder/name) for name in ('manifest.json','run-state.json','frames.jsonl','session-evidence/events.jsonl')})


def analyze(root):
    state=load(root/'study.json');release=load(root/'release-home/study.json')
    assert state['state']==release['state']=='completed'
    rows=[]
    for mode,key in [('ui','reference'),('v6','control')]:
        for r in state[key]:
            if r['repeat_id']<3:rows.append(trial(mode,r))
    for study in (state,release):
        for mode,value in study['modes'].items():
            rows.extend(trial(mode,e) for e in value['runs'])
    grouped=[];firsts=[];time_gate=[]
    for mode in dict.fromkeys(p['mode'] for p in rows):
        for scene in ('hf_uniform_3','hf_turn90_3'):
            rr=[r for r in rows if r['mode']==mode and r['scene']==scene]
            n=sum(p['metrics']['shots_fired'] for p in rr)
            grouped.append(dict(mode=mode,scene=scene,runs=len(rr),shots=n,hits=sum(p['metrics']['target_armor_hits'] for p in rr),
                exact=sum(p['metrics']['target_armor_hits'] for p in rr)/n if n else None,
                weighted=sum(p['metrics']['weighted_hit_score_sum'] for p in rr)/n if n else None,
                correct_tracking=statistics.mean(p['metrics']['correct_tracking_fraction'] for p in rr),
                first_pulse_ms=statistics.mean(p['first_pulse_ms'] for p in rr if p['first_pulse_ms'] is not None),
                input_ms=statistics.median(p['timing']['input_ns']['p50'] for p in rr)*1e-6,
                capture_interval_ms=statistics.median(p['capture_interval_ns']['p50'] for p in rr)*1e-6,
                control_hz=statistics.median(p['control_hz'] for p in rr)))
    for row in rows:
        if row['mode'] in ('ui','v6'):continue
        ui=next(p for p in rows if p['mode']=='ui' and (p['scene'],p['repeat_id'])==(row['scene'],row['repeat_id']))
        old=next(p for p in rows if p['mode']=='v6' and (p['scene'],p['repeat_id'])==(row['scene'],row['repeat_id']))
        firsts.append(dict(mode=row['mode'],scene=row['scene'],repeat_id=row['repeat_id'],
            first_image_unchanged_vs_v6=row['first_image_sha256']==old['first_image_sha256'],
            first_center_delta_to_ui_m=math.dist(row['first_result']['track_estimate']['center'],ui['first_result']['track_estimate']['center']),
            prior_first_center_delta_to_ui_m=math.dist(old['first_result']['track_estimate']['center'],ui['first_result']['track_estimate']['center']),
            first_detectors_equal_ui=row['first_result']['observations']==ui['first_result']['observations'],
            native_sensor_phase_equal_ui=row['first_header']['sensor']['phase']==ui['first_header']['sensor']['phase'],
            pid_live_at_first_exposure=bool(row['pre_first_exposure_pid_samples']),
            prior_pid_live_at_first_exposure=bool(old['pre_first_exposure_pid_samples'])))
    targets=load(root/'calibration.json')['target'];step_ns=1e9/targets['control_hz']
    for row in rows:
        if row['mode']!='ui-aligned-v7-clock':continue
        time_gate.append(dict(scene=row['scene'],repeat_id=row['repeat_id'],
            input_error_ns=row['timing']['input_ns']['p50']-targets['input_delay_ns'],
            capture_interval_error_ns=row['capture_interval_ns']['p50']-targets['capture_interval_ns'],
            control_hz_error=row['control_hz']-targets['control_hz'],
            input_within_one_control_step=abs(row['timing']['input_ns']['p50']-targets['input_delay_ns'])<=step_ns+2,
            capture_within_one_control_step=abs(row['capture_interval_ns']['p50']-targets['capture_interval_ns'])<=step_ns+2,
            control_within_one_percent=abs(row['control_hz']/targets['control_hz']-1)<.01))
    release_rows=[r for r in rows if r['mode']=='ui-aligned-v7-release']
    assert len(release_rows)==6 and all(r['preparation_release_phase'] for r in release_rows)
    assert not any(r['pre_first_exposure_pid_samples'] for r in release_rows)
    assert all(p['native_sensor_phase_equal_ui'] for p in firsts)
    result=dict(runs=rows,aggregates=grouped,first_capture_gate=firsts,timing_calibration_gate=time_gate,
        calibration_timing_passed=all(all(p[k] for k in ('input_within_one_control_step','capture_within_one_control_step','control_within_one_percent')) for p in time_gate),
        preparation_release_passed=True,initial_image_gate_passed=False,
        finding='Missing physical release command is fixed under explicit opt-in profile. '
            'Frozen timing medians are recalibrated. First native image/PnP mismatch remains upstream of delivery timing; '
            'no effect equivalence or concurrency qualification is inferred.',
        maximum_new_trial_wall_s=max(p['wall_s'] for p in rows if p['mode'] not in ('ui','v6')),
        ui_equivalence_certified=False,capacity_registration=False)
    write(root/'result.json',result);return result


def figures(root):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    result=load(root/'result.json');cross=load(root/'first-cross.json')['causal']
    fig,axes=plt.subplots(1,2,figsize=(13,4.5),layout='constrained')
    xx=np.arange(6)
    axes[0].scatter(xx,[max(p['center_delta_same_image_changed_own_pose_m'])*1000 for p in cross],label='Same native image; own-pose header changed',marker='s')
    axes[0].scatter(xx,[max(p['center_delta_same_own_pose_changed_native_image_m'])*1000 for p in cross],label='Same header; native image changed')
    axes[0].set(yscale='log',ylabel='First initialized center difference (mm)',xlabel='All six frozen UI/backend source pairs',title='24 one-frame cross replays; no control feedback')
    axes[0].legend(fontsize=8);axes[0].grid(alpha=.2)
    for scene,color in [('hf_uniform_3','tab:blue'),('hf_turn90_3','tab:orange')]:
        order=['ui','v6','ui-aligned-v7-delay','ui-aligned-v7-capture','ui-aligned-v7-clock','ui-aligned-v7-release']
        rr=[next(p for p in result['aggregates'] if p['scene']==scene and p['mode']==m) for m in order]
        axes[1].plot(range(len(rr)),[p['weighted']*100 for p in rr],marker='o',label=scene,color=color)
    axes[1].set(xticks=range(6),xticklabels=['Frozen UI','v6','Delay only','+ Capture','+ Control','Release only'],
        ylabel='Weighted hit rate (%)',title='All predeclared pairs retained; no score-selected winner')
    axes[1].tick_params(axis='x',labelrotation=20);axes[1].legend();axes[1].grid(alpha=.2)
    for ext in ('png','svg'):fig.savefig(root/('stage-results.'+ext),dpi=170)
    plt.close(fig)
    fig,axes=plt.subplots(1,3,figsize=(13,4),layout='constrained')
    order=['ui','v6','ui-aligned-v7-delay','ui-aligned-v7-capture','ui-aligned-v7-clock']
    for ax,field,label in zip(axes,['input_ms','capture_interval_ms','control_hz'],['Capture to send (ms)','Capture interval (ms)','Control frequency (Hz)']):
        rr=[p for p in result['aggregates'] if p['scene']=='hf_uniform_3']
        rr=[next(p for p in rr if p['mode']==m) for m in order]
        ax.bar(range(5),[p[field] for p in rr]);ax.set(xticks=range(5),xticklabels=['UI','v6','Delay','Capture','Control'],ylabel=label)
        ax.tick_params(axis='x',labelrotation=25);ax.grid(axis='y',alpha=.2)
    for ext in ('png','svg'):fig.savefig(root/('timing-stages.'+ext),dpi=170)
    plt.close(fig)
    independent=load(root/'holdout-result.json')
    if independent:
        fig,axes=plt.subplots(1,2,figsize=(12,4.3),layout='constrained')
        for ax,scene in zip(axes,('hf_uniform_3','hf_turn90_3')):
            for mode,color in [('holdout-ui','tab:blue'),('holdout-backend','tab:orange')]:
                rr=sorted([p for p in independent['trials'] if p['mode']==mode and p['scene']==scene],key=lambda p:p['sensor_seed'])
                ax.plot([p['sensor_seed'] for p in rr],[p['metrics']['weighted_hit_rate']*100 for p in rr],marker='o',label=mode.removeprefix('holdout-'),color=color)
            ax.set(title=scene+' independent seeds',xlabel='Native sensor seed (excluded from calibration)',ylabel='Weighted hit rate (%)',xticks=range(1704,1709))
            ax.grid(alpha=.2);ax.legend()
        for ext in ('png','svg'):fig.savefig(root/('independent-holdout.'+ext),dpi=170)
        plt.close(fig)


def holdout(root):
    from sync_study import bootstrap
    state=load(root/'holdout.json');assert state['state']=='completed'
    scenes=('hf_uniform_3','hf_turn90_3');results=[];trials=[]
    for scene in scenes:
        bymode={}
        for mode,value in state['modes'].items():
            rr={}
            for entry in value['runs']:
                if entry['scene']!=scene:continue
                f=manage.OPS/'experiments'/entry['run_id'];manifest=load(f/'manifest.json');summary=load(f/'summary.json')
                audit=load(f/'session-evidence-audit.json');assert audit['passed']
                metrics=summary['metrics']
                rr[entry['repeat_id']]=dict(run_id=f.name,duration=manifest['duration_s'],metrics=metrics,
                    invalid=summary['verdict']=='invalid')
                row=trial('holdout-'+mode,entry);trials.append(row)
            assert len(rr)==5
            bymode[mode]=rr
        results.append(dict(scene=scene,**bootstrap(bymode['ui'],bymode['backend'],.05,True)))
    value=dict(results=results,trials=trials,profile=state['timing_profile'],
        pairs_per_scene=5,independent_sensor_seeds=state['seeds'],
        aggregate_gate_passed=all(p['conclusion']=='passed' for p in results),
        initial_image_gate_passed=False,ui_equivalence_certified=False,
        note='Exactly five independent pairs; no extension, score tuning, reference replacement or concurrency registration.')
    write(root/'holdout-result.json',value);return value


def finish(root):
    result=analyze(root);state=load(root/'study.json')
    audit=load(root/'evidence-audit.json');release_audit=load(root/'release-home/evidence-audit.json')
    assert audit['passed'] and audit['runs']==18 and release_audit['passed'] and release_audit['runs']==6
    holdout_audit=load(root/'holdout-evidence/evidence-audit.json')
    assert holdout_audit['passed'] and holdout_audit['runs']==20
    causal=load(root/'first-cross.json');assert causal['state']=='completed' and len(causal['results'])==24
    reference=Path(state['reference_bundle'])
    changes=[]
    for name in ('sync_timing.py','sync_bridge.py'):
        before=(reference/'docker/auto_aim'/name).read_text();after=(manage.OPS/name).read_text()
        changes+=difflib.unified_diff(before.splitlines(True),after.splitlines(True),fromfile='before/'+name,tofile='after/'+name)
    (root/'backend-only.patch').write_text(''.join(changes))
    protected=['bridge.py','evaluator.py','background_runtime.py','experiments.py','input_contract.py','shots.py']
    assert all(sha(manage.OPS/n)==sha(reference/'docker/auto_aim'/n) for n in protected)
    assert sha(manage.ROOT/'sp_vision_25/build/simulator/sim_auto_aim')==sha(reference/'sp_vision_25/build/simulator/sim_auto_aim')
    text=['# 冻结 UI 后的后台时序与首帧定位','',
        'UI、算法二进制、模型、原生模拟器镜像、物理和评分保持冻结。完成 24 组首帧交叉回放、18 轮逐项时序对照、6 轮独立回零解除对照，以及 20 轮独立五种子检查。'
        '本轮未用命中分数选择参数或替换参考。','',
        '## 最早分叉在哪里','',
        '六对记录在第一张暖机图像曝光时已经不同，此时新会话的算法控制还未反馈到世界。'
        '相同原生 JPEG 只换自身位姿头，首帧初始化中心差约 0.4–0.7 mm；相同位姿头只换两侧原生 JPEG，差约 0.08–1.86 m。'
        '两侧 sensor seed/index/phase 和初始 ROI 相同。问题主要在图像所触发的观测/PnP 分支，不是位姿元数据换算误差。','',
        '根据独立 GT 的事后投影，两侧真实几何造成的装甲角点位移约 0.10–0.12 px，已有亚像素几何差异。'
        '不能仅凭这项对照断言渲染器实现有错，也不能宣称只匹配噪声种子就匹配了全部输入。','',
        '[原始首帧清单](inventory.json) · [24 组交叉因果结果](first-cross.json) · [事后几何投影](native-first-analysis.json)','',
        '## 找到并修复的准备状态语义差异','',
        'UI 的 reset_stream 会发送 distance=-1 解除旧云台目标。原后台路径仅把 hold 保存到 Python 对象，'
        '没有送入实际步进队列；首张暖机图像曝光时旧回零 PID 仍在驱动。'
        '新可选 preparation_release_home 在暖机前执行一次带步进确认的解除，不改算法、物理或增加额外等待。','',
        '六轮 release-only 对照均经过独立 release_home 阶段，首曝光前不再存在旧回零 PID 的控制采样。'
        '这修复了一个真实状态差异，但没有让首张图像和 UI 完全一致；不能把它描述为命中等效已经解决。','',
        '## 后台时序重校准','',
        '所有参数取六段冻结 UI 的时间记录中位数，算法分数没有参与校准。'
        '先只改输入延迟，再改采集间隔，最后改控制节拍及处理/回传/自身姿态延迟。'
        '新增 capture_interval_ns，以免仍隐含固定 30 Hz。原 v6 省略该字段时行为不变。','',
        '| 模式 | 匀速采集到发送 ms | 匀速采集间隔 ms | 控制 Hz |',
        '|---|---:|---:|---:|']
    for p in result['aggregates']:
        if p['scene']=='hf_uniform_3':text.append(f"| {p['mode']} | {p['input_ms']:.3f} | {p['capture_interval_ms']:.3f} | {p['control_hz']:.2f} |")
    text += ['',
        f"时序中位数校准门槛（输入/采集误差不超过一个控制步，控制频率误差 1% 内）：{result['calibration_timing_passed']}。"
        '固定模型仍不表示 UI 的逐帧抖动、准备状态或首曝光相位已一致。',
        '冻结 UI 的瞄准与开火采样中位间隔约 135 µs，当前固定模型仍合用一个逻辑决策时刻；该残差也没有冒称已对齐。',
        '输入延迟、采集间隔两项候选的首帧图像/观测与 v6 完全一致，是时间顺序的负对照：后面的发送节拍不会改写更早的首曝光。','',
        '## 全部闭环成绩：没有选择较好的版本','',
        '| 模式 | 场景 | 发数 | 精确命中 | 加权命中 | 正确跟踪 |',
        '|---|---|---:|---:|---:|---:|']
    for p in result['aggregates']:
        text.append(f"| {p['mode']} | {p['scene']} | {p['shots']} | {p['exact']:.2%} | {p['weighted']:.2%} | {p['correct_tracking']:.2%} |")
    text += ['',
        '这里仍使用原三个种子做单因素诊断，不是独立效果验收，也未达到五对/置信区间门槛。'
        '发送时序变得接近不能抵消首图像/PnP 分支差异；聚合命中率变高不能替代最早分叉的修复。','',
        '## 当前完成边界与下一步','',
        '- 已完成：冻结参考、24 组首帧图像/位姿交叉归因、后台输入/采集/控制时序的校准对照，以及旧回零控制残留的独立修复。',
        '- 未完成：准备结束时的姿态/控制状态和首曝光相位仍未配对；原生首图像与滤波初始化门槛仍未通过。'
        '下一步应在算法反馈前验证准备过程和首曝光，不能继续通过改发送延迟追分。',
        '- 不认证 UI 等效，不扩大同步并发，不覆盖历史 UI、默认实时镜像或原时序配置。普通实时自动双路资格独立保持；新输入配置仍自动单路。',
        '- 新候选配置单独保留：ui-aligned-v7-delay / capture / clock / release。release 与 clock 是独立对照，没有混成一个“最佳版本”。',
        '',f"24 轮新闭环最大用时 {result['maximum_new_trial_wall_s']:.2f} 秒；24 轮完整 JPEG/视频解码及会话审计通过。"
        '图表和固定首帧已检查，完整人工逐视频验收尚未进行。',
        '', '[逐轮与分层门槛](result.json) · [冻结时序计划](study.json) · [独立解除对照](release-home/study.json) · [后台代码变更](backend-only.patch) · [验证摘要](validation.json)']
    independent=holdout(root)
    text += ['', '## 独立五种子检查（1704–1708）','',
        '完整时序模型按时间校准定义固定选取，未挑选校准集命中率最高的候选。'
        'UI 和后台各两个场景、五个未参与校准的种子，固定 20 轮，不根据分数追加。'
        '原门槛保持：精确/加权 ±5 个百分点，发射频率相对 ±5%，正确跟踪 ±5 个百分点，最长丢失 ±0.2 秒，95% 配对 bootstrap。','',
        '| 场景 | UI/后台发数 | UI/后台加权命中 | 加权差 95% 区间（后台−UI） | 全门槛判定 |',
        '|---|---:|---:|---:|---|']
    for p in independent['results']:
        a,b=p['baseline'],p['candidate'];interval=p.get('intervals_95',{}).get('weighted_hit_rate')
        bounds=f"[{interval[0]*100:+.2f}, {interval[1]*100:+.2f}] pp" if interval else '样本不足'
        text.append(f"| {p['scene']} | {a['shots']}/{b['shots']} | {a['weighted_hit_rate']:.2%}/{b['weighted_hit_rate']:.2%} | {bounds} | {p['conclusion']} |")
    text += ['', '独立结果单独保留，不能与校准集混合来缩窄区间。首图像门槛仍未通过，因此本轮不授予 UI 等效或新同步并发资格。',
        '[独立逐轮与全部置信区间](holdout-result.json) · [事先固定的执行计划](holdout.json) · [独立证据审计](holdout-evidence/evidence-audit.json)']
    if not independent['aggregate_gate_passed']:
        text += ['', '**独立检查未通过整体接近门槛。校准集中的较小分差不能作为已修好或可扩大并发的依据。**']
    content='\n'.join(text)+'\n';(root/'report.md').write_text(content)
    from sync_acceptance import render_html
    body=render_html(content.splitlines()).replace('<h1>','<img style="max-width:100%" src="timing-stages.png"><img style="max-width:100%" src="stage-results.png"><img style="max-width:100%" src="independent-holdout.png"><h1>',1)
    (root/'report.html').write_text(body);experiments.publish_desktop(manage,root)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['analyze','figures','holdout','finish']);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();{'analyze':analyze,'figures':figures,'holdout':holdout,'finish':finish}[a.action](a.output.resolve())
