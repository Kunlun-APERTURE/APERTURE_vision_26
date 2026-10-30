#!/usr/bin/env python3
"""Read-only horizontal prediction/timing/execution attribution at actual launches."""
import argparse
import json
import math
from pathlib import Path

from background_alignment_diagnosis import stats
from diagnosis_v4_analysis import Evidence, add, sub
import manage
from report import load, write


def angle(point,origin):
    return math.atan2(point[1]-origin[1],point[0]-origin[0])


def degrees(a):return math.degrees(math.remainder(a,math.tau))


def intercept(ev,identity,shot):
    origin=shot['muzzle_position'];v=shot['initial_velocity'];t=.3
    for _ in range(12):
        target=ev.armor_history[identity].at(shot['fire_stamp_ns']+int(t*1e9))
        if target is None:return None,None
        n=target[3:6];a=-.5*9.81*n[2];b=sum(x*y for x,y in zip(v,n));c=sum(x*y for x,y in zip(sub(origin,target[:3]),n))
        if abs(a)<1e-12:roots=[-c/b] if abs(b)>1e-12 else []
        else:
            d=b*b-4*a*c
            roots=[(-b-math.sqrt(d))/(2*a),(-b+math.sqrt(d))/(2*a)] if d>=0 else []
        roots=[r for r in roots if 0<r<1]
        if not roots:return None,None
        new=min(roots)
        if abs(new-t)<1e-7:return new,ev.armor_history[identity].at(shot['fire_stamp_ns']+int(new*1e9))
        t=new
    return None,None


def pixel(point,frame):
    camera=frame['camera_in_map'];x,y,z,w=camera['quaternion']
    columns=((1-2*(y*y+z*z),2*(x*y+z*w),2*(x*z-y*w)),
        (2*(x*y-z*w),1-2*(x*x+z*z),2*(y*z+x*w)),
        (2*(x*z+y*w),2*(y*z-x*w),1-2*(x*x+y*y)))
    delta=sub(point,camera['position']);optical=[sum(a*b for a,b in zip(delta,c)) for c in columns]
    if optical[2]<=.05:return None
    k=frame['calibration']['k'];return (k[0]*optical[0]/optical[2]+k[2],k[4]*optical[1]/optical[2]+k[5])


def physical_plate(ev,frame,result,index):
    selected=result.get('selected_plate_id',-1)
    indexed=index.get(frame['stamp_ns'],{})
    if indexed.get('selected_gt_armor_id') is not None:return indexed['selected_gt_armor_id'],'recorded_unambiguous_3d_match'
    plates=(result.get('track_estimate') or {}).get('plates',[])
    if not 0<=selected<len(plates):return None,None
    p=pixel(add(plates[selected],frame['gimbal_position_map']),frame)
    if p is None:return None,None
    candidates=[]
    for identity,value in ev.plates(frame['stamp_ns']).items():
        normal=value[3:6];toward=sub(frame['camera_in_map']['position'],value[:3])
        if sum(a*b for a,b in zip(normal,toward))<=0:continue
        q=pixel(value[:3],frame)
        if q is not None:candidates.append((math.dist(p,q),identity))
    candidates.sort()
    if candidates and candidates[0][0]<18 and (len(candidates)==1 or candidates[1][0]-candidates[0][0]>3):
        return candidates[0][1],'projected_current_plate_match'
    return None,None


