#!/usr/bin/env python3
"""Offline firing-time audit and moving-shooter baseline; never sends commands."""
import argparse
import bisect
import collections
import hashlib
import html
import math
from pathlib import Path
import numpy as np

from diagnostics import Truth, analyze, jsonl, load, quantile, table, wrap, write
from gimbal_compare import controller_metrics


def firing_timing(folder, shots):
    observations, traces = [], []
    for packet in jsonl(folder/'telemetry.jsonl'):
        if packet['kind'] == 'observation':
            observations.append(packet['data'])
        elif packet['kind'] == 'gimbal_trace':
            traces.append(packet['data'])
    observations.sort(key=lambda row: row['receipt_ns'])
    traces.sort(key=lambda row: row['stamp_ns'])
    receipts = [row['receipt_ns'] for row in observations]
    stamps = [row['stamp_ns'] for row in traces]
    by_command = {row['frame']['stamp_ns']: row for row in observations
                  if row.get('command_emitted') and row['result'].get('valid')}
    pulses = [row for row in observations if row.get('fire_forwarded')]
    pulse_stamps = [row['receipt_ns'] for row in pulses]
    used_pulses = set()
    for shot in shots:
        stamp = shot['fire_stamp_ns']
        i = bisect.bisect_right(receipts, stamp)-1
        if i >= 0:
            latest = observations[i]
            shot['latest_raw_fire_advice'] = bool(latest['result'].get('fire_advice'))
            shot['latest_result_valid'] = bool(latest['result'].get('valid'))
            shot['latest_tracker_state'] = latest['result'].get('tracker')
            shot['latest_result_receipt_age_ms'] = (stamp-latest['receipt_ns'])/1e6
        i = bisect.bisect_right(stamps, stamp)-1
        if i >= 0 and 0 <= stamp-stamps[i] <= 30_000_000:
            trace = traces[i]
            if trace['accepted_wall_ns'] <= stamp:
                velocity = shot.get('muzzle_relative_velocity', shot['initial_velocity'])
                # Rust stores pitch with a -90 degree mount offset. Convert to
                # physical elevation before comparing to the launch direction.
                desired_pitch = trace['target_pitch_rad']+math.pi/2
                actual_pitch = math.atan2(velocity[2], math.hypot(*velocity[:2]))
                shot['accepted_pitch_error_deg'] = math.degrees(desired_pitch-actual_pitch)
                shot['accepted_image_age_ms'] = (stamp-trace['command_stamp_ns'])/1e6
                accepted = by_command.get(trace['command_stamp_ns'])
                if accepted is not None and accepted['receipt_ns'] <= stamp:
                    result = accepted['result']
                    shot['accepted_raw_fire_advice'] = bool(result.get('fire_advice'))
                    shot['accepted_tracker_state'] = result.get('tracker')
                    shot['accepted_angle_join_error_deg'] = abs(math.degrees(wrap(trace['target_yaw_rad']-result['yaw'])))
        i = bisect.bisect_right(pulse_stamps, stamp)-1
        if i >= 0 and 0 <= stamp-pulse_stamps[i] <= 250_000_000:
            if i in used_pulses:
                raise RuntimeError('One pulse matched multiple launches: '+folder.name)
            used_pulses.add(i)
            shot['matched_pulse_to_launch_ms'] = (stamp-pulse_stamps[i])/1e6
            shot['request_raw_advice'] = bool(pulses[i]['result'].get('fire_advice'))
            request=pulses[i]['result']
            velocity=shot.get('muzzle_relative_velocity',shot['initial_velocity'])
            shot['request_yaw_error_deg']=math.degrees(wrap(request['yaw']-math.atan2(velocity[1],velocity[0])))
            shot['request_pitch_error_deg']=math.degrees(-request['pitch']-math.atan2(velocity[2],math.hypot(*velocity[:2])))
            shot['request_frame_stamp_ns']=pulses[i]['frame']['stamp_ns']
    return {'raw_observations':len(observations), 'fire_pulses':len(pulses),
            'matched_launches':len(used_pulses), 'unmatched_pulses':len(pulses)-len(used_pulses)}


