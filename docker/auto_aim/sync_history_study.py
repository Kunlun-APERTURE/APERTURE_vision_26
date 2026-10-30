"""Eight bounded live confirmations of the explicit detector-history contract."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import time
from types import SimpleNamespace
import uuid

import background_jobs as jobs
import experiments
import manage
from report import load,write
from sync_study import DEFAULTS,ingest

SCENES=('hf_uniform_3','hf_turn90_3')


@contextlib.contextmanager
def matched_ui(bundle):
    """Use the SAME candidate simulator/model/binary as the backend diagnostic."""
    meta=load(bundle/'bundle.json')
    relative='.local/share/rm26-sim/detector-history-reference/'+bundle.name+'/sp_vision_25'
    reference=manage.DESKTOP/relative
    reference.mkdir(parents=True,exist_ok=True)
    def copy_payload(source,target):
        Path(target).unlink(missing_ok=True)
        return shutil.copy2(source,target)
    for name in ('assets','configs'):shutil.copytree(bundle/'sp_vision_25'/name,reference/name,dirs_exist_ok=True,copy_function=copy_payload)
    binary=reference/'build/simulator/sim_auto_aim';binary.parent.mkdir(parents=True,exist_ok=True)
    if binary.exists():binary.unlink()
    shutil.copy2(bundle/'sp_vision_25/build/simulator/sim_auto_aim',binary)
    names=('RM26_SIMULATOR_IMAGE','RM26_BRIDGE_IMAGE','RM26_ALGORITHM_ROOT')
    previous={name:os.environ.get(name) for name in names}
    os.environ.update(RM26_SIMULATOR_IMAGE=meta['images']['simulator'],RM26_BRIDGE_IMAGE=meta['images']['simulator'],
        RM26_ALGORITHM_ROOT='/home/openvino/'+relative)
    original_compose=manage.compose;original_inspect=manage.inspect
    overlay=manage.RUNTIME/'history-evaluator-overlay.json'
    write(overlay,dict(services=dict(evaluator=dict(image=meta['images']['evaluator']))))
    manage.compose=lambda *args,**kwargs:original_compose('--file',str(overlay),*args,**kwargs)
    manage.inspect=lambda name:original_inspect(meta['images']['evaluator'] if name=='daedalus:rm26-evaluator' else name)
    def snapshot(folder,scene,args):
        proxy=SimpleNamespace(ROOT=bundle,OPS=bundle/'docker/auto_aim',RUNTIME=manage.RUNTIME,
            inspect=manage.inspect,profile=manage.profile,run=manage.run,SIMULATOR=manage.SIMULATOR,
            BRIDGE=manage.BRIDGE,FROZEN_REPOSITORIES=meta['repositories'])
        result=experiments.snapshot(proxy,folder,scene,args)
        runtime=reference/'build/sim/calibration.yaml'
        if runtime.exists():shutil.copy2(runtime,folder/'configuration/runtime-calibration.yaml')
        shutil.copy2(bundle/'bundle.json',folder/'configuration/bundle.json')
        result['reference_kind']='matched_candidate_ui_session_reset'
        result['configuration_sha256']={str(p.relative_to(folder/'configuration')):experiments.digest(p)
            for p in (folder/'configuration').rglob('*') if p.is_file()}
        write(folder/'manifest.json',result);return result
    manage.snapshot=snapshot
    try:yield
    finally:
        delattr(manage,'snapshot');manage.compose=original_compose;manage.inspect=original_inspect
        for name,value in previous.items():
            if value is None:os.environ.pop(name,None)
            else:os.environ[name]=value


def ui(root,state):
    path=root/'history-live.json';entries=state['modes'].setdefault('ui_session',dict(runs=[]))['runs']
    if len(entries)==6:return
    with matched_ui(Path(state['bundle'])),(manage.RUNTIME/'operation.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            manage.stop()
            settings=dict(DEFAULTS,scene='static',control='off',view='off',replace=False,fire='auto',detector_history='session')
            manage.start(argparse.Namespace(**settings))
            for repeat in range(3):
                for scene in (SCENES if repeat%2==0 else tuple(reversed(SCENES))):
                    if any(r['scene']==scene and r['repeat_id']==repeat for r in entries):continue
                    args=argparse.Namespace(**dict(DEFAULTS,scene=scene,seed=2026102207+repeat%3,repeat_id=repeat,
                        label='detector-history-session-ui',detector_history='session',session_evidence='on'))
                    folder=experiments.run_one(manage,scene,args)
                    entry=dict(run_id=folder.name,scene=scene,repeat_id=repeat,seed=args.seed)
                    entries.append(entry);write(path,state)
                    if load(folder/'summary.json')['verdict']=='invalid':raise RuntimeError('Invalid diagnostic retained: '+folder.name)
        finally:
            manage.stop()
            profile=manage.profile();profile.update(scene='static',terrain='arena',control='off',fire='off',view='off',
                algorithm_profile='legacy',gimbal_profile='legacy',detector_history='process',max_fps=None,record_timing='off')
            manage.save_profile(profile)


def backend(root,state):
    entry=state['modes'].setdefault('lockstep_session',dict(runs=[],jobs=[]))
    if len(entry['runs'])==2:return
    if not entry['jobs']:
        rows=[]
        for scene in SCENES:
            settings=dict(DEFAULTS,scene=scene,max_fps=240,execution_mode='lockstep',timing_profile='ui-fixed-v6',
                detector_history='session',session_evidence='on',seed=2026102207,record_timing='off',label='detector-history-session-backend')
            row=jobs.validate({k:v for k,v in settings.items() if k in jobs.FIELDS})
            row.update(repeat_id=0,execution_profile='lockstep-ui-fixed-v6');rows.append(row)
        identifier='job_history_'+uuid.uuid4().hex[:12];now=time.time()
        with jobs.connect() as db,jobs.transaction(db):
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(identifier,'queued',now,now,
                json.dumps(dict(version=1,_protocol=2,_history_diagnostic=True)),state['bundle'],1,0,None))
            db.executemany('INSERT INTO items(job_id,ordinal,state,settings) VALUES(?,?,?,?)',
                [(identifier,i,'pending',json.dumps(r)) for i,r in enumerate(rows)])
            jobs.event(db,identifier,'Detector-history session reset; not a new fidelity qualification')
        entry['jobs'].append(identifier);write(root/'history-live.json',state)
    for identifier in entry['jobs']:
        while True:
            with jobs.connect() as db:status=jobs.status(db,identifier)
            if status['state']=='completed':break
            if status['state'] in ('paused','cancelled'):raise RuntimeError('Inspect failed diagnostic: '+identifier)
            time.sleep(1)
        with jobs.connect() as db:ingest(db,identifier,entry);jobs.report(db,identifier)
    write(root/'history-live.json',state)


def run(root):
    with (root/'live-controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        path=root/'history-live.json';state=load(path)
        if not state:
            state=dict(state='running',bundle=load(root/'candidate.json')['bundle'],modes={},
                plan='UI: two scenes x three runs. Backend: same two scenes x one run. Same simulator/model/algorithm; explicit session cache reset.',
                certified=False,maximum_trials=8,capacity_registration=False)
            write(path,state)
        try:
            ui(root,state);backend(root,state)
            state.update(state='completed',ended=time.time());write(path,state)
        except BaseException as error:
            state.update(state='failed',error=type(error).__name__+': '+str(error));write(path,state);raise


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args();run(args.output.resolve())
