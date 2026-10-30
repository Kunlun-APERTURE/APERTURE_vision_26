"""Posthoc physical difficulty and shooting effects. Never feeds the algorithm."""
import bisect
import math
from pathlib import Path
from report import load,write
from impact_quality import summarize

def quantile(a,p):
    a=sorted(v for v in a if v is not None)
    return a[max(0,math.ceil(len(a)*p)-1)] if a else None

def angle(a,b):
    return abs(math.remainder(a-b,2*math.pi))

def phase_at(phases,t):
    return next((p for p in phases if p['t0']<=t<p['t1']),phases[-1])

def joint_good(row,desired,tolerance=.15):
    def speed(actual,requested):return abs(actual-requested)<=max(.1,abs(requested)*tolerance)
    return (speed(row['speed'],desired['target_speed']) and speed(row['ego_speed'],desired['ego_speed']) and
        (abs(row['omega']-desired['target_omega'])<=max(.3,abs(desired['target_omega'])*tolerance)))

def interval_metrics(rows,predicate,start,end,max_gap=.08):
    eligible=[r for r in rows if start<=r['t']<=end];total=good=longest=0.;origin=None;previous=None;previous_good=False
    for row in eligible:
        yes=predicate(row);gap=row['t']-previous if previous is not None else 0
        if gap>max_gap:origin=None
        if 0<gap<=max_gap:
            total+=gap
            if yes and previous_good:good+=gap
        if yes:
            if origin is None:origin=row['t']
            longest=max(longest,row['t']-origin)
        else:origin=None
        previous=row['t'];previous_good=yes
    return dict(observed_s=total,good_s=good,fraction=good/total if total else None,longest_hold_s=longest)