def miss_geometry(folder,shots):
    """Observed front-plane crossings for misses, independent of valid estimates.

    Impacting spheres terminate before their centres cross the plate plane, so
    crossings and physical contact points remain separate measurements.
    """
    plates, paths = [], collections.defaultdict(list)
    for packet in jsonl(folder/'telemetry.jsonl'):
        if packet['kind']=='armors': plates.append(packet['data'])
        elif packet['kind']=='projectile_trace':
            for p in packet['data']['projectiles']:
                paths[p['shot_seq']].append(dict(p,stamp_ns=packet['data']['stamp_ns']))
    truth=Truth(plates)
    front_id=next(p['id'] for p in plates[0]['armors'] if p['robot_id']==1 and p['slot']==0)
    for shot in shots:
        if shot['scene']=='spin' or shot.get('front_hit'):continue
        for point in sorted(paths[shot['shot_seq']],key=lambda p:p['stamp_ns']):
            front=truth.at(point['stamp_ns'],front_id,'armors')
            if front is None:continue
            normal=np.array(front['normal']);normal/=np.linalg.norm(normal)
            center=np.array(front['position'])
            a,b=np.array(point['previous']),np.array(point['position'])
            da,db=float(np.dot(a-center,normal)),float(np.dot(b-center,normal))
            if da>=0 and db<=0 and da-db>1e-9:
                cross=a+(b-a)*da/(da-db)
                lateral=np.cross([0,0,1],normal);lateral/=np.linalg.norm(lateral)
                vertical=np.cross(normal,lateral)
                shot['miss_plane_lateral_cm']=float(np.dot(cross-center,lateral)*100)
                shot['miss_plane_vertical_cm']=float(np.dot(cross-center,vertical)*100)
                shot['miss_plane_stamp_ns']=point['stamp_ns']
                break


def statistics(shots, seconds):
    n = len(shots)
    return {'shots':n, 'target_hits':sum(s['target_hit'] for s in shots),
        'front_hits':sum(bool(s['front_hit']) for s in shots),
        'front_samples':sum(s['front_hit'] is not None for s in shots),
        'firing_hz':n/seconds if seconds else None,
        'front_hits_per_s':sum(bool(s['front_hit']) for s in shots)/seconds if seconds else None,
        'latest_advice_false':sum(s.get('latest_raw_fire_advice') is False for s in shots),
        'accepted_advice_false':sum(s.get('accepted_raw_fire_advice') is False for s in shots),
        'yaw_abs_median_deg':quantile([abs(s['accepted_command_yaw_error_deg']) for s in shots if s.get('accepted_command_yaw_error_deg') is not None]),
        'yaw_abs_p95_deg':quantile([abs(s['accepted_command_yaw_error_deg']) for s in shots if s.get('accepted_command_yaw_error_deg') is not None], .95),
        'pitch_abs_p95_deg':quantile([abs(s['accepted_pitch_error_deg']) for s in shots if s.get('accepted_pitch_error_deg') is not None], .95),
        'pulse_to_launch_median_ms':quantile([s.get('matched_pulse_to_launch_ms') for s in shots]),
        'pulse_to_launch_p95_ms':quantile([s.get('matched_pulse_to_launch_ms') for s in shots], .95)}


def stability(shots, duration):
    ordered = sorted(shots,key=lambda s:s['fire_stamp_ns'])
    longest, current = 0, 0
    for shot in ordered:
        current = current+1 if not shot['target_hit'] else 0
        longest = max(longest,current)
    front_longest, current = 0, 0
    for shot in ordered:
        current = current+1 if not shot['front_hit'] else 0
        front_longest = max(front_longest,current)
    times = [s['fire_time_s'] for s in ordered]
    gaps = [b-a for a,b in zip(times,times[1:])]
    bins = []
    for i in range(math.ceil(duration)):
        selected = [s for s in ordered if i<=s['fire_time_s']<min(i+1,duration)]
        bins.append({'start_s':i,'end_s':min(i+1,duration),**statistics(selected,min(i+1,duration)-i)})
    return {'longest_target_miss_streak':longest,'longest_front_miss_streak':front_longest,
        'sequence_target':''.join('H' if s['target_hit'] else 'M' for s in ordered),
        'sequence_front':''.join('H' if s['front_hit'] else 'M' for s in ordered),
        'spacing_median_s':quantile(gaps),'spacing_max_s':max(gaps,default=None),
        'first_shot_s':times[0] if times else None,'last_shot_s':times[-1] if times else None,
        'windows_1s':bins}


