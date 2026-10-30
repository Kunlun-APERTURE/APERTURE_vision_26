"""Post-inference geometry attribution. Truth never enters an algorithm replay."""
import argparse
import bisect
from collections import Counter,defaultdict
import itertools
import json
import math
from pathlib import Path
import statistics

import cv2
import numpy as np

import manage
from decision_benchmark import sha
from perception_study import quantile
from report import load,write
from sync_first_divergence_analysis import primary
from sync_timing_alignment_media import rotation


def timeline(source):
    values=[]
    for line in (source/'telemetry.jsonl').open():
        p=json.loads(line)
        if p['kind']=='armors':values.append(p['data'])
    stamps=[p['stamp_ns'] for p in values]
    def at(stamp):
        i=bisect.bisect_left(stamps,stamp)
        if i<len(stamps) and stamps[i]==stamp:return [x for x in values[i]['armors'] if x['robot_id']==1]
        if i==0 or i==len(stamps) or stamps[i]-stamps[i-1]>100_000_000:return []
        a,b=values[i-1:i+1];fraction=(stamp-stamps[i-1])/(stamps[i]-stamps[i-1]);out=[]
        for plate in a['armors']:
            if plate['robot_id']!=1:continue
            end=next(x for x in b['armors'] if x['id']==plate['id'])
            out.append(dict(plate,**{k:[u+(v-u)*fraction for u,v in zip(plate[k],end[k])] for k in ('position','normal')}))
        return out
    return at


def own_project(frame,points):
    camera=frame['camera_in_map'];r=rotation(camera['quaternion'],np)
    optical=(np.asarray(points)-np.asarray(camera['position']))@r
    if np.any(optical[:,2]<=.05):return None
    p=optical@np.asarray(frame['calibration']['k']).reshape(3,3).T
    return p[:,:2]/p[:,2,None]


def match_truth(frame,observations,plates):
    centers=[]
    for plate in plates:
        normal=np.asarray(plate['normal']);pos=np.asarray(plate['position']);camera=np.asarray(frame['camera_in_map']['position'])
        if np.dot(normal,camera-pos)<=0:continue
        uv=own_project(frame,[pos])
        if uv is not None:centers.append((plate,uv[0]))
    pairs=[]
    for i,a in enumerate(observations):
        if not a.get('position') or len(a.get('corners',[]))!=4:continue
        center=np.mean(a['corners'],axis=0)
        for j,(plate,pixel) in enumerate(centers):
            distance=float(np.linalg.norm(center-pixel))
            if distance<=12:pairs.append((distance,i,j))
    used_i=set();used_j=set();matched={}
    for dist,i,j in sorted(pairs):
        if i in used_i or j in used_j:continue
        used_i.add(i);used_j.add(j)
        p=centers[j][0];actual=np.asarray(observations[i]['position'])+frame['gimbal_position_map']
        matched[i]=dict(plate=p,match_distance_px=dist,position_error_m=float(np.linalg.norm(actual-p['position'])))
    return matched


def solver_project(frame,position,yaw):
    """Mirror frozen Solver::reproject_armor using lightbar endpoints, not board border."""
    pitch=math.pi/12;c,s=math.cos(yaw),math.sin(yaw);cp,sp=math.cos(pitch),math.sin(pitch)
    ori=np.asarray([[c*cp,-s,c*sp],[s*cp,c,s*sp],[-sp,0,cp]])
    rg=rotation(frame['q_gimbal2world'],np);rc=np.asarray(frame['calibration']['r_camera2gimbal']).reshape(3,3)
    t=np.asarray(frame['calibration']['t_camera2gimbal'])
    object_points=np.asarray([[0,.135/2,.056/2],[0,-.135/2,.056/2],[0,-.135/2,-.056/2],[0,.135/2,-.056/2]])
    r=rc.T@rg.T@ori;translation=rc.T@(rg.T@np.asarray(position)-t)
    rv,_=cv2.Rodrigues(r)
    projected,_=cv2.projectPoints(object_points,rv,translation,np.asarray(frame['calibration']['k'],dtype=float).reshape(3,3),np.asarray(frame['calibration']['d'],dtype=float))
    return projected.reshape(4,2).astype(np.float32)


