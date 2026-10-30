#!/usr/bin/env python3
"""Read-only attribution of the recorded UI/headless trials; truth stays offline."""
import argparse
from collections import Counter, defaultdict
import csv
import itertools
import json
import math
from pathlib import Path
import statistics

from diagnosis_v4_analysis import Evidence, add, sub, dot, norm, pitch, velocity
from impact_quality import quantile
import manage
from report import load, write


def stats(values):
    values=[v for v in values if v is not None and math.isfinite(v)]
    return dict(n=len(values),mean=statistics.mean(values) if values else None,
        p50=quantile(values,.5),p95=quantile(values,.95))


def crossing(origin,initial,center,normal,gravity):
    a=-.5*gravity*normal[2];b=dot(initial,normal);c=dot(sub(origin,center),normal)
    roots=[]
    if abs(a)<1e-12:
        if abs(b)>1e-12:roots=[-c/b]
    elif b*b-4*a*c>=0:
        d=math.sqrt(b*b-4*a*c);roots=[(-b-d)/(2*a),(-b+d)/(2*a)]
    roots=[t for t in roots if 0<t<1]
    if not roots:return None
    t=min(roots)
    return [origin[i]+initial[i]*t-(.5*gravity*t*t if i==2 else 0) for i in range(3)]


def rows(path):
    return [json.loads(line) for line in path.open()]


