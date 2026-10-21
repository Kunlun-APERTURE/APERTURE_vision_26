"""Offline scoring and a standalone, reviewable report from recorded evidence."""
import argparse
import html
import json
import math
from pathlib import Path
import subprocess
import shutil
import hashlib
from datetime import datetime, timezone
from shots import score as score_shots
from scenario_catalog import semantics, EXTRA


def quantile(values, fraction=.95):
    values = sorted(v for v in values if isinstance(v,(int,float)) and math.isfinite(v))
    return values[min(len(values)-1,max(0,math.ceil(len(values)*fraction)-1))] if values else None


def load(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError,ValueError):
        return default


def write(path, data):
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,allow_nan=False,indent=2))
    temporary.replace(path)


def gap_metrics(rows, duration):
    # Time-based gaps include missing input frames; a high rate of short bad
    # states cannot disappear behind an average frame-count percentage.
    lock = None
    lost_since = None
    longest = 0.0
    reacquisitions = []
    last_good = False
    for row in rows:
        t = row.get('time')
        if t is None:
            continue
        if row['visible_plates'] <= 0:
            if lost_since is not None:
                longest = max(longest,t-lost_since)
            lost_since = None
            last_good = False
            continue
        good = row['correct_tracking']
        if good:
            if lock is None:
                lock = t
            if not last_good and lost_since is not None:
                gap = max(0,t-lost_since)
                longest = max(longest,gap)
                if lock is not None and lost_since > 0:
                    reacquisitions.append(gap)
            lost_since = None
            last_good = True
        else:
            if lost_since is None:
                lost_since = t
            last_good = False
    if lost_since is not None:
        longest = max(longest,duration-lost_since)
    return lock,longest,quantile(reacquisitions)