def run(study,output):
    state=load(study/'alignment.json');records=[];groups=[]
    for mode,entry in state['modes'].items():
        for run_id in entry['runs']:
            manifest=load(manage.OPS/'experiments'/run_id/'manifest.json');scene=manifest['scene']
            if scene not in ('hf_uniform_3','hf_turn90_3'):continue
            ev=Evidence(dict(run_id=run_id));rr=[]
            index={r['stamp_ns']:r for r in [json.loads(line) for line in (ev.folder/'frames.jsonl').open()]}
            for shot in ev.shots:
                observation,latency=ev.launch_observation(shot)
                row=dict(mode=mode,scene=scene,run_id=run_id,seed=manifest['random_seed'],time_s=shot['fire_time_s'],
                    outcome=shot['outcome'],hit=shot['outcome']=='target_armor',front_hit=shot['useful_hit'],weight=shot.get('weighted_hit_score'),paired=False)
                if observation:
                    f=observation['frame'];r=observation['result'];aim=r.get('aim_point');horizon=r.get('prediction_horizon_s')
                    selected=r.get('selected_plate_id');vv=r.get('plate_velocities') or []
                    identity,method=physical_plate(ev,f,r,index)
                    if aim and horizon and identity in ev.armor_history:
                        world=add(aim,f['gimbal_position_map']);origin=shot['muzzle_position']
                        flight,actual=intercept(ev,identity,shot)
                        predicted_time=f['stamp_ns']+int(horizon*1e9)
                        actual_time=shot['fire_stamp_ns']+int(flight*1e9) if flight is not None else None
                        desired=ev.armor_history[identity].at(predicted_time)
                        robot=ev.robot_history.at(f['stamp_ns'])
                        if desired and actual:
                            velocity=shot['initial_velocity'];actual_yaw=math.atan2(velocity[1],velocity[0])
                            prediction=degrees(r['yaw']-angle(desired[:3],origin))
                            timing=degrees(angle(desired[:3],origin)-angle(actual[:3],origin))
                            execution=degrees(actual_yaw-r['yaw'])
                            tolerance=math.degrees(math.atan2(actual[7]*.5,math.hypot(actual[0]-origin[0],actual[1]-origin[1])))
                            row.update(paired=True,gt_armor_id=identity,plate_match_method=method,
                                prediction_yaw_error_deg=prediction,timing_yaw_error_deg=timing,
                                execution_yaw_error_deg=execution,total_yaw_error_deg=degrees(actual_yaw-angle(actual[:3],origin)),
                                intended_vs_launch_impact_time_ms=(predicted_time-actual_time)*1e-6,
                                prediction_point_error_m=math.dist(world,desired[:3]),
                                velocity_error_mps=math.hypot(vv[selected][0]-robot[3],vv[selected][1]-robot[4]) if len(vv)>selected and robot else None,
                                approx_plate_half_angle_deg=tolerance,flight_time_s=flight,
                                recorded_flight_time_s=shot.get('flight_time_s'),
                                dominant_yaw_component=max(('prediction','timing','execution'),key=lambda k:abs(dict(prediction=prediction,timing=timing,execution=execution)[k])),
                                fire_command_aligned=(r.get('fire_check') or {}).get('aligned'),
                                video_index=ev.index.get(f['stamp_ns'],{}).get('video_index'))
                rr.append(row)
            records+=rr
            groups.append(dict(mode=mode,scene=scene,run_id=run_id,shots=len(rr),paired=sum(r['paired'] for r in rr)))
            print(json.dumps(dict(progress=len(groups),mode=mode,scene=scene)),flush=True)
    summaries=[]
    for mode in state['modes']:
        for scene in ('hf_uniform_3','hf_turn90_3'):
            for outcome in ('all','hit','miss'):
                rr=[r for r in records if r['mode']==mode and r['scene']==scene and r['paired'] and
                    (outcome=='all' or r['hit']==(outcome=='hit'))]
                summaries.append(dict(mode=mode,scene=scene,outcome=outcome,shots=len(rr),
                    dominant={k:sum(r['dominant_yaw_component']==k for r in rr) for k in ('prediction','timing','execution')},
                    **{k:stats([abs(r[k]) if r.get(k) is not None else None for r in rr]) for k in
                        ('prediction_yaw_error_deg','timing_yaw_error_deg','execution_yaw_error_deg',
                            'total_yaw_error_deg','velocity_error_mps','intended_vs_launch_impact_time_ms','approx_plate_half_angle_deg')}))
    (output/'motion-shots.jsonl').write_text(''.join(json.dumps(r,allow_nan=False)+'\n' for r in records))
    write(output/'motion-summary.json',dict(runs=groups,groups=summaries,
        decomposition='Yaw error relative to actual-time GT plate center = prediction error at intended intercept + intended/actual intercept timing mismatch + launch/command execution error.',
        limits=['Only nonrotating translation is analyzed. Physical plate matching uses recorded unambiguous 3D association or current projected plate centers; ambiguous samples are excluded and coverage is reported.',
            'Intercept time solves the actual launch ballistic path against the moving GT armor plane. This is an independent vacuum/interpolated approximation, not the simulator collision solver.',
            'The intercept-time mismatch mixes launch timing with flight-time/depth prediction error; it must not be interpreted as network latency.',
            'Plate half-angle is an approximate scale, not the exact swept-hit rule. A miss can also be vertical or a wrong interception depth.']))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--study',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();run(args.study.resolve(),args.output.resolve())
