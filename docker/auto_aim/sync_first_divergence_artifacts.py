"""Offline evidence audit and exportable causal figures; no live experiment IO."""
import argparse
import hashlib
import json
from pathlib import Path

import manage
from report import load,write


def audit(root):
    import cv2
    import numpy as np
    from background_evidence import audit as measured_audit
    from session_evidence import audit as history_audit
    cv2.setNumThreads(1)
    live=load(root/'history-live.json');entries=[]
    for mode,value in live['modes'].items():
        for entry in value['runs']:
            folder=manage.OPS/'experiments'/entry['run_id'];state=load(folder/'run-state.json')
            measured=measured_audit(folder)
            history=history_audit(folder/'session-evidence',state['measurement_start_stamp_ns'],state['measurement_end_stamp_ns'],folder)
            events=[p for line in (folder/'session-evidence/events.jsonl').open() if (p:=json.loads(line))['kind']=='input']
            frames=0;errors=[];index={}
            with (folder/'session-evidence/input.mjpg').open('rb') as images:
                for packet in events:
                    images.seek(packet['jpeg_offset']);jpeg=images.read(packet['jpeg_bytes'])
                    image=cv2.imdecode(np.frombuffer(jpeg,np.uint8),cv2.IMREAD_COLOR)
                    if image is None:errors.append('Cannot decode full-session original input')
                    else:frames+=1
                    f=packet['data'];index[(f['seq'],f['stamp_ns'])]=packet['jpeg_sha256']
            with (folder/'input.mjpg').open('rb') as images,(folder/'frames.jsonl').open() as rows:
                for line in rows:
                    f=json.loads(line);images.seek(f['jpeg_offset']);jpeg=images.read(f['jpeg_bytes'])
                    if hashlib.sha256(jpeg).hexdigest()!=index.get((f['seq'],f['stamp_ns'])):
                        errors.append('Measured JPEG differs from full session')
            passed=measured['passed'] and history['passed'] and not errors and bool(measured['video'])
            entries.append(dict(mode=mode,run_id=entry['run_id'],passed=passed,errors=errors,
                measured=measured,history=history,decoded_full_session=frames))
            print(json.dumps(dict(progress=len(entries),run_id=entry['run_id'],passed=passed)),flush=True)
    value=dict(passed=all(e['passed'] for e in entries),runs=len(entries),results=entries,
        decoded_full_session=sum(e['decoded_full_session'] for e in entries),
        decoded_mp4_frames=sum(int(e['measured']['video'].get('nb_read_frames',0)) for e in entries))
    write(root/'live-evidence-audit.json',value);return value


def figures(root):
    import cv2
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    cv2.setNumThreads(1)
    fig,axes=plt.subplots(1,2,figsize=(13,4.5),layout='constrained')
    for label,color in [('backend-cold-0','tab:orange'),('backend-primed-0','tab:blue')]:
        rows=load(root/(label+'-error.json'))
        axes[0].plot([r['time_s'] for r in rows],[r['center_xy_error_m'] for r in rows],label=label,color=color)
    axes[0].axhline(.5,color='gray',ls=':',label='0.5m scoring threshold');axes[0].axvline(1,color='gray',ls='--')
    axes[0].set(xlabel='Scenario time (s)',ylabel='Center XY error (m)',title='Same recorded camera input; cache history only')
    axes[0].legend(fontsize=8);axes[0].grid(alpha=.2)
    value=load(root/'causal-result.json');items=value['restored_ui_runs']
    x=np.arange(len(items));axes[1].bar(x,[r['cold_filter_different_frames'] for r in items],color='tab:orange',label='Cold replay vs recorded UI')
    axes[1].scatter(x,[0]*len(items),marker='D',color='tab:blue',label='Matched-cache replay vs recorded UI',zorder=3)
    axes[1].set(xticks=x,xticklabels=[r['scene'].removeprefix('hf_')+' / '+str(r['repeat_id']) for r in items],
        ylabel='Frames with different filter state',title='Six existing UI records; all results retained')
    axes[1].tick_params(axis='x',labelrotation=30);axes[1].legend(fontsize=8);axes[1].grid(axis='y',alpha=.2)
    for ext in ('png','svg'):fig.savefig(root/('causal-curves.'+ext),dpi=160)
    plt.close(fig)
    cases=[]
    for case in ('backend-cold-0','backend-primed-0'):
        packet=next(p for line in (root/case/'events.jsonl').open() if (p:=json.loads(line))['stage']=='primary')
        cases.append((case,packet))
    source=manage.OPS/'experiments'/load(root/'backend-cold-0/result.json')['source_run']
    frame=cases[0][1]['frame']
    packet=next(p for line in (source/'session-evidence/events.jsonl').open()
        if (p:=json.loads(line))['kind']=='input' and p['data']['stamp_ns']==frame['stamp_ns'])
    with (source/'session-evidence/input.mjpg').open('rb') as images:
        images.seek(packet['jpeg_offset']);jpeg=images.read(packet['jpeg_bytes'])
    original=cv2.imdecode(np.frombuffer(jpeg,np.uint8),cv2.IMREAD_COLOR)
    points=np.array([p for _,r in cases for a in r['result']['observations'] for p in a['corners']])
    center=points.mean(axis=0);width,height=360,220
    left=int(np.clip(center[0]-width/2,0,original.shape[1]-width));top=int(np.clip(center[1]-height/2,0,original.shape[0]-height))
    fig,axes=plt.subplots(1,3,figsize=(14,4),layout='constrained')
    axes[0].imshow(cv2.cvtColor(original,cv2.COLOR_BGR2RGB));axes[0].set_title('Original JPEG (same in both runs)')
    for ax,(name,record) in zip(axes[1:],cases):
        ax.imshow(cv2.cvtColor(original,cv2.COLOR_BGR2RGB));r=record['result']
        plate=r['track_estimate']['plates'][0]
        chosen=min(range(len(r['observations'])),key=lambda i:sum((a-b)**2 for a,b in zip(r['observations'][i]['position'],plate)))
        for index,armor in enumerate(r['observations']):
            xy=np.asarray(armor['corners']);xy=np.concatenate((xy,xy[:1]))
            ax.plot(xy[:,0],xy[:,1],color='lime' if index==chosen else 'cyan',lw=1.5)
            ax.text(xy[0,0],xy[:,1].min()-5,f"{armor['confidence']:.4f}",color='white',fontsize=8)
        roi=r['detector_summary']['tile_roi'];ax.set_title(name+f'\nROI origin: {roi[:2]}; green = initial plate')
    for ax in axes:
        ax.set_xlim(left,left+width);ax.set_ylim(top+height,top);ax.axis('off')
    for ext in ('png','svg'):fig.savefig(root/('same-input-corners.'+ext),dpi=180)
    plt.close(fig)
    write(root/'figure-sources.json',dict(source_run=source.name,input_stamp_ns=frame['stamp_ns'],
        input_sha256=hashlib.sha256(jpeg).hexdigest(),crop=[left,top,width,height],
        selection='Predeclared backend turn repeat 0, first warmup frame; same original image in all panels.',
        counterfactual='Camera trajectory stays fixed in the original recording; no new closed-loop hit claims.'))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['audit','figures']);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();value=(audit if args.action=='audit' else figures)(args.output.resolve())
    if args.action=='audit' and not value['passed']:raise SystemExit(1)
