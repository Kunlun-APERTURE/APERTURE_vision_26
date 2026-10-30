"""Independent v4 diagnostics. Simulator truth is never sent to the algorithm."""
import bisect,collections,csv,json,math,statistics,subprocess,tomllib
from pathlib import Path
import yaml
from continuity_replay import Interpolate,q
from diagnosis_v4 import ALGORITHM,HISTORY,OPS,OUTPUT,ROOT,digest
from report import load,write

def wrap(x):return math.remainder(x,2*math.pi)
def dot(a,b):return sum(x*y for x,y in zip(a,b))
def sub(a,b):return [x-y for x,y in zip(a,b)]
def add(a,b):return [x+y for x,y in zip(a,b)]
def scale(a,s):return [x*s for x in a]
def norm(a):return math.sqrt(dot(a,a))
def average(values):return statistics.mean(values) if values else None
def summary(values):
    values=[v for v in values if v is not None and math.isfinite(v)]
    return dict(n=len(values),mean=average(values),p50=q(values),p90=q(values,.9),p95=q(values,.95))
def jsonl(p):return [json.loads(l) for l in Path(p).open()]
def write_rows(path,rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(''.join(json.dumps(r,allow_nan=False,separators=(',',':'))+'\n' for r in rows))
    keys=list(dict.fromkeys(k for r in rows for k in r))
    with path.with_suffix('.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=keys or ['no_samples']);w.writeheader()
        for row in rows:w.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v for k,v in row.items()})

class Evidence:
    def __init__(self,row):
        self.row=row;self.folder=HISTORY.parent/row['run_id'];self.state=load(self.folder/'run-state.json');self.manifest=load(self.folder/'manifest.json')
        self.start=self.state['measurement_start_stamp_ns'];self.end=self.state['measurement_end_stamp_ns']
        self.observations=[];self.traces=[];self.armors=collections.defaultdict(list);self.robot=[];self.projectiles=collections.defaultdict(list)
        self.commands=[];self.poses=[]
        for packet in jsonl(self.folder/'telemetry.jsonl'):
            d=packet['data'];kind=packet['kind']
            if kind=='observation':
                self.observations.append(d);p=d['frame'].get('latest_muzzle_pose')
                if p:self.poses.append((p['stamp_ns'],[p['yaw'],p['elevation']]))
            elif kind=='armors':
                for a in d['armors']:
                    if a['robot_id']==1:self.armors[a['id']].append((d['stamp_ns'],a['position']+a['normal']+[a['slot'],a['width'],a['height']]))
            elif kind=='robots':
                for r in d['robots']:
                    if r['id']==1:self.robot.append((d['stamp_ns'],r['position']+r['velocity']+[r['yaw_rate']]))
            elif kind=='gimbal_trace':self.traces.append(d)
            elif kind=='command':self.commands.append(d)
            elif kind=='projectile_trace':
                for p in d['projectiles']:self.projectiles[p['shot_seq']].append((d['stamp_ns'],p['position']+p['velocity']))
        self.by_stamp={d['frame']['stamp_ns']:d for d in self.observations}
        self.armor_history={identity:Interpolate(samples) for identity,samples in self.armors.items()}
        self.robot_history=Interpolate(self.robot);self.pose_history=Interpolate(sorted(dict(self.poses).items()))
        self.shots=jsonl(self.folder/'shots.jsonl');self.index={r['stamp_ns']:r for r in jsonl(self.folder/'frames.jsonl')}
        self.pulses=sorted((d['receipt_ns'],d) for d in self.observations if d.get('fire_forwarded'))
        self.pulse_times=[p[0] for p in self.pulses]
    def time(self,stamp):return (stamp-self.start)*1e-9
    def measured(self,stamp):return self.start<=stamp<=self.end
    def plates(self,stamp):return {identity:h.at(stamp) for identity,h in self.armor_history.items() if h.at(stamp) is not None}
    def front(self,stamp):return next(((identity,p) for identity,p in self.plates(stamp).items() if round(p[6])==0),None)
    def launch_observation(self,shot):
        stamp=shot['fire_stamp_ns'];i=bisect.bisect_right(self.pulse_times,stamp)-1
        if i<0:return None,None
        age=(stamp-self.pulse_times[i])*1e-9
        return (self.pulses[i][1],age) if age<=.1 else (None,None)
    def replay(self,variant='diagnosis'):return jsonl(OUTPUT/'replay'/variant/self.row['run_id']/'results.jsonl')

def pitch(v,d,h,g=9.7833):
    a=g*d*d/(2*v*v);delta=d*d-4*a*(a+h)
    return math.atan((d-math.sqrt(delta))/(2*a)) if delta>=0 and a>0 else None

