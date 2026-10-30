#!/usr/bin/env python3
"""Export repeat-zero traces and unmodified original-image crops for diagnosis.

Run inside the evaluator image (OpenCV, NumPy and Matplotlib are available).
The plotted repeat is fixed in advance; aggregate conclusions use all repeats.
"""
import argparse
import json
from pathlib import Path

import cv2
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np


def rows(path):return [json.loads(line) for line in path.open()]


def render(diagnosis,experiments,output):
    data=json.loads((diagnosis/'diagnosis.json').read_text())
    frames=rows(diagnosis/'frames.jsonl');shots=rows(diagnosis/'shots.jsonl')
    modes=list(dict.fromkeys(r['mode'] for r in frames if not r['mode'].endswith('_p2')))
    scenes=('hf_uniform_3','hf_turn90_3')
    colors=plt.get_cmap('tab10')
    fig,axes=plt.subplots(4,2,figsize=(15,12),sharex='col',layout='constrained')
    for col,scene in enumerate(scenes):
        for mi,mode in enumerate(modes):
            f=[r for r in frames if r['scene']==scene and r['mode']==mode and r['repeat_id']==0]
            s=[r for r in shots if r['scene']==scene and r['mode']==mode and r['repeat_id']==0]
            if not f:continue
            for ax,key in [(axes[0,col],'center_xy_error_m'),(axes[1,col],'capture_to_result_ms')]:
                ax.plot([r['time_s'] for r in f],[r[key] for r in f],label=mode,color=colors(mi),lw=1)
            axes[2,col].scatter([r['time_s'] for r in s],[r['weight'] for r in s],label=mode,
                color=colors(mi),marker=['o','s','^','D','v'][mi%5],s=30)
            axes[3,col].step([r['time_s'] for r in f],
                [mi*1.3+int(r['correct_tracking'] is True) for r in f],where='post',color=colors(mi),label=mode)
        axes[0,col].axhline(.5,color='black',ls=':',lw=1,label='tracking threshold (0.5m)')
        axes[0,col].set_ylim(bottom=0);axes[0,col].set_title(scene+' / preselected repeat 0')
        axes[2,col].set_ylim(-.05,1.05)
        for ax in axes[:,col]:
            ax.axvline(1,color='gray',ls='--',lw=.8)
            if scene=='hf_turn90_3':
                ax.axvspan(3.2,3.7,color='orange',alpha=.12)
            ax.grid(alpha=.2)
        axes[3,col].set_xlabel('Scenario time (s)')
    for i,label in enumerate(('Estimated center XY error (m)','Capture-to-result age (ms)',
            'Per-shot weighted score','Correct tracking (offset by condition)')):
        axes[i,0].set_ylabel(label)
    axes[0,0].legend(fontsize=8);fig.suptitle('Closed-loop timing diagnosis (illustrative traces; aggregate uses all repeats)')
    for ext in ('png','svg'):fig.savefig(output/('timelines.'+ext),dpi=150)
    plt.close(fig)
    # Fixed times and fixed repeat; never select a frame by hit score.
    selected=[r for r in data['runs'] if r['repeat_id']==0 and r['scene']=='hf_turn90_3'
        and not r['mode'].endswith('_p2')]
    sheet=np.full((len(selected)*260,3*440,3),235,dtype=np.uint8);index=[]
    for ri,run in enumerate(selected):
        folder=experiments/run['run_id'];ff=[r for r in rows(folder/'frames.jsonl') if r.get('measurement')]
        with (folder/'input.mjpg').open('rb') as stream:
            for ci,t in enumerate((.15,2.5,3.35)):
                f=min(ff,key=lambda r:abs(r['time']-t));stream.seek(f['jpeg_offset'])
                raw=stream.read(f['jpeg_bytes']);img=cv2.imdecode(np.frombuffer(raw,dtype=np.uint8),cv2.IMREAD_COLOR)
                points=[]
                for kind,args in f.get('drawings',[]):
                    if kind=='polylines' and args[2]==[255,255,0]:
                        points+=np.asarray(args[0]).reshape(-1,2).tolist()
                center=np.mean(points,axis=0).astype(int) if points else np.array([img.shape[1]//2,img.shape[0]//2])
                w,h=440,220;x=int(np.clip(center[0]-w//2,0,max(0,img.shape[1]-w)));y=int(np.clip(center[1]-h//2,0,max(0,img.shape[0]-h)))
                crop=img[y:y+h,x:x+w];crop=cv2.resize(crop,(440,220))
                sheet[ri*260+40:(ri+1)*260,ci*440:(ci+1)*440]=crop
                cv2.putText(sheet,run['mode']+f" t={f['time']:.3f}s",(ci*440+6,ri*260+25),
                    cv2.FONT_HERSHEY_SIMPLEX,.45,(20,20,20),1,cv2.LINE_AA)
                index.append(dict(mode=run['mode'],run_id=run['run_id'],requested_time_s=t,
                    time_s=f['time'],stamp_ns=f['stamp_ns'],jpeg_offset=f['jpeg_offset'],jpeg_bytes=f['jpeg_bytes'],crop=[x,y,w,h]))
    cv2.imwrite(str(output/'original-input-crops.png'),sheet)
    (output/'figure-sources.json').write_text(json.dumps(dict(selection='Repeat 0; times 0.15, 2.5, 3.35 seconds, closest retained frame. Original JPEG crops without annotations.',
        frames=index),indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--diagnosis',type=Path,required=True)
    p.add_argument('--experiments',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    render(args.diagnosis,args.experiments,args.output)
