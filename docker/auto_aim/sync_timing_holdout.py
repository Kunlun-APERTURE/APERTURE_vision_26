"""Independent, fixed five-seed effect check of the timing-calibrated profile."""
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
from sync_contract_study import settings
from sync_history_study import matched_ui
from sync_study import ingest

SCENES=('hf_uniform_3','hf_turn90_3')
SEEDS=(1704,1705,1706,1707,1708)


def run(root):
    with (root/'holdout.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        path=root/'holdout.json';state=load(path)
        if not state:
            parent=load(root/'study.json');release=load(root/'release-home/study.json')
            assert parent['state']==release['state']=='completed'
            state=dict(state='planned',reference_bundle=parent['reference_bundle'],bundle=parent['candidate_bundle'],
                timing_profile='ui-aligned-v7-clock',seeds=SEEDS,created=time.time(),modes=dict(ui=dict(runs=[]),backend=dict(runs=[],jobs=[])),
                maximum_trials=20,maximum_pairs_per_scene=5,capacity_registration=False,certified=False,
                plan='Exactly five new sensor seeds1704..1708, two scenes, frozen UI vs full timing-only calibrated model. '
                    'The full model is selected by timing definition, not calibration scores. '
                    'No release-only mixing, score-based reruns, or sample extension. '
                    'Original +/-5pp UI gates, firing +/-5%, tracking +/-5pp, loss +/-.2s, >=30shots/side. '
                    'First-image mismatch remains separately failed even if aggregate scores happen to agree.')
            write(path,state)
        ui=state['modes']['ui']['runs']
        if len(ui)<10:
            with matched_ui(Path(state['reference_bundle'])),(manage.RUNTIME/'operation.lock').open('a') as operation:
                fcntl.flock(operation,fcntl.LOCK_EX|fcntl.LOCK_NB)
                try:
                    for i,seed in enumerate(SEEDS):
                        for scene in SCENES if i%2==0 else SCENES[::-1]:
                            if any(p['scene']==scene and p['repeat_id']==i for p in ui):continue
                            manage.stop()
                            manage.start(argparse.Namespace(**dict(settings('static',seed),control='off',view='off',replace=False,fire='auto')))
                            args=argparse.Namespace(**dict(settings(scene,seed),repeat_id=i,label='timing-independent-holdout'))
                            folder=experiments.run_one(manage,scene,args)
                            ui.append(dict(run_id=folder.name,scene=scene,repeat_id=i,seed=2026102207,sensor_seed=seed))
                            write(path,state)
                            if load(folder/'summary.json')['verdict']=='invalid':raise RuntimeError('Invalid independent UI retained: '+folder.name)
                finally:
                    manage.stop()
                    profile=manage.profile();profile.update(scene='static',terrain='arena',control='off',fire='off',view='off',
                        algorithm_profile='legacy',gimbal_profile='legacy',detector_history='process',max_fps=None,
                        record_timing='off',input_contract='legacy',sensor_seed=None,warmup_frames=30)
                    manage.save_profile(profile)
        entry=state['modes']['backend']
        if not entry['jobs']:
            rows=[]
            for i,seed in enumerate(SEEDS):
                for scene in SCENES:
                    opts=dict(settings(scene,seed),execution_mode='lockstep',timing_profile=state['timing_profile'],max_fps=240,label='timing-independent-holdout')
                    row=jobs.validate({k:v for k,v in opts.items() if k in jobs.FIELDS})
                    row.update(repeat_id=i,execution_profile='lockstep-'+state['timing_profile']);rows.append(row)
            identifier='job_timing_holdout_'+uuid.uuid4().hex[:12];now=time.time()
            with jobs.connect() as db,jobs.transaction(db):
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(identifier,'queued',now,now,
                    json.dumps(dict(version=1,_protocol=2,_timing_holdout=True)),state['bundle'],1,0,None))
                db.executemany('INSERT INTO items(job_id,ordinal,state,settings) VALUES(?,?,?,?)',
                    [(identifier,i,'pending',json.dumps(row)) for i,row in enumerate(rows)])
                jobs.event(db,identifier,'Five independent predeclared sensor-seed pairs; no score tuning or capacity registration')
            entry['jobs'].append(identifier);write(path,state)
        for identifier in entry['jobs']:
            while True:
                with jobs.connect() as db:status=jobs.status(db,identifier)
                if status['state']=='completed':break
                if status['state'] in ('paused','cancelled'):raise RuntimeError('Inspect retained independent backend failure: '+identifier)
                time.sleep(1)
            with jobs.connect() as db:ingest(db,identifier,entry);jobs.report(db,identifier)
        from background_pipeline import enqueue
        with jobs.connect() as db:
            for identifier in entry['jobs']:enqueue(db,identifier,state['bundle'],[p['run_id'] for p in entry['runs']])
        while True:
            with jobs.connect() as db:media=[p for identifier in entry['jobs'] for p in jobs.status(db,identifier)['artifacts']]
            if any(p['state']=='failed' for p in media):raise RuntimeError('Offline evidence failure retained')
            if media and all(p['state']=='completed' for p in media):break
            time.sleep(1)
        state.update(state='completed',ended=time.time());write(path,state)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();run(a.output.resolve())