def finalize(folder):
    folder = Path(folder)
    manifest = load(folder/'manifest.json',{})
    state = load(folder/'run-state.json',{'errors':['No recorder final state'],'complete':False})
    try:
        rows = [json.loads(line) for line in (folder/'frames.jsonl').read_text().splitlines() if line]
    except (OSError,ValueError):
        rows = []
        state.setdefault('errors',[]).append('Frame evidence unreadable')
    rows = [r for r in rows if r.get('measurement',True)]
    mode = manifest.get('mode','track')
    duration = manifest.get('duration_s',60)
    scene = manifest.get('scene','unknown')
    timed = [r for r in rows if r.get('time') is not None]
    eligible = [r for r in timed if r.get('visible_plates',0)>0]
    correct = [r for r in eligible if r.get('correct_tracking')]
    def values(key):
        return [r[key] for r in rows if r.get(key) is not None]
    first_lock,longest_loss,reacquisition = gap_metrics(timed,duration)
    counts = {'frames':len(rows),'truth_frames':sum(r['truth_available'] for r in rows),
        'visible_frames':len(eligible),'strict_correct_tracking_frames':len(correct),
        'tracker_states':{name:sum(r.get('tracker_state')==name for r in rows)
            for name in ('lost','detecting','tracking','temp_lost','switching')}}
    times = [r['time'] for r in timed]
    gaps = [b-a for a,b in zip(times,times[1:])]
    metrics = {
        'processed_hz':len(rows)/duration,
        'age_p95_ms':quantile(values('age_ms')),
        'pipeline_p95_ms':quantile(values('pipeline_ms')),
        'truth_coverage':counts['truth_frames']/max(1,len(rows)),
        'visible_frame_fraction':len(eligible)/max(1,len(timed)),
        'correct_tracking_fraction':len(correct)/len(eligible) if eligible else None,
        'matched_detection_recall':sum(r['detection_matches'] for r in timed)/max(1,sum(r['visible_plates'] for r in timed)),
        'unmatched_detections':sum(r['false_detections'] for r in timed),
        'first_correct_lock_s':first_lock,
        'longest_loss_s':longest_loss,
        'reacquisition_p95_s':reacquisition,
        'plate_position_error_p95_m':quantile(values('plate_position_error_m')),
        'center_xy_error_p95_m':quantile(values('center_xy_error_m')),
        'velocity_error_p95_mps':quantile(values('velocity_error_mps')),
        'selected_plate_velocity_error_p95_mps':quantile(values('selected_plate_velocity_error_mps')),
        'selected_plate_position_error_p95_m':quantile(values('selected_plate_position_error_m')),
        'yaw_execution_error_p95_deg':quantile(values('yaw_execution_error_deg')),
        'pitch_execution_error_p95_deg':quantile(values('pitch_execution_error_deg')),
        'alignment_p95_ms':quantile(values('alignment_ms')),
        'first_frame_sim_s':min(times) if times else None,
        'last_frame_sim_s':max(times) if times else None,
        'max_frame_gap_s':max(gaps,default=None),
        'target_speed_median_mps':quantile(values('target_speed_mps'),.5),
        'target_speed_p95_mps':quantile(values('target_speed_mps')),
        'target_abs_yaw_rate_median':quantile([abs(v) for v in values('target_yaw_rate')],.5),
    }
    ego = values('ego_position')
    metrics['ego_drift_m'] = max((math.dist(p,ego[0]) for p in ego),default=None)
    target = values('target_position')
    metrics['target_travel_span_m'] = max((math.dist(p[:2],target[0][:2]) for p in target),default=None)
    rules = []
    def rule(name, category, passed, actual, expected):
        rules.append({'name':name,'category':category,'passed':bool(passed),'actual':actual,'expected':expected})
    def upper(name, category, actual, limit):
        rule(name,category,actual is not None and actual<=limit,actual,f'<= {limit}')
    def lower(name, category, actual, limit):
        rule(name,category,actual is not None and actual>=limit,actual,f'>= {limit}')
    rule('experiment_complete','validity',state.get('complete'),state.get('complete'),True)
    rule('environment_errors','validity',not state.get('errors'),state.get('errors',[]),[])
    rule('recorder_integrity','validity',not(state.get('observer_gaps') or state.get('recorder_queue_drops')),
        {'observer_gaps':state.get('observer_gaps'),'queue_drops':state.get('recorder_queue_drops')},'zero drops')
    lower('truth_coverage','validity',metrics['truth_coverage'],.99)
    lower('measurement_end','validity',metrics['last_frame_sim_s'],duration-.25)
    upper('measurement_start','validity',metrics['first_frame_sim_s'],.25)
    upper('maximum_frame_gap','validity',metrics['max_frame_gap_s'],.25)
    generated_scene=scene in EXTRA
    moving_ego = scene in ('ego_left','ego_right','ego_right_centered','ego_sweep','ego_sweep_reverse')
    if generated_scene:
        meta=manifest.get('scenario_semantics',semantics(scene))
        if meta['ego_motion']=='static':upper('ego_drift','validity',metrics['ego_drift_m'],.05)
        else:lower('ego_translates','validity',metrics['ego_drift_m'],.1)
        if 'translation' in meta['target_motion']:lower('target_translates','validity',metrics['target_travel_span_m'],.1)
        else:upper('target_static','validity',metrics['target_travel_span_m'],.1)
        if 'rotation' in meta['target_motion']:lower('target_rotates','validity',metrics['target_abs_yaw_rate_median'],.2)
    elif moving_ego:
        lower('ego_translation','validity',metrics['ego_drift_m'],1.5)
        upper('ego_translation_bound','validity',metrics['ego_drift_m'],2.5)
        steady = [r for r in timed if any(a<=r['time']<=b for a,b in ([(3,5.5),(7,9.5),(11,13.5)] if scene in ('ego_sweep','ego_sweep_reverse') else [(3,5.5)]))]
        ego_speeds = [r['ego_speed_mps'] for r in steady if r.get('ego_speed_mps') is not None]
        metrics['ego_steady_speed_mps'] = quantile(ego_speeds,.5)
        lower('ego_speed_lower','validity',metrics['ego_steady_speed_mps'],.3)
        upper('ego_speed_upper','validity',metrics['ego_steady_speed_mps'],.65)
        if ego:
            sign = 1 if scene in ('ego_left','ego_sweep') else -1
            metrics['ego_signed_y_span_m'] = (ego[-1][1]-ego[0][1])*sign
            metrics['ego_x_drift_m'] = max(abs(p[0]-ego[0][0]) for p in ego)
        lower('ego_motion_direction','validity',metrics.get('ego_signed_y_span_m'),1.5)
        upper('ego_x_drift','validity',metrics.get('ego_x_drift_m'),.1)
        upper('target_static','validity',metrics['target_travel_span_m'],.1)
    else:
        upper('ego_drift','validity',metrics['ego_drift_m'],.05)
    lower('target_visibility','validity',metrics['visible_frame_fraction'],.5)
    rule('spawn_ready','validity',state.get('scenario',{}).get('spawn_applied') and not state.get('scenario',{}).get('spawn_error'),
        state.get('scenario',{}).get('spawn_error'),'spawn_applied without error')
    if scene=='line':
        lower('target_translates','validity',metrics['target_travel_span_m'],.5)
        lower('target_speed_lower','validity',metrics['target_speed_median_mps'],.25)
        upper('target_speed_upper','validity',metrics['target_speed_median_mps'],.75)
    elif scene=='spin':
        lower('target_spins','validity',metrics['target_abs_yaw_rate_median'],.35)
        upper('target_spin_rate','validity',metrics['target_abs_yaw_rate_median'],.65)
        upper('target_spin_drift','validity',metrics['target_travel_span_m'],.1)
    elif scene in ('static','move_ref'):
        upper('target_static','validity',metrics['target_travel_span_m'],.1)
    lower('processed_frequency','performance',metrics['processed_hz'],20)
    upper('frame_age','performance',metrics['age_p95_ms'],150)
    if scene=='static':
        upper('yaw_execution_error','performance',metrics['yaw_execution_error_p95_deg'],2)
        upper('pitch_execution_error','performance',metrics['pitch_execution_error_p95_deg'],2)
    timing = load(folder/'timing.json')
    if timing:
        metrics['total_wall_s'] = timing['wall_seconds']
        upper('wall_time_budget','validity',timing['wall_seconds'],40)
    if mode == 'shoot':
        shot_metrics,shot_errors = score_shots(folder,manifest,state)
        metrics.update(shot_metrics)
        if manifest.get('shooting',{}).get('fire_policy') == 'continuous':
            lower('continuous_firing_sample_count','validity',metrics['shots_fired'],int(duration*1.8))
            upper('continuous_firing_max_gap','validity',metrics.get('maximum_launch_spacing_s'),.6)
        rule('shot_event_integrity','validity',not shot_errors,shot_errors,'paired, unique events; launches within measurement and configured rate limit')
    else:
        rule('no_fire_events','safety',state.get('fire_events',0)==0,state.get('fire_events',0),0)
        rule('no_fire_advice','safety',state.get('fire_advice_messages',0)==0,state.get('fire_advice_messages',0),0)
    baseline = manifest.get('acceptance_baseline')
    if baseline:
        for name,condition in baseline['limits'].items():
            if condition['direction']=='min':
                lower(name,'effect',metrics.get(name),condition['value'])
            else:
                upper(name,'effect',metrics.get(name),condition['value'])
    review = load(folder/'visual-review.json')
    invalid = any(not r['passed'] and r['category']=='validity' for r in rules)
    failed = any(not r['passed'] and r['category']!='validity' for r in rules)
    if invalid:
        verdict = 'invalid'
    elif failed or (review and review['decision']=='fail'):
        verdict = 'failed'
    elif mode == 'shoot' and not metrics.get('shots_fired'):
        verdict = 'no_shooting_samples'
    elif not review:
        verdict = 'pending_visual_review'
    elif not baseline:
        verdict = 'pending_effect_baseline'
    else:
        verdict = 'passed'
    summary = {'schema_version':2,'quantile_method':'nearest_rank','run_id':manifest.get('run_id',folder.name),'scene':scene,
        'scorer_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'shot_scorer_sha256':hashlib.sha256(Path(__file__).with_name('shots.py').read_bytes()).hexdigest() if mode=='shoot' else None,
        'verdict':verdict,'mode':mode,'shooting':manifest.get('shooting'),'counts':counts,'metrics':metrics,'rules':rules,
        'visual_review':review,'baseline':baseline.get('run_id') if baseline else None,
        'limitations':[
            'Visibility uses plate front face, full projected plate in FOV and a physics centre ray. It is not a render pixel segmentation mask.',
            'Target collision envelope is excluded from its own visibility ray; convex plate facing approximates self-occlusion.',
            'Correct tracking requires state=tracking, blue-three target association and centre XY error <=0.5 m. detecting/temp_lost do not count.',
            'Execution error compares the new command to image-time gimbal attitude. It is a same-frame following error, not an isolated actuator response test.',
            'Position/velocity truth metrics use the image capture time. Aimer includes ballistic lead; aiming at the image centre is not required.',
            'AVI/MP4 use a nominal 30 FPS playback clock; frames.jsonl is the authoritative timestamp and frame index mapping.',
            (('Hit v3 sweeps the actual 17 mm sphere along each travelled physics segment, compensates plate translation and uses exact collider identity and first solid impact. Plate rotation is sampled at 120 Hz (sub-mm orientation approximation at current speed). It is not real-world ballistic calibration.' if manifest.get('shooting',{}).get('hit_method') == 'swept_actual_segment_v3' else 'Legacy hit events include speculative contacts and are not a validated precision baseline.') if mode=='shoot' else 'No projectile hit-rate measurement is included in this tracking experiment.')
        ]}
    write(folder/'report-generation.json',{'generated_utc':datetime.now(timezone.utc).isoformat(),
        'report_sha256':summary['scorer_sha256'],'shots_sha256':summary['shot_scorer_sha256'],
        'capture_recorder_code':load(folder/'recorder-code.json',{})})
    write(folder/'summary.json',summary)
    plots(folder,rows)
    if (folder/'annotated.avi').exists() and not (folder/'annotated.mp4').exists() and shutil.which('ffmpeg'):
        result = subprocess.run(['ffmpeg','-nostdin','-loglevel','error','-y','-i',str(folder/'annotated.avi'),
            '-c:v','libx264','-preset','ultrafast','-crf','25','-pix_fmt','yuv420p','-movflags','+faststart',
            str(folder/'annotated.mp4')],capture_output=True,text=True)
        if result.returncode:
            summary['limitations'].append('MP4 transcode failed; annotated AVI is retained: '+result.stderr[-300:])
            write(folder/'summary.json',summary)
    render(folder,summary)
    return summary