def velocity(speed,yaw,elevation):return [speed*math.cos(elevation)*math.cos(yaw),speed*math.cos(elevation)*math.sin(yaw),speed*math.sin(elevation)]

def plane_path(position,initial,center,normal,gravity,beta,wind=(0,0,0),dt=1/960):
    """Independent RK4 quadratic drag, exact line interpolation at first plate-plane crossing."""
    def derivative(state):
        relative=sub(state[3:],wind);speed=norm(relative)
        return state[3:]+[-beta*speed*relative[0],-beta*speed*relative[1],-gravity-beta*speed*relative[2]]
    state=position+initial;previous=dot(sub(state[:3],center),normal)
    for step in range(math.ceil(1./dt)):
        k1=derivative(state);k2=derivative(add(state,scale(k1,dt*.5)));k3=derivative(add(state,scale(k2,dt*.5)));k4=derivative(add(state,scale(k3,dt)))
        nxt=[s+dt*(a+2*b+2*c+d)/6 for s,a,b,c,d in zip(state,k1,k2,k3,k4)]
        current=dot(sub(nxt[:3],center),normal)
        if current*previous<=0 and step>0:
            fraction=previous/(previous-current);return [a+fraction*(b-a) for a,b in zip(state[:3],nxt[:3])],(step+fraction)*dt
        state=nxt;previous=current
    return None,None

def static_analysis(rows):
    output=[];frames=[];runs=[]
    for row in rows:
        if row['stage']!='mt' or row['role'] not in ('formal','supplement') or row['scene']!='hf_static_8':continue
        ev=Evidence(row);replay={r['stamp_ns']:r for r in ev.replay()}
        config=tomllib.loads((ev.folder/'configuration/simulator.toml').read_text())['projectile'];aero=config['aerodynamics']
        beta=.5*aero['air_density']*aero['drag_coefficient']*math.pi*(config['diameter']/2)**2/config['mass'] if aero['enabled'] else 0
        for d in ev.observations:
            f=d['frame'];stamp=f['stamp_ns'];r=d['result'];target=ev.front(stamp)
            if not ev.measured(stamp) or not target:continue
            center=target[1][:3];origin=f['gimbal_position_map'];aim=r.get('aim_point');diag=replay.get(stamp,{})
            pos=[]
            for a in diag.get('continuity',{}).get('diagnosis_poses',[]):
                if a.get('pose_valid') and a.get('xyz'):pos.append(add(a['xyz'],origin))
            nearest=min(pos,key=lambda p:norm(sub(p,center))) if pos else None
            frames.append(dict(run_id=row['run_id'],profile=row['profile'],physics=row['physics'],seed=row['seed'],time_s=ev.time(stamp),stamp_ns=stamp,
                video_index=ev.index.get(stamp,{}).get('video_index'),pose_range_error_m=norm(sub(nearest,f['muzzle_position_map']))-norm(sub(center,f['muzzle_position_map'])) if nearest else None,
                pose_height_error_m=nearest[2]-center[2] if nearest else None,aim_height_error_m=aim[2]+origin[2]-center[2] if aim else None,
                command_pitch_rad=r.get('pitch'),command_pitch_rate_radps=r.get('pitch_rate_radps'),trusted_age_s=r.get('continuity',{}).get('trusted_update_age_s')))
        for shot in ev.shots:
            d,latency=ev.launch_observation(shot);target=ev.front(shot['fire_stamp_ns'])
            result=dict(run_id=row['run_id'],profile=row['profile'],physics=row['physics'],seed=row['seed'],shot_seq=shot['shot_seq'],
                time_s=shot['fire_time_s'],outcome=shot['outcome'],useful_hit=shot['useful_hit'],impact_y_m=shot.get('impact_y_m'),
                impact_x_m=shot.get('impact_x_m'),weight=shot.get('impact_center_proximity'),paired=d is not None,launch_latency_s=latency)
            if d is not None and target is not None:
                r=d['result'];f=d['frame'];center=target[1][:3];normal=target[1][3:6];length=math.hypot(*normal[:2]);up=[-normal[2]*normal[0]/length,-normal[2]*normal[1]/length,length]
                muzzle=shot['muzzle_position'];actual=shot['initial_velocity'];command=velocity(norm(actual),r['yaw'],-r['pitch'])
                vacuum_cmd,_=plane_path(muzzle,command,center,normal,9.7833,0)
                vacuum_actual,_=plane_path(muzzle,actual,center,normal,9.7833,0)
                physics_actual,flight=plane_path(muzzle,actual,center,normal,9.81,beta,aero['wind'])
                if vacuum_cmd and vacuum_actual and physics_actual:
                    vc=dot(sub(vacuum_cmd,center),up);va=dot(sub(vacuum_actual,center),up);pa=dot(sub(physics_actual,center),up)
                    result.update(geometry_command_error_m=vc,execution_error_m=va-vc,physics_model_error_m=pa-va,
                        independently_predicted_vertical_m=pa,contact_prediction_residual_m=shot['impact_y_m']-pa if shot.get('impact_y_m') is not None else None,independent_flight_time_s=flight)
                aim=r.get('aim_point');origin=f['gimbal_position_map'];ideal=pitch(norm(actual),math.hypot(center[0]-muzzle[0],center[1]-muzzle[1]),center[2]-muzzle[2])
                result.update(command_stamp_ns=f['stamp_ns'],video_index=ev.index.get(f['stamp_ns'],{}).get('video_index'),
                    capture_to_launch_s=(shot['fire_stamp_ns']-f['stamp_ns'])*1e-9,command_elevation_rad=-r['pitch'],
                    actual_elevation_rad=math.atan2(actual[2],math.hypot(*actual[:2])),ideal_vacuum_elevation_rad=ideal)
                if aim:result.update(aim_range_error_m=math.hypot(aim[0]+origin[0]-muzzle[0],aim[1]+origin[1]-muzzle[1])-math.hypot(center[0]-muzzle[0],center[1]-muzzle[1]),aim_height_error_m=aim[2]+origin[2]-center[2])
            output.append(result)
        ss=[r for r in output if r['run_id']==row['run_id']]
        runs.append(dict(run_id=row['run_id'],profile=row['profile'],physics=row['physics'],seed=row['seed'],shots=len(ss),hits=sum(r['useful_hit'] for r in ss),
            paired_fraction=sum(r['paired'] for r in ss)/len(ss),
            **{k:summary([r.get(k) for r in ss]) for k in ('impact_y_m','geometry_command_error_m','execution_error_m','physics_model_error_m','contact_prediction_residual_m','aim_range_error_m','aim_height_error_m')}))
        print(json.dumps(dict(static_analyzed=len(runs),run_id=row['run_id'])),flush=True)
    write_rows(OUTPUT/'static/shots.jsonl',output);write_rows(OUTPUT/'static/frames.jsonl',frames)
    write(OUTPUT/'static/summary.json',dict(runs=runs,source='All 8 original timing-stage 8m static runs plus the explicitly marked bounded supplemental recordings',
        decomposition='Vacuum commanded direction, vacuum actual launch, independent gravity+drag actual launch, observed contact residual, all projected onto the same actual plate plane.',
        limits='Pulse/launch matches are time-inferred within 100ms. Geometry and command remain combined until replay pose evidence separates them. Independent integration is not the simulator solver.'))

