"""Frozen UI first-divergence attribution and timing-only backend calibration.

No changes to UI, numerical aiming, simulator images, physics or scoring.
Source selection and phase profiles never depend on hit rate.
"""
import argparse
import copy
import hashlib
import itertools
import json
import math
from pathlib import Path
import statistics
import time
import uuid

import background_jobs as jobs
import manage
from background_capacity import control_hz
from report import load,write
from sync_first_divergence import archive,logical_frame,run_one,sha
from sync_first_divergence_analysis import primary
from sync_timing import load_profile,validate
from sync_study import DEFAULTS,ingest
from sync_diagnosis import freeze_candidate

ROOT=Path(__file__).resolve().parent
SCENES=('hf_uniform_3','hf_turn90_3')


def packets(path):return [json.loads(line) for line in path.open()]


def observations(folder):
    return [p['data'] for p in packets(folder/'session-evidence/events.jsonl') if p['kind']=='result']


def stats(values):
    v=sorted(values)
    return dict(n=len(v),p50=statistics.median(v),p05=v[int((len(v)-1)*.05)],
                p95=v[int((len(v)-1)*.95)],mean=statistics.mean(v))


def first(folder):
    _,rr=archive(folder,0.)
    p,r=rr[0]
    with (folder/'session-evidence/input.mjpg').open('rb') as stream:
        stream.seek(p['jpeg_offset']);jpeg=stream.read(p['jpeg_bytes'])
    assert hashlib.sha256(jpeg).hexdigest()==p['jpeg_sha256']
    return p,r,jpeg


def initialize(parent,root):
    root.mkdir(parents=True,exist_ok=True)
    path=root/'study.json';state=load(path)
    if state:return state
    previous=load(parent/'contract-study.json');bundle=Path(previous['bundle'])
    meta=load(bundle/'bundle.json')
    assert previous['state']=='completed'
    assert all(sha(bundle/name)==digest for name,digest in meta['files'].items())
    for name in ('bridge.py','evaluator.py','background_runtime.py','experiments.py','shots.py','input_contract.py'):
        assert sha(ROOT/name)==meta['files']['docker/auto_aim/'+name],name
    # The prior turn removed three manual-start parser flags only. Freeze and
    # disclose this launcher difference; the experimental run path is identical.
    launcher=(bundle/'docker/auto_aim/manage.py').read_text()
    for line in ("        p.add_argument('--input-contract',choices=['legacy','aligned-v1'])\n",
                 "        p.add_argument('--sensor-seed',type=int)\n",
                 "        p.add_argument('--warmup-frames',type=int)\n"):
        launcher=launcher.replace(line,'')
    assert launcher==(ROOT/'manage.py').read_text()
    assert sha(manage.ROOT/'sp_vision_25/build/simulator/sim_auto_aim')==meta['files']['sp_vision_25/build/simulator/sim_auto_aim']
    state=dict(state='frozen',created=time.time(),parent=str(parent),reference_bundle=str(bundle),
        reference=previous['modes']['ui']['runs'],control=previous['modes']['backend']['runs'],modes={},
        plan='First: all six UI/backend pairs crossed at first warmup frame (24 native-JPEG/header replays). '
            'Second: timing-only calibration from six frozen UI histories, no score selection. '
            'Third: baseline and cumulative delay/capture/control timing profiles, six same-seed runs each '
            'if timing properties pass; report first-image initialization separately from aggregate scores. '
            'UI, algorithm, simulator binary, sensor model, hit score and original evidence remain frozen.',
        ui_equivalence_certified=False,capacity_registration=False,
        launcher_difference='Three manual-start-only parser options removed in prior task; experiment runtime and source data unchanged.',
        frozen_files={name:digest for name,digest in meta['files'].items() if name.startswith(('sp_vision_25/',
            'vision_simulator_26/','docker/auto_aim/bridge.py','docker/auto_aim/evaluator.py','docker/auto_aim/background_runtime.py'))})
    write(path,state);return state