def plots(folder, rows):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        fig,axes = plt.subplots(4,1,figsize=(11,11),sharex=True)
        for key,label in [('age_ms','Frame age (ms)'),('pipeline_ms','Pipeline (ms)')]:
            points=[r for r in rows if r.get('time') is not None and r.get(key) is not None]
            axes[0].plot([r['time'] for r in points],[r[key] for r in points],label=label,linewidth=.7)
        for key in ('center_xy_error_m','plate_position_error_m'):
            points=[r for r in rows if r.get('time') is not None and r.get(key) is not None]
            axes[1].plot([r['time'] for r in points],[r[key] for r in points],label=key,linewidth=.7)
        for key in ('yaw_execution_error_deg','pitch_execution_error_deg'):
            points=[r for r in rows if r.get('time') is not None and r.get(key) is not None]
            axes[2].plot([r['time'] for r in points],[r[key] for r in points],label=key,linewidth=.7)
        points=[r for r in rows if r.get('time') is not None]
        axes[3].step([r['time'] for r in points],[int(r['correct_tracking']) for r in points],
            label='Strict correct tracking',linewidth=.7)
        axes[3].step([r['time'] for r in points],[int(r['visible_plates']>0) for r in points],
            label='Eligible visible target',linewidth=.7,alpha=.6)
        for axis,label in zip(axes,['ms','metres','degrees','state']):
            axis.set_ylabel(label)
            axis.grid(alpha=.25)
            axis.legend(loc='upper right',fontsize=8)
        axes[-1].set_xlabel('Scenario time (s), warmup excluded')
        fig.tight_layout()
        fig.savefig(folder/'metrics.png',dpi=140)
        plt.close(fig)
    except ImportError:
        pass


