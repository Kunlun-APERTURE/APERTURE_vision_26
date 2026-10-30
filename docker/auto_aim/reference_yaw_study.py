"""Frozen diagnostic of public reference yaw dynamics; no algorithm changes."""
import argparse,fcntl,json,os,shutil,time,uuid
from pathlib import Path
import background_jobs as jobs
import manage
from report import load,write
from decision_benchmark import sha,verify_bundle
from decision_benchmark_report import behavior
from perception_study import protected,trial_metrics
from sync_contract_study import settings
from sync_history_study import matched_ui
import movement_study
ROOT=manage.OPS/'experiments/reference-yaw-20261028'
HISTORY=manage.OPS/'experiments/movement-advanced-20261027-r2'
SCENES=('hf_static_5','adv_variable_pos','adv_spin_startstop_neg','adv_pursuit_pos')
VARIANTS=('baseline','R4')
ENVS={'legacy':('legacy','ui-aligned-v7-clock'),'refnominal':('rm2017-yaw-unitdc','ui-aligned-v7-clock'),'refdelay20':('rm2017-yaw-unitdc','reference-yaw-return20')}
def initialize():
    ROOT.mkdir(exist_ok=True)
    if (ROOT/'contract.json').exists():
        c=load(ROOT/'contract.json');assert sha(ROOT/'contract.json')==load(ROOT/'state.json')['contract_sha256'];assert protected()==c['protected']
        for p,h in c['source_hashes'].items():assert sha(manage.ROOT/p)==h, p
        assert sha(ROOT/'component-checks.json')==c['component_checks_sha256']
        for p,h in c['source_archives'].items():assert sha(ROOT/p)==h,p
        for p,h in c['historical_hashes'].items():assert sha(Path(p))==h,p
        for p in c['bundles'].values():verify_bundle(Path(p))
        return c
    checks=load(ROOT/'component-checks.json');assert checks['kernel_pass'] and checks['integration_pass']
    from gimbal_dynamics import description
    assert description('rm2017-yaw-unitdc')['coefficient_sha256']==load(ROOT/'sources/model.json')['coefficient_sha256']
    image=manage.inspect('daedalus:rm26-reference-yaw-20261028');assert image and image['Config']['Labels']['daedalus.cargo_features']=='ros2 bench'
    history=load(HISTORY/'state.json');hc=load(HISTORY/'contract.json');assert history['state']=='completed'
    profile=load(manage.OPS/'timing_profiles/ui-aligned-v7-clock.json');profile.update(name='reference-yaw-return20',return_delay_ns=profile.get('return_delay_ns',0)+20_000_000)
    profile['provenance']=dict(parent='ui-aligned-v7-clock',assumption='Additional 20 ms return transport sensitivity; all other timing unchanged')
    write(manage.OPS/'timing_profiles/reference-yaw-return20.json',profile)
    references=[next(r for r in history['runs'] if r['phase']=='audit' and r['scene']==scene and r['variant']==v) for scene in ('adv_variable_pos','adv_pursuit_pos') for v in VARIANTS]
    offset=0
    with jobs.connect() as db:
        used={json.loads(r[0]).get('sensor_seed') for r in db.execute('SELECT settings FROM items')}
    while {290091+offset,290101+offset,290102+offset,290103+offset}&used:offset+=1000
    c=dict(version=1,study='reference-yaw-diagnostic',created=time.time(),candidate='R4',bundles={},runtime_files={},protected=protected(),original_profile=manage.profile(),scenes=SCENES,environments=ENVS,variants=VARIANTS,compatibility=references,probe_seed=290091+offset,sensor_seeds=[290101+offset,290102+offset,290103+offset],maximum_complete_trial_s=40,expected_counts=dict(compatibility=4,probe=48,audit=72,ui=8),automatic_promotion=False,no_algorithm_changes=True,ui_equivalence_certified=False,assumptions=['Unit DC normalization G/G(0) for yaw velocity only','20 ms extra return communication delay sensitivity only','Public 2017 coefficients are not identification of this robot'],source_model=load(ROOT/'sources/model.json'),component_checks_sha256=sha(ROOT/'component-checks.json'))
    for v in VARIANTS:
        base=Path(hc['bundles'][v]);target=jobs.STATE/'bundles'/('reference_yaw_'+v+'_'+uuid.uuid4().hex[:8]);shutil.copytree(base,target,copy_function=os.link);meta=load(target/'bundle.json')
        for src in manage.OPS.iterdir():
            if src.is_file() and src.suffix in ('.py','.yml','.xml','.json','.toml','.Dockerfile'):
                dst=target/'docker/auto_aim'/src.name;dst.unlink(missing_ok=True);shutil.copy2(src,dst);meta['files']['docker/auto_aim/'+src.name]=sha(dst)
        for src in (manage.ROOT/'vision_simulator_26/src').rglob('*.rs'):
            rel=src.relative_to(manage.ROOT);dst=target/rel;dst.parent.mkdir(exist_ok=True,parents=True);dst.unlink(missing_ok=True);shutil.copy2(src,dst);meta['files'][str(rel)]=sha(dst)
        for src in (manage.OPS/'timing_profiles').glob('*.json'):
            rel=src.relative_to(manage.ROOT);dst=target/rel;dst.parent.mkdir(exist_ok=True,parents=True);dst.unlink(missing_ok=True);shutil.copy2(src,dst);meta['files'][str(rel)]=sha(dst)
        meta.update(bundle_id=target.name,parent_bundle=str(base),algorithm_unchanged=True);meta['images']['simulator']=image['Id']
        (target/'bundle.json').unlink();write(target/'bundle.json',meta);verify_bundle(target);c['bundles'][v]=str(target)
        assert meta['files']['sp_vision_25/build/simulator/sim_auto_aim']==load(base/'bundle.json')['files']['sp_vision_25/build/simulator/sim_auto_aim']
        if v=='baseline':c['runtime_files']={k:h for k,h in meta['files'].items() if k.startswith('docker/auto_aim/')}
    c['source_hashes']={str(p.relative_to(manage.ROOT)):sha(p) for p in [*manage.OPS.glob('*.py'),*(manage.ROOT/'vision_simulator_26/src').rglob('*.rs')]}
    c['source_archives']={str(p.relative_to(ROOT)):sha(p) for p in (ROOT/'sources').iterdir() if p.is_file()}
    c['historical_hashes']={str(HISTORY/n):sha(HISTORY/n) for n in ('contract.json','state.json','report-seal.json')}
    write(ROOT/'contract.json',c);write(ROOT/'state.json',dict(state='frozen',contract_sha256=sha(ROOT/'contract.json'),runs=[],jobs=[],parallelism_by_environment={}))
    return c