def inventory(parent,root):
    state=initialize(parent,root);rows=[];pairs=[]
    for mode,entries in [('ui',state['reference']),('backend',state['control'])]:
        for e in entries:
            if e['repeat_id']>=3:continue
            source=ROOT/'experiments'/e['run_id'];start=load(source/'run-state.json')['measurement_start_stamp_ns']
            rr=observations(source);p,r,jpeg=first(source);h=p['data']
            measured=[q for q in rr if start<=q['frame']['stamp_ns']<=load(source/'run-state.json')['measurement_end_stamp_ns']]
            clocks=[]
            for q in measured:
                f=q['frame'];c=q['result']['evaluation_clocks']
                if not all(k in c for k in ('aim_now_ns','fire_now_ns','pose_now_ns')):continue
                clocks.append(dict(input_ns=f['sent_ns']-f['stamp_ns'],
                    aim_ns=c['aim_now_ns']-f['sent_ns'],fire_ns=c['fire_now_ns']-f['sent_ns'],
                    pose_clock_offset_ns=c['pose_now_ns']-c['fire_now_ns'],
                    return_ns=q['receipt_ns']-c['fire_now_ns'],
                    pose_ns=f['sent_ns']-f['latest_muzzle_pose']['stamp_ns']))
            rows.append(dict(e,mode=mode,sensor_seed=load(source/'manifest.json')['sensor_seed'],
                control_hz=control_hz(source),first_input=dict(header=h,jpeg_sha256=p['jpeg_sha256'],
                    result=r['result'],capture_to_measurement_ms=(h['stamp_ns']-start)*1e-6),
                capture_intervals_ns=stats([b['frame']['stamp_ns']-a['frame']['stamp_ns'] for a,b in zip(measured,measured[1:])]),
                timing={k:stats([q[k] for q in clocks]) for k in clocks[0]},
                timing_samples=clocks,source_hashes={name:sha(source/name) for name in
                    ('manifest.json','run-state.json','session-evidence/events.jsonl','session-evidence/input.mjpg')}))
    for ui in [p for p in rows if p['mode']=='ui']:
        back=next(p for p in rows if p['mode']=='backend' and (p['scene'],p['repeat_id'])==(ui['scene'],ui['repeat_id']))
        a,b=ui['first_input'],back['first_input'];x,y=a['header'],b['header']
        qa,qb=x['camera_in_map']['quaternion'],y['camera_in_map']['quaternion']
        # Normalize: TF rotations have float32 norm roundoff.
        dot=sum(p*q for p,q in zip(qa,qb))/math.sqrt(sum(p*p for p in qa)*sum(q*q for q in qb))
        pairs.append(dict(scene=ui['scene'],repeat_id=ui['repeat_id'],sensor_seed=ui['sensor_seed'],
            same_sensor_phase=x['sensor']['phase']==y['sensor']['phase'],
            same_detector_roi=a['result']['detector_summary']==b['result']['detector_summary'],
            first_jpeg_same=a['jpeg_sha256']==b['jpeg_sha256'],
            camera_position_delta_m=math.dist(x['camera_in_map']['position'],y['camera_in_map']['position']),
            camera_rotation_delta_deg=math.degrees(2*math.acos(min(1,abs(dot)))),
            first_observations_same=a['result']['observations']==b['result']['observations'],
            first_center_delta_m=math.dist(a['result']['track_estimate']['center'],b['result']['track_estimate']['center']),
            classification='Divergence already exists at the first native rendered input, before any algorithm command has reached the world.'))
    result=dict(runs=rows,first_pairs=pairs,first_divergence='First warmup capture/initialization, before control feedback',
        source_contract='Same binary/model/native sensor phase and ROI; own pose and native image differ. '
            'Later input delivery timing cannot cause an earlier first exposure difference.')
    write(root/'inventory.json',result)
    return result