def render(folder, summary):
    escape = lambda value:html.escape(str(value))
    labels={'invalid':'实验无效','failed':'失败','pending_visual_review':'待视觉复核',
        'pending_effect_baseline':'待效果基线','no_shooting_samples':'无射击样本','passed':'通过'}
    rules=''.join(f"<tr><td>{escape(r['category'])}</td><td>{escape(r['name'])}</td><td>{'PASS' if r['passed'] else 'FAIL'}</td><td>{escape(r['actual'])}</td><td>{escape(r['expected'])}</td></tr>" for r in summary['rules'])
    metrics=''.join(f'<tr><td>{escape(k)}</td><td>{escape(round(v,5) if isinstance(v,float) else v)}</td></tr>' for k,v in summary['metrics'].items())
    limits=''.join('<li>'+escape(v)+'</li>' for v in summary.get('limitations',[]))
    original = ('<a href="input.mjpg">原始 JPEG 字节流</a>' if (folder/'input.mjpg').exists()
        and (folder/'input.mjpg').stat().st_size else '该早期记录未保存原始 JPEG 字节流')
    shooting = ('<h2>自动打击条件与逐发证据</h2><pre>'+escape(json.dumps(summary.get('shooting'),ensure_ascii=False,indent=2))+'</pre><p><a href="shots.csv">逐发 CSV</a> · <a href="shots.jsonl">逐发 JSONL</a></p>' if summary.get('mode')=='shoot' else '')
    text=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>{escape(summary['run_id'])}</title>