def submit(c,s,phase,env,v,scene,seed,level=1):
    key=(phase,env,v,scene,seed);e=next((j for j in s['jobs'] if tuple(j[k] for k in ('phase','environment','variant','scene','sensor_seed'))==key),None)
    if e:return e
    dynamics,timing=ENVS[env];opts=dict(settings(scene,seed),gimbal_dynamics=dynamics,execution_mode='realtime',timing_profile=None,max_fps=240,stage='screen',evidence='full',label='refyaw-'+phase+'-'+env+'-'+v)
    row=jobs.validate({k:v for k,v in opts.items() if k in jobs.FIELDS});row.update(execution_mode='lockstep',timing_profile=timing,algorithm_lifecycle='fresh',execution_profile='lockstep-'+timing,repeat_id=0)
    name='job_refyaw_'+uuid.uuid4().hex[:12];now=time.time()
    with jobs.connect() as db,jobs.transaction(db):
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(name,'queued',now,now,json.dumps(dict(version=1,_protocol=2,_motion_regression=True,_stop_on_invalid=True,study=str(ROOT),parallelism=level,scope='Reference yaw study only')),c['bundles'][v],level,int(level>1),None))
        db.execute('INSERT INTO items(job_id,ordinal,state,settings) VALUES(?,?,?,?)',(name,0,'pending',json.dumps(row)))
        jobs.event(db,name,'Frozen reference yaw diagnostic: retain low scores and failed motion difficulty')
    e=dict(id=name,phase=phase,environment=env,variant=v,scene=scene,sensor_seed=seed,repeat_id=0,level=level);s['jobs'].append(e);write(ROOT/'state.json',s);return e

def collect(c,s,entries):
    movement_study.collect(ROOT,c,s,entries)
    for e in entries:
        r=next(r for r in s['runs'] if r.get('job_id')==e['id']);a=r['attempt'];r['complete_wall_s']=a['ended']-a['started']
        write(ROOT/'state.json',s)
        if r['complete_wall_s']>40:raise RuntimeError('Complete background attempt exceeded 40 seconds')

def compatibility(c,s):
    checks=[]
    for old in c['compatibility']:
        e=submit(c,s,'compatibility','legacy',old['variant'],old['scene'],old['sensor_seed']);collect(c,s,[e]);r=next(r for r in s['runs'] if r.get('job_id')==e['id'])
        okay=all(r[k]==old[k] for k in ('shots','hits','weight')) and behavior(manage.OPS/'experiments'/r['run_id'])==behavior(manage.OPS/'experiments'/old['run_id'])
        checks.append(dict(scene=old['scene'],variant=old['variant'],historical_run=old['run_id'],new_run=r['run_id'],identical=okay));write(ROOT/'compatibility.json',checks)
        if not okay:raise RuntimeError('Legacy compatibility mismatch; retained for diagnosis')

