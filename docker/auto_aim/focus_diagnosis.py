"""Offline attribution over recorded truth and existing v3 read-only diagnostics."""
import collections,csv,json,math
from report import load,write
from impact_quality import summarize


def longest(rows,predicate):
    start=previous=None;best=0.
    for row in rows:
        t=row['t'];ok=predicate(row)
        if previous is not None and t-previous>.1:start=None
        if ok:
            if start is None:start=t
            best=max(best,t-start)
        else:start=None
        previous=t
    return best


def classify(frame,result):
    """Priority attribution; counts describe the earliest observed failed stage."""
    if not frame.get('truth_available'):return 'truth_unavailable'
    if frame.get('visible_plates',0)==0:return 'target_not_eligible_visible'
    if not result.get('observations'):return 'no_detector_output'
    if not frame.get('detection_matches'):return 'detector_output_unmatched'
    continuity=result.get('continuity') or {};observations=continuity.get('observations') or []
    updates=(continuity.get('filter') or {}).get('updates') or []
    if result.get('tracker')!='tracking':
        if observations and not any(x.get('pose_valid') and x.get('reprojection_rms_px',float('inf'))<=8 for x in observations):return 'pose_quality_rejected'
        if updates and not any(x.get('accepted') for x in updates):return 'filter_update_rejected'
        if observations and not any(x.get('compatible') for x in observations):return 'association_incompatible'
        return 'tracker_not_ready'
    if not result.get('valid'):return 'aim_invalid'
    check=result.get('fire_check') or {}
    if not check:return 'fire_diagnostic_unavailable'
    if not check.get('pose_valid'):return 'fire_pose_invalid'
    if not check.get('valid'):return 'fire_measurement_or_target_invalid'
    if not check.get('aligned'):return 'fire_not_aligned'
    if not result.get('fire_advice'):return 'fire_stability_or_other'
    return 'fire_advice_eligible'