def run(study,output):
    state=load(study/'alignment.json');output.mkdir(parents=True,exist_ok=True)
    records=[];frames=[];shots=[]
    for mode,entry in state['modes'].items():
        for run_id in entry['runs']:
            manifest=load(manage.OPS/'experiments'/run_id/'manifest.json')
            ev=Evidence(dict(run_id=run_id))
            measured=[d for d in ev.observations if ev.measured(d['frame']['stamp_ns'])]
            traces=sorted((d for d in ev.traces if ev.measured(d['stamp_ns'])),key=lambda d:d['stamp_ns'])
            continuous=[];resets=0
            for a,b in zip(traces,traces[1:]):
                dt=(b['stamp_ns']-a['stamp_ns'])*1e-9;steps=b['control_steps']-a['control_steps']
                if steps<0:resets+=1
                if 0<dt<=.1 and 0<steps<=200:continuous.append((steps,dt))
            steps=sum(a for a,b in continuous);elapsed=sum(b for a,b in continuous)
            raw_frames=rows(ev.folder/'frames.jsonl')
            mr=[r for r in raw_frames if r['measurement']]
            rois=[]
            for d in measured:
                roi=(d['result'].get('detector_summary') or {}).get('tile_roi')
                rois.append(json.dumps(roi))
            record=dict(mode=mode,run_id=run_id,scene=manifest['scene'],seed=manifest['random_seed'],
                frames=len(measured),valid_fraction=sum(bool(d['result'].get('valid')) for d in measured)/len(measured),
                fire_advice_frames=sum(bool(d['result'].get('fire_advice')) for d in measured),
                roi_switches=sum(a!=b for a,b in zip(rois,rois[1:])),control_counter_resets=resets,
                corrected_active_control_hz=steps/elapsed if elapsed else None,
                negative_aggregate_hz=entry['rows'][entry['runs'].index(run_id)]['control_hz']<0,
                roi_counts=dict(Counter(rois)),
                velocity_error_mps=stats([r.get('velocity_error_mps') for r in mr]),
                plate_position_error_m=stats([r.get('plate_position_error_m') for r in mr]),
                capture_to_result_age_ms=stats([r.get('age_ms') for r in mr]),
                aim_generation_counts=dict(Counter(d['result'].get('continuity',{}).get('generation') for d in measured)))
            if manifest['scene']=='hf_static_8':
                run_frames=[]
                for d in measured:
                    f=d['frame'];r=d['result'];target=ev.front(f['stamp_ns']);aim=r.get('aim_point')
                    if not target or not aim:continue
                    center=target[1][:3];origin=f['gimbal_position_map'];world=add(aim,origin);muzzle=f['muzzle_position_map']
                    estimate=r.get('filter_state') or []
                    poses=[add(o['position'],origin) for o in r.get('observations',[]) if o.get('pose_valid') and o.get('position')]
                    pose=min(poses,key=lambda v:norm(sub(v,center))) if poses else None
                    frame=dict(mode=mode,run_id=run_id,seed=manifest['random_seed'],time_s=ev.time(f['stamp_ns']),
                        aim_range_error_m=norm(sub(world,muzzle))-norm(sub(center,muzzle)),
                        aim_height_error_m=world[2]-center[2],
                        pose_range_error_m=norm(sub(pose,muzzle))-norm(sub(center,muzzle)) if pose else None,
                        pose_height_error_m=pose[2]-center[2] if pose else None,
                        filter_phase_deg=math.degrees(math.remainder(estimate[6],math.tau)) if len(estimate)>6 else None,
                        roi=json.dumps((r.get('detector_summary') or {}).get('tile_roi')),
                        pitch=r['pitch'],video_index=ev.index.get(f['stamp_ns'],{}).get('video_index'))
                    run_frames.append(frame)
                frames+=run_frames
                run_shots=[]
                for shot in ev.shots:
                    observation,latency=ev.launch_observation(shot);target=ev.front(shot['fire_stamp_ns'])
                    out=dict(mode=mode,run_id=run_id,seed=manifest['random_seed'],time_s=shot['fire_time_s'],
                        shot_seq=shot['shot_seq'],weight=shot.get('impact_center_proximity'),impact_y_m=shot.get('impact_y_m'),
                        impact_x_m=shot.get('impact_x_m'),paired=observation is not None,launch_latency_s=latency)
                    if observation is not None and target is not None:
                        f=observation['frame'];r=observation['result'];center=target[1][:3];normal=target[1][3:6]
                        length=math.hypot(*normal[:2]);up=[-normal[2]*normal[0]/length,-normal[2]*normal[1]/length,length]
                        origin=shot['muzzle_position'];initial=shot['initial_velocity'];speed=norm(initial)
                        command=velocity(speed,r['yaw'],-r['pitch'])
                        pc=crossing(origin,command,center,normal,9.7833)
                        pa=crossing(origin,initial,center,normal,9.7833)
                        pp=crossing(origin,initial,center,normal,9.81)
                        if pc and pa and pp:
                            yc=dot(sub(pc,center),up);ya=dot(sub(pa,center),up);yp=dot(sub(pp,center),up)
                            out.update(command_geometry_error_m=yc,execution_error_m=ya-yc,physics_error_m=yp-ya,
                                predicted_impact_y_m=yp,contact_residual_m=shot['impact_y_m']-yp if shot.get('impact_y_m') is not None else None)
                        aim=r.get('aim_point')
                        if aim:
                            world=add(aim,f['gimbal_position_map'])
                            out.update(aim_range_error_m=norm(sub(world,origin))-norm(sub(center,origin)),
                                aim_height_error_m=world[2]-center[2])
                        x=r.get('filter_state') or []
                        if len(x)>6:out['filter_phase_deg']=math.degrees(math.remainder(x[6],math.tau))
                        out['video_index']=ev.index.get(f['stamp_ns'],{}).get('video_index')
                        ideal=pitch(speed,math.hypot(center[0]-origin[0],center[1]-origin[1]),center[2]-origin[2])
                        if ideal is not None:out['command_elevation_error_deg']=math.degrees(-r['pitch']-ideal)
                    run_shots.append(out)
                shots+=run_shots
                record['static_frames']={k:stats([r.get(k) for r in run_frames]) for k in
                    ('aim_range_error_m','aim_height_error_m','pose_range_error_m','pose_height_error_m','filter_phase_deg')}
                record['static_shots']={k:stats([r.get(k) for r in run_shots]) for k in
                    ('weight','impact_y_m','command_geometry_error_m','execution_error_m','physics_error_m',
                        'contact_residual_m','aim_range_error_m','aim_height_error_m','filter_phase_deg')}
                record['paired_shots']=sum(r['paired'] for r in run_shots)
            records.append(record)
            print(json.dumps(dict(progress=len(records),mode=mode,scene=manifest['scene'])),flush=True)
    write(output/'diagnosis.json',dict(runs=records,
        limitations=['Truth is used only in offline matching and attribution.',
            'Launch-to-command association uses the last forwarded pulse within 100 ms; unmatched shots are excluded from decomposition and counted.',
            'Static ballistic calculation uses the actual launch state and plate plane; all source trials are no-drag.',
            'Active control frequency excludes counter-reset intervals; it is not an unconditional simulator FPS estimate.']))
    for name,data in [('static-frames',frames),('static-shots',shots)]:
        (output/(name+'.jsonl')).write_text(''.join(json.dumps(r,allow_nan=False)+'\n' for r in data))
        columns=list(dict.fromkeys(k for row in data for k in row))
        with (output/(name+'.csv')).open('w') as stream:
            writer=csv.DictWriter(stream,fieldnames=columns);writer.writeheader();writer.writerows(data)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--study',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();run(args.study.resolve(),args.output.resolve())
