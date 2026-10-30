"""Offline native first-image geometry and sealed experiment/media audits."""
import argparse
import bisect
import hashlib
import json
from pathlib import Path

import manage
from report import load,write
from sync_timing_alignment import first,packets


def rotation(q,np):
    x,y,z,w=np.asarray(q,dtype=float)/np.linalg.norm(q)
    return np.array([[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)],
        [2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)],
        [2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]])


def armor_at(folder,stamp):
    rr=[p['data'] for p in packets(folder/'telemetry.jsonl') if p['kind']=='armors']
    ii=bisect.bisect_left([p['stamp_ns'] for p in rr],stamp)
    assert 0<ii<len(rr),'Need real pre-inference GT brackets'
    a,b=rr[ii-1:ii+1];assert b['stamp_ns']-a['stamp_ns']<100_000_000
    fraction=(stamp-a['stamp_ns'])/(b['stamp_ns']-a['stamp_ns'])
    out=[]
    for x in a['armors']:
        y=next(p for p in b['armors'] if p['id']==x['id'])
        if x['robot_id']!=1:continue
        out.append(dict(x,position=[u+(v-u)*fraction for u,v in zip(x['position'],y['position'])],
            normal=[u+(v-u)*fraction for u,v in zip(x['normal'],y['normal'])]))
    return out


def project(frame,plate,np):
    camera=frame['camera_in_map'];r=rotation(camera['quaternion'],np)
    c=np.asarray(plate['position']);n=np.asarray(plate['normal']);n/=np.linalg.norm(n)
    right=np.cross([0,0,1],n);right/=np.linalg.norm(right);up=np.cross(n,right)
    xyz=np.asarray([c+sx*right*plate['width']/2+sy*up*plate['height']/2 for sx,sy in ((-1,1),(1,1),(1,-1),(-1,-1))])
    optical=(xyz-np.asarray(camera['position']))@r
    pixels=optical@np.asarray(frame['calibration']['k']).reshape(3,3).T
    return pixels[:,:2]/pixels[:,2,None]


def native(root):
    import cv2,numpy as np
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    cv2.setNumThreads(1);state=load(root/'study.json');results=[]
    for ui in state['reference']:
        back=next(p for p in state['control'] if (p['scene'],p['repeat_id'])==(ui['scene'],ui['repeat_id']))
        sources=[manage.OPS/'experiments'/e['run_id'] for e in (ui,back)]
        inputs=[first(s) for s in sources]
        projected=[];decoded=[]
        for source,(p,r,jpeg) in zip(sources,inputs):
            frame=p['data'];truth=armor_at(source,frame['stamp_ns'])
            projected.append({plate['id']:project(frame,plate,np) for plate in truth})
            image=cv2.imdecode(np.frombuffer(jpeg,np.uint8),cv2.IMREAD_COLOR);assert image is not None;decoded.append(image)
        pixel_deltas=[np.linalg.norm(projected[0][k]-projected[1][k],axis=1) for k in projected[0]]
        diff=np.abs(decoded[0].astype(float)-decoded[1].astype(float))
        crop=(slice(540,710),slice(580,820))
        results.append(dict(scene=ui['scene'],repeat_id=ui['repeat_id'],runs=[ui['run_id'],back['run_id']],
            maximum_gt_projected_corner_delta_px=float(np.max(pixel_deltas)),
            mean_gt_projected_corner_delta_px=float(np.mean(pixel_deltas)),
            target_crop_mean_abs_channel_delta=float(np.mean(diff[crop])),
            target_crop_different_pixel_fraction=float(np.mean(np.any(diff[crop]>0,axis=2))),
            truth_used_only_after_algorithm_completed=True,
            limitation='Own-camera and target geometry actually differ; subpixel projected changes can alter native rasterization and JPEG. '
                'This does not isolate renderer implementation from physical preparation or sensor/render scheduling.'))
        if ui['repeat_id']==0:
            fig,axes=plt.subplots(1,3,figsize=(12,4),layout='constrained')
            for ax,image,mode in zip(axes[:2],decoded,('Frozen UI first image','Frozen backend first image')):
                ax.imshow(cv2.cvtColor(image,cv2.COLOR_BGR2RGB));ax.set(xlim=(580,820),ylim=(710,540),title=mode);ax.axis('off')
            axes[2].imshow(np.max(diff,axis=2),cmap='magma',vmin=0,vmax=20)
            axes[2].set(xlim=(580,820),ylim=(710,540),title='Absolute native JPEG pixel difference (0..20)');axes[2].axis('off')
            for ext in ('png','svg'):fig.savefig(root/(ui['scene']+'-native-first.'+ext),dpi=170)
            plt.close(fig)
    write(root/'native-first-analysis.json',dict(pairs=results,
        stage='Before any new-session algorithm control feedback. Frozen native original JPEGs, no resampling.'))


def audit(root):
    import cv2,numpy as np
    from background_evidence import audit as measured_audit
    from session_evidence import audit as history_audit
    cv2.setNumThreads(1);state=load(root/'study.json');out=[]
    for mode,value in state['modes'].items():
        for e in value['runs']:
            f=manage.OPS/'experiments'/e['run_id'];run=load(f/'run-state.json')
            measured=measured_audit(f);history=history_audit(f/'session-evidence',run['measurement_start_stamp_ns'],run['measurement_end_stamp_ns'],f)
            assert measured['passed'] and measured['video'] and history['passed'],(e,measured,history)
            n=0
            with (f/'session-evidence/input.mjpg').open('rb') as images:
                for p in packets(f/'session-evidence/events.jsonl'):
                    if p['kind']!='input':continue
                    images.seek(p['jpeg_offset']);jpeg=images.read(p['jpeg_bytes'])
                    assert hashlib.sha256(jpeg).hexdigest()==p['jpeg_sha256']
                    decoded=cv2.imdecode(np.frombuffer(jpeg,np.uint8),cv2.IMREAD_COLOR)
                    assert decoded is not None and decoded.shape[:2]==(1080,1440);n+=1
            out.append(dict(mode=mode,**e,measured=measured,history=history,decoded_full_session=n,passed=True))
            print(json.dumps(dict(audit_progress=len(out),run_id=e['run_id'])),flush=True)
    write(root/'evidence-audit.json',dict(passed=True,runs=len(out),results=out,
        decoded_full_session_jpegs=sum(p['decoded_full_session'] for p in out),
        decoded_measured_jpegs=sum(p['measured']['decoded_jpegs'] for p in out),
        decoded_mp4_frames=sum(int(p['measured']['video']['nb_read_frames']) for p in out)))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['native','audit']);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();(native if a.action=='native' else audit)(a.output.resolve())