def static_prior_analysis(rows):
    runs=[];records=[]
    for row in rows:
        if row['stage']!='mt' or row['role'] not in ('formal','supplement') or row['scene']!='hf_static_8':continue
        ev=Evidence(row)
        for variant in ('diagnosis','no-hint','delay-original','delay-measured'):
            errors=[];phase=[];pitch_values=[]
            for r in ev.replay(variant):
                stamp=r['stamp_ns'];t=ev.time(stamp);front=ev.front(stamp);a=r.get('aim_point');x=r.get('filter_state')
                if not ev.measured(stamp) or t<2 or not front or not a or not x:continue
                f=ev.by_stamp[stamp]['frame'];world=add(a,f['gimbal_position_map']);truth=front[1][:3];m=f['muzzle_position_map']
                error=norm(sub(world,m))-norm(sub(truth,m));angle=abs(wrap(x[6]))
                errors.append(error);phase.append(angle);pitch_values.append(r['pitch'])
                records.append(dict(run_id=row['run_id'],role=row['role'],profile=row['profile'],physics=row['physics'],seed=row['seed'],variant=variant,
                    time_s=t,stamp_ns=stamp,video_index=ev.index.get(stamp,{}).get('video_index'),range_error_m=error,
                    absolute_phase_rad=angle,pitch_rad=r['pitch']))
            runs.append(dict(run_id=row['run_id'],role=row['role'],profile=row['profile'],physics=row['physics'],seed=row['seed'],variant=variant,
                range_error=summary(errors),absolute_phase=summary(phase),pitch=summary(pitch_values)))
    write_rows(OUTPUT/'static/prior-intervention.jsonl',records);write(OUTPUT/'static/prior-summary.json',dict(runs=runs,
        interpretation='Supplemental recorded-session reproduction isolates the pose prior on identical detections. Historical counterfactuals lack the original initialization.'))

