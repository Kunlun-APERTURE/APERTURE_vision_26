#!/usr/bin/env python3
"""Supplemental raw-JPEG image association audit. No algorithm or render changes."""
import argparse
import bisect
import collections
import json
import math
from pathlib import Path
import cv2
import numpy as np
from diagnostics import Truth
from reversal_diagnose import paths,jsonl,load,write,table,MOVING

def rotation(q):
    x,y,z,w=np.asarray(q,dtype=float)/np.linalg.norm(q)
    return np.array([[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)],
        [2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)],
        [2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]])

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();exp,study,batch=paths(a.root);out=a.output
    all_rows=[];run_ids=[r['run_id'] for r in batch['runs'] if r['algorithm_profile']=='tracking_v2' and r['scene'] in MOVING]
    for number,run_id in enumerate(run_ids):
        f=exp/run_id;manifest=load(f/'manifest.json');observations={};armors=[]
        for packet in jsonl(f/'telemetry.jsonl'):
            if packet['kind']=='observation':observations[packet['data']['frame']['stamp_ns']]=packet['data']
            elif packet['kind']=='armors':armors.append(packet['data'])
        truth=Truth(armors);front_id=next(p['id'] for p in armors[0]['armors'] if p['robot_id']==1 and p['slot']==0)
        with (f/'input.mjpg').open('rb') as source:
            for index in jsonl(f/'frames.jsonl'):
                if not index.get('measurement') or index.get('time') is None:continue
                stamp=index['stamp_ns'];obs=observations.get(stamp);plate=truth.at(stamp,front_id,'armors')
                if not obs or not plate:continue
                frame=obs['frame'];normal=np.asarray(plate['normal']);normal/=np.linalg.norm(normal);position=np.asarray(plate['position']);camera=frame['camera_in_map'];origin=np.asarray(camera['position']);R=rotation(camera['quaternion']);K=np.asarray(frame['calibration']['k']).reshape(3,3)
                if float(normal@(origin-position))<=0:continue
                horizontal=np.cross([0,0,1],normal);horizontal/=np.linalg.norm(horizontal);vertical=np.cross(normal,horizontal)
                # Approximate the 135 x 56 mm detector quadrilateral. This is
                # a geometric ROI, not pixel-perfect foreground segmentation.
                points=np.array([position+sx*.0675*horizontal+sy*.028*vertical for sx,sy in [(-1,-1),(1,-1),(1,1),(-1,1)]])
                optical=(points-origin)@R
                if np.any(optical[:,2]<=.05):continue
                uvw=optical@K.T;uv=uvw[:,:2]/uvw[:,2,None]
                w,h=frame['calibration']['width'],frame['calibration']['height']
                if np.any(uv[:,0]<0) or np.any(uv[:,0]>=w) or np.any(uv[:,1]<0) or np.any(uv[:,1]>=h):continue
                x0=max(0,int(np.floor(uv[:,0].min()))-3);x1=min(w,int(np.ceil(uv[:,0].max()))+4)
                y0=max(0,int(np.floor(uv[:,1].min()))-3);y1=min(h,int(np.ceil(uv[:,1].max()))+4)
                source.seek(index['jpeg_offset']);image=cv2.imdecode(np.frombuffer(source.read(index['jpeg_bytes']),np.uint8),cv2.IMREAD_COLOR)
                roi=image[y0:y1,x0:x1].astype(np.int16);mask=np.zeros(roi.shape[:2],np.uint8);cv2.fillConvexPoly(mask,np.round(uv-[x0,y0]).astype(np.int32),1)
                green=(roi[:,:,1]>=140)&(roi[:,:,1]>=roi[:,:,0]+50)&(roi[:,:,1]>=roi[:,:,2]+50)&(mask>0)
                n=int(green.sum());area=int(mask.sum());near=bool(n>=3 and n/max(area,1)>=.005)
                detections=obs['result'].get('observations') or [];enemy=[d for d in detections if d.get('color')=='blue']
                row={'run_id':run_id,'scene':manifest['scene'],'physics':manifest['shooting']['physics'],'seq':index['seq'],'stamp_ns':stamp,'time_s':index['time'],
                    'green_overlap':near,'green_pixels':n,'roi_pixels':area,'green_fraction':n/max(area,1),'enemy_detected':bool(enemy),
                    'any_detection':bool(detections),'front_roi_uv':uv.tolist(),'gt_visible_plates':index['visible_plates']}
                all_rows.append(row)
        print(f'Image audit {number+1}/18 {run_id}',flush=True)
    table(out/'raw-image-overlap.csv',all_rows)
    def rate(rs):return {'frames':len(rs),'no_enemy_detection':sum(not r['enemy_detected'] for r in rs),'no_enemy_detection_fraction':sum(not r['enemy_detected'] for r in rs)/len(rs) if rs else None}
    groups={}
    for v in (False,True):groups[str(v)]=rate([r for r in all_rows if r['green_overlap']==v])
    per_run=[]
    for run_id in run_ids:per_run.append({'run_id':run_id,'green_overlap':rate([r for r in all_rows if r['run_id']==run_id and r['green_overlap']]),'no_green_overlap':rate([r for r in all_rows if r['run_id']==run_id and not r['green_overlap']])})
    events=[e for e in load(out/'events.json') if e['algorithm_profile']=='tracking_v2'];event_rows=[]
    for e in events:
        rs=[r for r in all_rows if r['run_id']==e['run_id'] and -.75<=(r['stamp_ns']-e['stamp_ns'])*1e-9<=2]
        missing=[r for r in rs if not r['enemy_detected']]
        event_rows.append({'event_id':e['event_id'],'slow':e['slow'],'frames':len(rs),'missing_detection_frames':len(missing),
            'missing_with_green_overlap':sum(r['green_overlap'] for r in missing)})
    worst=next(e for e in events if e['run_id'].endswith('0133280d') and e['ordinal']==2)
    sequence=[r for r in all_rows if r['run_id']==worst['run_id'] and 552<=r['seq']<=559]
    result={'pixel_rule':'Raw JPEG BGR: G>=140, G>=B+50, G>=R+50; >=3 pixels and >=0.5% within projected front detector ROI (135 x 56 mm).',
        'source_code':'vision_simulator_26/src/systems/projectile.rs setup_projectile: opaque green emissive material',
        'groups':groups,'runs':per_run,'events':event_rows,'worst_dropout_frames':sequence,
        'limitations':['Association only; green pixels are not proven to be a particular projectile.',
            'No counterfactual re-render or modified detector was run. View angle, position, firing timing and scene phase confound this comparison.',
            'ROI is projected from truth in the scorer only. Results outside front-facing fully in-image ROI are excluded.',
            'No algorithm, physics, graphics or scoring acceptance thresholds changed.']}
    write(out/'raw-image-overlap.json',result);contact_sheet(exp,out,worst);print(json.dumps(groups),flush=True)