def poses_for(result):
    observed=result.get('observations',[]);poses=result.get('continuity',{}).get('diagnosis_poses',[])
    out=[]
    # The tracker may sort observations after solve; associate by actual image center.
    for i,a in enumerate(observed):
        if not a.get('pose_valid') or a['confidence']<=.8 or a['type']!='small' or a.get('reprojection_rms_px',999)>8:continue
        mean=np.mean(a['corners'],axis=0)
        candidates=[p for p in poses if p.get('pose_valid') and 'xyz' in p and 'yaw' in p]
        if not candidates:continue
        pose=min(candidates,key=lambda p:np.linalg.norm(np.asarray(p['image_center'])-mean))
        if np.linalg.norm(np.asarray(pose['image_center'])-mean)>1e-3:raise ValueError('Unmatched recorded solver pose')
        out.append(dict(a,xyz=pose['xyz'],yaw=pose['yaw'],original_index=i))
    out.sort(key=lambda a:(-a['confidence'],float(np.mean(a['corners'],axis=0)[0])))
    return out[:8]


def initial_geometry(frame,observed,*,heights=(0.,),radii=(.19,.20,.21),axis='both',truth=None,truth_mode='both'):
    """Diagnostic only: relax one assumption at a time; never feeds the tracker."""
    if len(observed)<2:return dict(support=0,best_cross=None)
    best=None
    for anchor,a in enumerate(observed):
        if truth is not None:
            gt=truth.get(a['original_index'])
            if gt is None:continue
            p=gt['plate']
            position=np.asarray(p['position'])-frame['gimbal_position_map'] if truth_mode in ('both','position') else np.asarray(a['xyz'])
            phase=math.atan2(-p['normal'][1],-p['normal'][0]) if truth_mode in ('both','yaw') else a['yaw']
        else:position=np.asarray(a['xyz']);phase=a['yaw']
        for radius in radii:
            center=position+np.array([radius*math.cos(phase),radius*math.sin(phase),0])
            projected=[];all_points=[]
            for slot in range(4):
                yaw=phase+slot*math.pi/2;xyz=center-np.array([radius*math.cos(yaw),radius*math.sin(yaw),0])
                p=solver_project(frame,xyz,yaw);projected.append((xyz,yaw,p));all_points.extend(p)
            all_points=np.asarray(all_points,dtype=np.float32)
            if not np.isfinite(all_points).all() or np.abs(all_points).max()>1e6:continue
            x,y,w,h=cv2.boundingRect(all_points);pad=max(4,int(a.get('box_width',np.ptp(np.asarray(a['corners'])[:,0])+1)))
            costs=np.full((4,len(observed)),math.inf);details={};local=0
            for j,b in enumerate(observed):
                if b['name']!=a['name'] or b['color']!=a['color']:continue
                target=np.asarray(b['corners']);mid=np.mean(target,axis=0)
                if not(x-pad<=int(mid[0])<x+w+pad and y-pad<=int(mid[1])<y+h+pad):continue
                local+=1
                scale=max(4.,(np.linalg.norm(target[0]-target[3])+np.linalg.norm(target[1]-target[2]))*.5)
                for slot,(xyz,yaw,p) in enumerate(projected):
                    options=[]
                    for dz in heights if slot!=0 else (0.,):
                        pp=p if dz==0 else solver_project(frame,xyz+np.array([0,0,dz]),yaw)
                        delta=pp-target;rx=float(np.sqrt(np.mean(delta[:,0]**2))/scale);ry=float(np.sqrt(np.mean(delta[:,1]**2))/scale)
                        cost=math.hypot(rx,ry) if axis=='both' else rx
                        options.append((cost,rx,ry,dz))
                    chosen=min(options);costs[slot,j]=chosen[0];details[slot,j]=chosen
            if local<2 or costs[0,anchor]>.35:continue
            support=1;residual=costs[0,anchor]
            # Maximum one-to-one assignment for at most three other plates.
            optimal=(1,float(costs[0,anchor]))
            def assign(slot,used,count,total):
                nonlocal optimal
                if slot==4:
                    value=(count,total/count)
                    if value[0]>optimal[0] or (value[0]==optimal[0] and value[1]<optimal[1]):optimal=value
                    return
                assign(slot+1,used,count,total)
                for j in range(len(observed)):
                    if j not in used and costs[slot,j]<=.35:assign(slot+1,used|{j},count+1,total+float(costs[slot,j]))
            assign(1,{anchor},1,float(costs[0,anchor]));support,residual=optimal
            cross=[(float(costs[k,j]),k,j) for k in (1,2,3) for j in range(len(observed)) if j!=anchor]
            nearest=min(cross)
            if not math.isfinite(nearest[0]):continue
            _,slot,j=nearest;cost,rx,ry,dz=details[slot,j]
            record=dict(support=support,mean_residual=float(residual),best_cross=cost,
                        x_component=rx,y_component=ry,height_adjustment_m=dz,anchor=anchor,
                        other=j,slot=slot,radius=radius,local=local)
            if best is None or (-support,cost)<(-best['support'],best['best_cross']):best=record
    return best or dict(support=0,best_cross=None)