def timing_closed_loop_summary():
    batch=load(HISTORY/'stage-mt/batch.json');groups=[]
    for scene in batch['definition']['scenes']:
        for physics in batch['definition']['physics']:
            for profile in (batch['definition']['baseline'],batch['definition']['candidate']):
                selected=[r for r in batch['runs'] if r['metrics']['scene']==scene and r['metrics']['physics']==physics and r['metrics']['profile']==profile]
                scores=[r['metrics']['all'] for r in selected];n=sum(s['shots'] for s in scores);h=sum(s['useful_armor_hits'] for s in scores);duration=sum(s['duration_s'] for s in scores)
                weighted=sum(s['weighted_hit_score_sum'] for s in scores)
                groups.append(dict(scene=scene,physics=physics,profile=profile,runs=len(selected),shots=n,hits=h,hit_rate=h/n if n else None,
                    weighted_hit_rate=weighted/n if n else None,useful_hits_per_s=h/duration if duration else None,
                    per_seed=[dict(seed=r['metrics']['seed'],run_id=r['run_id'],all=r['metrics']['all'],target=r['metrics']['target']) for r in selected]))
    write(OUTPUT/'timing/closed-loop-summary.json',dict(runs=64,groups=groups,original_acceptance=load(HISTORY/'stage-mt/acceptance.json'),
        interpretation='Original observed closed-loop outcomes, grouped by scene, physics and seed. Supplemental diagnostic recordings are excluded.'))

def project(frame,world):
    cam=frame['camera_in_map'];x,y,z,w=cam['quaternion'];v=sub(world,cam['position'])
    R=[[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)],
       [2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)],
       [2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]]
    local=[sum(R[j][i]*v[j] for j in range(3)) for i in range(3)]
    if local[2]<=.01:return None
    k=frame['calibration']['k'];return [k[0]*local[0]/local[2]+k[2],k[4]*local[1]/local[2]+k[5]]

def match_image_pose(ev,frame,pose):
    center=pose.get('image_center')
    if center is None:return None
    candidates=[];camera=frame['camera_in_map']['position']
    for identity,p in ev.plates(frame['stamp_ns']).items():
        if dot(p[3:6],sub(camera,p[:3]))<=0:continue
        pixel=project(frame,p[:3])
        if pixel:candidates.append((math.dist(center,pixel),identity,p))
    candidates.sort()
    if not candidates or candidates[0][0]>25:return None
    if len(candidates)>1 and candidates[1][0]-candidates[0][0]<5:return None
    return candidates[0]