def contact_sheet(exp,out,event):
    folder=exp/event['run_id'];indices={r['seq']:r for r in jsonl(folder/'frames.jsonl')}
    rows={r['seq']:r for r in jsonl(out/'runs'/event['run_id']/f"event-{event['ordinal']}.jsonl")}
    # Frozen worst-event inspection samples, including before/during/after loss.
    selected=[550,554,558,560,570,587];panels=[]
    with (folder/'input.mjpg').open('rb') as stream:
        for seq in selected:
            r=rows[seq];idx=indices[seq];stream.seek(idx['jpeg_offset']);image=cv2.imdecode(np.frombuffer(stream.read(idx['jpeg_bytes']),np.uint8),cv2.IMREAD_COLOR)
            crop=image[440:740,520:1000].copy()
            for a in r['raw_observations']:cv2.polylines(crop,[np.asarray(a['corners'],np.int32)-[520,440]],True,(0,220,0),1)
            panel=np.zeros((340,480,3),np.uint8);panel[40:]=crop
            cv2.putText(panel,f"seq {seq}, t={r['event_dt_s']:+.3f}s, detections={r['raw_detection_count']}",(8,23),cv2.FONT_HERSHEY_SIMPLEX,.48,(255,255,255),1);panels.append(panel)
    cv2.imwrite(str(out/'worst-event-input-contact-sheet.png'),np.vstack([np.hstack(panels[:3]),np.hstack(panels[3:])]))
    write(out/'contact-sheet-index.json',{'event_id':event['event_id'],'source':'input.mjpg','seq':selected,
        'source_crop_xywh':[520,440,480,300],'overlay':'Green outline copies recorded detection corners; green solid objects are in the source JPEG.'})

if __name__=='__main__':main()