def first_cross(parent,root):
    state=initialize(parent,root);path=root/'first-cross.json'
    study=load(path) or dict(state='running',plan='Six pairs x all four original first JPEG / own-pose header combinations. '
        'Original JPEG bytes unchanged. Every header uses its original recorded evaluation clocks. '
        'Fresh detector session; offline native TCP input only. Not new hit-rate trials.',results=[],maximum_replays=24)
    write(path,study)
    for scene in SCENES:
        for repeat in range(3):
            sources={mode:ROOT/'experiments'/next(p['run_id'] for p in state[key] if p['scene']==scene and p['repeat_id']==repeat)
                     for mode,key in [('ui','reference'),('backend','control')]}
            inputs={mode:first(source) for mode,source in sources.items()}
            for image_mode,header_mode in itertools.product(('ui','backend'),repeat=2):
                case=f'{scene}_{repeat}_image-{image_mode}_header-{header_mode}'
                if any(p['case']==case for p in study['results']):continue
                source=root/'first-cross-inputs'/case;source.mkdir(parents=True)
                ev=source/'session-evidence';ev.mkdir()
                p,r,_=inputs[header_mode];jpeg=inputs[image_mode][2]
                ip=copy.deepcopy(p);rp=copy.deepcopy(r)
                ip.update(jpeg_offset=0,jpeg_bytes=len(jpeg),jpeg_sha256=hashlib.sha256(jpeg).hexdigest())
                (ev/'input.mjpg').write_bytes(jpeg)
                (ev/'events.jsonl').write_text(json.dumps(dict(ip,kind='input'))+'\n'+json.dumps(dict(kind='result',data=rp))+'\n')
                write(source/'manifest.json',load(sources[header_mode]/'manifest.json'))
                write(source/'run-state.json',dict(measurement_start_stamp_ns=ip['data']['stamp_ns']))
                result=run_one(source,root/'first-cross-replays'/case,Path(state['reference_bundle']),seconds=0.,detector_history='session')
                study['results'].append(dict(case=case,scene=scene,repeat_id=repeat,image_mode=image_mode,header_mode=header_mode,
                    image_run=sources[image_mode].name,header_run=sources[header_mode].name,
                    input_sha256=result['primary_input_sha256'],jpeg_sha256=ip['jpeg_sha256'],result=result['first_result']))
                write(path,study)
    causal=[]
    for scene in SCENES:
        for repeat in range(3):
            def get(im,hd):return next(p['result'] for p in study['results'] if (p['scene'],p['repeat_id'],p['image_mode'],p['header_mode'])==(scene,repeat,im,hd))
            a,b,c,d=get('ui','ui'),get('ui','backend'),get('backend','ui'),get('backend','backend')
            def detections(p):return [{k:r[k] for k in ('corners','confidence','name','type','color')} for r in p['observations']]
            assert detections(a)==detections(b) and detections(c)==detections(d)
            same_image_pose=[math.dist(x['track_estimate']['center'],y['track_estimate']['center']) for x,y in [(a,b),(c,d)]]
            same_pose_image=[math.dist(x['track_estimate']['center'],y['track_estimate']['center']) for x,y in [(a,c),(b,d)]]
            causal.append(dict(scene=scene,repeat_id=repeat,detector_invariant_to_header=True,
                detections_changed_with_image=detections(a)!=detections(c),
                center_delta_same_image_changed_own_pose_m=same_image_pose,
                center_delta_same_own_pose_changed_native_image_m=same_pose_image,
                raw_source_center_delta_m=math.dist(a['track_estimate']['center'],d['track_estimate']['center'])))
    study.update(state='completed',causal=causal);write(path,study)


def calibrate(parent,root):
    data=inventory(parent,root);ui=[p for p in data['runs'] if p['mode']=='ui']
    samples=[q for p in ui for q in p['timing_samples']]
    base=load_profile(ROOT/'timing_profiles/ui-fixed-v6.json')
    target=dict(input_delay_ns=round(statistics.median(q['input_ns'] for q in samples)),
        capture_interval_ns=round(statistics.median(p['capture_intervals_ns']['p50'] for p in ui)),
        control_hz=round(statistics.median(p['control_hz'] for p in ui),6),
        processing_delay_ns=round(statistics.median(q['fire_ns'] for q in samples)),
        return_delay_ns=round(statistics.median(q['return_ns'] for q in samples)),
        pose_delay_ns=round(statistics.median(q['pose_ns'] for q in samples)))
    definitions=[]
    stages=[('ui-aligned-v7-delay',('input_delay_ns',)),
            ('ui-aligned-v7-capture',('input_delay_ns','capture_interval_ns')),
            ('ui-aligned-v7-clock',tuple(target))]
    for name,fields in stages:
        p=copy.deepcopy(base);p['name']=name
        for k in fields:p[k]=target[k]
        p['provenance']=dict(parent_profile='ui-fixed-v6',selection='All six frozen current UI runs; median timing only. No score input.',
            input_contract='aligned-v1',source_runs=[dict(run_id=r['run_id'],files=r['source_hashes']) for r in ui],
            intervention_fields=list(fields),limits='Fixed timing still omits per-frame jitter and preparation capture phase. Does not certify initial native image equivalence.')
        validate(p);write(ROOT/'timing_profiles'/(name+'.json'),p);write(root/(name+'.json'),p);definitions.append(p)
    write(root/'calibration.json',dict(target=target,profiles=definitions,
        independent_holdout_required=True,no_aiming_scores_used=True,
        gate_order='First-image cross attribution then timing-only ablations; no claim of UI equivalence from median calibration.'))
    return definitions


