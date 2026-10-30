"""Reconcile causal proof, scoped fix, live results and runtime maintenance."""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import statistics

import background_capacity as capacity
import background_jobs as jobs
import experiments
import manage
from report import load,write
from sync_continuity_report import document
from sync_study import row


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate(root):
    causal=load(root/'causal-result.json');assert causal['passed'] and len(causal['live'])==8
    checks=load(root/'unit-checks.json');protocol=load(root/'protocol/protocol-checks.json')
    evidence=load(root/'evidence-audit.json');live=load(root/'live-evidence-audit.json')
    assert checks['passed'] and protocol['passed'] and evidence['passed'] and live['passed']
    assert evidence['runs']==40 and live['runs']==8
    plans=sorted(root.rglob('plan.json'));assert len(plans)==42
    source_hashes={};sealed={}
    for path in plans:
        plan=load(path);source=Path(plan['source'])/'session-evidence/input.mjpg'
        if str(source) not in source_hashes:source_hashes[str(source)]=sha(source)
        assert source_hashes[str(source)]==plan['source_archive_sha256']
        assert sha(Path(plan['bundle'])/'sp_vision_25/build/simulator/sim_auto_aim')==plan['binary_sha256']
        assert plan['truth_in_algorithm'] is False
        for name in ('plan.json','events.jsonl','result.json'):
            p=path.with_name(name);sealed[str(p.relative_to(root))]=sha(p)
    write(root/'replay-evidence-sha256.json',sealed)
    before=Path(load(root/'study.json')['bundle']);after=Path(load(root/'candidate.json')['bundle'])
    a=load(before/'bundle.json');b=load(after/'bundle.json')
    changes=[name for name,value in a['files'].items() if name.startswith('sp_vision_25/') and b['files'].get(name)!=value]
    expected=['sp_vision_25/build/simulator/sim_auto_aim','sp_vision_25/src/sim_auto_aim.cpp',
        'sp_vision_25/tasks/auto_aim/yolo.hpp','sp_vision_25/tasks/auto_aim/yolos/yolov5.hpp']
    assert sorted(changes)==sorted(expected),changes
    math_names=['tasks/auto_aim/'+name for name in ('solver.cpp','tracker.cpp','target.cpp','aimer.cpp','shooter.cpp','plate_filter.hpp','continuity.hpp')]
    numerical={name:sha(before/'sp_vision_25'/name)==sha(after/'sp_vision_25'/name) for name in math_names}
    assert all(numerical.values()),numerical
    patch=[]
    for name in changes:
        if name.endswith('sim_auto_aim'):continue
        patch+=list(difflib.unified_diff((before/name).read_text().splitlines(True),(after/name).read_text().splitlines(True),
            fromfile='before/'+name,tofile='after/'+name))
    (root/'candidate-changes.diff').write_text(''.join(patch))
    protected=sha(manage.ROOT/'sp_vision_25/tests/auto_aim_test.cpp')
    assert protected=='7c08603df343de8c503b8e44c3ec07d68f3a6fee0471d7a9666fe890bd3a1ee2'
    for name in ('bridge.py','background_runtime.py','background_jobs.py','background_capacity.py','experiments.py','manage.py','algorithm_runner.py','shots.py'):
        assert sha(manage.OPS/name)==b['files']['docker/auto_aim/'+name],name
    maintained=load(root/'runtime-maintenance.json');benchmark=load(Path(maintained['benchmark']))
    assert benchmark['state']=='completed' and benchmark['recommended_parallelism']==2
    assert len(benchmark['results'])==2 and all(x['healthy'] for x in benchmark['results'])
    default_job=dict(spec=json.dumps({'_protocol':2}),parallelism=0,exploratory=0,bundle=benchmark['bundle'],
        settings=json.dumps(dict(execution_profile='backend-240-v1',detector_history='process')))
    default_lanes=capacity.limit(default_job,load(jobs.STATE/'capacity.json'),{})
    reset_job=dict(default_job,settings=json.dumps(dict(detector_history='session')))
    assert default_lanes==2 and capacity.limit(reset_job,load(jobs.STATE/'capacity.json'),{})==1
    times=[load(manage.OPS/'experiments'/r['run_id']/'timing.json')['wall_seconds'] for r in causal['live']]
    assert max(times)<40
    with jobs.connect() as db:
        active=db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running','pausing')").fetchone()[0]
        media=db.execute("SELECT count(*) FROM artifacts WHERE state IN ('queued','running')").fetchone()[0]
    assert active==0 and media==0,(active,media)
    vt=Path('/sys/class/tty/tty0/active').read_text().strip();assert vt=='tty8'
    result=dict(passed=True,unit_checks=checks,protocol_checks=dict(passed=True,count=len(protocol['checks'])),
        evidence_audit={k:evidence[k] for k in ('passed','runs','decoded_jpegs','decoded_mp4_frames')},
        live_history_audit={k:live[k] for k in ('passed','runs','decoded_full_session','decoded_mp4_frames')},
        restored_ui_runs=6,restored_ui_frames=causal['restored_ui_frames'],session_reset_cases=8,
        replay_cases=6+12+24,algorithm_changes=changes,numerical_core_unchanged=numerical,
        original_binary_sha256=sha(before/'sp_vision_25/build/simulator/sim_auto_aim'),
        candidate_binary_sha256=sha(after/'sp_vision_25/build/simulator/sim_auto_aim'),protected_test_sha256=protected,
        ordinary_realtime_auto_parallelism=default_lanes,session_reset_auto_parallelism=1,
        maximum_live_trial_wall_s=max(times),active_jobs=active,active_media=media,active_vt=vt,
        ui_equivalence_certified=False,human_video_acceptance='not_completed',
        causal_scope='Detector-history initialization mismatch; sensor phase, warmup duration and closed-loop UI equivalence remain separate.')
    write(root/'validation.json',result);return result


