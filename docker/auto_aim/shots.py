"""Per-projectile scoring from observed simulator events; never commands a robot."""
import csv
import json
import math
import bisect


def baseline_key(manifest):
    if manifest.get('mode','track') != 'shoot':
        return manifest['scene']
    p = manifest['shooting']
    base = f"shoot_{manifest['scene']}_{p['physics']}_{p['bullet_speed']:g}_{p['reported_bullet_speed']:g}"
    if manifest.get('scenario_semantics'):
        base += '_scenario_'+str(manifest.get('random_seed',20261019))+'_visible_'+manifest.get('projectile_visible','on')
    if manifest.get('control_source')=='fixed_script':base+='_fixed_'+manifest['command_script_sha256'][:12]
    if p.get('hit_method'):
        base += f"_v3_{p['aim_origin']}_{manifest['duration_s']:g}s"
    if p.get('gimbal_profile','legacy') != 'legacy':
        base += '_gimbal_'+p['gimbal_profile']
    if manifest.get('algorithm_profile','legacy') != 'legacy':
        base += '_algorithm_'+manifest['algorithm_profile']
    if p.get('fire_rate_limit_hz',2) != 2:
        base += '_rate_'+str(p['fire_rate_limit_hz']).replace('.','p')+'hz'
    if p.get('fire_policy','advice') != 'advice':
        base += '_continuous_2hz'
    if p.get('launch_speed_frame'):
        base += '_restframe_speed_v1'
    return base