def analyze(folder,output=None,shots=None):
    folder=Path(folder);output=Path(output) if output else folder
    plan=load(folder/'configuration/scenario-plan.json');state=load(folder/'run-state.json')
    if not plan or plan.get('suite')!='movement-v1':raise ValueError('Not a movement-v1 scene')
    output.mkdir(parents=True,exist_ok=True);start=state['measurement_start_stamp_ns'];end=state['measurement_end_stamp_ns']
    samples=[]
    for line in (folder/'telemetry.jsonl').open():
        p=__import__('json').loads(line)
        if p['kind']!='robots' or not start<=p['data']['stamp_ns']<=end:continue
        robots={r['id']:r for r in p['data']['robots']}
        if 0 not in robots or 1 not in robots:continue
        ego,target=robots[0],robots[1];t=(p['data']['stamp_ns']-start)*1e-9
        samples.append(dict(t=t,speed=math.hypot(*target['velocity'][:2]),omega=target['yaw_rate'],
            ego_speed=math.hypot(*ego['velocity'][:2]),relative_speed=math.dist(ego['velocity'][:2],target['velocity'][:2]),
            range=math.dist(ego['position'][:2],target['position'][:2]),vx=target['velocity'][0],vy=target['velocity'][1],
            ego_position=ego['position'],target_position=target['position'],heading=math.atan2(target['velocity'][1],target['velocity'][0])))
    samples.sort(key=lambda r:r['t']);times=[r['t'] for r in samples]
    desired=lambda r:phase_at(plan['phases'],r['t'])
    joint=interval_metrics(samples,lambda r:joint_good(r,desired(r)),1,9)
    phase_results=[]
    for p in plan['phases'][1:]:
        # A two-second stage includes one second to accelerate and one stable second.
        val=interval_metrics(samples,lambda r:joint_good(r,p),p['t0'],p['t1'])
        phase_results.append(dict(p,**val,achieved=val['longest_hold_s']>=1.))
    active=[r for r in samples if r['t']>=1.]
    distance=interval_metrics(samples,lambda r:5<=r['range']<=8,1,9)
    turn_results=[]
    for t in plan['motion_change_times_s'] if plan['case'].startswith('turn') else []:
        before=[r for r in samples if t-.15<=r['t']<t]
        interval=plan['turn_command_interval_s'];after=[r for r in samples if t<=r['t']<min(9.,t+interval)]
        knot=min(plan['reference_knots'],key=lambda k:abs(k['t']-t));goal=math.radians(knot['target_heading_deg'])
        original=before[-1]['heading'] if before else None
        first=next((r for r in after if r['speed']>=1. and angle(r['heading'],goal)<=math.radians(10)),None)
        achieved_angle=max((angle(original,r['heading']) for r in after if r['speed']>=1.),default=0) if original is not None else None
        command_angle=45. if t<5 else 90.
        turn_results.append(dict(t=t,command_angle_deg=command_angle,actual_turn_deg=math.degrees(achieved_angle) if achieved_angle is not None else None,
            response_s=first['t']-t if first else None,responded_before_next=first is not None,
            minimum_speed_mps=min((r['speed'] for r in after),default=None)))
    turn_fraction=sum(r['responded_before_next'] for r in turn_results)/len(turn_results) if turn_results else None
    actual_highspeed=(quantile([r['speed'] for r in active],.5) or 0)>=3.
    turning_ok=not turn_results or (turn_fraction>=.75 and actual_highspeed)
    achieved=all(p['achieved'] for p in phase_results) and turning_ok and (distance['fraction'] or 0)>=.8
    if shots is None:shots=[__import__('json').loads(l) for l in (folder/'shots.jsonl').open()]
    enriched=[];joint_shots=[]
    for shot in shots:
        i=bisect.bisect_left(times,shot['fire_time_s']);near=min(samples[max(0,i-1):i+1],key=lambda r:abs(r['t']-shot['fire_time_s']),default=None)
        if near and abs(near['t']-shot['fire_time_s'])>.1:near=None
        row=dict(shot,launch_target_speed_mps=near['speed'] if near else None,launch_yaw_rate_radps=near['omega'] if near else None,
            launch_ego_speed_mps=near['ego_speed'] if near else None,launch_range_m=near['range'] if near else None,
            joint_motion_qualified=bool(near and near['t']>=1 and joint_good(near,desired(near))))
        enriched.append(row)
        if row['joint_motion_qualified']:joint_shots.append(row)
    def scores(a,b):
        selected=[s for s in enriched if a<=s['fire_time_s']<b];quality=summarize(selected)
        return dict(shots=len(selected),**quality,hits_per_s=sum(bool(s['useful_hit']) for s in selected)/(b-a))
    phases=[dict(name=p['name'],t0=p['t0'],t1=p['t1'],**scores(p['t0'],p['t1'])) for p in plan['phases']]
    for t in plan['motion_change_times_s']:
        for a,b in ((0,.5),(.5,1)):
            if t+b<=9:phases.append(dict(name=f'after_{t:g}_{a:g}_{b:g}',t0=t+a,t1=t+b,**scores(t+a,t+b)))
    hits=sorted(s['fire_time_s'] for s in shots if s['useful_hit']);edges=[0,*hits,9.]
    failures=[]
    if not all(p['achieved'] for p in phase_results):failures.append('simultaneous_speed_rate_stage_hold')
    if not turning_ok:failures.append('actual_direction_response_or_speed')
    if (distance['fraction'] or 0)<.8:failures.append('distance_band')
    result=dict(version=1,scene=plan['scene'],tier=plan['tier'],case=plan['case'],pressure=plan['pressure'],
        intensity_achieved=achieved,joint_motion=joint,phase_intensity=phase_results,turn_responses=turn_results,
        turn_response_fraction=turn_fraction,range_focus=distance,range_p50_m=quantile([r['range'] for r in active],.5),
        range_p95_m=quantile([r['range'] for r in active],.95),speed_p50_mps=quantile([r['speed'] for r in active],.5),
        omega_p50_radps=quantile([r['omega'] for r in active],.5),failure_reasons=failures,
        at_requested_motion=dict(shots=len(joint_shots),**summarize(joint_shots),hits_per_qualified_s=sum(bool(s['useful_hit']) for s in joint_shots)/joint['good_s'] if joint['good_s'] else None),
        phases=phases,range_groups=[dict(range_m=[a,b],shots=len(rr),**summarize(rr)) for a,b in ((0,5),(5,8),(8,1000)) for rr in [[s for s in enriched if s['launch_range_m'] is not None and a<=s['launch_range_m']<b]]],
        longest_no_hit_launch_interval_s=max((b-a for a,b in zip(edges,edges[1:])),default=9.),
        within_range_ground=all(abs(x)<59 and abs(y)<59 for r in samples for x,y,*_ in (r['ego_position'],r['target_position'])),
        samples=samples,truth_only_posthoc=True,scope='Motion difficulty is separate from evidence validity and shot effect. No low-score retry.')
    write(output/'motion-analysis.json',result)
    (output/'quality-shots.jsonl').write_text(''.join(__import__('json').dumps(r,allow_nan=False)+'\n' for r in enriched))
    return result

def report_metrics(folder):
    r=analyze(folder)
    return dict(motion_intensity_achieved=r['intensity_achieved'],motion_intensity=dict(joint=r['joint_motion'],failures=r['failure_reasons']),
        actual_range_p50_m=r['range_p50_m'],actual_range_p95_m=r['range_p95_m'],within_range_ground=r['within_range_ground'],
        relative_speed_p95_mps=quantile([s['relative_speed'] for s in r['samples']],.95))
