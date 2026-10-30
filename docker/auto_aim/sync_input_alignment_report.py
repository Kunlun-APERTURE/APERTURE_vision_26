"""Seal causal input-alignment evidence, native render controls and limitations."""
import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import statistics
import subprocess

import manage
import experiments
from report import load, write
from sync_first_divergence import STATE_FIELDS, sha, archive
from sync_first_divergence_analysis import primary
from sync_input_alignment import input_digest, measured_records, coverage


def packets(path):
    return [json.loads(line) for line in path.open()]


def sensor_records(folder):
    state = load(folder/'run-state.json')
    end = state['measurement_end_stamp_ns']
    events = packets(folder/'session-evidence/events.jsonl')
    inputs = {p['data']['seq']:p for p in events if p['kind']=='input'}
    return [dict(frame=p['data']['frame'], result=p['data']['result'],
                 jpeg_sha256=inputs[p['data']['frame']['seq']]['jpeg_sha256'],
                 input_packet=inputs[p['data']['frame']['seq']])
            for p in events if p['kind']=='result' and p['data']['frame']['stamp_ns'] <= end]


def normalized_digest(rows):
    digest = hashlib.sha256()
    for p in rows:
        header = {k:v for k,v in p['frame'].items() if k != 'session'}
        digest.update(json.dumps(header,sort_keys=True,separators=(',',':')).encode())
        digest.update(p['jpeg_sha256'].encode())
    return digest.hexdigest()


def sensor_analysis(root):
    state = load(root/'sensor-study.json')
    assert state['state']=='completed'
    cases = []
    streams = {}
    for condition, value in state['modes'].items():
        bundle = Path(value['bundle'])
        meta = load(bundle/'bundle.json')
        assert all(sha(bundle/name)==expected for name,expected in meta['files'].items())
        assert manage.inspect(meta['retained_images']['simulator'])['Id']==meta['images']['simulator']
        for entry in value['runs']:
            folder = root.parent/entry['run_id']
            rows = sensor_records(folder)
            streams[(condition,entry['scene'],entry['repeat_id'])] = rows
            summary = load(folder/'summary.json')
            assert summary['verdict'] != 'invalid'
            cases.append(dict(condition=condition, **entry, metrics=summary['metrics'],
                warmup_and_measured_frames=len(rows), normalized_input_sha256=normalized_digest(rows),
                binary_sha256=sha(bundle/'sp_vision_25/build/simulator/sim_auto_aim'),
                phase_offset=meta['diagnostic']['phase_offset'], grain=meta['diagnostic']['grain']))
    assert len(cases)==12 and len({r['binary_sha256'] for r in cases})==1
    controls = []
    for scene in ('hf_uniform_3','hf_turn90_3'):
        for label,left,right in (
                ('fixed-phase-repeat',('p0g05',scene,0),('p0g05',scene,1)),
                ('zero-grain-phase-negative-control',('p0g0',scene,0),('p1024g0',scene,0))):
            a,b=streams[left],streams[right]
            assert len(a)==len(b) and normalized_digest(a)==normalized_digest(b)
            differences={k:sum(x['result'].get(k)!=y['result'].get(k) for x,y in zip(a,b)) for k in STATE_FIELDS}
            assert not any(differences.values()),differences
            controls.append(dict(label=label,scene=scene,frames=len(a),
                input_sha256=normalized_digest(a),differences=differences,
                excluded_header_fields=['session'],excluded_result_fields='Comparison uses all 14 declared substantive fields; pipeline wall time excluded.'))
    first=[]
    for scene in ('hf_uniform_3','hf_turn90_3'):
        base=streams[('p0g05',scene,0)][0]
        for condition in ('p1024g05','p2048g05'):
            new=streams[(condition,scene,0)][0]
            different=[k for k in base['frame'] if k!='session' and base['frame'][k]!=new['frame'].get(k)]
            assert not different and base['jpeg_sha256']!=new['jpeg_sha256']
            assert base['result']['detector_summary']==new['result']['detector_summary']
            a,b=base['result']['track_estimate']['center'],new['result']['track_estimate']['center']
            first.append(dict(scene=scene,condition=condition,seq=base['frame']['seq'],
                header_differences_excluding_session=different,same_detector_roi=True,
                first_image_sha256=[base['jpeg_sha256'],new['jpeg_sha256']],
                first_observations_changed=base['result']['observations']!=new['result']['observations'],
                initialized_center_xy_delta_m=sum((x-y)**2 for x,y in zip(a[:2],b[:2]))**.5,
                baseline_confidence=[r['confidence'] for r in base['result']['observations']],
                changed_confidence=[r['confidence'] for r in new['result']['observations']]))
    result=dict(cases=cases,controls=controls,first_native_noise_effect=first,
        excluded_postmeasurement_tail='Settlement duration differs by terminal-shot timing and wall recorder stop. '
            'Pixel/output controls cover the full warmup and measurement up to recorded end, not asynchronous settlement tails.',
        limitation='Three predefined deterministic phases, not random noise samples, confidence intervals, '
            'or a UI equivalence qualification. Zero grain is a diagnostic negative control only.')
    write(root/'sensor-result.json',result)
    return result


