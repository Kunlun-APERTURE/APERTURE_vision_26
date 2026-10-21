#!/usr/bin/env python3
"""Measure fixed-control renderer A/B differences without feeding truth to vision."""
import argparse,bisect,json,math
from pathlib import Path
import cv2,numpy as np
from diagnostics import Truth
from reversal_image_audit import rotation
from continuity_replay import Interpolate,q


def read(folder):
    packets=[json.loads(s) for s in (folder/'telemetry.jsonl').open()]
    observations={p['data']['frame']['stamp_ns']:p['data'] for p in packets if p['kind']=='observation'}
    armor=[p['data'] for p in packets if p['kind']=='armors'];truth=Truth(armor)
    identity=next(p['id'] for p in armor[0]['armors'] if p['robot_id']==1 and p['slot']==0)
    shots=[json.loads(s) for s in (folder/'shots.jsonl').open()]
    robots=[]
    for p in packets:
        if p['kind']=='robots':
            r=p['data'];values={v['id']:v['position'] for v in r['robots']}
            if 0 in values and 1 in values:robots.append((r['time'],values[0]+values[1]))
    rows=[];frames=[]
    with (folder/'input.mjpg').open('rb') as f:
      for idx in map(json.loads,(folder/'frames.jsonl').read_text().splitlines()):
        o=observations.get(idx['stamp_ns']);plate=truth.at(idx['stamp_ns'],identity,'armors')
        if not o or not plate or idx.get('time') is None:continue
        frame=o['frame'];normal=np.array(plate['normal']);normal/=np.linalg.norm(normal);pos=np.array(plate['position']);origin=np.array(frame['camera_in_map']['position'])
        if normal@(origin-pos)<=0:continue
        horizontal=np.cross([0,0,1],normal);horizontal/=np.linalg.norm(horizontal);vertical=np.cross(normal,horizontal)
        points=np.array([pos+x*.0675*horizontal+y*.028*vertical for x,y in [(-1,-1),(1,-1),(1,1),(-1,1)]])
        optical=(points-origin)@rotation(frame['camera_in_map']['quaternion']);K=np.array(frame['calibration']['k']).reshape(3,3)
        if np.any(optical[:,2]<=.05):continue
        uv=(optical@K.T);uv=uv[:,:2]/uv[:,2,None];w=frame['calibration']['width'];h=frame['calibration']['height']
        if np.any(uv[:,0]<0) or np.any(uv[:,0]>=w) or np.any(uv[:,1]<0) or np.any(uv[:,1]>=h):continue
        x0=max(0,int(uv[:,0].min())-3);x1=min(w,int(uv[:,0].max())+4);y0=max(0,int(uv[:,1].min())-3);y1=min(h,int(uv[:,1].max())+4)
        f.seek(idx['jpeg_offset']);img=cv2.imdecode(np.frombuffer(f.read(idx['jpeg_bytes']),np.uint8),cv2.IMREAD_COLOR)
        roi=img[y0:y1,x0:x1].astype(np.int16);mask=np.zeros(roi.shape[:2],np.uint8);cv2.fillConvexPoly(mask,np.round(uv-[x0,y0]).astype(np.int32),1)
        green=(roi[:,:,1]>=140)&(roi[:,:,1]>=roi[:,:,0]+50)&(roi[:,:,1]>=roi[:,:,2]+50)&(mask>0)
        n=int(green.sum());area=int(mask.sum());enemy=any(d['color']=='blue' for d in o['result'].get('observations',[]))
        rows.append(dict(time_s=idx['time'],green_pixels=n,green_overlap=n>=3 and n/max(1,area)>=.005,enemy_detected=enemy,seq=idx['seq']))
        frames.append((idx,uv.mean(axis=0)))
    return dict(rows=rows,frames=frames,shots=shots,robots=Interpolate(robots),observations=observations)


def main():
    p=argparse.ArgumentParser();p.add_argument('--study',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    batch=json.loads((a.study/'display-scheduled/display-batch.json').read_text());exp=a.study.parent
    pair={r['key'][2]:r for r in batch['runs']};runs={visible:read(exp/r['run_id']) for visible,r in pair.items()}
    on,off=runs['on'],runs['off'];body=[]
    for t in np.arange(.2,17.8,.05):
        x=on['robots'].at(float(t));y=off['robots'].at(float(t))
        if x is not None and y is not None:body.append(float(np.linalg.norm(np.array(x)-y)))
    shot_pairs=list(zip(on['shots'],off['shots']));shot_time=[abs(x['fire_time_s']-y['fire_time_s']) for x,y in shot_pairs]
    muzzle=[math.dist(x['muzzle_position'],y['muzzle_position']) for x,y in shot_pairs]
    initial_velocity=[math.dist(x['initial_velocity'],y['initial_velocity']) for x,y in shot_pairs]
    stats={v:dict(run_id=pair[v]['run_id'],frames=len(r['rows']),green_overlap_frames=sum(x['green_overlap'] for x in r['rows']),
      no_enemy_frames=sum(not x['enemy_detected'] for x in r['rows']),shots=len(r['shots']),target_hits=sum(x['outcome']=='target_armor' for x in r['shots'])) for v,r in runs.items()}
    value=dict(paired_runs=stats,same_shot_count=len(on['shots'])==len(off['shots']),body_position_difference_max_m=max(body),
        launch_time_difference_p95_s=q(shot_time,.95),launch_time_difference_max_s=max(shot_time),
        muzzle_difference_p95_m=q(muzzle,.95),initial_velocity_difference_p95_mps=q(initial_velocity,.95),
        interpretation='Fixed control input schedule; actual launch times and poses have measured scheduling differences. Detection differences remain conditional on these differences.',
        physics_changed=False,hit_rules_changed=False,limitations=['Single paired repeat.','Geometric front ROI, not segmentation.','Inference and ROS scheduling are nondeterministic; this is not an exact pixel counterfactual.'])
    a.output.mkdir(parents=True,exist_ok=True);(a.output/'display-audit.json').write_text(json.dumps(value,ensure_ascii=False,indent=2))
    # Match the most visibly affected input with the other run at nearest capture time.
    chosen=sorted(on['rows'],key=lambda r:r['green_pixels'],reverse=True)[:3];panels=[];index=[]
    for row in chosen:
      for visible in ('on','off'):
        data=runs[visible];idx,centre=min(data['frames'],key=lambda z:abs(z[0]['time']-row['time_s']))
        with (exp/pair[visible]['run_id']/'input.mjpg').open('rb') as f:
            f.seek(idx['jpeg_offset']);img=cv2.imdecode(np.frombuffer(f.read(idx['jpeg_bytes']),np.uint8),cv2.IMREAD_COLOR)
        x,y=np.round(centre).astype(int);crop=img[max(0,y-130):min(img.shape[0],y+130),max(0,x-220):min(img.shape[1],x+220)]
        canvas=np.zeros((300,440,3),np.uint8);canvas[40:]=cv2.resize(crop,(440,260));cv2.putText(canvas,f"{visible} t={idx['time']:.3f}s",(8,25),cv2.FONT_HERSHEY_SIMPLEX,.6,(255,255,255),1)
        panels.append(canvas);index.append(dict(visible=visible,seq=idx['seq'],time_s=idx['time']))
    cv2.imwrite(str(a.output/'display-contact-sheet.png'),np.vstack([np.hstack(panels[i:i+2]) for i in range(0,len(panels),2)]))
    (a.output/'display-contact-sheet-index.json').write_text(json.dumps(index,indent=2));print(json.dumps(value,ensure_ascii=False))

if __name__=='__main__':main()