def launch(parent,root):
    """Three cumulative timing interventions, fixed original inputs/seeds."""
    state=initialize(parent,root);path=root/'study.json'
    assert load(root/'first-cross.json')['state']=='completed'
    calibration=load(root/'calibration.json')
    with (root/'controller.lock').open('a') as lock:
        import fcntl
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if 'candidate_bundle' not in state:
            with jobs.connect() as db:
                if db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running','pausing')").fetchone()[0]:raise RuntimeError('Drain existing live jobs')
            state['candidate_bundle']=str(freeze_candidate('timing_alignment_'+uuid.uuid4().hex[:12],
                'daedalus:rm26-input-contract-v2','daedalus:rm26-evaluator-input-contract-v2'))
            state.update(state='running',maximum_new_trials=6*len(calibration['profiles']))
            write(path,state)
        for profile in calibration['profiles']:
            name=profile['name'];entry=state['modes'].setdefault(name,dict(runs=[],jobs=[]))
            if not entry['jobs']:
                rows=[]
                for repeat,sensor_seed in enumerate((1701,1702,1703)):
                    for scene in SCENES:
                        options=dict(DEFAULTS,scene=scene,input_contract='aligned-v1',detector_history='session',
                            sensor_seed=sensor_seed,warmup_frames=30,session_evidence='on',
                            execution_mode='lockstep',timing_profile=name,max_fps=240,
                            seed=2026102207,record_timing='off',label='frozen-ui-timing-only')
                        row=jobs.validate({k:v for k,v in options.items() if k in jobs.FIELDS})
                        row.update(repeat_id=repeat,execution_profile='lockstep-'+name);rows.append(row)
                identifier='job_timing_'+uuid.uuid4().hex[:12];now=time.time()
                with jobs.connect() as db,jobs.transaction(db):
                    db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(identifier,'queued',now,now,
                        json.dumps(dict(version=1,_protocol=2,_timing_alignment=True)),state['candidate_bundle'],1,0,None))
                    db.executemany('INSERT INTO items(job_id,ordinal,state,settings) VALUES(?,?,?,?)',
                        [(identifier,i,'pending',json.dumps(p)) for i,p in enumerate(rows)])
                    jobs.event(db,identifier,'Timing-only candidate, frozen UI/model/algorithm; original first-image gate remains separately reported')
                entry['jobs'].append(identifier);write(path,state)
            for identifier in entry['jobs']:
                while True:
                    with jobs.connect() as db:status=jobs.status(db,identifier)
                    if status['state']=='completed':break
                    if status['state'] in ('paused','cancelled'):
                        state.update(state='failed',failed_job=identifier);write(path,state)
                        raise RuntimeError('Failed timing diagnostic retained: '+identifier)
                    time.sleep(1)
                with jobs.connect() as db:ingest(db,identifier,entry);jobs.report(db,identifier)
            # Check the intervention itself before proceeding to another factor.
            checks=[]
            for r in entry['runs']:
                folder=ROOT/'experiments'/r['run_id'];audit=load(folder/'session-evidence-audit.json')
                assert audit['passed'],audit['errors']
                recorded=load(folder/'configuration/timing-profile.json')
                assert recorded==profile
                old=ROOT/'experiments'/next(p['run_id'] for p in state['control'] if (p['scene'],p['repeat_id'])==(r['scene'],r['repeat_id']))
                oldp,oldr,_=first(old);newp,newr,_=first(folder)
                first_same=oldp['jpeg_sha256']==newp['jpeg_sha256']
                if name in ('ui-aligned-v7-delay','ui-aligned-v7-capture'):
                    assert first_same and oldr['result']['observations']==newr['result']['observations'], 'Delivery/cadence must not mutate the earlier first rendered image'
                checks.append(dict(run_id=r['run_id'],scene=r['scene'],repeat_id=r['repeat_id'],
                    full_session_passed=True,profile_bytes_match=True,first_jpeg_same_as_v6=first_same))
            entry['intervention_checks']=checks
            write(path,state)
        from background_pipeline import enqueue
        with jobs.connect() as db:
            for entry in state['modes'].values():
                for identifier in entry['jobs']:enqueue(db,identifier,state['candidate_bundle'],[r['run_id'] for r in entry['runs']])
        while True:
            with jobs.connect() as db:
                media=[a for e in state['modes'].values() for j in e['jobs'] for a in jobs.status(db,j)['artifacts']]
            if any(r['state']=='failed' for r in media):raise RuntimeError('Offline media failure retained')
            if media and all(r['state']=='completed' for r in media):break
            time.sleep(1)
        state.update(state='completed',ended=time.time());write(path,state)


def release(parent,root):
    # Wait only for the parent live diagnostic to finish; never mix candidates
    # or alter a running trial/controller's durable state.
    while load(root/'study.json').get('state')!='completed':
        if load(root/'study.json').get('state')=='failed':raise RuntimeError('Inspect parent timing failure first')
        time.sleep(1)
    target=root/'release-home';initialize(parent,target)
    cross=target/'first-cross.json'
    if not cross.exists():cross.symlink_to(root/'first-cross.json')
    profile=load_profile(ROOT/'timing_profiles/ui-aligned-v7-release.json')
    write(target/'calibration.json',dict(profiles=[profile],
        plan='One source-confirmed preparation-release command only; original v6 timings, six declared scene/seed pairs. '
            'No score tuning, no extra settling time, no change to UI or native simulator image.'))
    launch(parent,target)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['inventory','first-cross','calibrate','launch','release'])
    p.add_argument('--parent',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();{'inventory':inventory,'first-cross':first_cross,'calibrate':calibrate,'launch':launch,'release':release}[args.action](args.parent.resolve(),args.output.resolve())