def score(folder, manifest, state):
    events = state.get('shot_events',[])
    strict = manifest.get('shooting',{}).get('hit_method') == 'swept_actual_segment_v3'
    traces = {}
    for packet in state.get('projectile_traces',[]):
        for projectile in packet['projectiles']:
            traces.setdefault(projectile['shot_seq'],[]).append(projectile['position'])
    grouped = {}
    errors = []
    start, end = state.get('measurement_start_stamp_ns'), state.get('measurement_end_stamp_ns')
    rest_speed = manifest.get('shooting',{}).get('launch_speed_frame') == 'shooter_rest_from_recorded_gt'
    robot_rows = []
    if rest_speed and not (folder/'telemetry.jsonl').exists():errors.append('Missing telemetry for launch speed validation')
    if rest_speed and (folder/'telemetry.jsonl').exists():
        with (folder/'telemetry.jsonl').open() as source:
            for line in source:
                packet = json.loads(line)
                if packet['kind'] == 'robots':
                    robot_rows.append(packet['data'])
        robot_rows.sort(key=lambda row:row['stamp_ns'])
    robot_stamps = [row['stamp_ns'] for row in robot_rows]
    def shooter_velocity(stamp, identity):
        i = bisect.bisect_left(robot_stamps,stamp)
        if i < len(robot_rows) and robot_stamps[i] == stamp:
            return next((r['velocity'] for r in robot_rows[i]['robots'] if r['id']==identity),None)
        if i==0 or i==len(robot_rows): return None
        a,b = robot_rows[i-1],robot_rows[i]
        if max(stamp-a['stamp_ns'],b['stamp_ns']-stamp)>100000000: return None
        left = next((r['velocity'] for r in a['robots'] if r['id']==identity),None)
        right = next((r['velocity'] for r in b['robots'] if r['id']==identity),None)
        if left is None or right is None:return None
        fraction = (stamp-a['stamp_ns'])/(b['stamp_ns']-a['stamp_ns'])
        return [x+(y-x)*fraction for x,y in zip(left,right)]
    for event in events:
        grouped.setdefault(event['shot_seq'],[]).append(event)
    rows = []
    for seq, values in sorted(grouped.items()):
        fires = [e for e in values if e['kind']==0]
        armor = [e for e in values if e['kind']==1]
        environment = [e for e in values if e['kind']==2]
        # The simulator permits an environment touch followed by an armor hit
        # (e.g. a ricochet). An environment event is not a terminal event.
        if len(fires)!=1 or len(armor)>1 or len(environment)>1:
            errors.append(f'shot {seq}: {len(fires)} fire / {len(armor)} armor / {len(environment)} environment events')
        if not fires:
            continue
        fire = fires[0]
        if start is None or end is None or not start <= fire['stamp_ns'] <= end:
            errors.append(f'shot {seq}: launched outside measurement')
        terminal = armor[0] if armor else environment[0] if environment else None
        if terminal and terminal['stamp_ns'] < fire['stamp_ns']:
            errors.append(f'shot {seq}: terminal predates launch')
        outcome = ('target_armor' if terminal and terminal['kind']==1 and terminal['target_id']==1
            else 'other_robot' if terminal and terminal['kind']==1
            else 'environment' if terminal else 'no_hit_event')
        point = terminal.get('contact_point') if terminal else None
        if not point or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in point):
            point = None
        trace_points = [fire['world_point']]+traces.get(seq,[])
        trace_gap = None
        if point and len(trace_points)>1:
            distances = []
            for a,b in zip(trace_points,trace_points[1:]):
                d = [y-x for x,y in zip(a,b)]
                length2 = sum(x*x for x in d)
                u = max(0,min(1,sum((x-y)*z for x,y,z in zip(point,a,d))/length2)) if length2>1e-14 else 0
                distances.append(math.dist(point,[x+u*y for x,y in zip(a,d)]))
            trace_gap = min(distances)
        if strict and not traces.get(seq):
            errors.append(f'shot {seq}: no observed physics trace')
        if strict and armor and (point is None or trace_gap is None or trace_gap>.011):
            errors.append(f'shot {seq}: contact is not within 11 mm of the observed sphere-centre path: {trace_gap}')
        t = (fire['stamp_ns']-start)/1e9 if start else None
        rows.append({'shot_seq':seq,'fire_time_s':t,'fire_stamp_ns':fire['stamp_ns'],
            'shooter_id':fire['shooter_id'],'outcome':outcome,
            'muzzle_position':fire['world_point'],'initial_velocity':fire['velocity'],
            'initial_speed_mps':math.sqrt(sum(v*v for v in fire['velocity'])),
            'environment_contact':bool(environment),
            'target_id':terminal['target_id'] if terminal else None,
            'armor_id':terminal['armor_id'] if terminal and terminal['kind']==1 else None,
            'contact_point':point,'contact_trace_gap_m':trace_gap,'trace_samples':len(traces.get(seq,[])),'flight_time_s':terminal['flight_time_s'] if terminal else None,
            'terminal_stamp_ns':terminal['stamp_ns'] if terminal else None})
        if rest_speed:
            own = shooter_velocity(fire['stamp_ns'],fire['shooter_id'])
            if own is None:
                errors.append(f'shot {seq}: no bracketed shooter velocity for rest-frame speed validation')
            relative = [v-u for v,u in zip(fire['velocity'],own)] if own is not None else None
            rows[-1].update(shooter_velocity_world=own,muzzle_relative_velocity=relative,
                muzzle_relative_speed_mps=math.sqrt(sum(v*v for v in relative)) if relative is not None else None)
    stamps = sorted(r['fire_stamp_ns'] for r in rows)
    rate = manifest.get('shooting',{}).get('fire_rate_limit_hz',2)
    if not isinstance(rate,(int,float)) or not math.isfinite(rate) or not 0 < rate <= 5:
        errors.append('Invalid configured firing rate')
    elif any(b-a < round((1/rate-.01)*1e9) for a,b in zip(stamps,stamps[1:])):
        errors.append(f'Actual launch spacing exceeded {rate:g} Hz limit')
    expected = manifest.get('shooting',{}).get('bullet_speed')
    speed_key = 'muzzle_relative_speed_mps' if rest_speed else 'initial_speed_mps'
    if expected is not None and any(r.get(speed_key) is None or abs(r[speed_key]-expected)>.2 for r in rows):
        errors.append('Observed muzzle speed differs from configured speed by >0.2 m/s')
    path = folder/'shots.jsonl'
    path.write_text(''.join(json.dumps(r,allow_nan=False)+'\n' for r in rows))
    with (folder/'shots.csv').open('w',newline='') as out:
        fields = list(rows[0]) if rows else ['shot_seq','fire_time_s','outcome']
        writer = csv.DictWriter(out,fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    hits = [r for r in rows if r['outcome']=='target_armor']
    flights = sorted(r['flight_time_s'] for r in rows if r['flight_time_s'] is not None)
    metrics = {'algorithm_fire_suggestions':state.get('algorithm_fire_suggestions',0),
        'fire_pulses':state.get('fire_advice_messages',0),'shots_fired':len(rows),
        'target_armor_hits':len(hits),'target_hit_rate':len(hits)/len(rows) if rows else None,
        'other_robot_hits':sum(r['outcome']=='other_robot' for r in rows),
        'environment_hits':sum(r['outcome']=='environment' for r in rows),
        'shots_with_environment_contact':sum(r['environment_contact'] for r in rows),
        'no_hit_events':sum(r['outcome']=='no_hit_event' for r in rows),
        'flight_time_p50_s':flights[len(flights)//2] if flights else None,
        'flight_time_p95_s':flights[int((len(flights)-1)*.95)] if flights else None,
        'maximum_launch_spacing_s':max(((b-a)/1e9 for a,b in zip(stamps,stamps[1:])),default=None),
        'minimum_launch_spacing_s':min(((b-a)/1e9 for a,b in zip(stamps,stamps[1:])),default=None),
        'first_hit_s':min(((r['terminal_stamp_ns']-start)/1e9 for r in hits),default=None) if start else None,
        'hit_contact_missing':sum(r['outcome']=='target_armor' and r['contact_point'] is None for r in rows),
        'hit_trace_gap_max_m':max((r['contact_trace_gap_m'] for r in rows if r['outcome']=='target_armor' and r['contact_trace_gap_m'] is not None),default=None),
        'projectile_trace_packets':len(state.get('projectile_traces',[])),
        'shot_sample_available':bool(rows)}
    return metrics, errors