def report(root):
    c=load(root/'causal-result.json');v=validate(root)
    aggregates=[]
    for mode in ('ui_session','lockstep_session'):
        for scene in ('hf_uniform_3','hf_turn90_3'):
            entries=[e for e in c['live'] if e['mode']==mode and e['scene']==scene]
            scored=[row(manage.OPS/'experiments'/e['run_id'],root/'rescore'/e['run_id'],e['repeat_id']) for e in entries]
            shots=sum(r['metrics']['shots_fired'] for r in scored)
            aggregates.append(dict(mode=mode,scene=scene,runs=len(scored),shots=shots,
                exact=sum(r['metrics']['target_armor_hits'] for r in scored)/shots,
                weighted=sum(r['metrics']['weighted_hit_score_sum'] for r in scored)/shots,
                correct_tracking=statistics.mean(r['metrics']['correct_tracking_fraction'] for r in scored)))
    write(root/'live-aggregates.json',aggregates)
    lines=['# 暖机首帧分叉：具体原因、修复与验证','',
        '结论：已通过同图、同姿态、同时间的受控回放定位一项具体原因：UI 会话重置了跟踪器，但沿用上一会话的检测区域/扫描历史；新后台进程没有这段历史。新增显式会话重置策略已消除这项前史影响。整体 UI 等效仍未认证。','',
        '## 因果证据','',
        '1. 直接使用原冻结算法的在线接口回放后台完整暖机与测量前两秒，两次均逐帧重现检测、位姿、滤波、瞄准和开火建议。',
        '2. 对六段原 UI 输入，主输入 JPEG、姿态、标定与时间保持相同，只改变检测区域缓存前史。恢复各自原缓存区域后，共 663 帧检测、位姿、滤波状态、装甲选择和开火建议与原 UI 记录完全一致。',
        '3. 五段 UI 记录的冷启动滤波结果发生分叉；一段原缓存区域恰好等于冷启动区域，检测与滤波本来就一致，是负对照。',
        '4. 新策略下，八组输入分别做进程冷启动、会话冷启动、预热后新会话三种回放。三者主输入哈希一致，检测、滤波、瞄准和开火建议一致。',
        'UI 时间映射仍有边界：原 UI 内部在不同位置读取真实时钟，回放使用记录的自身姿态年龄恢复决策时间。恢复缓存后，滤波完全一致，瞄准角的最大残差低于 0.006 度；不能把这一重建近似称为全部在线时钟逐位一致。','',
        '| 场景 / repeat | 原缓存区域起点 | 帧数 | 冷启动滤波不同帧数 | 恢复缓存后不同帧数 |',
        '|---|---|---:|---:|---:|']
    for r in c['restored_ui_runs']:
        lines.append(f"| {r['scene']} / {r['repeat_id']} | {r['roi'][:2]} | {r['frames']} | {r['cold_filter_different_frames']} | 0 |")
    lines+=['','## 对应代码与机制','',
        '`YOLOV5::detect()` 依据 cached_roi_、cached_failures_ 和 search_index_ 决定第二次局部检测的输入区域。同一 JPEG 在不同裁剪位置下，会给出不同的置信度和亚像素角点。',
        '`Tracker::track_robust()` 初始化会按质量与置信度选择装甲；`Solver` 位姿和 `Target` 初始中心由此不同，随后滤波先验与选板持续分叉。旧适配器在 session 改变时重建 Pipeline，却没有重置外部 YOLO 对象的区域历史。',
        '[本轮最小代码修改](candidate-changes.diff) · [原回放计划](study.json) · [六段确认](confirmation/confirmation.json) · [修复验证](verification/verification.json)','',
        '## 同输入下的定位影响','',
        '以下使用同一段后台原始相机轨迹，只改变检测缓存。真值仅在事后评分时使用；新命令没有反馈到模拟器，因此这些数据不是新的闭环命中率。','',
        '| 时间段 | 原冷启动中心误差中位数 | 改变缓存后 |','|---|---:|---:|']
    for bounds in ([0,1],[1,2]):
        a=next(r for r in c['fixed_input_cache_effect'] if r['case']=='backend-cold-0' and r['time_range_s']==bounds)
        b=next(r for r in c['fixed_input_cache_effect'] if r['case']=='backend-primed-0' and r['time_range_s']==bounds)
        lines.append(f"| {bounds[0]}～{bounds[1]} 秒 | {a['median_center_xy_error_m']:.3f} 米 | {b['median_center_xy_error_m']:.3f} 米 |")
    lines+=['',
        '缓存改变了收敛路径，早段也可能更差。修复采用明确、可复现的会话初始状态，没有按较高命中率选择某个缓存区域。',
        '[因果曲线 PNG](causal-curves.png) · [SVG](causal-curves.svg) · [同一图像的角点与选板](same-input-corners.png) · [图像来源](figure-sources.json)','',
        '## 已实施的修复','',
        '`--detector-history session` 在输入 session 改变时清空区域缓存、失败计数与扫描索引，复用已加载模型。实验将记录策略，并强制完整会话证据，验证第一帧确实重置。不同策略使用不同基准键。',
        '普通手动运行和旧冻结任务继续采用原 process 策略。新策略暂为显式诊断选项，自动并发限单路。检测阈值、PnP、滤波、预测、物理和命中公式没有调整。',
        '实验命令可添加 `--detector-history session`；手动 start/restart 也支持同名选项。','',
        '## 实际 UI / 后台确认','',
        '八轮实际实验均确认会话重置生效，首帧 cached_tile=false、扫描区域一致。UI 三次/场景，后台一次/场景；本表为有限样本描述，不用于 UI 等效认证。','',
        '| 模式 | 场景 | 轮数 | 发数 | 精确命中 | 加权命中 | 正确跟踪 |','|---|---|---:|---:|---:|---:|---:|']
    for r in aggregates:
        lines.append(f"| {r['mode']} | {r['scene']} | {r['runs']} | {r['shots']} | {r['exact']:.2%} | {r['weighted']:.2%} | {r['correct_tracking']:.2%} |")
    lines+=['',
        '后台分数与旧冷启动结果相同；UI 即使清空前史仍有波动。初始缓存不一致已被隔离，但它没有解释全部 UI / 后台闭环差异。',
        '剩余已知条件：UI 暖机 46～47 帧，后台固定处理 30 帧；UI 传感器噪声取真实运行时钟，后台噪声相位取固定仿真时钟。场景 seed 没有统一这两种相位。下一项应对齐传感器种子/采样相位与暖机契约，评估多条噪声轨迹，而不是反复复制一条固定噪声轨迹。','',
        '## 验证与容量维护','',
        f"- {v['unit_checks']['count']} 项单元检查、{v['protocol_checks']['count']} 项真实步进协议检查通过。",
        f"- {v['replay_cases']} 组在线接口回放、8 轮实际实验通过本轮相应检查；所有候选与原记录分别保存。",
        f"- 实际实验与容量基准共 {v['evidence_audit']['runs']} 轮证据审计通过；8 轮实际实验完整会话原图额外逐张解码。",
        f"- 八轮实际实验单轮最长 {v['maximum_live_trial_wall_s']:.2f} 秒；VT8 保持使用。",
        '- 新运行入口改变了容量签名，已按现有标准重新完成 16 轮单路、16 轮双路基准。普通实时自动双路恢复；新会话策略仍为单路，未授予四路/八路或 UI 效果资格。',
        '- 原 protected auto_aim_test.cpp 哈希保持不变，数值核心源码与模型配置未变；新增的是缓存生命周期 API 和模拟器适配器开关。',
        '- 完整人工视频验收尚未完成。','',
        '[最终校验](validation.json) · [原图与视频审计](evidence-audit.json) · [会话历史审计](live-evidence-audit.json) · [容量复核](runtime-maintenance.json) · [完整因果数据](causal-result.json)','',
        '## 实际实验录像','']
    for r in c['live']:lines.append(f"- {r['mode']} / {r['scene']} / {r['repeat_id']}：[报告与录像](../{r['run_id']}/report.html)")
    text='\n'.join(lines)+'\n';(root/'report.md').write_text(text);(root/'report.html').write_text(document(lines))
    experiments.publish_desktop(manage,root)
    return v


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    print(json.dumps(report(args.output.resolve()),indent=2))