def gate_sensitivity(shots, seconds):
    complete = [s for s in shots if all(s.get(k) is not None for k in
        ('latest_raw_fire_advice','accepted_raw_fire_advice','accepted_command_yaw_error_deg',
         'accepted_pitch_error_deg','accepted_image_age_ms'))]
    def fresh(s):
        return (s['latest_raw_fire_advice'] and s['accepted_raw_fire_advice']
                and s['latest_result_valid'] and s['latest_result_receipt_age_ms'] <= 150
                and s['accepted_image_age_ms'] <= 150)
    masks = [('recorded_shots', lambda s:True), ('latest_and_accepted_advice_fresh', fresh)]
    for threshold in (.5,.75,1.0):
        masks.append((f'fresh_both_axes_{threshold:g}_deg',
            lambda s, limit=threshold:fresh(s) and abs(s['accepted_command_yaw_error_deg'])<=limit
                and abs(s['accepted_pitch_error_deg'])<=limit))
    result = {}
    for name, predicate in masks:
        kept = [s for s in complete if predicate(s)]
        removed = [s for s in complete if not predicate(s)]
        result[name] = {**statistics(kept,seconds), 'matched_denominator':len(complete),
            'missing_diagnostics':len(shots)-len(complete),
            'removed_target_hits':sum(s['target_hit'] for s in removed),
            'removed_target_misses':sum(not s['target_hit'] for s in removed),
            'removed_front_hits':sum(bool(s['front_hit']) for s in removed)}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch', type=Path, nargs='+', required=True)
    parser.add_argument('--continuous', type=Path, nargs='+', required=True)
    parser.add_argument('--centered', type=Path, nargs='+', required=True)
    parser.add_argument('--previous', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reuse', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    records, runs, shots, signatures = [], [], [], collections.defaultdict(set)
    for path in args.batch:
        batch = load(path)
        if not batch['complete']: raise RuntimeError('Incomplete confirmation batch: '+str(path))
        records.extend((path.parent/r['run_id'], 'advice_reference') for r in batch['runs'])
    for path in args.continuous:
        batch = load(path)
        if not batch['complete']: raise RuntimeError('Incomplete continuous batch: '+str(path))
        records.extend((path.parent/r['run_id'], 'continuous_baseline') for r in batch['runs'])
    for path in args.centered:
        batch=load(path)
        if not batch['complete']:raise RuntimeError('Incomplete centered-heading batch')
        records.extend((path.parent/r['run_id'],'centered_heading_check') for r in batch['runs'])
    for role,scenes,physics_set in [('advice_reference',('move_ref','ego_left','ego_right'),('no-drag',)),
                                  ('continuous_baseline',('move_ref','line','ego_left','ego_right'),('no-drag','drag')),
                                  ('centered_heading_check',('ego_right_centered',),('no-drag','drag'))]:
        counts = collections.Counter((load(f/'manifest.json')['scene'],load(f/'manifest.json')['shooting']['physics']) for f,r in records if r==role)
        expected = collections.Counter({(scene,physics):3 for scene in scenes for physics in physics_set})
        if counts != expected: raise RuntimeError('Expected three repeats for '+role+': '+str(counts))
    previous = load(args.previous/'comparison.json')
    for r in previous['runs']:
        if r['gimbal_profile'] == 'feedforward':
            records.append((args.previous.parent/r['run_id'], 'prior_firing_audit'))
    for folder, role in records:
        manifest, summary = load(folder/'manifest.json'), load(folder/'summary.json')
        failed = [r for r in summary['rules'] if not r['passed']]
        if failed: raise RuntimeError('Rule failure in '+folder.name+': '+str(failed))
        if manifest['gimbal_profile'] != 'feedforward': raise RuntimeError('Unexpected controller profile')
        signatures['algorithm_binary'].add(manifest['algorithm_binary_sha256'])
        signatures['weights'].add(str(sorted(manifest['algorithm_weights_sha256'].items())))
        signatures['simulator_image_'+role].add(manifest['containers']['simulator_image_id'])
        signatures['algorithm_yaml'].add(manifest['configuration_sha256']['algorithm.yaml'])
        cache = args.output/folder.name
        old_cache = args.previous/folder.name
        reuse = cache if args.reuse and (cache/'summary.json').exists() else old_cache if role == 'prior_firing_audit' else None
        if reuse:
            result, shot_rows = load(reuse/'summary.json'), load(reuse/'shots.json')
            for name, digest in result['source_sha256'].items():
                if hashlib.sha256((folder/name).read_bytes()).hexdigest() != digest:
                    raise RuntimeError('Changed source: '+str(folder/name))
            cache.mkdir(exist_ok=True)
        else:
            result, _, shot_rows = analyze(folder, cache)
        result['controller'] = controller_metrics(folder, shot_rows)
        result['timing_audit'] = firing_timing(folder, shot_rows)
        miss_geometry(folder,shot_rows)
        result['role'] = role
        result['fire_policy'] = manifest['shooting'].get('fire_policy','advice')
        if (result['fire_policy']=='continuous') != (role in ('continuous_baseline','centered_heading_check')):
            raise RuntimeError('Unexpected firing policy for '+folder.name)
        result['stability'] = stability(shot_rows,result['duration_s'])
        result['tracking_fraction'] = summary['metrics']['correct_tracking_fraction']
        result['ego_speed_mps'] = summary['metrics'].get('ego_steady_speed_mps',0)
        result['rules_all_passed'] = True
        for s in shot_rows: s['role'] = role; s['fire_policy'] = result['fire_policy']
        write(cache/'summary.json',result)
        write(cache/'shots.json',shot_rows)
        table(cache/'shots.csv',shot_rows)
        runs.append(result); shots.extend(shot_rows)
        print('Analyzed',role,folder.name,flush=True)
    if any(len(v)!=1 for v in signatures.values()): raise RuntimeError('Algorithm/image inputs changed')
    groups = {}
    for role in ('continuous_baseline','centered_heading_check','advice_reference','prior_firing_audit'):
        for scene in sorted({r['scene'] for r in runs if r['role']==role}):
            for physics in ('no-drag','drag'):
                selected = [r for r in runs if r['role']==role and r['scene']==scene and r['physics']==physics]
                selected_shots = [s for s in shots if s['role']==role and s['scene']==scene and s['physics']==physics]
                seconds = sum(r['duration_s'] for r in selected)
                key = role+'/'+scene+'/'+physics
                groups[key] = {**statistics(selected_shots,seconds),'runs':len(selected),
                    'duration_s':seconds, 'phases':{}, 'filters':gate_sensitivity(selected_shots,seconds),
                    'longest_target_miss_streak':max((r['stability']['longest_target_miss_streak'] for r in selected),default=0),
                    'longest_front_miss_streak':max((r['stability']['longest_front_miss_streak'] for r in selected),default=0),
                    'spacing_max_s':max((r['stability']['spacing_max_s'] or 0 for r in selected),default=None),
                    'tracking_fraction_median':quantile([r['tracking_fraction'] for r in selected])}
                for phase in sorted({s['phase'] for s in selected_shots}):
                    phase_seconds = (2.5 if scene.startswith('ego_') else 13.0 if scene=='line' else 0)*len(selected) if phase=='steady' else None
                    groups[key]['phases'][phase] = statistics([s for s in selected_shots if s['phase']==phase],phase_seconds)
    data = {'runs':runs,'groups':groups,'signatures':{k:list(v) for k,v in signatures.items()},
        'continuous_confirmation_runs':24, 'centered_heading_runs':6, 'advice_reference_runs':9, 'prior_audit_runs':len(runs)-39,
        'limits':['Offline filters describe retained recorded shots only; no new closed-loop firing policy was tested.',
            'Moving scenes traverse the same open corridor in opposite directions; range and plate aspect change over the path.',
            'Ground truth is used only by the evaluator, including shooter-rest launch-speed validation.',
            'Visual review pending; no formal baseline selected.']}
    write(args.output/'comparison.json',data)
    table(args.output/'all-shots.csv',shots)
    table(args.output/'runs.csv',[{k:v for k,v in r.items() if not isinstance(v,(dict,list))} for r in runs])
    plot_stability(args.output,runs)
    render(args.output,data)


def plot_stability(output,runs):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    selected=[r for r in runs if r['role'] in ('continuous_baseline','centered_heading_check')]
    selected.sort(key=lambda r:(r['physics'],r['scene'],r['run_id']))
    values=np.full((len(selected),18),np.nan)
    labels=[]
    for i,r in enumerate(selected):
        labels.append(r['physics']+'/'+r['scene']+' '+r['run_id'].split('_')[0][-6:])
        for window in r['stability']['windows_1s']:
            if window['shots']:values[i,window['start_s']]=window['target_hits']/window['shots']
    fig,ax=plt.subplots(figsize=(12,12))
    im=ax.imshow(values,aspect='auto',vmin=0,vmax=1,cmap='RdYlGn',interpolation='none')
    ax.set_yticks(range(len(labels)),labels,fontsize=8)
    ax.set_xticks(range(18),[f'{i}-{i+1}' for i in range(18)],rotation=45)
    ax.set_xlabel('Scenario time window (s); blank = no samples / outside measurement')
    ax.set_title('Continuous 2 Hz: target armor hit fraction per second')
    fig.colorbar(im,ax=ax,label='Target armor hit fraction')
    fig.tight_layout();fig.savefig(output/'stability.png',dpi=150);plt.close(fig)


def fmt(value): return 'n/a' if value is None else f'{value:.2f}'


def render(output,data):
    lines = ['# 持续连发命中稳定性与开火时机评估','',
        '算法二进制、模型、YAML、仅前馈云台控制、弹道和 v3 命中判定保持不变。',
        '按用户追加要求，新增 24 次独立连发实验：静止对照、目标平移带动云台跟随、己方沿地图 +Y / −Y 平移；各 3 次、各两种阻力。',
        '连发不依赖算法开火建议或跟踪有效性，由模拟器独立按 2 Hz 请求发射；丢跟踪时枪口停止受无效指令驱动，仍继续发射并计入结果。',
        '连发授权要求桥接器保持连接、控制开启、场景运行和新鲜心跳；关闭控制/射击或场景结束后停止，心跳失效超过 250 ms 停止。',
        '新增 bench 触发系统在位姿传播后调用原发射函数，因此使用了新模拟器镜像；与旧建议开火记录比较不能把差异只归因于开火门限。',
        '另外 6 次 ego_right_centered 对照只调整已知初始己方朝向为 −156°，相机初始对准走廊目标，检验画面边缘起始丢跟踪的影响；不向算法提供目标真值。原反向起点记录全部保留。',
        '移动测量 8 秒：0–2 秒静止，2–6 秒以名义 0.5 m/s 平移，6–8 秒静止；主要稳定窗口为 3–5.5 秒。',
        '静止测量 6 秒、目标平移测量 18 秒。另保留此前 9 次无阻力建议开火对照；先前 6 次目标平移和 4 次静止/旋转记录仅补充开火时机评估。','',
        '## 实际发射结果','',
        '| 策略/场景 | 物理 | 命中任意目标装甲 | 正面装甲 | 发射 Hz | 稳定移动正面 | 稳定移动 Hz | 最长连续未命中目标 |',
        '|---|---|---:|---:|---:|---:|---:|---:|']
    for key,g in data['groups'].items():
        if key.startswith('prior_firing_audit/'):continue
        role,scene,physics=key.split('/')
        steady=g['phases'].get('steady',{})
        lines.append(f"| {role}/{scene} | {physics} | {g['target_hits']}/{g['shots']} | {g['front_hits']}/{g['shots']} | {fmt(g['firing_hz'])} | {steady.get('front_hits',0)}/{steady.get('shots',0)} | {fmt(steady.get('firing_hz'))} | {g['longest_target_miss_streak']} |")
    lines += ['','## 逐轮连续性','', 'H=命中任意目标装甲，M=未命中；序列按实际出膛时间排序。逐秒明细在 comparison.json 各轮 stability.windows_1s。','',
        '| 轮次 | Hz | 最大间隔 s | 最长连失 | 逐发序列 |','|---|---:|---:|---:|---|']
    for r in data['runs']:
        if r['role'] not in ('continuous_baseline','centered_heading_check'): continue
        s=r['stability']
        lines.append(f"| {r['run_id']} | {fmt(r['firing_hz'])} | {fmt(s['spacing_max_s'])} | {s['longest_target_miss_streak']} | {s['sequence_target']} |")
    lines += ['', '## 发射瞬间的开火建议与执行误差','',
        '连续算法开火建议与桥接器发出的 2 Hz 脉冲分开统计。ROS `fire_advice=false` 可能仅表示限频间隔；下表的 false 来自原始算法结果。',
        '将发射前 30 ms 内的控制遥测与其已接收指令时间戳匹配。移动时从弹丸世界初速度减去记录的己方速度，还原枪口方向。','',
        '| 场景/物理 | 发射数 | 最新建议 false | 已接收建议 false | yaw 绝对误差中位数/P95 | pitch 绝对误差 P95 | 脉冲到出膛中位数/P95 ms |',
        '|---|---:|---:|---:|---:|---:|---:|']
    for key,g in data['groups'].items():
        lines.append(f"| {key} | {g['shots']} | {g['latest_advice_false']} | {g['accepted_advice_false']} | {fmt(g['yaw_abs_median_deg'])}/{fmt(g['yaw_abs_p95_deg'])}° | {fmt(g['pitch_abs_p95_deg'])}° | {fmt(g['pulse_to_launch_median_ms'])}/{fmt(g['pulse_to_launch_p95_ms'])} |")
    lines += ['', '## 离线门限敏感性（不是新策略的实测命中率）','',
        '只筛选已实际发射的弹丸，统计保留发数、移除的命中/未命中和保留频率。无法估计被延后发射的弹丸结果，不能据此宣称修改门限后命中率提高。',
        'fresh 条件：最新结果与已接收结果均建议开火，最新结果有效，结果接收距出膛 ≤150 ms，已接收图像距出膛 ≤150 ms；双轴条件再限制实际枪口与指令的 yaw、pitch 误差。','',
        '| 场景/物理 | 离线条件 | 保留/可匹配发数 | 保留目标命中 | 移除目标命中/未命中 | 保留发射 Hz |',
        '|---|---|---:|---:|---:|---:|']
    for key,g in data['groups'].items():
        for name,f in g['filters'].items():
            lines.append(f"| {key} | {name} | {f['shots']}/{f['matched_denominator']} | {f['target_hits']} | {f['removed_target_hits']}/{f['removed_target_misses']} | {fmt(f['firing_hz'])} |")
    lines += ['', '## 复核入口','', '所有结果仍待人工视觉验收；未选择正式基线。']
    for r in data['runs']:
        lines.append(f"- {r['run_id']}：[逐发诊断]({r['run_id']}/shots.csv) · [原始录像/报告](../{r['run_id']}/report.html)")
    (output/'report.md').write_text('\n'.join(lines)+'\n')
    body='<html lang="zh-CN"><meta charset="UTF-8"><title>移动射击与开火时机</title><style>body{max-width:1200px;margin:30px auto;font:16px sans-serif}pre{white-space:pre-wrap}img,video{max-width:100%}</style><h1>持续连发稳定性</h1><p><a href="report.md">完整报告 Markdown</a> · <a href="all-shots.csv">所有逐发数据</a></p><img src="stability.png"><pre>'+html.escape('\n'.join(lines[:lines.index('## 复核入口')]))+'</pre>'
    for r in data['runs']:
        name=r['run_id']
        body+=f'<h2>{html.escape(name)}</h2><a href="../{name}/report.html">原始完整报告</a> · <a href="{name}/shots.csv">逐发诊断</a>'
        if (output/name/'diagnosis.png').exists():body+=f'<p><img src="{name}/diagnosis.png" loading="lazy"></p>'
        for clip in r.get('clips',[]):
            clip_path=output/name/clip['file']
            if clip_path.exists():body+=f'<p>{html.escape(clip["name"])} · shot {clip["shot_seq"]}</p><video controls preload="none" src="{name}/{clip["file"]}"></video>'
    (output/'report.html').write_text(body+'</html>')


if __name__ == '__main__': main()