def rotation_analysis(rows):
    all_frames=[];all_poses=[];runs=[];rejects=[]
    for row in rows:
        if row['stage']!='mtr' or row['role'] not in ('formal','supplement') or 'spin' not in row['scene']:continue
        ev=Evidence(row)
        for variant in ('diagnosis','no-hint'):
            rr=[];poses=[];prior_identity=None;prior_phase=None;prior_stamp=None;prior_plate=None
            for r in ev.replay(variant):
                stamp=r['stamp_ns'];obs=ev.by_stamp[stamp];frame=obs['frame'];robot=ev.robot_history.at(stamp);t=ev.time(stamp)
                if not ev.measured(stamp) or robot is None:continue
                filter=r.get('continuity',{}).get('filter') or {};x=r.get('filter_state');rigid=filter.get('rigid_state');drotation=filter.get('diagnosis_rotation') or {}
                model=r.get('diagnosis',{}).get('aimer',{}).get('current_plates',[])
                selected=r.get('selected_plate_id',-1);physical=None;distance=None
                if 0<=selected<len(model):
                    world=add(model[selected][:3],frame['gimbal_position_map']);matches=sorted((norm(sub(world,p[:3])),identity) for identity,p in ev.plates(stamp).items())
                    if matches and matches[0][0]<.25 and (len(matches)<2 or matches[1][0]-matches[0][0]>.03):distance,physical=matches[0]
                rec=dict(run_id=row['run_id'],variant=variant,profile=row['profile'],scene=row['scene'],physics=row['physics'],seed=row['seed'],time_s=t,stamp_ns=stamp,
                    video_index=ev.index.get(stamp,{}).get('video_index'),valid=r['valid'],tracker=r['tracker'],truth_omega=robot[6],
                    estimated_omega=x[7] if x else None,rigid_omega=rigid[7] if rigid else None,rigid_phase=rigid[6] if rigid else None,
                    circular=filter.get('circular_active',False),rotation_sigma=filter.get('rotation_sigma'),good_frames=drotation.get('good_frames'),bad_frames=drotation.get('bad_frames'),
                    selected_id=selected,physical_id=physical,physical_distance_m=distance,selected_identity_changed=physical is not None and prior_identity is not None and physical!=prior_identity)
                if rigid and prior_phase is not None and prior_stamp is not None and .01<(stamp-prior_stamp)*1e-9<.1:
                    rec['rigid_phase_step']=wrap(rigid[6]-prior_phase)
                    rec['phase_step_expected']=robot[6]*(stamp-prior_stamp)*1e-9
                if physical is not None:prior_identity=physical
                if rigid:prior_phase=rigid[6];prior_stamp=stamp
                rr.append(rec)
                for pose in r.get('continuity',{}).get('diagnosis_poses',[]):
                    if not pose.get('pose_valid'):continue
                    match=match_image_pose(ev,frame,pose);candidates=pose.get('candidates',[]);best=pose.get('best_cost');index=pose.get('chosen_index')
                    pr=dict(run_id=row['run_id'],variant=variant,scene=row['scene'],profile=row['profile'],physics=row['physics'],seed=row['seed'],time_s=t,stamp_ns=stamp,
                        video_index=ev.index.get(stamp,{}).get('video_index'),matched=match is not None,hint=pose.get('supplied_hint'),
                        chosen_yaw=pose.get('chosen_yaw',pose.get('yaw')),raw_yaw=pose.get('raw_yaw'),best_cost=best,chosen_cost=pose.get('chosen_cost'),rms=pose.get('rms'))
                    if match:
                        pixel_error,identity,p=match;truth_yaw=math.atan2(-p[4],-p[3]);yaw=pr['chosen_yaw'];raw=pr['raw_yaw'];hint=pr['hint']
                        best_yaw=min(candidates,key=lambda a:a[1])[0] if candidates else None
                        pr.update(physical_id=identity,pixel_match_error=pixel_error,truth_yaw=truth_yaw,pose_yaw_error=wrap(yaw-truth_yaw),
                            raw_yaw_error=wrap(raw-truth_yaw) if raw is not None else None,hint_error=wrap(hint-truth_yaw) if hint is not None else None,
                            best_yaw_error=wrap(best_yaw-truth_yaw) if best_yaw is not None else None,
                            range_error_m=norm(sub(add(pose['xyz'],frame['gimbal_position_map']),frame['camera_in_map']['position']))-norm(sub(p[:3],frame['camera_in_map']['position'])),
                            truth_outside_search=abs(wrap(truth_yaw-pose.get('search_centre_yaw',truth_yaw)))>math.radians(70),
                            identity_changed=prior_plate is not None and identity!=prior_plate)
                        prior_plate=identity
                    poses.append(pr)
                for update in drotation.get('updates',[]):
                    rejects.append(dict(run_id=row['run_id'],variant=variant,time_s=t,stamp_ns=stamp,scene=row['scene'],**update))
            steady=[r for r in rr if r['time_s']>=2 and abs(r['truth_omega'])>=2.55 and r['tracker']=='tracking']
            measured=[r for r in poses if r['time_s']>=2];matched=[r for r in measured if r['matched']]
            runs.append(dict(run_id=row['run_id'],variant=variant,profile=row['profile'],scene=row['scene'],physics=row['physics'],seed=row['seed'],
                frames=len(steady),omega_abs_error=summary([abs(r['estimated_omega']-r['truth_omega']) for r in steady if r['estimated_omega'] is not None]),
                rigid_omega_abs_error=summary([abs(r['rigid_omega']-r['truth_omega']) for r in steady if r['rigid_omega'] is not None]),
                active_fraction=sum(r['circular'] for r in steady)/len(steady) if steady else None,
                pose_matching_coverage=len(matched)/len(measured) if measured else None,
                yaw_error=summary([abs(r['pose_yaw_error']) for r in matched]),raw_yaw_error=summary([abs(r['raw_yaw_error']) for r in matched if r['raw_yaw_error'] is not None]),
                hint_error=summary([abs(r['hint_error']) for r in matched if r['hint_error'] is not None]),
                outside_search_fraction=sum(r['truth_outside_search'] for r in matched)/len(matched) if matched else None,
                update_reasons=dict(collections.Counter(r['reason'] for r in rejects if r['run_id']==row['run_id'] and r['variant']==variant))))
            all_frames+=rr;all_poses+=poses
        print(json.dumps(dict(rotation_analyzed=len(runs)//2,run_id=row['run_id'])),flush=True)
    write_rows(OUTPUT/'rotation/frames.jsonl',all_frames);write_rows(OUTPUT/'rotation/poses.jsonl',all_poses);write_rows(OUTPUT/'rotation/updates.jsonl',rejects)
    write(OUTPUT/'rotation/summary.json',dict(runs=runs,pose_match='Unique visible-facing GT center in image <=25px and >=5px margin; phase uses negative physical outward normal to match solver convention.',
        interventions='Yaw hint suppression is offline only, with identical recorded detections; no hit gain is inferred.'))

def physical_prediction(ev,r):
    stamp=r['stamp_ns'];frame=ev.by_stamp[stamp]['frame'];aimer=r.get('diagnosis',{}).get('aimer',{});iterations=aimer.get('iterations',[])
    if not r['valid'] or not iterations:return None
    i=iterations[-1];world=add(i['current_plate'][:3],frame['gimbal_position_map'])
    candidates=sorted((norm(sub(world,p[:3])),identity) for identity,p in ev.plates(stamp).items())
    if not candidates or candidates[0][0]>.25 or len(candidates)>1 and candidates[1][0]-candidates[0][0]<=.03:return None
    identity=candidates[0][1];future=ev.armor_history[identity].at(stamp+round(i['horizon_s']*1e9))
    if future is None:return None
    truth=sub(future[:3],frame['muzzle_position_map']);pred=sub(i['point'][:3],sub(frame['muzzle_position_map'],frame['gimbal_position_map']))
    return dict(physical_id=identity,horizon_s=i['horizon_s'],yaw_error_rad=wrap(math.atan2(pred[1],pred[0])-math.atan2(truth[1],truth[0])),
        point_error_m=norm(sub(pred,truth)),vertical_error_m=pred[2]-truth[2],current_match_error_m=candidates[0][0],
        yaw_variance_current=i['yaw_variance_current'],yaw_variance_propagated=i['yaw_variance_propagated_state'],
        yaw_variance_process=i['yaw_variance_future_process'],yaw_variance_future=i['yaw_variance_future'])

def confidence_analysis(rows):
    records=[];runs=[]
    for row in rows:
        if row['stage']!='m' or row['role']!='formal' or row['profile']!='tracking_fire_v4m':continue
        ev=Evidence(row);rr=[];eligible=0
        for r in ev.replay():
            if not ev.measured(r['stamp_ns']) or ev.time(r['stamp_ns'])<2:continue
            iterations=r.get('diagnosis',{}).get('aimer',{}).get('iterations',[])
            if r['valid'] and iterations:eligible+=1
            match=physical_prediction(ev,r)
            if match is None:continue
            live=ev.by_stamp[r['stamp_ns']]['result'];check=live.get('fire_check') or {};sigma=math.sqrt(max(0,match['yaw_variance_future']));tolerance=check.get('yaw_tolerance_rad')
            rec=dict(run_id=row['run_id'],scene=row['scene'],physics=row['physics'],seed=row['seed'],time_s=ev.time(r['stamp_ns']),stamp_ns=r['stamp_ns'],
                video_index=ev.index.get(r['stamp_ns'],{}).get('video_index'),**match,
                sigma_rad=sigma,normalized_error=abs(match['yaw_error_rad'])/sigma if sigma>0 else None,
                covered95=abs(match['yaw_error_rad'])<=1.96*sigma,interval_to_target=1.96*sigma/tolerance if tolerance and tolerance>0 else None,
                process_fraction=match['yaw_variance_process']/match['yaw_variance_future'] if match['yaw_variance_future']>0 else None,
                propagated_fraction=match['yaw_variance_propagated']/match['yaw_variance_future'] if match['yaw_variance_future']>0 else None)
            rr.append(rec)
        records+=rr
        runs.append(dict(run_id=row['run_id'],scene=row['scene'],physics=row['physics'],seed=row['seed'],valid_frames=eligible,matched_frames=len(rr),
            matching_coverage=len(rr)/eligible if eligible else None,coverage95=sum(r['covered95'] for r in rr)/len(rr) if rr else None,
            **{k:summary([r.get(k) for r in rr]) for k in ('sigma_rad','normalized_error','interval_to_target','process_fraction','propagated_fraction','yaw_variance_current','yaw_variance_future')}))
    write_rows(OUTPUT/'confidence/frames.jsonl',records);write(OUTPUT/'confidence/summary.json',dict(runs=runs,
        policy='No uncertainty scaling or threshold is fit. Covariance is decomposed into propagated state and future process noise at the actual two-stage prediction horizon.',
        limits='Coverage is conditional on unique physical plate matching; unmatched frames are retained in denominator and reported.'))

def fire_replay(ev,rows,aligned,output):
    config=ALGORITHM/'build/sim/diagnosis-fire.yaml';base=yaml.safe_load((ev.folder/'configuration/algorithm.yaml').read_text());base['algorithm_profile']='tracking_fire_v3';base['auto_fire']=True;config.write_text(yaml.safe_dump(base))
    inputs=[];previous=None
    for r in rows:
        stamp=r['stamp_ns'];obs=ev.by_stamp[stamp];f=obs['frame'];p=f.get('latest_muzzle_pose');live=obs['result'];check=live.get('fire_check') or {}
        yaw=elevation=0;age=1.;rates=None
        if p:
            yaw=p['yaw'];elevation=p['elevation'];age=(obs['receipt_ns']-p['stamp_ns'])*1e-9
            if previous:
                dt=(p['stamp_ns']-previous['stamp_ns'])*1e-9
                if 0<dt<=.1:
                    yr=wrap(p['yaw']-previous['yaw'])/dt;pr=(p['elevation']-previous['elevation'])/dt
                    if abs(yr)<=8 and abs(pr)<=8:rates=(yr,pr)
            previous=p
        if aligned:
            extrapolate=age+.002
            if rates is not None and 0<=extrapolate<=.020:yaw=wrap(yaw+rates[0]*extrapolate);elevation+=rates[1]*extrapolate
            else:age=1.
        iterations=r.get('diagnosis',{}).get('aimer',{}).get('iterations',[]);half=-1.
        if iterations:
            a=iterations[-1]['point'];muzzle=sub(f['muzzle_position_map'],f['gimbal_position_map']);xyz=sub(a[:3],muzzle);facing=max(0,math.cos(wrap(a[3]-math.atan2(xyz[1],xyz[0]))));half=math.atan2(.135*.5*facing,math.hypot(*xyz[:2]))
        inputs.append(dict(seq=r['seq'],stamp_ns=stamp,valid=r['valid'],yaw=r['yaw'],pitch=r['pitch'],pose_yaw=yaw,pose_elevation=elevation,pose_age_s=age,
            target_half_width_rad=half,tracking=r['tracker']=='tracking',measurement_fresh=r.get('continuity',{}).get('trusted_update_age_s',1)<=.05,
            generation=r.get('track_generation',0),plate_id=r.get('selected_plate_id',-1),reset=bool(r.get('continuity',{}).get('reset_reason'))))
    command=['docker','exec','-i','--user','1000:1000','--workdir','/workspace/sp_vision_25','sp_vision_sunshine','build/diagnosis_v4_fire/diagnostic_fire_replay','build/sim/diagnosis-fire.yaml']
    result=subprocess.run(command,input=''.join(json.dumps(r)+'\n' for r in inputs),text=True,capture_output=True,check=True)
    values=[json.loads(l) for l in result.stdout.splitlines() if l.startswith('{')];write_rows(output,values);return values

def timing_analysis(rows):
    runs=[];all_frames=[];factors=[];control=[]
    for row in rows:
        if row['stage']!='mt' or row['role'] not in ('formal','supplement') or row['scene'] not in ('hf_uniform_3','hf_reverse_3','hf_turn90_3'):continue
        ev=Evidence(row);plan=load(ev.folder/'configuration/scenario-plan.json');changes=plan['motion_change_times_s']
        for delay in ('original','measured'):
            rr=ev.replay('delay-'+delay);by_stamp={r['stamp_ns']:r for r in rr};rates={};prior=None
            for r in rr:
                if prior:
                    dt=(r['stamp_ns']-prior['stamp_ns'])*1e-9;dy=wrap(r['yaw']-prior['yaw']);dp=r['pitch']-prior['pitch']
                    if r['valid'] and prior['valid'] and r.get('track_generation')==prior.get('track_generation') and 0<dt<=.1 and abs(dy)<=.06 and abs(dp)<=.06:
                        rates[r['stamp_ns']]=(max(-.6,min(.6,dy/dt)),max(-.6,min(.6,dp/dt)))
                prior=r
                stamp=r['stamp_ns'];t=ev.time(stamp)
                if not ev.measured(stamp):continue
                match=physical_prediction(ev,r);x=r.get('filter_state');truth=ev.robot_history.at(stamp)
                phase='steady' if not changes or t<changes[0] else next((f'{a:g}..{b:g}' for a,b in ((0,.5),(.5,1),(1,2)) if a<=t-changes[0]<b),'late')
                all_frames.append(dict(run_id=row['run_id'],source_profile=row['profile'],scene=row['scene'],physics=row['physics'],seed=row['seed'],delay=delay,
                    time_s=t,stamp_ns=stamp,phase=phase,valid=r['valid'],matched=match is not None,
                    video_index=ev.index.get(stamp,{}).get('video_index'),
                    velocity_error_mps=math.hypot(x[1]-truth[3],x[3]-truth[4]) if x is not None and truth is not None else None,
                    **(match or {})))
            for pose in (False,True):
                path=OUTPUT/'timing/fire-replay'/row['run_id']/(delay+'-pose'+str(int(pose))+'.jsonl')
                fire=fire_replay(ev,rr,pose,path);measured=[r for r in fire if ev.measured(r['stamp_ns']) and ev.time(r['stamp_ns'])>=2]
                for continuous in (False,True):
                    residual=[];pitch_residual=[]
                    for trace in ev.traces:
                        stamp=trace.get('command_stamp_ns');r=by_stamp.get(stamp);obs=ev.by_stamp.get(stamp)
                        if r is None or obs is None or not r['valid'] or not ev.measured(trace['stamp_ns']):continue
                        actual=ev.pose_history.at(trace['stamp_ns'])
                        if actual is None:continue
                        dt=(trace['stamp_ns']-(obs['receipt_ns']+2_000_000))*1e-9
                        if not -.05<=dt<=.1:continue
                        rate=rates.get(stamp,(0,0));target_yaw=r['yaw']+(rate[0]*dt if continuous else 0);target_elev=-r['pitch']-(rate[1]*dt if continuous else 0)
                        residual.append(abs(wrap(target_yaw-actual[0])));pitch_residual.append(abs(target_elev-actual[1]))
                    factors.append(dict(run_id=row['run_id'],source_profile=row['profile'],scene=row['scene'],physics=row['physics'],seed=row['seed'],delay=delay,
                        pose_alignment=pose,continuous_control=continuous,frames=len(measured),eligible_frames=sum(r['eligible'] for r in measured),
                        fixed_trajectory_yaw_error=summary(residual),fixed_trajectory_pitch_error=summary(pitch_residual),
                        limitation='Recorded own trajectory is fixed; continuous control changes the reference only. Gate eligibility is identical across continuous-control factor by construction; no new launches/hits are inferred.'))
            frames=[r for r in all_frames if r['run_id']==row['run_id'] and r['delay']==delay]
            runs.append(dict(run_id=row['run_id'],source_profile=row['profile'],scene=row['scene'],physics=row['physics'],seed=row['seed'],delay=delay,
                valid_frames=sum(r['valid'] for r in frames),matched_frames=sum(r['matched'] for r in frames),
                phases={phase:dict(frames=len([r for r in frames if r['phase']==phase]),
                    prediction_error=summary([abs(r['yaw_error_rad']) for r in frames if r['phase']==phase and r.get('yaw_error_rad') is not None]),
                    velocity_error=summary([r.get('velocity_error_mps') for r in frames if r['phase']==phase])) for phase in ('steady','0..0.5','0.5..1','1..2')}))
        # Observed physical shots, not replay opportunities.
        for shot in ev.shots:
            obs,latency=ev.launch_observation(shot);t=shot['fire_time_s'];phase='steady' if not changes or t<changes[0] else next((f'{a:g}..{b:g}' for a,b in ((0,.5),(.5,1),(1,2)) if a<=t-changes[0]<b),'late')
            record=dict(run_id=row['run_id'],source_profile=row['profile'],scene=row['scene'],physics=row['physics'],seed=row['seed'],shot_seq=shot['shot_seq'],time_s=t,phase=phase,
                useful_hit=shot['useful_hit'],weight=shot.get('weighted_hit_score'),paired=obs is not None,latency_s=latency)
            if obs:
                r=obs['result'];v=shot['initial_velocity'];truth=ev.robot_history.at(obs['frame']['stamp_ns']);x=r.get('filter_state')
                record.update(yaw_execution_error_rad=wrap(math.atan2(v[1],v[0])-r['yaw']),pitch_execution_error_rad=math.atan2(v[2],math.hypot(*v[:2]))+r['pitch'],
                    velocity_error_mps=math.hypot(x[1]-truth[3],x[3]-truth[4]) if x and truth else None,
                    video_index=ev.index.get(obs['frame']['stamp_ns'],{}).get('video_index'))
            control.append(record)
        print(json.dumps(dict(timing_analyzed=len(runs)//2,run_id=row['run_id'])),flush=True)
    write_rows(OUTPUT/'timing/frames.jsonl',all_frames);write_rows(OUTPUT/'timing/factorial.jsonl',factors);write_rows(OUTPUT/'timing/shots.jsonl',control)
    write(OUTPUT/'timing/summary.json',dict(runs=runs,factorial_variants=8,
        interpretation='Within-run fixed-observation analysis separates delay and pose gate effects. Controller feedback effects cannot be causally identified from frozen own-pose recordings.',
        original_receipt_age_proxy='Native replay evaluation uses bridge receipt time, not the unrecorded original pre-send Aimer wall timestamp; this limitation is common to paired interventions.'))

def main():
    inventory=load(OUTPUT/'inventory.json')['runs']
    supplement=load(OUTPUT/'supplement/batch.json',{})
    if supplement.get('complete'):
        inventory += [r['inventory'] for r in supplement['runs']]
    static_analysis(inventory);static_prior_analysis(inventory);rotation_analysis(inventory);confidence_analysis(inventory);timing_analysis(inventory);timing_closed_loop_summary()
    write(OUTPUT/'analysis-complete.json',dict(complete=True,truth_used_only_in_external_evaluation=True))

if __name__=='__main__':main()