def audit(root):
    import cv2
    import numpy as np
    from background_evidence import audit as measured_audit
    from session_evidence import audit as history_audit
    cv2.setNumThreads(1)
    state=load(root/'sensor-study.json');checks=[]
    for condition,value in state['modes'].items():
        for entry in value['runs']:
            folder=root.parent/entry['run_id'];run=load(folder/'run-state.json')
            measured=measured_audit(folder)
            full=history_audit(folder/'session-evidence',run['measurement_start_stamp_ns'],run['measurement_end_stamp_ns'],folder)
            images=0
            with (folder/'session-evidence/input.mjpg').open('rb') as stream:
                for p in packets(folder/'session-evidence/events.jsonl'):
                    if p['kind']!='input':continue
                    stream.seek(p['jpeg_offset']);data=stream.read(p['jpeg_bytes'])
                    decoded=cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_COLOR)
                    assert decoded is not None and decoded.shape[:2]==(1080,1440)
                    images+=1
            assert measured['passed'] and full['passed'],(entry,measured,full)
            checks.append(dict(condition=condition,**entry,measured=measured,history=full,
                decoded_full_session_jpegs=images))
            print(json.dumps(dict(audit_progress=len(checks),expected=12,run_id=entry['run_id'],passed=True)),flush=True)
    value=dict(passed=True,runs=len(checks),results=checks,
        decoded_full_session_jpegs=sum(p['decoded_full_session_jpegs'] for p in checks),
        decoded_measured_jpegs=sum(p['measured']['decoded_jpegs'] for p in checks),
        decoded_mp4_frames=sum(int(p['measured']['video'].get('nb_read_frames',0)) for p in checks))
    write(root/'evidence-audit.json',value)
    return value


