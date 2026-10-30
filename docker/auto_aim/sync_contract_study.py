"""Frozen acceptance of physical boundaries and the explicit input contract."""
import argparse
import fcntl
import json
from pathlib import Path
import time
import uuid

import background_jobs as jobs
import experiments
import manage
from report import load,write
from sync_diagnosis import freeze_candidate
from sync_history_study import matched_ui
from sync_study import DEFAULTS,ingest

SCENES=('hf_uniform_3','hf_turn90_3')
SENSOR_SEEDS=(1701,1702,1703)


def settings(scene,seed):
    return dict(DEFAULTS,scene=scene,detector_history='session',session_evidence='on',
        input_contract='aligned-v1',sensor_seed=seed,warmup_frames=30,
        seed=2026102207,record_timing='off',label='input-contract-v1')


def background(root,state,pilot=False):
    key='pilot_backend' if pilot else 'backend'
    entry=state['modes'].setdefault(key,dict(runs=[],jobs=[]))
    if not entry['jobs']:
        rows=[]
        conditions=[(SCENES[0],SENSOR_SEEDS[0],0)] if pilot else [
            (scene,seed,i) for i,seed in enumerate(SENSOR_SEEDS) for scene in SCENES]+[
            (scene,SENSOR_SEEDS[0],3) for scene in SCENES]
        for scene,seed,repeat in conditions:
            value=settings(scene,seed)
            value.update(execution_mode='lockstep',timing_profile='ui-fixed-v6',max_fps=240)
            row=jobs.validate({k:v for k,v in value.items() if k in jobs.FIELDS})
            row.update(repeat_id=repeat,execution_profile='lockstep-ui-fixed-v6')
            rows.append(row)
        identifier='job_contract_'+uuid.uuid4().hex[:12];now=time.time()
        with jobs.connect() as db,jobs.transaction(db):
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(identifier,'queued',now,now,
                json.dumps(dict(version=1,_protocol=2,_contract_validation=True)),state['bundle'],1,0,None))
            db.executemany('INSERT INTO items(job_id,ordinal,state,settings) VALUES(?,?,?,?)',
                [(identifier,i,'pending',json.dumps(row)) for i,row in enumerate(rows)])
            jobs.event(db,identifier,'Physical stage / counted warmup / native sensor sequence acceptance; no fidelity qualification')
        entry['jobs'].append(identifier);write(root/'contract-study.json',state)
    for identifier in entry['jobs']:
        while True:
            with jobs.connect() as db:status=jobs.status(db,identifier)
            if status['state']=='completed':break
            if status['state'] in ('paused','cancelled'):raise RuntimeError('Inspect retained failed validation: '+identifier)
            time.sleep(1)
        with jobs.connect() as db:ingest(db,identifier,entry);jobs.report(db,identifier)
    from background_pipeline import enqueue
    for identifier in entry['jobs']:
        with jobs.connect() as db:
            enqueue(db,identifier,state['bundle'],[r['run_id'] for r in entry['runs']])
    # Finish CPU-bound offline rendering before the real-time UI reference.
    # The source trials and their scores are unchanged by this wait.
    while True:
        with jobs.connect() as db:
            media=[r for identifier in entry['jobs'] for r in jobs.status(db,identifier)['artifacts']]
        if any(r['state']=='failed' for r in media):raise RuntimeError('Offline media failed; retain evidence and inspect')
        if media and all(r['state']=='completed' for r in media):break
        time.sleep(1)
    write(root/'contract-study.json',state)


def ui(root,state,pilot=False):
    key='pilot_ui' if pilot else 'ui'
    entry=state['modes'].setdefault(key,dict(runs=[]))
    conditions=[(SCENES[0],SENSOR_SEEDS[0],0)] if pilot else [
        (scene,seed,i) for i,seed in enumerate(SENSOR_SEEDS) for scene in (SCENES if i%2==0 else SCENES[::-1])]
    with matched_ui(Path(state['bundle'])),(manage.RUNTIME/'operation.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            for scene,seed,repeat in conditions:
                if any(r['scene']==scene and r['repeat_id']==repeat for r in entry['runs']):continue
                manage.stop()
                args=argparse.Namespace(**dict(settings(scene,seed),repeat_id=repeat))
                manage.start(argparse.Namespace(**dict(settings('static',seed),control='off',view='off',
                    replace=False,fire='auto')))
                folder=experiments.run_one(manage,scene,args)
                entry['runs'].append(dict(run_id=folder.name,scene=scene,repeat_id=repeat,seed=2026102207,sensor_seed=seed))
                write(root/'contract-study.json',state)
                if load(folder/'summary.json')['verdict']=='invalid':raise RuntimeError('Invalid UI validation retained: '+folder.name)
        finally:
            manage.stop()
            profile=manage.profile();profile.update(scene='static',terrain='arena',control='off',fire='off',view='off',
                algorithm_profile='legacy',gimbal_profile='legacy',detector_history='process',max_fps=None,
                record_timing='off',input_contract='legacy',sensor_seed=None,warmup_frames=30)
            manage.save_profile(profile)


def run(root,action):
    root.mkdir(parents=True,exist_ok=True)
    with (root/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        path=root/'contract-study.json';state=load(path)
        if not state:
            with jobs.connect() as db:
                if db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running','pausing')").fetchone()[0]:raise RuntimeError('Drain live tasks first')
            state=dict(state='freezing',created=time.time(),modes={},
                plan='Two one-trial pipeline pilots, then UI/backend two scenes x three predefined sensor seeds; '
                    'backend seed1701 repeated twice. 16 trials maximum on this frozen candidate. '
                    'No score-based exclusions or timing tuning; originals unchanged.',
                certified=False,capacity_registration=False)
            state['bundle']=str(freeze_candidate('input_contract_'+uuid.uuid4().hex[:12],
                'daedalus:rm26-input-contract-v2','daedalus:rm26-evaluator-input-contract-v2'))
            write(path,state)
        try:
            state.update(state='running',action=action);write(path,state)
            if action=='pilot-backend':background(root,state,True)
            elif action=='pilot-ui':ui(root,state,True)
            elif action=='all':background(root,state);ui(root,state)
            state.update(state='completed' if action=='all' else 'pilot_completed',ended=time.time());write(path,state)
        except BaseException as error:
            state.update(state='failed',error=type(error).__name__+': '+str(error),ended=time.time());write(path,state);raise


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    p.add_argument('--action',choices=['pilot-backend','pilot-ui','all'],required=True)
    args=p.parse_args();run(args.output.resolve(),args.action)
