#!/usr/bin/env python3
"""Offline 2 x 2 gimbal comparison; never publishes commands or changes verdicts."""
import argparse
import bisect
import collections
import hashlib
import html
import json
import math
from pathlib import Path
import tomllib

from diagnostics import analyze, jsonl, load, quantile, summarize, table, wrap, write

PROFILES = ('legacy', 'timing', 'feedforward', 'combined')


def controller_metrics(folder, shots):
    traces = []
    for row in jsonl(folder/'telemetry.jsonl'):
        if row['kind'] == 'gimbal_trace':
            traces.append(row['data'])
    traces.sort(key=lambda row: row['stamp_ns'])
    stamps = [row['stamp_ns'] for row in traces]
    steps, elapsed = 0, 0
    for left, right in zip(traces, traces[1:]):
        dt = (right['stamp_ns']-left['stamp_ns'])*1e-9
        count = right['control_steps']-left['control_steps']
        if 0 < dt <= .1 and count >= 0:
            steps += count
            elapsed += dt
    accepted_errors, ages = [], []
    for shot in shots:
        i = bisect.bisect_right(stamps, shot['fire_stamp_ns'])-1
        if i < 0 or shot['fire_stamp_ns']-stamps[i] > 30_000_000:
            continue
        trace = traces[i]
        if trace['accepted_wall_ns'] > shot['fire_stamp_ns']:
            continue
        velocity = shot.get('muzzle_relative_velocity',shot['initial_velocity'])
        error = wrap(trace['target_yaw_rad']-math.atan2(velocity[1], velocity[0]))*180/math.pi
        shot['accepted_command_yaw_error_deg'] = error
        shot['accepted_command_trace_age_ms'] = (shot['fire_stamp_ns']-trace['stamp_ns'])/1e6
        shot['accepted_command_age_ms'] = (shot['fire_stamp_ns']-trace['accepted_wall_ns'])/1e6
        shot['accepted_command_stamp_ns'] = trace['command_stamp_ns']
        if shot.get('direction_at_fire'):
            sign = 1 if shot['direction_at_fire'] == 'plus_y' else -1
            directional_error = error*sign*shot.get('relative_forward_x_sign',1)
            shot['accepted_command_directional_error_deg'] = directional_error
            accepted_errors.append(directional_error)
        ages.append(shot['accepted_command_age_ms'])
    profile = load(folder/'manifest.json')['gimbal_profile']
    expected_fixed = profile in ('timing', 'combined')
    expected_gain = 1.0 if profile in ('feedforward', 'combined') else 0.0
    if not traces or any(row['fixed_update'] != expected_fixed or row['feedforward_gain'] != expected_gain for row in traces):
        raise RuntimeError('Controller trace does not match profile: '+folder.name)
    if expected_fixed and any(abs(row['dt_s']-1/120) > .00001 for row in traces if row['dt_s'] > 0):
        raise RuntimeError('Fixed controller dt differs from 120 Hz')
    return {'trace_packets': len(traces), 'control_hz_observed': steps/elapsed if elapsed else None,
        'control_dt_median_s': quantile([row['dt_s'] for row in traces]),
        'feedforward_yaw_abs_median_rad_s': quantile([abs(row['feedforward_world_rad_s'][0]) for row in traces]),
        'accepted_command_directional_error_median_deg': quantile(accepted_errors),
        'accepted_command_absolute_error_median_deg': quantile([abs(v) for v in accepted_errors]),
        'accepted_command_directional_error_abs_p95_deg': quantile([abs(v) for v in accepted_errors], .95),
        'accepted_command_age_median_ms': quantile(ages),
        'shot_trace_matched': len(ages), 'profile_trace_verified': True}


def batch_runs(path):
    batch = load(path)
    if not batch['complete']:
        raise RuntimeError('Incomplete batch: '+str(path))
    return [(path.parent/row['run_id'], row) for row in batch['runs']]


def fmt(value, digits=2):
    return f'{value:.{digits}f}' if value is not None else 'n/a'


def control_summary(rows):
    result = summarize(rows)
    errors = [r['accepted_command_directional_error_deg'] for r in rows if r.get('accepted_command_directional_error_deg') is not None]
    result.update(accepted_error_samples=len(errors),
        accepted_error_median_deg=quantile(errors),
        accepted_error_abs_p95_deg=quantile([abs(v) for v in errors], .95))
    return result