def warmup_validation(root):
    study=load(root/'study.json');confirm=load(root/'warmup-confirmation.json')
    assert study['state']==confirm['state']=='completed'
    assert len(study['results'])==34 and len(confirm['results'])==16
    assert all(p['changed_frames']['observations']==0
        for p in load(root/'warmup-result.json')['comparisons']+confirm['results'])
    preserved=('detector_summary','observations','tracker','continuity','filter_state','track_estimate','selected_plate_id')
    original_matches=[]
    sealed=[]
    for r in study['results']+confirm['results']:
        folder=root/'replays'/r['case'];plan=load(folder/'plan.json')
        data=primary(folder)
        assert data[0]['result']['detector_reset'] is True
        assert not any(p['result']['detector_reset'] for p in data[1:])
        assert sha(Path(plan['bundle'])/'sp_vision_25/build/simulator/sim_auto_aim')==plan['binary_sha256']
        if r['case'].endswith('_full'):
            assert all(r['differences_from_recorded'][k]['different_frames']==0 for k in preserved)
            if r['mode']=='lockstep_session':
                assert not any(v['different_frames'] for v in r['differences_from_recorded'].values())
            _,original=archive(Path(plan['source']),6.)
            clock_residuals=[]
            for p,(_,old) in zip(data,original):
                if p['result']['fire_advice']!=old['result']['fire_advice']:
                    clock_residuals.append(dict(seq=p['frame']['seq'],
                        original_advice=old['result']['fire_advice'],reconstructed_advice=p['result']['fire_advice'],
                        original_fire_check=old['result']['fire_check'],reconstructed_fire_check=p['result']['fire_check'],
                        yaw_delta_deg=abs(p['result']['yaw']-old['result']['yaw'])*180/3.141592653589793,
                        horizon_delta_us=(p['result']['prediction_horizon_s']-old['result']['prediction_horizon_s'])*1e6))
            original_matches.append(dict(case=r['case'],frames=r['frames'],fields=list(preserved),
                fire_advice_different_frames=r['differences_from_recorded']['fire_advice']['different_frames'],
                clock_reconstruction_residuals=clock_residuals))
        sealed.append(dict(case=r['case'],frames=len(data),
            files={name:sha(folder/name) for name in ('plan.json','events.jsonl','result.json')}))
    duplicates=[]
    for r in confirm['results']:
        if r['mode']!='lockstep_session' or not r['case'].endswith('_drop_second'):continue
        other=r['case'].removesuffix('drop_second')+'first_last30'
        a,b=primary(root/'replays'/r['case']),primary(root/'replays'/other)
        assert input_digest(a)==input_digest(b)
        assert all(x['result'].get(k)==y['result'].get(k) for x,y in zip(a,b) for k in STATE_FIELDS)
        duplicates.append(dict(cases=[r['case'],other],frames=len(a)))
    write(root/'replay-seal.json',dict(replays=50,original_history_matches=original_matches,
        identical_history_controls=duplicates,cases=sealed))


def figures(root):
    import cv2
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    cv2.setNumThreads(1)
    fig,axes=plt.subplots(1,2,figsize=(13,4.7),layout='constrained')
    for condition,label in [('full','All 31 warmup inputs'),('last30','Final 30 (first input omitted)'),
            ('drop_second','30 inputs (first retained, second omitted)')]:
        trace=load(root/'traces'/('lockstep_session_hf_turn90_3_0_'+condition+'.json'))
        axes[0].plot([p['time_s'] for p in trace],[p['center_error_m'] for p in trace],label=label,lw=1)
    axes[0].set(xlim=(0,2),xlabel='Scenario time (s)',ylabel='Center XY error (m)',
        title='Same measured JPEGs/poses; warmup history only')
    axes[0].axhline(.5,color='gray',ls=':');axes[0].legend(fontsize=8);axes[0].grid(alpha=.2)
    rows=load(root/'sensor-result.json')['cases']
    for scene,color in [('hf_uniform_3','tab:blue'),('hf_turn90_3','tab:orange')]:
        selected=[next(p for p in rows if p['condition']==c and p['scene']==scene and p['repeat_id']==0)
                  for c in ('p0g05','p1024g05','p2048g05')]
        axes[1].plot([0,1024,2048],[p['metrics']['weighted_hit_rate']*100 for p in selected],
            marker='o',label=scene,color=color)
    axes[1].set(xlabel='Predeclared native sensor phase offset',ylabel='Weighted hit rate (%)',xticks=[0,1024,2048],
        title='Closed loop; original grain strength 0.05')
    axes[1].legend();axes[1].grid(alpha=.2)
    for ext in ('png','svg'):fig.savefig(root/('alignment-causes.'+ext),dpi=160)
    plt.close(fig)
    study=load(root/'sensor-study.json');fig,axes=plt.subplots(1,3,figsize=(13,4.5),layout='constrained')
    sources=[]
    for ax,condition in zip(axes,('p0g05','p1024g05','p2048g05')):
        entry=next(r for r in study['modes'][condition]['runs'] if r['scene']=='hf_uniform_3' and r['repeat_id']==0)
        folder=root.parent/entry['run_id'];first=sensor_records(folder)[0];packet=first['input_packet']
        with (folder/'session-evidence/input.mjpg').open('rb') as stream:
            stream.seek(packet['jpeg_offset']);jpeg=stream.read(packet['jpeg_bytes'])
        image=cv2.imdecode(np.frombuffer(jpeg,np.uint8),cv2.IMREAD_COLOR)
        ax.imshow(cv2.cvtColor(image,cv2.COLOR_BGR2RGB))
        for armor in first['result']['observations']:
            xy=np.asarray(armor['corners']);xy=np.concatenate((xy,xy[:1]));ax.plot(xy[:,0],xy[:,1],color='lime',lw=1)
            ax.text(xy[:,0].min(),xy[:,1].min()-4,f"{armor['confidence']:.5f}",color='white',fontsize=8)
        ax.set(xlim=(560,860),ylim=(710,520),title=condition+'; same initial geometry')
        ax.axis('off');sources.append(dict(condition=condition,run_id=entry['run_id'],stamp_ns=first['frame']['stamp_ns'],jpeg_sha256=first['jpeg_sha256']))
    for ext in ('png','svg'):fig.savefig(root/('native-noise-first-frame.'+ext),dpi=180)
    plt.close(fig)
    write(root/'figure-sources.json',sources)


