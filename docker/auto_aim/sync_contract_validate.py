"""Independent coverage/media audits and exact per-stage-clock wire replays."""
import argparse
import hashlib
import json
from pathlib import Path

import manage
from report import load,write
from sync_first_divergence import run_one,sha,STATE_FIELDS
from sync_first_divergence_analysis import primary


def entries(root):
    state=load(root/'contract-study.json') or load(root/'acceptance-r2/contract-study.json')
    assert state['state']=='completed'
    return state,[(mode,r) for mode,value in state['modes'].items() for r in value['runs']]


def audit(root):
    import cv2
    import numpy as np
    from background_evidence import audit as measured_audit
    from session_evidence import audit as history_audit
    cv2.setNumThreads(1);state,all_runs=entries(root);out=[]
    for mode,entry in all_runs:
        folder=manage.OPS/'experiments'/entry['run_id'];run=load(folder/'run-state.json')
        measured=measured_audit(folder)
        history=history_audit(folder/'session-evidence',run['measurement_start_stamp_ns'],run['measurement_end_stamp_ns'],folder)
        assert measured['passed'] and history['passed'],(mode,entry,measured,history)
        assert measured['video'],'Every predeclared trial needs complete media'
        rows=[json.loads(line) for line in (folder/'session-evidence/events.jsonl').open()]
        count=0
        with (folder/'session-evidence/input.mjpg').open('rb') as source:
            for p in rows:
                if p['kind']!='input':continue
                source.seek(p['jpeg_offset']);jpeg=source.read(p['jpeg_bytes'])
                assert hashlib.sha256(jpeg).hexdigest()==p['jpeg_sha256']
                image=cv2.imdecode(np.frombuffer(jpeg,np.uint8),cv2.IMREAD_COLOR)
                assert image is not None and image.shape[:2]==(1080,1440)
                count+=1
        out.append(dict(mode=mode,**entry,passed=True,measured=measured,history=history,
            decoded_full_session_jpegs=count))
        print(json.dumps(dict(stage='audit',completed=len(out),total=len(all_runs),run_id=folder.name)),flush=True)
    value=dict(passed=True,runs=len(out),results=out,
        decoded_full_session_jpegs=sum(r['decoded_full_session_jpegs'] for r in out),
        decoded_measured_jpegs=sum(r['measured']['decoded_jpegs'] for r in out),
        decoded_mp4_frames=sum(int(r['measured']['video'].get('nb_read_frames',0)) for r in out))
    write(root/'evidence-audit.json',value)


def replay(root):
    state,all_runs=entries(root);bundle=Path(state['bundle'])
    path=root/'clock-replay.json';result=load(path) or dict(results=[],plan='All eight UI/backend pilot and main UI archives, full warmup + six seconds. '
        'Actual aim/fire/pose sampling clocks; no reconstruction from pose age; unchanged JPEGs and own poses. '
        'Replay two repeated backend histories for algorithm compatibility.',truth_in_algorithm=False)
    chosen=[(mode,e) for mode,e in all_runs if mode in ('pilot_ui','ui') or mode=='backend' and e['repeat_id']==3]
    for mode,e in chosen:
        case=mode+'_'+e['scene']+'_'+str(e['repeat_id'])
        if any(r['case']==case and r.get('classified') for r in result['results']):continue
        output=root/'replays'/case
        r=load(output/'result.json') if (output/'result.json').exists() else run_one(
            manage.OPS/'experiments'/e['run_id'],output,bundle,seconds=6.,detector_history='session')
        changed={k:v for k,v in r['differences_from_recorded'].items() if v['different_frames']}
        from sync_first_divergence import archive
        _,old=archive(manage.OPS/'experiments'/e['run_id'],6.)
        current=primary(output);benign=[];unexpected=[]
        for packet,(_,recorded) in zip(current,old):
            a,b=packet['result'],recorded['result']
            for name in STATE_FIELDS:
                if a.get(name)==b.get(name):continue
                if name=='prediction_horizon_s' and abs(a[name]-b[name])<=2e-15:
                    benign.append(dict(seq=packet['frame']['seq'],field=name,kind='floating_point_unit_roundoff',absolute_delta=abs(a[name]-b[name])))
                elif name=='fire_check' and {k for k in a[name] if a[name].get(k)!=b[name].get(k)}=={'last_good_age_s'} and min(a[name]['last_good_age_s'],b[name]['last_good_age_s'])>3600:
                    benign.append(dict(seq=packet['frame']['seq'],field=name+'.last_good_age_s',kind='uninitialized_zero_timepoint_has_different_clock_origin',original=b[name]['last_good_age_s'],replay=a[name]['last_good_age_s']))
                else:unexpected.append(dict(seq=packet['frame']['seq'],field=name))
        previous=next((p for p in result['results'] if p['case']==case),None)
        if previous:result.setdefault('strict_probe_history',[]).append(previous);result['results'].remove(previous)
        result['results'].append(dict(case=case,mode=mode,**e,classified=True,passed=not unexpected,
            raw_bitwise_identical=not changed,frames=r['frames'],differences=changed,
            explained_diagnostic_deltas=benign,unexpected_differences=unexpected,
            input_sha256=r['primary_input_sha256'],files={name:sha(output/name) for name in ('plan.json','events.jsonl','result.json')}))
        write(path,result)
        if unexpected:raise RuntimeError('Actual per-stage-clock replay changed commands/state: '+str(unexpected))
    result.update(passed=all(r['passed'] for r in result['results']),cases=len(chosen),
        frames=sum(r['frames'] for r in result['results']),
        comparison_note='Commands, fire advice, observations and filter must match exactly. Raw differences are retained: '
            'unit roundoff in the displayed prediction horizon and an uninitialized last-good zero-timepoint clock origin '
            'are classified separately; these do not certify all diagnostics as bitwise identical.')
    write(path,result)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['audit','replay']);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();(audit if args.action=='audit' else replay)(args.output.resolve())