def analyze(folder):
    manifest=load(folder/'manifest.json');plan=load(folder/'configuration/scenario-plan.json',{});state=load(folder/'run-state.json');start=state['measurement_start_stamp_ns'];end=state['measurement_end_stamp_ns']
    if start is None or end is None:raise ValueError('Measurement timestamps unavailable')
    frames={r['stamp_ns']:r for r in (json.loads(l) for l in (folder/'frames.jsonl').open()) if r.get('measurement',True)}
    motion=load(folder/'motion-analysis.json',{});samples=motion.get('samples',[]);times=[r['t'] for r in samples]
    import bisect
    rows=[]
    for line in (folder/'telemetry.jsonl').open():
        packet=json.loads(line)
        if packet['kind']!='observation':continue
        obs=packet['data'];f=frames.get(obs['frame']['stamp_ns'])
        if f is None or f.get('time') is None:continue
        r=obs['result'];c=r.get('continuity') or {};fire=r.get('fire_check') or {};t=f['time']
        i=bisect.bisect_left(times,t);near=samples[max(0,i-1):min(len(samples),i+1)]
        truth=min(near,key=lambda x:abs(x['t']-t)) if near else None
        if truth is not None and abs(truth['t']-t)>.1:truth=None
        updates=(c.get('filter') or {}).get('updates') or []
        row=dict(t=t,stamp_ns=f['stamp_ns'],video_index=f['video_index'],range_m=truth['range'] if truth else None,
            speed_mps=truth['speed'] if truth else None,vx=truth['vx'] if truth else None,vy=truth['vy'] if truth else None,
            tracker=r.get('tracker'),correct_tracking=f.get('correct_tracking'),visible=f.get('visible_plates',0)>0,
            detector_output=len(r.get('observations') or []),matched_detections=f.get('detection_matches',0),
            stage=classify(f,r),reset_reason=c.get('reset_reason'),update_accepted=any(x.get('accepted') for x in updates),
            update_reasons=[x.get('reason') for x in updates],association_reasons=[x.get('reason') for x in c.get('associations') or []],
            fire_valid=fire.get('valid'),fire_aligned=fire.get('aligned'),fire_pose_valid=fire.get('pose_valid'),fire_eligible=fire.get('eligible'),
            fire_advice=bool(r.get('fire_advice')),fire_forwarded=obs.get('fire_forwarded'),command_emitted=obs.get('command_emitted'),
            yaw_error_rad=fire.get('yaw_error_rad'),yaw_tolerance_rad=fire.get('yaw_tolerance_rad'),pitch_error_rad=fire.get('pitch_error_rad'),
            stable_time_s=fire.get('stable_time_s'),discontinuity=fire.get('discontinuity'),trusted_age_s=c.get('trusted_update_age_s'),generation=c.get('generation'))
        rows.append(row)
    shots=[json.loads(l) for l in (folder/'shots.jsonl').open()]
    change_times=plan.get('motion_change_times_s',[])
    windows=[('all',0,manifest['duration_s'])]+[('steady',a,b) for a,b in plan.get('steady_windows_s',[])]
    for change in change_times:
        windows += [(f'change_{change:g}_{a:g}_{b:g}',change+a,min(change+b,manifest['duration_s'])) for a,b in ((-.5,0),(0,.5),(.5,1),(1,2),(0,2))]
    window_results=[]
    def summarize_frames(subset):
        n=len(subset)
        return dict(frames=n,stage_counts=dict(collections.Counter(r['stage'] for r in subset)),
            visible_fraction=sum(r['visible'] for r in subset)/n if n else None,
            correct_tracking_fraction=sum(bool(r['correct_tracking']) for r in subset)/n if n else None,
            matched_detection_fraction=sum(r['matched_detections']>0 for r in subset)/n if n else None,
            tracker_tracking_fraction=sum(r['tracker']=='tracking' for r in subset)/n if n else None,
            fire_advice_fraction=sum(r['fire_advice'] for r in subset)/n if n else None,
            trusted_reset_count=sum(r['reset_reason']=='trusted_update_timeout' for r in subset),
            update_reasons=dict(collections.Counter(reason for r in subset for reason in r['update_reasons'])),
            fire_block_counts={key:sum(r[key] is False for r in subset) for key in ('fire_valid','fire_aligned','fire_pose_valid')})
    for name,a,b in windows:
        if b<=a:continue
        rs=[r for r in rows if a<=r['t']<b];ss=[s for s in shots if a<=s['fire_time_s']<b]
        inrange=[r for r in rs if r['range_m'] is not None and 5<=r['range_m']<=8]
        window_results.append(dict(name=name,t0=a,t1=b,**summarize_frames(rs),shots=len(ss),**summarize(ss),
            within_5_8m_fraction=len(inrange)/len(rs) if rs else None,near_range=summarize_frames(inrange)))
    primary=[r for r in rows if r['range_m'] is not None and 5<=r['range_m']<=8 and r['t']>=2]
    maneuvers=[]
    requested=manifest.get('scenario_semantics',{}).get('requested_target_speed_mps',0.)
    for j,change in enumerate(change_times):
        pre=[r for r in rows if change-1<=r['t']<change];post=[r for r in rows if change<=r['t']<=min(change+2,manifest['duration_s'])]
        # Restart is preceded by an intentional stop, so only the first maneuver requires high speed.
        speed_hold=longest(pre,lambda r:r['speed_mps'] is not None and abs(r['speed_mps']-requested)<=.15*requested) if j==0 else None
        window=[r for r in rows if change-.5<=r['t']<=min(change+2,manifest['duration_s'])]
        near=sum(r['range_m'] is not None and 5<=r['range_m']<=8 for r in window)/len(window) if window else 0.
        first_advice=next((r['t']-change for r in post if r['fire_advice']),None)
        launched=[s for s in shots if change<=s['fire_time_s']<=min(change+2,manifest['duration_s'])]
        impacts=[(s['terminal_stamp_ns']-start)*1e-9-change for s in launched if s.get('useful_hit') and s.get('terminal_stamp_ns') is not None and (s['terminal_stamp_ns']-start)*1e-9<=min(change+2,manifest['duration_s'])]
        # Physical response reference: first sustained velocity-vector change from the last 0.2 s of pre-command motion.
        last=[r for r in pre if r['t']>=change-.2 and r['vx'] is not None]
        physical_start=None
        if last:
            vx=sum(r['vx'] for r in last)/len(last);vy=sum(r['vy'] for r in last)/len(last)
            for k,r in enumerate(post):
                following=post[k:k+3]
                if len(following)==3 and all(s['vx'] is not None and math.hypot(s['vx']-vx,s['vy']-vy)>=.3 for s in following):physical_start=r['t'];break
        maneuvers.append(dict(command_change_s=change,physical_velocity_departure_s=physical_start,
            physical_reference='First 3 samples with velocity vector >=0.3 m/s from preceding 0.2 s mean; diagnostic only.',
            pre_speed_hold_s=speed_hold,pre_speed_qualified=speed_hold>=.5 if speed_hold is not None else None,
            near_range_fraction=near,near_range_qualified=near>=.85,first_advice_delay_s=first_advice,
            first_launch_delay_s=min((s['fire_time_s']-change for s in launched),default=None),
            first_useful_impact_delay_s=min(impacts,default=None),post_2s_shots=len(launched),post_2s=summarize(launched)))
    result=dict(run_id=folder.name,scene=manifest['scene'],rows=len(rows),primary_5_8m_after_2s=summarize_frames(primary),windows=window_results,
        maneuvers=maneuvers,limitations=['Attribution is a priority classification of observed failures, not a causal ablation.',
        'Unmatched detector output may reflect detector or pose/projection/matching error; raw JPEG replay is needed to distinguish.',
        'Fire checks are overlapping diagnostics; frame advice is distinct from forwarded pulses and actual launches.'])
    write(folder/'focus-diagnosis.json',result)
    (folder/'focus-frames.jsonl').write_text(''.join(json.dumps(r,allow_nan=False)+'\n' for r in rows))
    with (folder/'focus-frames.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]) if rows else ['t']);writer.writeheader();writer.writerows(rows)
    return result