def probe(c,s):
    for env in ENVS:
        for scene in SCENES:
            for v in VARIANTS:collect(c,s,[submit(c,s,'probe_p1',env,v,scene,c['probe_seed'])])
        checks=[]
        for scene in SCENES:
            entries=[submit(c,s,'probe_p2',env,v,scene,c['probe_seed'],2) for v in VARIANTS];collect(c,s,entries)
            rows=[next(r for r in s['runs'] if r.get('job_id')==e['id']) for e in entries];a,b=[r['attempt'] for r in rows]
            overlap=a['slot']!=b['slot'] and a['started']<b['ended'] and b['started']<a['ended']
            for r in rows:
                prior=next(p for p in s['runs'] if (p['phase'],p['environment'],p['scene'],p['variant'])==('probe_p1',env,scene,r['variant']))
                same=all(r[k]==prior[k] for k in ('shots','hits','weight')) and behavior(manage.OPS/'experiments'/r['run_id'])==behavior(manage.OPS/'experiments'/prior['run_id'])
                checks.append(dict(scene=scene,variant=r['variant'],same_scores_and_behavior=same,actual_overlap=overlap,within_budget=max(r['wall_s'],prior['wall_s'])<=40))
        s['parallelism_by_environment'][env]=2 if all(x['same_scores_and_behavior'] and x['actual_overlap'] and x['within_budget'] for x in checks) else 1
        write(ROOT/('parallel-'+env+'.json'),dict(pairs=checks,parallelism=s['parallelism_by_environment'][env],global_qualification=False));write(ROOT/'state.json',s)

def ui(c,s):
    import experiments,subprocess
    if s.get('current_ui_case'):raise RuntimeError('Interrupted UI case retained; inspect before resume')
    subprocess.run(['python3',str(manage.ROOT/'docker/daedalus/display_auth.py')],check=True)
    with (manage.RUNTIME/'operation.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            for env in ('legacy','refnominal'):
                for scene in ('adv_variable_pos','adv_pursuit_pos'):
                    for v in (VARIANTS if scene=='adv_variable_pos' else VARIANTS[::-1]):
                        if any(r['phase']=='ui' and r['environment']==env and r['scene']==scene and r['variant']==v for r in s['runs']):continue
                        case=dict(phase='ui',engine='ui',environment=env,variant=v,scene=scene,sensor_seed=c['sensor_seeds'][0],repeat_id=0);s['current_ui_case']=case;write(ROOT/'state.json',s);started=time.monotonic()
                        opts=dict(settings(scene,case['sensor_seed']),gimbal_dynamics=ENVS[env][0])
                        with matched_ui(Path(c['bundles'][v])):
                            try:
                                manage.stop();manage.start(argparse.Namespace(**dict(opts,scene='static',control='off',view='off',replace=False,fire='auto')))
                                folder=experiments.run_one(manage,scene,argparse.Namespace(**dict(opts,repeat_id=0,label='refyaw-ui-'+env+'-'+v)))
                            finally:manage.stop()
                        row=trial_metrics(ROOT,dict(case,run_id=folder.name,complete_wall_s=time.monotonic()-started),c);s['runs'].append(row);s.pop('current_ui_case');write(ROOT/'state.json',s)
                        if row['complete_wall_s']>40:raise RuntimeError('UI exceeded 40 seconds; retained')
        finally:manage.save_profile(c['original_profile'])

def run():
    c=initialize()
    with (ROOT/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);s=load(ROOT/'state.json')
        if s['state']=='completed':return
        try:
            s['state']='running';write(ROOT/'state.json',s);compatibility(c,s);probe(c,s)
            for env in ENVS:
                for scene in SCENES:
                    entries=[]
                    for seed in c['sensor_seeds']:
                        for v in (VARIANTS if seed%2 else VARIANTS[::-1]):
                            e=submit(c,s,'audit',env,v,scene,seed,s['parallelism_by_environment'][env]);entries.append(e)
                            if s['parallelism_by_environment'][env]==1:collect(c,s,[e])
                    collect(c,s,entries)
            ui(c,s)
            assert len(s['runs'])==132
            from reference_yaw_report import finish
            finish(ROOT,c,s)
        except BaseException as error:s.update(state='failed',error=repr(error));write(ROOT/'state.json',s);raise
if __name__=='__main__':run()
