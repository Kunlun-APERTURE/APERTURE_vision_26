"""Replay complete archived sessions through the frozen LIVE wire adapter.

No ROS, simulator truth, new algorithm binary, or score-based case selection.
Priming changes detector history only; primary JPEG/header bytes stay identical.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import time
import uuid

from observer import receive,send
from report import load,write

ROOT=Path(__file__).resolve().parent
STATE_FIELDS=('detector_summary','observations','tracker','continuity','filter_state','track_estimate',
    'selected_plate_id','aim_point','yaw','pitch','distance','fire_advice','fire_check','prediction_horizon_s')


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def archive(folder,seconds=2.):
    start=load(folder/'run-state.json')['measurement_start_stamp_ns']
    inputs={};result=[]
    for line in (folder/'session-evidence/events.jsonl').open():
        p=json.loads(line)
        if p['kind']=='input':inputs[(p['data']['session'],p['data']['seq'],p['data']['stamp_ns'])]=p
        if p['kind']=='result':
            data=p['data'];f=data['frame'];key=(f['session'],f['seq'],f['stamp_ns'])
            if f['stamp_ns']<=start+round(seconds*1e9):result.append((inputs[key],data))
    return start,result


def logical_frame(header,original):
    frame=copy.deepcopy(header)
    clocks=original['result'].get('evaluation_clocks')
    if clocks and 'fire_now_ns' in clocks and 'pose_now_ns' in clocks:
        frame.update(clock_mode='simulation',logical_now_ns=clocks['fire_now_ns'],replay_clocks=clocks)
        return frame
    if frame.get('clock_mode')!='simulation':
        # Recover the decision clock by the existing shared-host pose-age
        # calibration. UI aimer/shooter sample wall time at distinct instants;
        # detector and tracker comparisons do not depend on this approximation.
        age=original['result']['fire_check'].get('pose_age_s')
        if not isinstance(age,(int,float)):raise ValueError('Missing UI decision-time pose age')
        frame.update(clock_mode='simulation',logical_now_ns=frame['latest_muzzle_pose']['stamp_ns']+round(age*1e9))
    return frame


def tiles(width,height):
    w,h=min(640,width),min(640,height)
    result=[]
    for y in ((height-h)//2,0,height-h):
        for x in ((width-w)//2,0,width-w):
            tile=[x,y,w,h]
            if tile not in result:result.append(tile)
    return result


def black_jpeg(path,width,height,image):
    if path.exists():return
    code='import cv2,numpy as np,sys; assert cv2.imwrite(sys.argv[1],np.zeros((int(sys.argv[3]),int(sys.argv[2]),3),np.uint8))'
    subprocess.run(['docker','run','--rm','--network','none','--user',f'{os.getuid()}:{os.getgid()}',
        '-v',str(path.parent)+':/output','--entrypoint','python3',image,'-c',code,
        '/output/'+path.name,str(width),str(height)],check=True,timeout=30,capture_output=True)


def run_one(source,output,bundle,*,prime_tile=None,seconds=2.,detector_history='process',diagnosis=False):
    if output.exists():raise ValueError('Do not replace a completed or partial replay: '+str(output))
    output.mkdir(parents=True)
    metadata=load(bundle/'bundle.json');start,rows=archive(source,seconds)
    if not rows:raise ValueError('No archived complete inputs')
    width=rows[0][0]['data']['calibration']['width'];height=rows[0][0]['data']['calibration']['height']
    first=logical_frame(rows[0][0]['data'],rows[0][1]);tile_index=None
    if prime_tile is not None:
        tile_index=tiles(width,height).index(prime_tile)
        black_jpeg(output.parent/'black.jpg',width,height,metadata['images']['evaluator'])
    work=output/'work';work.mkdir()
    for name in ('assets','configs'):(work/name).symlink_to('/snapshot/sp_vision_25/'+name)
    server=socket.socket();server.bind(('127.0.0.1',0));server.listen(1);server.settimeout(20)
    port=server.getsockname()[1];name='rm26-input-replay-'+uuid.uuid4().hex[:12]
    command=['docker','run','--rm','--name',name,'--network','host','--cpus','2',
        '--user',f'{os.getuid()}:{os.getgid()}',
        '-v',str(bundle)+':/snapshot:ro','-v',str(work)+':/work','-w','/work',
        '--entrypoint','/snapshot/sp_vision_25/build/simulator/sim_auto_aim',metadata['images']['algorithm'],
        '--config=/snapshot/sp_vision_25/configs/simulator.yaml','--runtime=/work/calibration.yaml',
        '--view=false','--fire=auto','--aim-origin=muzzle','--algorithm-profile=tracking_fire_v3',
        '--execution-mode=lockstep','--port='+str(port)]
    if detector_history!='process':command.append('--detector-history='+detector_history)
    if diagnosis:command.append('--diagnosis=true')
    launched=time.monotonic();input_hash=hashlib.sha256();primary=[];priming=[]
    write(output/'plan.json',dict(source=str(source),bundle=str(bundle),prime_tile=prime_tile,seconds=seconds,
        detector_history=detector_history,diagnosis=diagnosis,
        binary_sha256=sha(bundle/'sp_vision_25/build/simulator/sim_auto_aim'),
        source_archive_sha256=sha(source/'session-evidence/input.mjpg'),command=command,
        truth_in_algorithm=False,classification='Open-loop causal diagnostic; not a hit-rate experiment'))
    with (output/'algorithm.log').open('w') as log,(output/'events.jsonl').open('w') as events:
        process=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT)
        try:
            connection,_=server.accept();connection.settimeout(15)
            def exchange(frame,jpeg,stage,reference=None):
                message,binary=receive(connection)
                if message.get('type')!='next' or binary:raise ValueError('Expected next input request')
                send(connection,frame,jpeg)
                result,payload=receive(connection)
                if payload or result.get('type')!='command':raise ValueError('Expected algorithm command response')
                if any(result[k]!=frame[k] for k in ('session','seq','stamp_ns')):raise ValueError('Mismatched replay response')
                packet=dict(stage=stage,frame=frame,jpeg_sha256=hashlib.sha256(jpeg).hexdigest(),result=result)
                events.write(json.dumps(packet,separators=(',',':'))+'\n');events.flush()
                if stage=='primary':
                    input_hash.update(json.dumps(frame,sort_keys=True,separators=(',',':')).encode());input_hash.update(jpeg)
                    primary.append(packet)
                else:priming.append(packet)
                return result
            with (source/'session-evidence/input.mjpg').open('rb') as images:
                if prime_tile is not None:
                    black=(output.parent/'black.jpg').read_bytes()
                    # Miss-only images advance the existing detector scan index.
                    # The unmodified first source image then establishes the
                    # requested tile cache. A new session resets the tracker.
                    for index in range(tile_index+1):
                        frame=copy.deepcopy(first);offset=(tile_index+1-index)*100_000_000
                        for field in ('stamp_ns','sent_ns','logical_now_ns','logical_receipt_ns'):
                            if field in frame:frame[field]-=offset
                        frame['latest_muzzle_pose']['stamp_ns']-=offset
                        frame.update(session='controlled-priming',seq=index+1)
                        if index==tile_index:
                            p=rows[0][0];images.seek(p['jpeg_offset']);jpeg=images.read(p['jpeg_bytes'])
                        else:jpeg=black
                        exchange(frame,jpeg,'priming')
                    last=priming[-1]['result']['detector_summary']
                    if last.get('tile_roi')!=prime_tile or last.get('output_count',0)<1:
                        raise ValueError('Failed to establish declared detector cache: '+str(last))
                for p,original in rows:
                    images.seek(p['jpeg_offset']);jpeg=images.read(p['jpeg_bytes'])
                    if len(jpeg)!=p['jpeg_bytes'] or hashlib.sha256(jpeg).hexdigest()!=p['jpeg_sha256']:
                        raise ValueError('Corrupt source input')
                    exchange(logical_frame(p['data'],original),jpeg,'primary')
            connection.close()
        finally:
            server.close()
            subprocess.run(['docker','stop','--time','1',name],capture_output=True,timeout=10)
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait()
            subprocess.run(['docker','rm','--force',name],capture_output=True,timeout=10)
    differences={}
    for field in STATE_FIELDS:
        changed=[i for i,(new,(_,old)) in enumerate(zip(primary,rows)) if new['result'].get(field)!=old['result'].get(field)]
        differences[field]=dict(different_frames=len(changed),first_frame=changed[0] if changed else None)
    result=dict(source_run=source.name,source_mode=load(source/'manifest.json')['execution']['mode'],
        complete=len(primary)==len(rows),frames=len(primary),priming_frames=len(priming),
        prime_tile=prime_tile,detector_history=detector_history,primary_input_sha256=input_hash.hexdigest(),
        measurement_start_ns=start,wall_s=time.monotonic()-launched,differences_from_recorded=differences,
        first_result=primary[0]['result'],
        clock_note='Backend logical clock preserved exactly. UI decision clock reconstructed; UI aimer/fire differences need separate timing attribution.')
    write(output/'result.json',result);print(json.dumps(dict(case=output.name,frames=len(primary),differences=differences)),flush=True)
    return result


def sources(parent):
    ui=load(parent/'continuity-state.json')['reference']
    backend=load(parent/'receipt-candidate/continuity-state.json')['modes']['ui-fixed-v6_p1']['runs']
    return dict(ui=ui,backend=backend)


def pilot(parent,output):
    output.mkdir(parents=True,exist_ok=True)
    state=load(parent/'runtime-smoke/continuity-state.json');bundle=Path(state['bundle'])
    groups=sources(parent);scene='hf_turn90_3'
    selected={name:ROOT/'experiments'/next(r['run_id'] for r in rr if r['scene']==scene and r['repeat_id']==0)
        for name,rr in groups.items()}
    _,ui_rows=archive(selected['ui']);tile=ui_rows[0][1]['result']['detector_summary']['tile_roi']
    cases=[('backend-cold-0','backend',None),('backend-cold-1','backend',None),
        ('ui-cold-0','ui',None),('ui-primed-0','ui',tile),('ui-primed-1','ui',tile),
        ('backend-primed-0','backend',tile)]
    plan=dict(parent=str(parent),bundle=str(bundle),cases=cases,results=[],
        intervention='Known detector ROI history; unchanged primary images, headers, algorithm and model')
    target=output/'study.json'
    if target.exists():plan=load(target)
    else:write(target,plan)
    for label,source,tile in cases:
        if any(r['case']==label for r in plan['results']):continue
        result=run_one(selected[source],output/label,bundle,prime_tile=tile)
        plan['results'].append(dict(case=label,**result));write(target,plan)
    plan['complete']=True;write(target,plan)


def confirm(parent,output):
    """All six pre-existing UI records, including the same-ROI negative control."""
    output.mkdir(parents=True,exist_ok=True)
    bundle=Path(load(parent/'runtime-smoke/continuity-state.json')['bundle'])
    plan=load(output/'confirmation.json') or dict(parent=str(parent),bundle=str(bundle),results=[])
    for entry in sources(parent)['ui']:
        folder=ROOT/'experiments'/entry['run_id'];_,rr=archive(folder)
        tile=rr[0][1]['result']['detector_summary']['tile_roi']
        for condition,prime in (('cold',None),('primed',tile)):
            case=entry['scene']+'_'+str(entry['repeat_id'])+'_'+condition
            if any(r['case']==case for r in plan['results']):continue
            result=run_one(folder,output/case,bundle,prime_tile=prime)
            plan['results'].append(dict(case=case,scene=entry['scene'],repeat_id=entry['repeat_id'],**result))
            write(output/'confirmation.json',plan)
    plan['complete']=True;write(output/'confirmation.json',plan)


def verify_fix(parent,output,bundle):
    output.mkdir(parents=True,exist_ok=True)
    plan=load(output/'verification.json') or dict(parent=str(parent),bundle=str(bundle),results=[])
    for source_name,rr in sources(parent).items():
        for entry in rr:
            if source_name=='backend' and entry['repeat_id']!=0:continue
            source=ROOT/'experiments'/entry['run_id'];_,data=archive(source)
            tile=data[0][1]['result']['detector_summary']['tile_roi']
            # Both a cold history and a declared cached ROI precede the SAME
            # primary session. Session policy must erase that prior difference.
            if source_name=='backend':tile=[400,0,640,640]
            for label,history,prime in (('process-cold','process',None),('session-cold','session',None),
                    ('session-primed','session',tile)):
                case=source_name+'_'+entry['scene']+'_'+str(entry['repeat_id'])+'_'+label
                if any(r['case']==case for r in plan['results']):continue
                result=run_one(source,output/case,bundle,prime_tile=prime,detector_history=history)
                plan['results'].append(dict(case=case,scene=entry['scene'],repeat_id=entry['repeat_id'],**result))
                write(output/'verification.json',plan)
    plan['complete']=True;write(output/'verification.json',plan)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--parent',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--action',choices=['pilot','confirm','verify'],default='pilot');p.add_argument('--bundle',type=Path)
    args=p.parse_args()
    if args.action=='verify':
        if args.bundle is None:p.error('Verification requires a separately frozen candidate bundle')
        verify_fix(args.parent.resolve(),args.output.resolve(),args.bundle.resolve())
    else:(pilot if args.action=='pilot' else confirm)(args.parent.resolve(),args.output.resolve())