def report(root):
    result=load(root/'sensor-result.json');warm=load(root/'warmup-result.json')
    confirm=load(root/'warmup-confirmation.json');gate=load(root/'phase-gate.json');cov=load(root/'coverage.json')
    text=['# UI / 后台差异：输入历史、噪声相位与阶段状态','',
        '本轮完成 50 组原始输入回放、12 轮原生渲染闭环对照，以及冻结开火桥的单因素门控复现。'
        '全部采用 tracking_fire_v3 和显式 detector-history=session；没有调整算法、物理或正式 UI 基线。'
        '结果用于定位差异，未授予 UI 等效或新并发资格。','',
        '## 1. 暖机输入历史确实改变滤波与开火建议','',
        '六条 UI 历史有 46–47 个测量前输入，后台有 31 个（30 个完成暖机，另有一个排队输入在开始后处理）。'
        '固定测量阶段所有 JPEG、位姿和时间戳，分别保留完整、末 30、末 15、零暖机；随后补齐保留原始首帧的两个对照。'
        '八条源记录均保留，不按分数选样。','',
        '完整历史回放精确复现八条源记录的检测、滤波和选板；后台两条的 14 项主要输出完全复现。'
        'UI 瞄准时钟由历史姿态年龄重建，六条 UI 中一条有 1 帧开火建议不一致：'
        '该帧预测时域差约 149.82 µs、角度差约 0.00355°，越过了开火对齐阈值。'
        '因此不能宣称 UI 的开火输出也全部逐位一致。暖机组间对照都使用相同的重建时钟。'
        '所有暖机干预组的测量检测结果都与完整组完全相同，'
        '但滤波分支不同。保留首帧、仅删掉第二个暖机更新，也能改变后续状态；所以问题不止是首帧选板。','',
        '| 后台转向回放条件 | 0–1 s 中位中心误差 | 1–2 s 中位中心误差 | 开火建议变化帧数 |',
        '|---|---:|---:|---:|']
    for condition in ('full','last30'):
        p=next(p for p in warm['comparisons'] if p['mode']=='lockstep_session' and p['scene']=='hf_turn90_3' and p['condition']==condition)
        text.append(f"| {condition} | {p['intervals'][0]['median_center_error_m']:.4f} m | {p['intervals'][1]['median_center_error_m']:.4f} m | {p['changed_frames']['fire_advice']} |")
    p=next(p for p in confirm['results'] if p['mode']=='lockstep_session' and p['scene']=='hf_turn90_3' and p['case'].endswith('_drop_second'))
    text += [f"| 保留首帧，仅删第二帧 | {p['intervals'][0]['median_center_error_m']:.4f} m | {p['intervals'][1]['median_center_error_m']:.4f} m | {p['changed_frames']['fire_advice']} |",'',
        '这些是固定原始相机/控制轨迹的开放回放，输出没有反馈给模拟器，不能当作新命中率。'
        '删暖机有时变好、有时变坏；不能据此挑一个帧数当作算法优化。需要对齐明确的暖机输入起点、更新数与阶段边界。','',
        '[34 组主对照](study.json) · [16 组保留首帧确认](warmup-confirmation.json) · [逐组测量比较](warmup-result.json)','',
        '## 2. 相同场景种子没有对齐传感器噪声','',
        '当前 UI 的噪声 seed 来自真实运行时间；lockstep 来自逻辑时间减重置起点。场景 seed 不参与此计算。'
        '因此固定相位后台重复只能证明一条噪声轨迹的可重复性。','',
        '在原生 camera_lens.wgsl 中只给 seed 加预定相位偏置（4096 内取模），维持原始 grain=0.05，'
        '其余镜头效果、模拟器二进制、模型、算法、暖机与延迟均保持冻结。不是给 JPEG 再加噪声。','',
        '| 场景 | 原生相位 | 发数/命中 | 精确命中率 | 加权命中率 | 正确跟踪率 |',
        '|---|---:|---:|---:|---:|---:|']
    for p in result['cases']:
        if p['grain']!=.05 or p['repeat_id']!=0:continue
        m=p['metrics'];text.append(f"| {p['scene']} | {p['phase_offset']} | {m['shots_fired']}/{m['target_armor_hits']} | {m['target_hit_rate']:.2%} | {m['weighted_hit_rate']:.2%} | {m['correct_tracking_fraction']:.2%} |")
    text += ['',
        '第一暖机帧的几何、位姿、时间戳和 ROI 相同，相位变化已使原生 JPEG、观测/PnP 和初始化中心不同。'
        'phase=0 两次重复完全复现；grain=0 时 phase=0/1024 的完整暖机+测量 JPEG、输入头（仅排除随机 session ID）'
        '和 14 项主要算法输出也完全相同。结算尾段因弹丸终止与异步停止时间而长短不同，未冒称整段归档逐字节一致。','',
        '三种相位只用于证明敏感性，不是独立随机样本或置信区间；不能挑表现好的相位作为改进版。'
        'grain=0 也仅是负对照，正式实验继续保留传感器噪声。','',
        '一个派生镜像作业在第一帧之前有两次预配失败：相位变体共用标签导致冻结摘要失效。'
        '已保留失败作业和构建日志，改用唯一标签的新冻结版本继续；没有替换任何已有试验分数。'
        '有效闭环试验仍为预先声明的 12 轮。','',
        '[冻结计划及全部 12 轮](sensor-study.json) · [逐轮指标、像素/输出控制](sensor-result.json)','',
        '## 3. 10 Hz 阶段状态造成计分与开火边界差异','',
        '两条后台参考各有 2 个已测量输入在 Running 状态消息到达前被记录器排除：'
        '相对原有计分起点约 18.75 / 52.08 ms。原始观测完整存在，没有丢 JPEG。六条 UI 参考没有此漏帧。'
        '按缺失帧全部正确/全部错误计算，正确跟踪率最大影响约 1.3 个百分点，无法解释几十个百分点的差距。','',
        '冻结的 Bridge.result 单因素复现还表明：匀速场景在物理开始 ACK 后 28.72 ms 已可接受开火建议，'
        '但实际等到 128.72 ms，阶段门控抑制了 3 个建议，首脉冲多等整整 100 ms。'
        '转向场景前面本来没有有效建议，阶段门控的首脉冲额外延迟为 0；不能把转向问题也归因于这一项。','',
        '现有计分起点由低频状态的 stamp−t_scenario 反推，这两条后台记录比物理开始 ACK 早约 6.25 ms。'
        '精确归因应区分物理 ACK、图像曝光、结果收到与低频显示状态。原有指标本轮未重算覆盖。','',
        '[逐帧覆盖与影响界限](coverage.json) · [原开火桥门控复现](phase-gate.json)','',
        '## 下一步修复顺序','',
        '1. 为记录器和开火桥使用确认过的物理阶段边界；记录器等待迟到状态或暂存早到观测，补上阶段边界覆盖检查。'
        '不要把 10 Hz 展示状态当作精确开火时钟。',
        '2. 显式记录并对齐两侧暖机输入起点、帧数、时间和队列；保留首帧/逐次更新摘要，避免通过调帧数挑分。',
        '3. 为 UI 和后台提供独立、可记录的传感器种子/相位机制；固定噪声序列用于成对比较，多个独立种子用于稳定性验收。'
        '正式输入仍有噪声。',
        '4. 记录瞄准计算和开火判定各自使用的时间，避免只凭 pose_age 重建多个采样点；'
        '固定平均延迟也不能替代真实 UI 的时序波动。',
        '5. 在上述实验条件统一后，再做 UI 对齐与 1/2 路成对实验；当前不扩大 4/8 路。','',
        '## 验证范围','',
        '完整历史复现、测量输入 SHA256、原生无噪声负对照和全会话审计分别验证不同问题。'
        '全部视频已做解码与证据核验；本轮仅检查固定首帧图及因果曲线，没有完成逐条完整视频的人工验收。'
        '未改正式算法、默认模拟器镜像、运行时、容量记录和历史参考。',
        '', '[回放封存](replay-seal.json) · [12 轮证据审计](evidence-audit.json) · [验证摘要](validation.json)']
    content='\n'.join(text)+'\n';(root/'report.md').write_text(content)
    (root/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>输入条件差异核查</title>'
        '<style>body{font:15px system-ui;max-width:1200px;margin:2em auto;padding:0 1em}img{max-width:100%}pre{white-space:pre-wrap}</style>'
        '<h1>UI / 后台输入条件差异核查</h1><img src="alignment-causes.png"><img src="native-noise-first-frame.png">'
        '<pre>'+html.escape(content)+'</pre><p><a href="sensor-result.json">原生噪声结果</a> · '
        '<a href="warmup-result.json">暖机对照</a> · <a href="phase-gate.json">阶段门控证据</a></p>')
    experiments.publish_desktop(manage,root)