def plot_groups(output, groups):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, 3, figsize=(14, 4))
    for offset, physics in ((-.18, 'no-drag'), (.18, 'drag')):
        selected = [groups[mode+'/'+physics] for mode in PROFILES]
        x = [index+offset for index in range(4)]
        axes[0].bar(x, [g['front_hit_rate']*100 for g in selected], .36, label=physics)
        axes[1].bar(x, [g['accepted_absolute_error_median_deg'] or 0 for g in selected], .36, label=physics)
        axes[2].bar(x, [g['firing_hz_median'] for g in selected], .36, label=physics)
    for axis, label in zip(axes, ('Front plate hit %', 'Absolute yaw error median (deg)', 'Shots / measurement second')):
        axis.set_xticks(range(4), PROFILES, rotation=15)
        axis.set_ylabel(label)
        axis.axhline(0, color='grey', linewidth=.6)
        axis.legend()
        axis.grid(axis='y', alpha=.2)
    axes[0].set_ylim(0, 100)
    axes[2].axhline(1, color='orange', linestyle='--', linewidth=.8)
    fig.tight_layout()
    fig.savefig(output/'comparison.png', dpi=150)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirmation', type=Path, required=True)
    parser.add_argument('--regression', type=Path, nargs='*', default=[])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reuse', action='store_true', help='Reuse unchanged per-run diagnostic artifacts')
    parser.add_argument('--no-fire-run', type=Path, help='Completed track-mode experiment for the fire-off regression')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    records = [(folder, row, 'confirmation') for folder, row in batch_runs(args.confirmation)]
    for batch in args.regression:
        records.extend((folder, row, 'regression') for folder, row in batch_runs(batch) if row['scene'] in ('static', 'spin'))
    counts = collections.Counter((row['shooting']['gimbal_profile'], row['shooting']['physics']) for _, row, role in records if role == 'confirmation')
    if counts != collections.Counter({(mode, physics): 3 for mode in PROFILES for physics in ('no-drag', 'drag')}):
        raise RuntimeError('Confirmation requires exactly three line runs per profile/physics')
    if any(row['scene'] != 'line' for _, row, role in records if role == 'confirmation'):
        raise RuntimeError('Unexpected confirmation scene')
    runs, all_shots = [], []
    signatures = collections.defaultdict(set)
    for folder, entry, role in records:
        manifest = load(folder/'manifest.json')
        if entry['verdict'] == 'invalid': raise RuntimeError('Invalid run included')
        for key in ('algorithm_binary_sha256', 'algorithm_weights_sha256'):
            signatures[key].add(json.dumps(manifest[key], sort_keys=True))
        signatures['simulator_image'].add(manifest['containers']['simulator_image_id'])
        signatures['algorithm_yaml'].add(manifest['configuration_sha256']['algorithm.yaml'])
        config = tomllib.loads((folder/'configuration/simulator.toml').read_text())
        pid = config['vehicle']['gimbal_pid']
        signatures['pid_axis_gains'].add(json.dumps([pid['yaw'], pid['pitch']], sort_keys=True))
        cached = args.output/folder.name
        if args.reuse and (cached/'summary.json').exists():
            result, shots = load(cached/'summary.json'), load(cached/'shots.json')
            for name, expected in result['source_sha256'].items():
                if hashlib.sha256((folder/name).read_bytes()).hexdigest() != expected:
                    raise RuntimeError('Cached diagnostic source changed: '+folder.name+'/'+name)
        else:
            result, _, shots = analyze(folder, cached)
        control = controller_metrics(folder, shots)
        result.update(role=role, controller=control)
        write(args.output/folder.name/'summary.json', result)
        write(args.output/folder.name/'shots.json', shots)
        table(args.output/folder.name/'shots.csv', shots)
        for shot in shots:
            shot['gimbal_profile'] = manifest['gimbal_profile']
            shot['role'] = role
        all_shots.extend(shots)
        runs.append(result)
        print('Compared', role, manifest['gimbal_profile'], folder.name, flush=True)
    if any(len(values) != 1 for values in signatures.values()):
        raise RuntimeError('Non-controller inputs changed between conditions: '+str({k:len(v) for k,v in signatures.items()}))
    groups = {}
    for mode in PROFILES:
        for physics in ('no-drag', 'drag'):
            selected = [r for r in runs if r['role'] == 'confirmation' and r['gimbal_profile'] == mode and r['physics'] == physics]
            shots = [r for r in all_shots if r['role'] == 'confirmation' and r['gimbal_profile'] == mode and r['physics'] == physics]
            moving = [r for r in shots if r.get('accepted_command_directional_error_deg') is not None]
            n, hits, front = sum(r['shots'] for r in selected), sum(r['target_hits'] for r in selected), sum(r['front_hits'] for r in selected)
            groups[mode+'/'+physics] = {'runs': len(selected), 'shots': n, 'target_hits': hits,
                'front_hits': front, 'front_hit_rate': front/n if n else 0,
                'front_hit_rate_run_range': [min(r['front_hits']/r['shots'] for r in selected), max(r['front_hits']/r['shots'] for r in selected)],
                'firing_hz_median': quantile([r['firing_hz'] for r in selected]),
                'front_hits_per_s_median': quantile([r['front_hits_per_s'] for r in selected]),
                'accepted_directional_error_median_deg': quantile([r['accepted_command_directional_error_deg'] for r in moving]),
                'accepted_absolute_error_median_deg': quantile([abs(r['accepted_command_directional_error_deg']) for r in moving]),
                'accepted_directional_error_abs_p95_deg': quantile([abs(r['accepted_command_directional_error_deg']) for r in moving], .95),
                'controller_dt_s': quantile([r['controller']['control_dt_median_s'] for r in selected]),
                'observed_control_hz': quantile([r['controller']['control_hz_observed'] for r in selected]),
                'phases': {key: control_summary([r for r in shots if r['phase'] == key]) for key in ('steady', 'reversal_0_1s')},
                'directions': {key: control_summary([r for r in shots if r.get('direction_at_fire') == key]) for key in ('plus_y', 'minus_y')},
                'at_fire': summarize(shots)}
    effects = {}
    for physics in ('no-drag', 'drag'):
        g = {mode: groups[mode+'/'+physics] for mode in PROFILES}
        effects[physics] = {name: {metric:g[b][metric]-g[a][metric] for metric in ('front_hit_rate', 'firing_hz_median', 'front_hits_per_s_median')}
            for name,a,b in (('timing_without_feedforward', 'legacy', 'timing'), ('feedforward_without_timing', 'legacy', 'feedforward'),
                ('timing_with_feedforward', 'feedforward', 'combined'), ('feedforward_with_timing', 'timing', 'combined'), ('combined_vs_legacy', 'legacy', 'combined'))}
    excluded = {}
    batch = load(args.confirmation)
    ancestor = batch.get('resumed_from')
    while ancestor:
        prior = load(args.confirmation.parent/(ancestor+'.json'))
        for row in prior['runs']:
            if row['verdict'] == 'invalid':
                excluded[row['run_id']] = [r for r in load(args.confirmation.parent/row['run_id']/'summary.json')['rules'] if not r['passed']]
        ancestor = prior.get('resumed_from')
    no_fire = None
    if args.no_fire_run:
        check = load(args.no_fire_run/'summary.json')
        state = load(args.no_fire_run/'run-state.json')
        if check['mode'] != 'track' or state['fire_events'] or state['fire_advice_messages'] or any(not r['passed'] for r in check['rules'] if r['category'] in ('validity','safety')):
            raise RuntimeError('Fire-off regression did not pass')
        no_fire = {'run_id':args.no_fire_run.name,'fire_events':state['fire_events'],
            'fire_pulses':state['fire_advice_messages'],'wall_seconds':load(args.no_fire_run/'timing.json')['wall_seconds']}
    data = {'runs': runs, 'groups': groups, 'effects': effects, 'excluded_runs': excluded,'no_fire_regression':no_fire,
        'signatures': {k:list(v) for k,v in signatures.items()},
        'limits': 'Closed-loop rendered inputs differ by controller; not replay of identical images. Three independent starts per condition. Timing includes frequency, fresh hierarchy and post-propagation ROS launch. No GT in feedforward. No formal baseline or visual pass assigned.'}
    write(args.output/'comparison.json', data)
    table(args.output/'runs.csv', [{k:v for k,v in r.items() if not isinstance(v, (dict, list))} for r in runs])
    table(args.output/'all-shots.csv', all_shots)
    plot_groups(args.output, groups)
    lines = ['# 云台时序与角速度前馈对照', '',
        '算法二进制、模型、算法 YAML、PID 增益、开火门限与 v3 命中判定保持不变；同一新模拟器镜像包含四种可选配置。', '',
        '## 本轮结论', '']
    for physics in ('no-drag', 'drag'):
        baseline, candidate = groups['legacy/'+physics], groups['feedforward/'+physics]
        lines.append(f"- {physics}：仅前馈正面命中 {candidate['front_hits']}/{candidate['shots']}（{candidate['front_hit_rate']:.1%}），原控制 {baseline['front_hits']}/{baseline['shots']}（{baseline['front_hit_rate']:.1%}）；角误差绝对值中位数由 {baseline['accepted_absolute_error_median_deg']:.2f}° 变为 {candidate['accepted_absolute_error_median_deg']:.2f}°。")
    lines += ['- 本轮主要改善来自前馈；仅时序调整在两种阻力条件下未表现出一致收益，组合也没有显示超出仅前馈的收益。三次重复不足以证明两种前馈配置之间的小差异。',
        '- 带前馈配置在换向后第一秒的正面命中仍低于稳定运动阶段。下一步应独立分析换向时的估计恢复和开火时机。']
    for mode in ('legacy', 'feedforward', 'combined'):
        spin = [r for r in runs if r['role'] == 'regression' and r['gimbal_profile'] == mode and r['scene'] == 'spin']
        lines.append(f"- {mode} 旋转回归：{sum(r['target_hits'] for r in spin)}/{sum(r['shots'] for r in spin)}。每个物理条件仅一轮，不足以断言退化或通过泛化验收。")
    lines += ['- 优先复核仅前馈旋转回归的未命中片段，再决定仅前馈或组合配置；代码与当前运行配置仍保留 legacy，控制和射击在实验结束后关闭。', '',
        '- legacy：原渲染循环与原 ROS 出膛时序，无前馈。',
        '- timing：120 Hz 控制、实时计算层级位姿、位姿传播后 ROS 出膛，无前馈。',
        '- feedforward：原时序，加入角度指令变化率前馈。',
        '- combined：时序调整与前馈组合。', '',
        '前馈增益 1，时间常数 60 ms，指令角速度限幅 0.6 rad/s；指令间隔超过 150 ms、非递增时间戳或角度跳变超过 0.06 rad 时清零。目标真值只用于评分。', '',
        '| 配置 | 物理 | 正面命中 | 命中率 | 三次命中率范围 | 发射 Hz 中位数 | 正面命中/秒中位数 | 指令超前枪口的有符号误差中位数 | 角误差绝对值中位数 |', '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for mode in PROFILES:
        for physics in ('no-drag', 'drag'):
            g = groups[mode+'/'+physics];lo, hi = g['front_hit_rate_run_range']
            lines.append(f"| {mode} | {physics} | {g['front_hits']}/{g['shots']} | {g['front_hit_rate']:.1%} | {lo:.1%}–{hi:.1%} | {g['firing_hz_median']:.2f} | {g['front_hits_per_s_median']:.2f} | {fmt(g['accepted_directional_error_median_deg'])}° | {fmt(g['accepted_absolute_error_median_deg'])}° |")
    lines += ['', '## 实际控制节拍', '', '| 配置 | 物理 | 控制步长中位数 | 遥测估计控制频率 |', '|---|---|---:|---:|']
    for mode in PROFILES:
        for physics in ('no-drag', 'drag'):
            g = groups[mode+'/'+physics]
            lines.append(f"| {mode} | {physics} | {g['controller_dt_s']*1000:.3f} ms | {fmt(g['observed_control_hz'])} Hz |")
    lines += ['', '原渲染循环在此机器上可能高于 120 Hz。固定控制频率改善节拍确定性，不能直接理解为提高控制频率；稳态跟随误差还取决于控制律。']
    lines += ['', '## 单项与组合差异', '', '| 物理 | 对比 | 正面命中率差 | 正面命中/秒中位数差 |', '|---|---|---:|---:|']
    for physics, rows in effects.items():
        for key, value in rows.items():
            lines.append(f"| {physics} | {key} | {value['front_hit_rate']*100:+.1f} 百分点 | {value['front_hits_per_s_median']:+.2f} |")
    lines += ['', '## 稳定运动与换向', '', '| 配置 | 物理 | 阶段 | 正面命中 | 已接收指令超前枪口中位数 |', '|---|---|---|---:|---:|']
    for mode in PROFILES:
        for physics in ('no-drag', 'drag'):
            for name, phase in groups[mode+'/'+physics]['phases'].items():
                lines.append(f"| {mode} | {physics} | {name} | {phase['front_hit_count']}/{phase['samples']} | {fmt(phase['accepted_error_median_deg'])}° |")
    lines += ['', '角误差只统计可对齐且明显运动的射击；命中率分母仍为实际出膛数。有符号误差接近零不代表绝对误差为零。', '',
        '## 静止与旋转回归', '', '| 配置 | 场景 | 物理 | 目标装甲命中 | 正面 slot 0 | 规则结论 |', '|---|---|---|---:|---:|---|']
    for r in runs:
        if r['role'] != 'regression': continue
        front = f"{r['front_hits']}/{r['shots']}" if r['front_hits'] is not None else '不适用'
        lines.append(f"| {r['gimbal_profile']} | {r['scene']} | {r['physics']} | {r['target_hits']}/{r['shots']} | {front} | {r['verdict']} |")
    misses = [s for s in all_shots if s['role']=='regression' and s['gimbal_profile']=='feedforward' and s['scene']=='spin' and s['outcome']!='target_armor']
    if misses:
        lines += ['', '### 仅前馈旋转未命中样本', '', '| 物理 | 弹号 | 出膛时间 | 已接收指令与枪口 yaw 差 | 最新图像开火建议 | 脉冲到出膛 |', '|---|---:|---:|---:|---|---:|']
        for s in misses:
            lines.append(f"| {s['physics']} | {s['shot_seq']} | {s['fire_time_s']:.3f} s | {fmt(s.get('accepted_command_yaw_error_deg'))}° | {s.get('fire_advice_at_latest_image')} | {fmt(s.get('fire_pulse_to_launch_ms'))} ms |")
        lines += ['', '这些样本需要同时检查瞬时角差和开火脉冲时序。最新图像建议为 False 的射击仍可能由此前挂起的脉冲触发；不能据此单独认定预测符号或前馈参数错误。对应 missed 视频在下方记录中。']
    lines += ['', '## 结果边界与运行状态', '',
        f"- 正式平移对照 24 次，静止/旋转回归 {sum(r['role']=='regression' for r in runs)} 次；另保留 {len(excluded)} 次无效记录并按相同条件补采。组合配置首轮平移试验未纳入正式对照。",
        f"- 单次准备、测量、结算及原始报告用时 {min(r['wall_seconds'] for r in runs):.2f}–{max(r['wall_seconds'] for r in runs):.2f} 秒；批次离线汇总另计。",
        '- 每组仅三次，范围展示重复差异，不声称实车标定或确定的泛化命中率。',
        '- 闭环控制会改变后续图像输入，算法参数保持相同，但不是同一录像回放。',
        '- timing 同时调整控制频率、位姿读取及出膛顺序；本轮只能评价这组时序调整，不能再拆分各自贡献。',
        '- 枪口误差优先匹配模拟器已接收的角度指令；控制跟踪消息为渲染帧时间戳，匹配误差上限 30 ms。',
        '- 原碰撞层仍允许正面脱靶后命中侧面/背面；正面率与任意装甲率分开统计。',
        '- 代码默认仍为 legacy；未自动建立正式基线，所有实验等待人工视觉验收。',
        '- 固定频率由遥测核对为 1/120 秒；重力、阻力、目标场景与开火最大 2 Hz 保持原设置。']
    for name, reasons in excluded.items(): lines.append('- 无效保留 '+name+'：'+json.dumps(reasons, ensure_ascii=False))
    if no_fire:
        lines += ['', '## 关闭射击回归', '',
            f"组合配置平移跟踪：实际发射 {no_fire['fire_events']}，开火脉冲 {no_fire['fire_pulses']}；完整用时 {no_fire['wall_seconds']:.2f} 秒，安全与有效性规则通过。",
            '原始记录：'+no_fire['run_id']]
    markdown = '\n'.join(lines)+'\n'
    (args.output/'report.md').write_text(markdown)
    body = '<h1>云台控制对照</h1><img src="comparison.png" style="max-width:100%"><pre>'+html.escape(markdown)+'</pre>'
    for run in runs:
        name = run['run_id']
        body += f'<h2>{html.escape(run["gimbal_profile"])} / {html.escape(name)}</h2><p><a href="{name}/shots.csv">逐发诊断</a> · <a href="../{name}/report.html">原始报告</a></p><img src="{name}/diagnosis.png" loading="lazy" style="max-width:100%">'
        for clip in run['clips']:
            body += f'<p>{html.escape(clip["name"])} / shot {clip["shot_seq"]}</p><video width="720" controls preload="none" src="{name}/{clip["file"]}"></video>'
    (args.output/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>云台对照实验</title><style>body{font:15px system-ui;max-width:1250px;margin:2em auto}pre{white-space:pre-wrap}</style>'+body)
    write(args.output/'analysis-source.json', {name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in ('gimbal_compare.py', 'diagnostics.py')})


if __name__ == '__main__':
    main()
