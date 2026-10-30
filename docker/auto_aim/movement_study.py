"""Frozen tier-selectable v3/R4 movement capability study; no algorithm tuning."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import time
import uuid

import background_jobs as jobs
import manage
from decision_benchmark import sha,verify_bundle
from decision_benchmark_report import behavior
from movement_scenarios import select,NEW_SCENES,definition,generated
from perception_study import protected,trial_metrics
from report import load,write
from sync_contract_study import settings
from sync_history_study import matched_ui

PARENT=manage.OPS/'experiments/pixel-rotation-replication-20261027'
VARIANTS=('baseline','R4')
UI_FIXED=('adv_variable_pos','adv_turn08_pos','adv_turn04_neg','adv_spin_startstop_neg','adv_pursuit_pos')

def output_path(root):
    path=Path(root)
    if path.is_relative_to('/home/openvino'):path=manage.DESKTOP/path.relative_to('/home/openvino')
    elif not path.is_absolute():path=manage.ROOT/path
    return path.resolve()

def initialize(root,tier='basic',cases=None,pressure=False):
    root=output_path(root);root.mkdir(parents=True,exist_ok=True)
    if (root/'contract.json').exists():
        c=load(root/'contract.json')
        if c['tier']!=tier or c['scenes']!=select(tier,cases,pressure):raise ValueError('Requested tier/cases differ from frozen study; use a new output directory')
        if sha(root/'contract.json')!=load(root/'state.json')['contract_sha256']:raise ValueError('Frozen contract changed')
        if protected()!=c['protected']:raise ValueError('Protected algorithm files changed')
        for b in c['bundles'].values():verify_bundle(Path(b))
        return c
    scenes=select(tier,cases,pressure);pc=load(PARENT/'contract.json');ps=load(PARENT/'state.json')
    if ps['state']!='completed' or not ps['independently_verified']:raise ValueError('Need frozen verified R4')
    seed_offset=0
    with jobs.connect() as db:
        used={json.loads(x[0]).get('sensor_seed') for x in db.execute('SELECT settings FROM items')}
        while {6091+seed_offset,6101+seed_offset,6102+seed_offset,6103+seed_offset}&used:seed_offset+=1000
        if db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running','pausing')").fetchone()[0]:raise ValueError('Drain existing jobs first')
    c=dict(version=1,created=time.time(),study='movement-v1',tier=tier,cases=cases,pressure=pressure,
        scenes=scenes,variants=VARIANTS,candidate='R4',bundles={},protected=protected(),parent=str(PARENT),
        runtime_files={},backend_timing_profile='ui-aligned-v7-clock',probe_seed=6091+seed_offset,sensor_seeds=[6101+seed_offset,6102+seed_offset,6103+seed_offset],
        basic_references=[r for r in ps['runs'] if r['variant'] in VARIANTS and r['phase'] in ('audit','regression')] if tier=='advanced' else [],
        expected_ui_scenes=[s for s in UI_FIXED if s in scenes] if tier=='advanced' else [s for s in scenes if s in ('basic_move3_spin_pos','basic_move4_spin_neg')],
        maximum_complete_trial_s=40,no_algorithm_changes=True,automatic_promotion=False,ui_equivalence_certified=False,
        acceptance='Difficulty, evidence integrity and shooting effects are separate. No old low-difficulty accuracy gate. '
            'Speed and signed angular rate must qualify simultaneously; physically unachieved scenes stay visible. '
            'All registered scores retained, no score-based extension or retry.',
        historical_files={str(PARENT/n):h for n,h in load(PARENT/'report-seal.json').items()},
        original_profile=manage.profile(),suite_source_hashes={p.name:sha(p) for p in (manage.OPS/'movement_scenarios.py',Path(__file__),manage.OPS/'movement_analysis.py',manage.OPS/'movement_study_report.py')})
    label=uuid.uuid4().hex[:8]
    for v in VARIANTS:
        base=Path(pc['bundles'][v]);dst=jobs.STATE/'bundles'/('movement_v1_'+v+'_'+label);shutil.copytree(base,dst,copy_function=os.link)
        meta=load(dst/'bundle.json')
        for source in manage.OPS.iterdir():
            if source.is_file() and source.suffix in ('.py','.yml','.xml','.json','.toml','.Dockerfile'):
                target=dst/'docker/auto_aim'/source.name;target.unlink(missing_ok=True);shutil.copy2(source,target)
                meta['files']['docker/auto_aim/'+source.name]=sha(target)
        meta.update(bundle_id=dst.name,parent_bundle=str(base),created=time.time(),algorithm_unchanged=True)
        (dst/'bundle.json').unlink();write(dst/'bundle.json',meta);verify_bundle(dst);c['bundles'][v]=str(dst)
        if v=='baseline':c['runtime_files']={k:h for k,h in meta['files'].items() if k.startswith('docker/auto_aim/')}
        if meta['files']['sp_vision_25/build/simulator/sim_auto_aim']!=load(base/'bundle.json')['files']['sp_vision_25/build/simulator/sim_auto_aim']:raise ValueError('Binary changed')
    c['scenario_plans']={s:generated(s,2026102207)[1] for s in scenes if s in NEW_SCENES}
    write(root/'contract.json',c);write(root/'state.json',dict(state='frozen',contract_sha256=sha(root/'contract.json'),runs=[],jobs=[],parallelism=1,parallelism_by_scene={}))
    return c

def submit(root,c,s,phase,variant,scene,seed,level=1):
    prior=next((x for x in s['jobs'] if (x['phase'],x['variant'],x['scene'],x['sensor_seed'])==(phase,variant,scene,seed)),None)
    if prior:return prior
    opts=dict(settings(scene,seed),execution_mode='realtime',timing_profile=None,max_fps=240,stage='screen',evidence='full',label='movement-'+phase+'-'+variant)
    row=jobs.validate({k:v for k,v in opts.items() if k in jobs.FIELDS})
    row.update(execution_mode='lockstep',timing_profile=c['backend_timing_profile'],algorithm_lifecycle='fresh',execution_profile='lockstep-'+c['backend_timing_profile'],repeat_id=0)
    name='job_movement_'+uuid.uuid4().hex[:12];now=time.time()
    with jobs.connect() as db,jobs.transaction(db):
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(name,'queued',now,now,json.dumps(dict(version=1,_protocol=2,_motion_regression=True,study=str(root),parallelism=level,scope='Study-local movement tier only; no global capacity/fidelity qualification')),c['bundles'][variant],level,int(level>1),None))
        db.execute('INSERT INTO items(job_id,ordinal,state,settings) VALUES(?,?,?,?)',(name,0,'pending',json.dumps(row)))
        jobs.event(db,name,'Frozen movement tier; original v3 and R4 unchanged; keep low-score and difficulty failures')
    e=dict(id=name,phase=phase,variant=variant,scene=scene,sensor_seed=seed,repeat_id=0,level=level)
    s['jobs'].append(e);write(root/'state.json',s);return e

def collect(root,c,s,entries):
    for e in entries:
        if any(r.get('job_id')==e['id'] for r in s['runs']):continue
        deadline=time.monotonic()+1200
        while True:
            with jobs.connect() as db:status=jobs.status(db,e['id'])
            if status['state']=='completed':break
            if status['state'] in ('paused','cancelled') or time.monotonic()>deadline:raise RuntimeError('Inspect retained task '+e['id'])
            time.sleep(1)
        with jobs.connect() as db:
            attempts=[dict(r) for r in db.execute('SELECT a.* FROM attempts a JOIN items i ON a.item_id=i.id WHERE i.job_id=?',(e['id'],))]
        if len(attempts)!=1 or attempts[0]['verdict']=='invalid':raise ValueError('Unexpected duplicate/invalid attempt retained')
        entry=dict(e,job_id=e['id'],engine='backend',run_id=attempts[0]['run_id'],attempt=attempts[0])
        row=trial_metrics(root,entry,c)
        if row['wall_s']>40:raise RuntimeError('Full trial exceeded 40 seconds; retain evidence')
        s['runs'].append(row);write(root/'state.json',s)
        print(json.dumps(dict(done=len(s['runs']),phase=e['phase'],scene=e['scene'],variant=e['variant'],wall_s=row['wall_s'])),flush=True)

def probe(root,c,s):
    # Single-lane R4 is also the physical trajectory preflight. Scores never determine inclusion.
    for scene in c['scenes']:
        for v in VARIANTS:
            e=submit(root,c,s,'probe_p1',v,scene,c['probe_seed']);collect(root,c,s,[e])
    entries=[submit(root,c,s,'probe_p2',v,scene,c['probe_seed'],2) for scene in c['scenes'] for v in VARIANTS]
    collect(root,c,s,entries);result=[]
    for scene in c['scenes']:
        for v in VARIANTS:
            a,b=[next(r for r in s['runs'] if r['phase']==p and r['variant']==v and r['scene']==scene) for p in ('probe_p1','probe_p2')]
            result.append(dict(scene=scene,variant=v,same_scores=all(a[k]==b[k] for k in ('shots','hits','weight')),
                same_behavior=behavior(manage.OPS/'experiments'/a['run_id'])==behavior(manage.OPS/'experiments'/b['run_id']),
                within_budget=max(a['wall_s'],b['wall_s'])<=40))
    for scene in c['scenes']:
        rr=[x for x in result if x['scene']==scene]
        attempts=[r['attempt'] for r in s['runs'] if r['phase']=='probe_p2' and r['scene']==scene]
        overlap=len(attempts)==2 and attempts[0]['slot']!=attempts[1]['slot'] and attempts[0]['started']<attempts[1]['ended'] and attempts[1]['started']<attempts[0]['ended']
        s['parallelism_by_scene'][scene]=2 if overlap and all(x['same_scores'] and x['same_behavior'] and x['within_budget'] for x in rr) else 1
    write(root/'parallel-result.json',dict(pairs=result,parallelism_by_scene=s['parallelism_by_scene'],global_qualification=False))
    write(root/'state.json',s)

def ui(root,c,s):
    if s.get('current_ui_case'):raise ValueError('Interrupted UI case retained')
    import experiments
    subprocess=__import__('subprocess');subprocess.run(['python3',str(manage.ROOT/'docker/daedalus/display_auth.py')],check=True)
    with (manage.RUNTIME/'operation.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            for i,scene in enumerate(c['expected_ui_scenes']):
                for v in VARIANTS if i%2==0 else VARIANTS[::-1]:
                    if any(r['phase']=='ui' and r['scene']==scene and r['variant']==v for r in s['runs']):continue
                    case=dict(phase='ui',engine='ui',variant=v,scene=scene,sensor_seed=c['sensor_seeds'][0],repeat_id=0)
                    s['current_ui_case']=case;write(root/'state.json',s);started=time.monotonic()
                    with matched_ui(Path(c['bundles'][v])):
                        try:
                            manage.stop();manage.start(argparse.Namespace(**dict(settings('static',case['sensor_seed']),control='off',view='off',replace=False,fire='auto')))
                            folder=experiments.run_one(manage,scene,argparse.Namespace(**dict(settings(scene,case['sensor_seed']),repeat_id=0,label='movement-ui-'+v)))
                        finally:manage.stop()
                    row=trial_metrics(root,dict(case,run_id=folder.name,complete_wall_s=time.monotonic()-started),c)
                    s['runs'].append(row);s.pop('current_ui_case',None);write(root/'state.json',s)
                    if row['complete_wall_s']>40:raise RuntimeError('Full UI case exceeded 40 seconds')
                    print(json.dumps(dict(ui_done=True,scene=scene,variant=v,wall_s=row['complete_wall_s'])),flush=True)
        finally:manage.save_profile(c['original_profile'])

def run(root,tier='basic',cases=None,pressure=False):
    root=output_path(root);c=initialize(root,tier,cases,pressure)
    with (root/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);s=load(root/'state.json')
        if s['state']=='completed':return
        try:
            s['state']='running';s.pop('error',None);write(root/'state.json',s)
            if not (root/'parallel-result.json').exists():probe(root,c,s)
            for scene in c['scenes']:
                # Unqualified scenes run serially; qualified scenes only overlap with each other.
                entries=[]
                for seed in c['sensor_seeds']:
                    for v in VARIANTS if seed%2 else VARIANTS[::-1]:
                        e=submit(root,c,s,'audit',v,scene,seed,s['parallelism_by_scene'][scene]);entries.append(e)
                        if s['parallelism_by_scene'][scene]==1:collect(root,c,s,[e])
                collect(root,c,s,entries)
            # Basic regression remains separate, not mixed into advanced aggregate scores.
            if c['tier']=='advanced':
                # Finish any already-created supplementary legacy case before narrowing future work.
                for existing in [e for e in s['jobs'] if e['phase']=='basic_regression']:
                    collect(root,c,s,[existing])
                for scene in [x for x in select('basic') if x in NEW_SCENES]:
                    entries=[]
                    for seed in c['sensor_seeds']:
                        for v in VARIANTS:
                            e=submit(root,c,s,'basic_regression',v,scene,seed,1);entries.append(e);collect(root,c,s,[e])
            ui(root,c,s)
            s.update(state='acquisition_complete',ended=time.time());write(root/'state.json',s)
            from movement_study_report import finish
            finish(root)
        except BaseException as error:
            s.update(state='failed',error=repr(error));write(root/'state.json',s);raise

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--tier',choices=('basic','advanced'),default='basic')
    p.add_argument('--cases');p.add_argument('--pressure',action='store_true');p.add_argument('--list',action='store_true')
    p.add_argument('--output',type=Path)
    a=p.parse_args()
    if a.list:print(json.dumps(select(a.tier,a.cases,a.pressure),ensure_ascii=False,indent=2))
    elif not a.output:p.error('--output required for acquisition')
    else:run(a.output,a.tier,a.cases,a.pressure)
if __name__=='__main__':main()