def run(root, cached_audit=False):
    warmup_validation(root)
    sensor=sensor_analysis(root)
    evidence=load(root/'evidence-audit.json') if cached_audit else audit(root)
    assert evidence['passed'] and evidence['runs']==12
    if not cached_audit:figures(root)
    base=Path(load(root/'study.json')['bundle'])
    protected=manage.ROOT/'sp_vision_25/tests/auto_aim_test.cpp'
    assert sha(protected)=='7c08603df343de8c503b8e44c3ec07d68f3a6fee0471d7a9666fe890bd3a1ee2'
    unchanged={name:sha(manage.ROOT/name)==sha(base/name) for name in
        ('docker/auto_aim/background_runtime.py','docker/auto_aim/evaluator.py','docker/auto_aim/bridge.py',
         'docker/auto_aim/sync_bridge.py','vision_simulator_26/config.toml',
         'sp_vision_25/build/simulator/sim_auto_aim')}
    assert all(unchanged.values()),unchanged
    defaults=dict(simulator=manage.inspect('daedalus:rm26-integration')['Id'],
        evaluator=manage.inspect('daedalus:rm26-evaluator')['Id'])
    assert defaults==dict(simulator='sha256:fe4cdd4ac839c3dda864d556f01dcc9a911996a759c314805cff0e20dcaa4968',
        evaluator='sha256:996303fb883e68922ea630ac53de2425c62a8ca4874b7cfce024fb5751eca9ad')
    value=dict(passed=True,replay_cases=50,live_cases=12,major_output_control_fields=len(STATE_FIELDS),
        controls=sensor['controls'],evidence=dict(runs=evidence['runs'],
            decoded_full_session_jpegs=evidence['decoded_full_session_jpegs'],
            decoded_measured_jpegs=evidence['decoded_measured_jpegs'],decoded_mp4_frames=evidence['decoded_mp4_frames']),
        runtime_unchanged=unchanged,default_images=defaults,protected_file_unchanged=True,
        sources={p.name:sha(p) for p in Path(__file__).parent.glob('sync_*') if p.name in
            ('sync_input_alignment.py','sync_sensor_phase_study.py','sync_phase_gate_probe.py','sync_input_alignment_report.py')},
        complete_human_video_acceptance=False,figures_visually_reviewed=True,
        ui_equivalence_certified=False,capacity_registered=False)
    write(root/'validation.json',value)
    report(root)
    print(json.dumps({k:v for k,v in value.items() if k!='controls'},indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    p.add_argument('--action',choices=['all','offline','finish'],default='all')
    args=p.parse_args();root=args.output.resolve()
    if args.action=='offline':audit(root);figures(root)
    else:run(root,cached_audit=args.action=='finish')
