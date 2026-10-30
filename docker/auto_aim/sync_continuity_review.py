"""Read-only full-history audit and phase attribution for continuity pilots."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import statistics

import manage
from report import load,write
from sync_continuity import rows
from sync_diagnosis import analyze_run,aggregate
from sync_study import row


def packets(folder):
    return [json.loads(line) for line in (folder/'session-evidence/events.jsonl').open()]


def collected(root,include_setup=False):
    sources=[root,root/'receipt-candidate']
    if include_setup:sources.extend([root/'runtime-smoke',root.parent/'lockstep-continuity-20261023'])
    for source in sources:
        state=load(source/'continuity-state.json')
        if not state:continue
        for mode,entry in rows(state):
            folder=manage.OPS/'experiments'/entry['run_id']
            if load(folder/'summary.json')['verdict']=='invalid':continue
            yield (('setup_'+mode) if source==sources[-1] and include_setup else mode),entry


def analyze(root):
    runs=[];frames=[];shots=[];histories={};output=root/'final-review';output.mkdir(exist_ok=True)
    for mode,entry in collected(root):
        folder=manage.OPS/'experiments'/entry['run_id'];scored=output/'rescore'/entry['run_id']
        row(folder,scored,entry['repeat_id'])
        r,f,s=analyze_run(folder,entry,mode,output,scored_folder=scored)
        runs.append(r);frames+=f;shots+=s
        start=load(folder/'run-state.json')['measurement_start_stamp_ns']
        events=packets(folder);inputs=[p for p in events if p['kind']=='input']
        result=[p['data'] for p in events if p['kind']=='result']
        warm=[p for p in inputs if p['data']['stamp_ns']<start]
        first=next((p for p in events if p['kind']=='measurement_pipeline'),None)
        histories[entry['run_id']]=dict(mode=mode,scene=entry['scene'],repeat_id=entry['repeat_id'],
            warmup_inputs=len(warm),first_thirty_image_sha256=[p['jpeg_sha256'] for p in warm[:30]],
            initial_detector_summary=result[0]['result'].get('detector_summary') if result else None,
            first_measured_capture_ms=(next(p['data']['stamp_ns'] for p in inputs if p['data']['stamp_ns']>=start)-start)*1e-6,
            queued_pre_measurement_images=first['data']['before']['queued_images'] if first else None)
    phases=[]
    for mode in sorted(set(r['mode'] for r in runs)):
        for scene in ('hf_uniform_3','hf_turn90_3'):
            ff=[f for f in frames if f['mode']==mode and f['scene']==scene]
            ss=[s for s in shots if s['mode']==mode and s['scene']==scene]
            for phase in ('stationary','acceleration','straight','late_straight','turn','after_turn'):
                f=[r for r in ff if r['phase']==phase];s=[r for r in ss if r['phase']==phase]
                if f or s:phases.append(dict(mode=mode,scene=scene,phase=phase,**aggregate(f,s)))
    controls=[]
    for scene in ('hf_uniform_3','hf_turn90_3'):
        control=next((v for v in histories.values() if v['mode']=='ui-fixed-v4_p1' and v['scene']==scene),None)
        if control is None:continue
        for v in histories.values():
            if v['mode'] in ('ui-fixed-v5_p1','ui-fixed-v6_p1') and v['scene']==scene:
                controls.append(dict(scene=scene,mode=v['mode'],repeat_id=v['repeat_id'],
                    first_thirty_warmup_images_identical=control['first_thirty_image_sha256']==v['first_thirty_image_sha256']))
    write(output/'diagnosis.json',dict(runs=runs,phases=phases,histories=histories,warmup_controls=controls,
        phase_assignment='Shot launch time, not impact time. Empty shot groups have undefined hit rate.',
        limitation='A complete bridge session does not export YOLO internal cache from an earlier process session. UI lifecycle is recorded separately.'))
    for name,data in (('frames.jsonl',frames),('shots.jsonl',shots)):
        with (output/name).open('w') as stream:
            for item in data:stream.write(json.dumps(item)+'\n')
    return controls


def audit(root):
    import cv2
    import numpy as np
    from background_evidence import audit as measured_audit
    from session_evidence import audit as session_audit
    cv2.setNumThreads(1);results=[]
    for mode,entry in collected(root,include_setup=True):
        folder=manage.OPS/'experiments'/entry['run_id'];run_state=load(folder/'run-state.json')
        start=run_state['measurement_start_stamp_ns'];end=run_state['measurement_end_stamp_ns']
        session=session_audit(folder/'session-evidence',start,end,folder)
        measured=measured_audit(folder);errors=[];index={};decoded=0
        inputs=[p for p in packets(folder) if p['kind']=='input']
        with (folder/'session-evidence/input.mjpg').open('rb') as stream:
            for p in inputs:
                stream.seek(p['jpeg_offset']);data=stream.read(p['jpeg_bytes'])
                image=cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_COLOR)
                header=p['data']
                if image is None or image.shape[:2]!=(header['calibration']['height'],header['calibration']['width']):
                    errors.append('Full-session JPEG decode/dimension failure')
                else:decoded+=1
                index[(header['seq'],header['stamp_ns'])]=p['jpeg_sha256']
        matched=0
        with (folder/'input.mjpg').open('rb') as stream,(folder/'frames.jsonl').open() as source:
            for line in source:
                frame=json.loads(line);stream.seek(frame['jpeg_offset']);data=stream.read(frame['jpeg_bytes'])
                if index.get((frame['seq'],frame['stamp_ns']))!=hashlib.sha256(data).hexdigest():
                    errors.append('Measurement/session JPEGs differ')
                else:matched+=1
        item=dict(run_id=folder.name,mode=mode,passed=not errors and session['passed'] and measured['passed'],
            errors=errors,session=session,measurement=measured,full_history_decoded=decoded,
            byte_identical_measurement_images=matched)
        results.append(item);print(json.dumps(dict(progress=len(results),run_id=folder.name,passed=item['passed'])),flush=True)
    value=dict(passed=all(r['passed'] for r in results),runs=len(results),results=results,
        full_history_decoded=sum(r['full_history_decoded'] for r in results),
        measurement_decoded=sum(r['measurement']['decoded_jpegs'] for r in results))
    write(root/'final-full-history-audit.json',value)
    return value


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['analyze','audit']);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();result=(analyze if args.action=='analyze' else audit)(args.output.resolve())
    print(json.dumps(result),flush=True)
    if args.action=='audit' and not result['passed']:raise SystemExit(1)
