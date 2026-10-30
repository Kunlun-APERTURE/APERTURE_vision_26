"""Read-only high-speed analysis; figures and reports live beside new evidence."""
import bisect,csv,html,json,math
from pathlib import Path
from impact_quality import summarize,quantile
from highspeed_scenarios import properties
from report import load,write


def motion_analysis(folder,media=True):
    if load(Path(folder)/'configuration/scenario-plan.json',{}).get('suite')=='movement-v1':
        from movement_analysis import report_metrics
        return report_metrics(folder)
    manifest=load(folder/'manifest.json');plan=load(folder/'configuration/scenario-plan.json',{})
    state=load(folder/'run-state.json',{});start=state.get('measurement_start_stamp_ns');end=state.get('measurement_end_stamp_ns')
    samples=[];shots=[json.loads(s) for s in (folder/'shots.jsonl').open()]
    for line in (folder/'telemetry.jsonl').open():
        packet=json.loads(line)
        if packet['kind']!='robots':continue
        data=packet['data'];stamp=data['stamp_ns']
        if start is None or end is None or not start<=stamp<=end:continue
        robots={r['id']:r for r in data['robots']}
        if not all(k in robots for k in (0,1)):continue
        ego,target=robots[0],robots[1]
        samples.append(dict(t=data['time'],speed=math.hypot(*target['velocity'][:2]),omega=target['yaw_rate'],
            ego_speed=math.hypot(*ego['velocity'][:2]),relative_speed=math.dist(ego['velocity'][:2],target['velocity'][:2]),
            range=math.dist(ego['position'][:2],target['position'][:2]),vx=target['velocity'][0],vy=target['velocity'][1],
            ego_position=ego['position'],target_position=target['position']))
    samples.sort(key=lambda r:r['t']);p=properties(manifest['scene'])
    sample_times=[r['t'] for r in samples]
    enriched=[]
    for shot in shots:
        i=bisect.bisect_left(sample_times,shot['fire_time_s'])
        choices=samples[max(0,i-1):min(len(samples),i+1)]
        nearest=min(choices,key=lambda r:abs(r['t']-shot['fire_time_s'])) if choices else None
        if nearest is not None and abs(nearest['t']-shot['fire_time_s'])>.1:nearest=None
        enriched.append(dict(shot,launch_target_speed_mps=nearest['speed'] if nearest else None,
            launch_range_m=nearest['range'] if nearest else None,launch_yaw_rate_radps=nearest['omega'] if nearest else None,launch_ego_speed_mps=nearest['ego_speed'] if nearest else None))
    (folder/'quality-shots.jsonl').write_text(''.join(json.dumps(r,allow_nan=False)+'\n' for r in enriched))
    with (folder/'quality-shots.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(enriched[0]) if enriched else ['shot_seq']);w.writeheader();w.writerows(enriched)
    at_speed=[]
    for r in enriched:
        requirements=[(r['launch_target_speed_mps'],p['speed']),(r['launch_yaw_rate_radps'],p['omega']),(r['launch_ego_speed_mps'],p['ego_speed'])]
        active=[(actual,req) for actual,req in requirements if req]
        if active and all(actual is not None and abs(abs(actual)-abs(req))<=.15*abs(req) for actual,req in active):at_speed.append(r)
    at_speed_scores=dict(shots=len(at_speed),**summarize(at_speed))
    range_groups=[]
    for a,b in ((0,5),(5,8),(8,12),(12,1000)):
        subset=[r for r in enriched if r['launch_range_m'] is not None and a<=r['launch_range_m']<b]
        range_groups.append(dict(range_m=[a,b],shots=len(subset),**summarize(subset)))
    def intensity(key,requested):
        eligible=[r for r in samples if r['t']>=1]
        if not requested:return dict(requested=0.,required=False,achieved=True,fraction=None,longest_hold_s=None)
        origin=None;previous=None;previous_good=False;longest=total=duration=0.
        for row in eligible:
            t=row['t'];good=abs(abs(row[key])-abs(requested))<=abs(requested)*.15
            gap=0 if previous is None else t-previous
            if gap>.08:origin=None
            if gap<=.08:duration+=gap
            if good:
                if origin is None:origin=t
                longest=max(longest,t-origin)
                if gap<=.08 and previous_good:total+=gap
            else:origin=None
            previous=t;previous_good=good
        return dict(requested=requested,required=True,achieved=longest>=1.,fraction=total/duration if duration else None,longest_hold_s=longest,
            p50=quantile([abs(r[key]) for r in eligible],.5),p95=quantile([abs(r[key]) for r in eligible],.95))
    intensity_results={key:intensity(key,req) for key,req in [('speed',p['speed']),('omega',p['omega']),('ego_speed',p['ego_speed'])]}
    windows=[('all',0,manifest['duration_s'])]+[('steady',a,b) for a,b in plan.get('steady_windows_s',[])]
    for change in plan.get('motion_change_times_s',[]):
        windows += [(f'change_{change:g}_{a:g}_{b:g}',change+a,change+b) for a,b in ((0,.5),(.5,1),(1,2))]
    phase_rows=[]
    for name,a,b in windows:
        subset=[s for s in shots if a<=s['fire_time_s']<b]
        frames=[json.loads(s) for s in (folder/'frames.jsonl').open()]
        fs=[r for r in frames if r.get('measurement',True) and r.get('time') is not None and a<=r['time']<b]
        phase_rows.append(dict(phase=name,t0=a,t1=b,shots=len(subset),**summarize(subset),
            useful_hits_per_s=sum(s['useful_hit'] for s in subset)/(b-a),visible_fraction=sum(r.get('visible_plates',0)>0 for r in fs)/len(fs) if fs else None,
            correct_tracking_fraction=sum(bool(r.get('correct_tracking')) for r in fs)/len(fs) if fs else None))
    result=dict(intensity=intensity_results,intensity_achieved=all(v['achieved'] for v in intensity_results.values()),
        phase_time_reference='Shot launch time; change bins are relative to scripted command changes, not a physical instantaneous reversal.',
        at_requested_motion=at_speed_scores,phases=phase_rows,range_groups=range_groups,samples=samples,range_p50_m=quantile([s['range'] for s in samples],.5),range_p95_m=quantile([s['range'] for s in samples],.95),
        relative_speed_p95_mps=quantile([s['relative_speed'] for s in samples],.95),
        within_range_ground=all(abs(pos[0])<59 and abs(pos[1])<59 for s in samples for pos in (s['ego_position'],s['target_position'])),
        collision_rotation_approximation='120 Hz sweep uses end-step orientation and fractional translation; score uses the same geometry.')
    write(folder/'motion-analysis.json',result)
    if media:
        try:figures(folder,samples,shots,plan)
        except ImportError:pass
    return dict(motion_intensity_achieved=result['intensity_achieved'],motion_intensity=intensity_results,
        actual_range_p50_m=result['range_p50_m'],actual_range_p95_m=result['range_p95_m'],relative_speed_p95_mps=result['relative_speed_p95_mps'],
        within_range_ground=result['within_range_ground'])


def figures(folder,samples,shots,plan):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(3,1,figsize=(9,8),sharex=True)
    for key in ('speed','ego_speed','relative_speed'):axes[0].plot([s['t'] for s in samples],[s[key] for s in samples],label=key)
    axes[0].set_ylabel('Speed (m/s)');axes[0].legend()
    axes[1].plot([s['t'] for s in samples],[s['omega'] for s in samples]);axes[1].set_ylabel('Yaw rate (rad/s)')
    for key in ('vx','vy'):axes[2].plot([s['t'] for s in samples],[s[key] for s in samples],label=key)
    axes[2].set_ylabel('Velocity (m/s)');axes[2].legend();axes[2].set_xlabel('Scenario time (s)')
    for ax in axes:
        ax.grid(alpha=.25)
        for t in plan.get('motion_change_times_s',[]):ax.axvline(t,color='r',alpha=.4)
    fig.tight_layout();fig.savefig(folder/'motion.png',dpi=130);fig.savefig(folder/'motion.svg');plt.close(fig)
    fig,ax=plt.subplots(figsize=(6,6));valid=[s for s in shots if s.get('useful_hit') and s.get('impact_x_fraction') is not None]
    ax.add_patch(plt.Rectangle((-1,-1),2,2,fill=False,color='black'))
    if valid:
        points=ax.scatter([s['impact_x_fraction'] for s in valid],[s['impact_y_fraction'] for s in valid],c=[s['fire_time_s'] for s in valid],cmap='viridis',s=20)
        fig.colorbar(points,ax=ax,label='Launch time (s)')
    ax.axhline(0,color='gray',lw=.5);ax.axvline(0,color='gray',lw=.5);ax.set(xlabel='Horizontal offset / half width',ylabel='Vertical offset / half height',title='Useful impacts; misses remain in rate denominator',xlim=(-1.2,1.2),ylim=(-1.2,1.2),aspect='equal')
    fig.tight_layout();fig.savefig(folder/'impact-quality.png',dpi=140);fig.savefig(folder/'impact-quality.svg');plt.close(fig)


def batch_report(output):
    batch=load(output/'highspeed-batch.json',{});rows=[]
    for entry in batch.get('runs',[]):
        folder=output.parent/entry['run_id'];manifest=load(folder/'manifest.json');summary=load(folder/'summary.json');motion=load(folder/'motion-analysis.json',{})
        rows.append(dict(run_id=entry['run_id'],scene=entry['scene'],seed=manifest['random_seed'],physics=manifest['shooting']['physics'],
            verdict=summary['verdict'],shots=summary['metrics'].get('shots_fired',0),hits=summary['metrics'].get('useful_armor_hits',0),
            hit_rate=summary['metrics'].get('useful_armor_hit_rate'),weighted_hit_rate=summary['metrics'].get('weighted_hit_rate'),
            weighted_score_sum=summary['metrics'].get('weighted_hit_score_sum'),center_p95_m=summary['metrics'].get('impact_center_distance_p95_m'),
            intensity_achieved=motion.get('intensity_achieved'),intensity=motion.get('intensity'),wall_s=summary['metrics'].get('total_wall_s'),
            at_requested_motion=motion.get('at_requested_motion'),phases=motion.get('phases'),coverage=summary['metrics'].get('correct_tracking_fraction')))
    groups=[]
    for scene in dict.fromkeys(r['scene'] for r in rows):
      for physics in ('no-drag','drag'):
        rs=[r for r in rows if r['scene']==scene and r['physics']==physics];n=sum(r['shots'] for r in rs);h=sum(r['hits'] for r in rs)
        score=sum(r['weighted_score_sum'] for r in rs) if rs and all(r['weighted_score_sum'] is not None for r in rs) else None
        active=[r.get('at_requested_motion') or {} for r in rs];an=sum(r.get('shots',0) for r in active)
        ah=sum(r.get('useful_armor_hits',0) for r in active)
        aq=sum(r['weighted_hit_score_sum'] for r in active) if active and all(r.get('weighted_hit_score_sum') is not None for r in active) else None
        groups.append(dict(scene=scene,physics=physics,runs=len(rs),shots=n,hits=h,hit_rate=h/n if n else None,weighted_hit_rate=score/n if score is not None and n else None,
            at_speed_shots=an,at_speed_hits=ah,at_speed_hit_rate=ah/an if an else None,at_speed_weighted_hit_rate=aq/an if aq is not None and an else None,
            intensity_achieved=all(r['intensity_achieved'] for r in rs) if rs else None))
    result=dict(complete=batch.get('complete',False),runs=rows,groups=groups,visual_review='pending',baseline_promoted=False,
        acceptance='New-condition baseline; no absolute weighted-hit-rate gate. Motion intensity and evidence validity are separate.',
        invalid_attempts=batch.get('invalid_attempts',[]),timing=batch.get('timing'))
    write(output/'result-summary.json',result)
    if rows:
        with (output/'results.csv').open('w',newline='') as f:
            flat=[{k:v for k,v in r.items() if k not in ('phases','intensity','at_requested_motion')} for r in rows];w=csv.DictWriter(f,fieldnames=list(flat[0]));w.writeheader();w.writerows(flat)
    def pct(x):return 'N/A' if x is None else f'{100*x:.2f}%'
    table=''.join('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in (g['scene'],g['physics'],g['runs'],f"{g['hits']}/{g['shots']}",pct(g['hit_rate']),pct(g['weighted_hit_rate']),f"{g['at_speed_hits']}/{g['at_speed_shots']}",pct(g['at_speed_weighted_hit_rate']),'达标' if g['intensity_achieved'] else '未达标'))+'</tr>' for g in groups)
    links=''.join(f'<li>{html.escape(r["scene"])} / {r["physics"]} / {r["seed"]}: <a href="../{r["run_id"]}/report.html">报告与完整视频</a> · <a href="../{r["run_id"]}/impact-quality.png">落点</a> · <a href="../{r["run_id"]}/motion.png">实际运动</a></li>' for r in rows)
    (output/'report.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>v3 高速靶场短测</title><style>body{font:15px system-ui;margin:2em}table{border-collapse:collapse}td,th{padding:.5em;border:1px solid #aaa}</style><h1>v3 高速靶场短测</h1><p>加权命中率＝有效命中中心接近度之和／全部实际发射数。人工视觉待复核；默认基线未升级。运动未达标仍保留成绩。</p><p>完整：'+str(result['complete'])+'；有效记录：'+str(len(rows))+'</p><table><tr><th>场景</th><th>弹道</th><th>次数</th><th>命中/发射</th><th>普通命中率</th><th>加权命中率</th><th>达速时命中/发射</th><th>达速时加权命中率</th><th>运动强度</th></tr>'+table+'</table><h2>原始证据</h2><ul>'+links+'</ul></html>')
    return result