def phase(t,scene):
    if t<0:return 'warmup'
    if t<1:return 'stationary'
    if scene=='hf_turn90_3' and t>=3.2:return 'after_turn'
    return 'moving'


def stats(values):
    v=[x for x in values if x is not None]
    return dict(n=len(v),p50=quantile(v,.5),p95=quantile(v,.95),mean=statistics.mean(v) if v else None)


def analyze(root):
    c=load(root/'contract.json');state=load(root/'state.json')
    if state['state']!='completed':raise ValueError('Complete all frozen replays before analysis')
    out=root/'analysis';out.mkdir(exist_ok=True)
    traces=[];metrics=[];fallbacks=[];initial=[];divergences=[]
    for source_entry in c['sources']:
        source=manage.OPS/'experiments'/source_entry['run_id'];run=load(source/'run-state.json')
        truth=timeline(source);records={}
        for variant in c['variants']:
            entry=next(r for r in state['replays'] if r['source_run']==source.name and r['variant']==variant)
            records[variant]=primary(Path(entry['output']))
        base=records['baseline']
        for variant,rr in records.items():
            init=next((p for p in rr if p['result'].get('filter_state') is not None),None)
            first=rr[0]['frame']['stamp_ns'];last=rr[-1]['frame']['stamp_ns']
            for label in ('warmup','stationary','moving','after_turn'):
                selected=[];count=0;tracked=0;fire=0
                for p in rr:
                    f=p['frame'];r=p['result'];t=(f['stamp_ns']-run['measurement_start_stamp_ns'])*1e-9
                    if phase(t,source_entry['scene'])!=label:continue
                    count+=1;plates=truth(f['stamp_ns']);x=r.get('filter_state');err=None
                    if x is not None and plates:
                        gt=np.mean([a['position'] for a in plates],axis=0)
                        err=float(np.linalg.norm((np.asarray([x[0],x[2],x[4]])+f['gimbal_position_map']-gt)[:2]));selected.append(err)
                        tracked+=r.get('tracker')=='tracking' and err<=.5
                    fire+=bool(r.get('fire_advice'))
                    traces.append(dict(source_run=source.name,source_engine=source_entry['engine'],source_variant=source_entry['variant'],
                        scene=source_entry['scene'],sensor_seed=source_entry['sensor_seed'],variant=variant,stamp_ns=f['stamp_ns'],
                        time_s=t,phase=label,center_error_m=err,tracker=r.get('tracker'),fire_advice=r.get('fire_advice'),
                        selected_plate=r.get('selected_plate_id'),initial_state=(r.get('continuity',{}).get('initial_consistency') or {}).get('status')))
                if count:
                    metrics.append(dict(source_run=source.name,source_engine=source_entry['engine'],source_variant=source_entry['variant'],
                        scene=source_entry['scene'],sensor_seed=source_entry['sensor_seed'],variant=variant,phase=label,frames=count,
                        center_error=stats(selected),valid_fraction=len(selected)/count,correct_tracking_fraction=tracked/count,
                        advice_frames=fire,initial_latency_s=(init['frame']['stamp_ns']-first)*1e-9 if init else (last-first)*1e-9,
                        initialization_censored=init is None))
        for variant in ('A35','B35'):
            rr=records[variant];onset={}
            for a,b in zip(base,rr):
                if a['frame']!=b['frame'] or a['jpeg_sha256']!=b['jpeg_sha256']:raise ValueError('Replay input pairing mismatch')
                t=(a['frame']['stamp_ns']-run['measurement_start_stamp_ns'])*1e-9
                def obs(result):return [{k:x.get(k) for k in ('corners','confidence','name','color','type','position')} for x in result.get('observations',[])]
                for field in ('observations','filter_state','tracker','fire_advice'):
                    av,bv=(obs(a['result']),obs(b['result'])) if field=='observations' else (a['result'].get(field),b['result'].get(field))
                    different=av!=bv
                    if field=='filter_state' and av is not None and bv is not None:different=max(abs(x-y) for x,y in zip(av,bv))>1e-6
                    if different and field not in onset:onset[field]=dict(time_s=t,seq=a['frame']['seq'])
            divergences.append(dict(source_run=source.name,source_engine=source_entry['engine'],source_variant=source_entry['variant'],
                scene=source_entry['scene'],sensor_seed=source_entry['sensor_seed'],variant=variant,onsets=onset))
        # Direct corner intervention only when the rejected candidate equals baseline's actual refined corners.
        for a,b in zip(base,records['A35']):
            f=a['frame'];t=(f['stamp_ns']-run['measurement_start_stamp_ns'])*1e-9;plates=truth(f['stamp_ns'])
            ao=a['result'].get('observations',[]);bo=b['result'].get('observations',[])
            ta,tb=match_truth(f,ao,plates),match_truth(f,bo,plates)
            for j,ob in enumerate(bo):
                guard=ob.get('refinement')
                if not guard or guard.get('accepted') or 'candidate' not in guard:continue
                target=np.asarray(guard['candidate'])
                matches=[(float(np.max(np.linalg.norm(np.asarray(x['corners'])-target,axis=1))),i) for i,x in enumerate(ao) if len(x.get('corners',[]))==4]
                if not matches:continue
                delta,i=min(matches)
                direct=delta<1e-3
                same_plate=i in ta and j in tb and ta[i]['plate']['id']==tb[j]['plate']['id']
                fallback=dict(source_run=source.name,source_engine=source_entry['engine'],source_variant=source_entry['variant'],
                    scene=source_entry['scene'],sensor_seed=source_entry['sensor_seed'],time_s=t,seq=f['seq'],
                    direct_same_candidate=direct,matching_corner_delta_px=delta,matched_same_truth_plate=same_plate,
                    reason=guard['reason'],geometry={k:guard.get(k) for k in ('endpoint_fraction','length_fraction','ratio_fraction')},
                    baseline_error_m=ta[i]['position_error_m'] if same_plate else None,
                    fallback_error_m=tb[j]['position_error_m'] if same_plate else None,
                    error_delta_m=tb[j]['position_error_m']-ta[i]['position_error_m'] if same_plate and direct else None,
                    baseline_observation=i,fallback_observation=j,
                    baseline_reprojection_px=ao[i].get('reprojection_rms_px'),fallback_reprojection_px=ob.get('reprojection_rms_px'))
                fallbacks.append(fallback)
        # B's actual initialization decision is checked against the mirrored numerical geometry.
        for packet in records['B35']:
            f=packet['frame'];r=packet['result'];d=r.get('continuity',{}).get('initial_consistency')
            if not d:continue
            observed=poses_for(r);original=initial_geometry(f,observed)
            expected=2 if d['status']=='consistent' else 1
            agrees=(original['support']>=2)==(expected==2)
            if not agrees:raise ValueError('Diagnostic projection failed to reproduce frozen initial gate: '+source.name)
            entry=dict(source_run=source.name,source_engine=source_entry['engine'],source_variant=source_entry['variant'],
                scene=source_entry['scene'],sensor_seed=source_entry['sensor_seed'],seq=f['seq'],
                time_s=(f['stamp_ns']-run['measurement_start_stamp_ns'])*1e-9,wait_s=d['wait_s'],
                status=d['status'],frozen_decision_reproduced=True,original=original,
                observations=len(observed))
            if d['status']=='pending_inconsistent':
                truth_matches=match_truth(f,r.get('observations',[]),truth(f['stamp_ns']))
                entry['vertical_free']=initial_geometry(f,observed,axis='horizontal')
                entry['height_free']=initial_geometry(f,observed,heights=tuple(np.arange(-.08,.081,.02)))
                entry['radius_free']=initial_geometry(f,observed,radii=(.15,.175,.20,.225,.25,.275,.30))
                entry['truth_anchor']=initial_geometry(f,observed,truth=truth_matches)
                entry['truth_position_only']=initial_geometry(f,observed,truth=truth_matches,truth_mode='position')
                entry['truth_yaw_only']=initial_geometry(f,observed,truth=truth_matches,truth_mode='yaw')
                centers=[np.asarray(o['xyz'])+np.array([.2*math.cos(o['yaw']),.2*math.sin(o['yaw']),0]) for o in observed]
                entry['implied_center_spread_m']=max((float(np.linalg.norm(a-b)) for a,b in itertools.combinations(centers,2)),default=None)
            initial.append(entry)
        print(json.dumps(dict(analyzed_source=source.name,source_count=len(divergences)//2,
                              fallback_events=len(fallbacks),initial_events=len(initial))),flush=True)
    for name,rows in [('traces',traces),('fallbacks',fallbacks),('initial',initial),('divergences',divergences)]:
        (out/(name+'.jsonl')).write_text(''.join(json.dumps(r,allow_nan=False)+'\n' for r in rows))
    groups=[]
    for engine,variant,scene,label in itertools.product(('ui','backend'),c['variants'],('hf_uniform_3','hf_turn90_3'),('warmup','stationary','moving','after_turn')):
        rr=[r for r in traces if (r['source_engine'],r['variant'],r['scene'],r['phase'])==(engine,variant,scene,label)]
        if not rr:continue
        vv=[r['center_error_m'] for r in rr if r['center_error_m'] is not None]
        groups.append(dict(source_engine=engine,variant=variant,scene=scene,phase=label,frames=len(rr),error=stats(vv),
                           valid_fraction=len(vv)/len(rr),within_half_m_fraction=sum(x<=.5 for x in vv)/len(rr)))
    fallback_summary=[]
    for engine,scene in itertools.product(('ui','backend'),('hf_uniform_3','hf_turn90_3')):
        rr=[x for x in fallbacks if x['source_engine']==engine and x['scene']==scene and x['direct_same_candidate'] and x['matched_same_truth_plate']]
        delta=[r['error_delta_m'] for r in rr]
        fallback_summary.append(dict(source_engine=engine,scene=scene,matched_direct_events=len(rr),delta=stats(delta),
            improved_over_5cm=sum(x<-.05 for x in delta),worsened_over_5cm=sum(x>.05 for x in delta),
            negligible_within_5cm=sum(abs(x)<=.05 for x in delta),reasons=dict(Counter(x['reason'] for x in rr))))
    pending=[r for r in initial if r['status']=='pending_inconsistent'];initial_summary=[]
    for engine,scene in itertools.product(('ui','backend'),('hf_uniform_3','hf_turn90_3')):
        rr=[x for x in pending if (x['source_engine'],x['scene'])==(engine,scene)]
        initial_summary.append(dict(source_engine=engine,scene=scene,pending_frames=len(rr),
            rescued={k:sum(x[k]['support']>=2 for x in rr) for k in ('vertical_free','height_free','radius_free','truth_anchor','truth_position_only','truth_yaw_only')},
            cross_residual=stats([x['original']['best_cross'] for x in rr]),center_spread=stats([x['implied_center_spread_m'] for x in rr])))
    result=dict(completed=True,sources=len(c['sources']),replays=len(state['replays']),new_closed_loop_trials=0,groups=groups,per_replay_metrics=metrics,
        fallback_summary=fallback_summary,initial_summary=initial_summary,
        initial_decisions_checked=len(initial),pending_frames=len(pending),fallback_events=len(fallbacks),
        same_configuration_reproductions=sum(r['native_configuration'] for r in state['replays']),
        limitations=['Fixed-camera replay does not predict new hit rate or new closed-loop control trajectory.',
            'Repeated frames and three recorded trajectories per source mode/seed are correlated; no statistical acceptance.',
            'Posthoc truth is matched by image position and outward normal, with 12px gate and one-to-one matching; coverage is reported.',
            'Geometry relaxation is diagnostic only. Free vertical residual or wider radius can accept incorrect vehicles.',
            'The truth-anchor diagnostic changes pose only in analysis; ground truth is never sent to the algorithm.',
            'Lightbar endpoints use frozen 135mm by 56mm solver dimensions. They are not the 125mm-high physical board outline.'])
    write(root/'result.json',result)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    analyze(a.output.resolve())