<style>body{{font:16px system-ui;max-width:1150px;margin:2em auto;padding:0 1em}}table{{border-collapse:collapse;width:100%;margin:1em 0}}td,th{{border:1px solid #aaa;padding:.4em;text-align:left}}video,img{{max-width:100%}}code{{background:#eee;padding:.2em}}</style>
<h1>{escape(summary['run_id'])}</h1><h2>结论：{labels[summary['verdict']]}</h2>
<p>场景：{escape(summary['scene'])}。规则结论与视觉复核分别保存；环境无效不能由人工复核覆盖。</p>
<h2>同步标注录像</h2><p>青色：真值投影；绿色：检测；紫色：预测装甲；黄色：弹道瞄准点。</p>
<video controls preload="metadata" src="annotated.mp4"></video>
<p><a href="input.avi">输入浏览录像</a> · {original} · <a href="annotated.avi">标注 AVI</a> · <a href="frames.jsonl">逐帧评分、视频时间与 JPEG 字节偏移</a> · <a href="telemetry.jsonl">原始观测和真值</a> · <a href="manifest.json">版本、配置和模型摘要</a></p>
{shooting}<h2>指标曲线</h2><img src="metrics.png"><h2>自动规则</h2><table><tr><th>分类</th><th>规则</th><th>结果</th><th>实际</th><th>要求</th></tr>{rules}</table>
<h2>指标</h2><table>{metrics}</table><h2>视觉复核</h2><pre>{escape(json.dumps(summary['visual_review'],ensure_ascii=False,indent=2))}</pre>
<h2>解释边界</h2><ul>{limits}</ul></html>'''
    (folder/'report.html').write_text(text)
    (folder/'report.md').write_text(f"# {summary['run_id']}\n\n结论：**{labels[summary['verdict']]}**\n\n"
        + '\n'.join(f'- {key}: {value}' for key,value in summary['metrics'].items())+'\n\n## 规则\n\n'
        + '\n'.join(f"- {r['name']}: {'PASS' if r['passed'] else 'FAIL'} — {r['actual']}（{r['expected']}）" for r in summary['rules'])
        + '\n\n查看 report.html 获取录像、曲线与解释边界。\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('folder')
    args=parser.parse_args()
    print(json.dumps(finalize(args.folder),ensure_ascii=False,indent=2))
