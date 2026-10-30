"""Single-factor repairs, parallel archived-input checks, then scoped backend batches."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

import background_jobs as jobs
import manage
from background_capacity import assess
from decision_benchmark import sha,verify_bundle
from decision_benchmark_report import behavior
from perception_study import SOURCE_FILES,protected,quantile,trial_metrics,effect
from report import load,write
from sync_contract_study import settings
from sync_first_divergence import run_one
from sync_first_divergence_analysis import primary,truth_centers

PARENT=manage.OPS/'experiments/perception-attribution-20261023'
VARIANTS=dict(baseline={},Afix=dict(refinement_guard=True,refinement_limit=.35,refinement_ratio_guard=False),
              Bfix=dict(initial_consistency=True,initial_limit=.35,initial_joint_position=True))
FIELDS=('observations','filter_state','selected_plate_id','tracker','yaw','pitch','fire_advice')
SCENES=('hf_uniform_3','hf_turn90_3')


def initialize(root):
    root.mkdir(parents=True,exist_ok=True)
    if (root/'contract.json').exists():
        c=load(root/'contract.json');s=load(root/'state.json')
        if sha(root/'contract.json')!=s['contract_sha256'] or protected()!=c['protected']:
            raise ValueError('Frozen definition or protected artifacts changed')
        for b in c['bundles'].values():verify_bundle(Path(b))
        return c
    parents=[PARENT,PARENT/'longest-A'];sources=[]
    for folder in parents:
        pc=load(folder/'contract.json');ps=load(folder/'state.json')
        if ps['state']!='completed':raise ValueError('Complete prior attribution first')
        for e in pc['sources']:
            b=next(r for r in ps['replays'] if r['source_run']==e['run_id'] and r['variant']=='baseline')
            sources.append(dict(e,baseline_replay=b))
    old=load(manage.OPS/'experiments/perception-optimization-20261023/contract.json')
    binary=root/'build-source/sp_vision_25/build/perception/sim_auto_aim'
    if 'PASSED 72 perception checks' not in (root/'cpp-checks.log').read_text():raise ValueError('Run C++ checks first')
    c=dict(version=1,created=time.time(),protected=protected(),sources=sources,
        variants=VARIANTS,bundles={},runtime_files=old['runtime_files'],
        base_bundle=old['base_bundle'],candidate_binary_sha256=sha(binary),
        build_source_sha256={f:sha(root/'build-source/sp_vision_25'/f) for f in SOURCE_FILES},
        backend_timing_profile='ui-aligned-v7-clock',replay_workers=2,
        probe_seed=2091,screen_seeds=list(range(2101,2106)),confirm_seeds=list(range(2201,2211)),
        replay_gate=dict(coverage_drop=.02,initial_latency_p90_add_s=.1),
        effect_gate=old['effect_gate'],bootstrap=old['bootstrap'],
        maximum_trials=dict(parallel_probe=12,screen=30,conditional_confirmation=40,conditional_repeat=4,ui=0),
        parallel_gate='All six matched variant/scene paths must have identical image/algorithm behavior and scores at p1 and p2; healthy resources, actual overlap and each trial <=40s. Study-local only; no automatic capacity registration.',
        ui_policy='No UI in this repair batch. Independent backend improvement is required before proposing UI verification.',
        algorithm_truth_input=False,automatic_promotion=False)
    for name,opts in VARIANTS.items():
        base=Path(c['base_bundle']);target=jobs.STATE/'bundles'/('repair_'+name+'_'+sha(binary)[:12])
        if target.exists():raise ValueError('Unexpected existing bundle; inspect rather than overwrite')
        shutil.copytree(base,target,copy_function=os.link)
        for f in SOURCE_FILES:
            dst=target/'sp_vision_25'/f;dst.parent.mkdir(parents=True,exist_ok=True);dst.unlink(missing_ok=True)
            shutil.copy2(root/'build-source/sp_vision_25'/f,dst)
        dst=target/'sp_vision_25/build/simulator/sim_auto_aim';dst.unlink();shutil.copy2(binary,dst)
        dst=target/'sp_vision_25/configs/simulator.yaml';txt=dst.read_text();dst.unlink()
        dst.write_text(txt+'\nperception_v1:\n'+''.join('  '+k+': '+str(v).lower()+'\n' for k,v in opts.items()) if opts else txt)
        meta=load(base/'bundle.json');meta.update(bundle_id=target.name,created=time.time(),parent_bundle=str(base),perception_options=opts)
        for f in SOURCE_FILES:meta['files']['sp_vision_25/'+f]=sha(target/'sp_vision_25'/f)
        for f in ('build/simulator/sim_auto_aim','configs/simulator.yaml'):meta['files']['sp_vision_25/'+f]=sha(target/'sp_vision_25'/f)
        (target/'bundle.json').unlink();write(target/'bundle.json',meta);verify_bundle(target)
        c['bundles'][name]=str(target)
    write(root/'contract.json',c)
    write(root/'state.json',dict(state='frozen',contract_sha256=sha(root/'contract.json'),replays=[],runs=[],jobs=[]))
    return c


def metrics_replay(path,source):
    rows=primary(path);truth=truth_centers(source);errors=[];valid=0;init=None
    first=rows[0]['frame']['stamp_ns'];statuses={}
    for p in rows:
        f=p['frame'];r=p['result'];x=r.get('filter_state');g=truth(f['stamp_ns'])
        if x is not None and init is None:init=(f['stamp_ns']-first)*1e-9
        status=(r.get('continuity',{}).get('initial_consistency') or {}).get('status')
        if status:statuses[status]=statuses.get(status,0)+1
        if x is not None and g is not None:
            errors.append(math.dist([x[0]+f['gimbal_position_map'][0],x[2]+f['gimbal_position_map'][1]],g[:2]));valid+=1
    return dict(frames=len(rows),valid=valid,errors_m=errors,initial_latency_s=init,
                statuses=statuses,first_filter_state=rows[0]['result'].get('filter_state'))


def replay_one(root,c,e,v):
    source=manage.OPS/'experiments'/e['run_id'];out=root/'replays'/source.name/v
    r=load(out/'result.json') if (out/'result.json').exists() else run_one(source,out,Path(c['bundles'][v]),
        seconds=e['duration_s'],detector_history='session',diagnosis=True)
    prior=e['baseline_replay']
    if not r['complete'] or r['primary_input_sha256']!=prior['input_sha256']:raise ValueError('Replay input changed')
    if v=='baseline' and any(r['differences_from_recorded'][k]['different_frames'] for k in FIELDS):
        raise ValueError('Disabled repairs did not reproduce native v3')
    baseline_path=Path(prior['output']);ref=metrics_replay(baseline_path,source)
    row=dict(source_run=source.name,engine=e['engine'],source_variant=e['variant'],scene=e['scene'],
        variant=v,output=str(out),input_sha256=r['primary_input_sha256'],metrics=metrics_replay(out,source),baseline=ref,
        output_hashes={n:sha(out/n) for n in ('plan.json','events.jsonl','result.json')})
    write(out/'metrics.json',row)
    return row


def replays(root,c,s):
    pending=[(e,v) for e in c['sources'] for v in VARIANTS if (v!='baseline' or e['variant']=='baseline') and
        not any(r['source_run']==e['run_id'] and r['variant']==v for r in s['replays'])]
    with ThreadPoolExecutor(max_workers=c['replay_workers']) as pool:
        for r in pool.map(lambda p:replay_one(root,c,*p),pending):
            s['replays'].append(r);write(root/'state.json',s)
            print(json.dumps(dict(stage='replay',completed=len(s['replays']),variant=r['variant'],source=r['source_run'])),flush=True)
    summaries={};eligible=[]
    for v in ('Afix','Bfix'):
        rows=[r for r in s['replays'] if r['variant']==v]
        def combine(field):
            mm=[r[field] for r in rows];errors=[x for m in mm for x in m['errors_m']]
            return dict(coverage=sum(m['valid'] for m in mm)/sum(m['frames'] for m in mm),
                error_p95_m=quantile(errors,.95),initial_latency_p90_s=quantile([
                    m['initial_latency_s'] if m['initial_latency_s'] is not None else 99. for m in mm],.9))
        a,b=combine('metrics'),combine('baseline')
        passed=a['coverage']>=b['coverage']-.02 and a['initial_latency_p90_s']<=b['initial_latency_p90_s']+.1
        summaries[v]=dict(candidate=a,baseline=b,eligible=passed)
        if passed:eligible.append(v)
    s['screen_variants']=['baseline',*eligible];s['replay_summaries']=summaries;write(root/'state.json',s)
    write(root/'replay-result.json',dict(summaries=summaries,disabled_native_reproductions=sum(r['variant']=='baseline' for r in s['replays'])))


def submit(root,c,s,phase,variant,scene,seed,level,repeat):
    key=(phase,variant,scene,seed,repeat)
    existing=next((j for j in s['jobs'] if tuple(j[k] for k in ('phase','variant','scene','sensor_seed','repeat_id'))==key),None)
    if existing:return existing
    opts=dict(settings(scene,seed),execution_mode='lockstep',timing_profile=c['backend_timing_profile'],max_fps=240,
              stage='screen',evidence='full',label='repair-'+phase+'-'+variant)
    row=jobs.validate({k:v for k,v in opts.items() if k in jobs.FIELDS})
    row.update(repeat_id=repeat,execution_profile='lockstep-'+c['backend_timing_profile'])
    identifier='job_repair_'+uuid.uuid4().hex[:12];now=time.time()
    with jobs.connect() as db,jobs.transaction(db):
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(identifier,'queued',now,now,json.dumps(
            dict(version=1,_protocol=2,_repair_study=True,study=str(root),parallelism=level)),c['bundles'][variant],level,int(level>1),None))
        db.execute('INSERT INTO items(job_id,ordinal,state,settings) VALUES(?,?,?,?)',(identifier,0,'pending',json.dumps(row)))
        jobs.event(db,identifier,'Frozen perception repair; parallel probe/effect separate; no automatic capacity or UI promotion')
    entry=dict(phase=phase,variant=variant,scene=scene,sensor_seed=seed,repeat_id=repeat,id=identifier,level=level)
    s['jobs'].append(entry);write(root/'state.json',s);return entry


def collect(root,c,s,entries):
    for e in entries:
        if any(r['job_id']==e['id'] for r in s['runs']):continue
        deadline=time.monotonic()+900
        while True:
            with jobs.connect() as db:status=jobs.status(db,e['id'])
            if status['state']=='completed':break
            if status['state'] in ('paused','cancelled') or time.monotonic()>deadline:
                raise RuntimeError('Inspect retained task '+e['id'])
            time.sleep(1)
        with jobs.connect() as db:
            attempts=[dict(a) for a in db.execute('SELECT a.* FROM attempts a JOIN items i ON a.item_id=i.id WHERE i.job_id=?',(e['id'],))]
        if len(attempts)!=1 or attempts[0]['verdict']=='invalid':raise ValueError('Retain duplicate/invalid attempt for inspection')
        a=attempts[0];entry=dict(e,engine='backend',job_id=e['id'],run_id=a['run_id'],attempt=a)
        m=trial_metrics(root,entry,c);m['health']=assess(manage.OPS/'experiments'/entry['run_id'])
        s['runs'].append(m);write(root/'state.json',s)
        print(json.dumps(dict(stage=e['phase'],completed=len(s['runs']),run=entry['run_id'],wall_s=m['wall_s'])),flush=True)


def parallel_probe(root,c,s):
    # Serial reference first. The parallel set is retained even if it fails;
    # failure falls back to p1 without putting probe results in efficacy samples.
    for v in s['screen_variants']:
        for scene in SCENES:
            e=submit(root,c,s,'probe_p1',v,scene,c['probe_seed'],1,0);collect(root,c,s,[e])
    entries=[submit(root,c,s,'probe_p2',v,scene,c['probe_seed'],2,0)
             for v in s['screen_variants'] for scene in SCENES]
    collect(root,c,s,entries);pairs=[]
    for v in s['screen_variants']:
        for scene in SCENES:
            a,b=[next(r for r in s['runs'] if r['phase']==phase and r['variant']==v and r['scene']==scene)
                 for phase in ('probe_p1','probe_p2')]
            paths=[manage.OPS/'experiments'/r['run_id'] for r in (a,b)]
            pairs.append(dict(variant=v,scene=scene,same_behavior=behavior(paths[0])==behavior(paths[1]),
                same_scores=all(a[k]==b[k] for k in ('shots','hits','weight')),
                healthy=a['health']['healthy'] and b['health']['healthy'],within_budget=max(a['wall_s'],b['wall_s'])<=40))
    pp=[r for r in s['runs'] if r['phase']=='probe_p2']
    overlap=any(a['attempt']['started']<b['attempt']['ended'] and b['attempt']['started']<a['attempt']['ended']
                and a['attempt']['slot']!=b['attempt']['slot'] for i,a in enumerate(pp) for b in pp[i+1:])
    passed=overlap and all(all(r[k] for k in ('same_behavior','same_scores','healthy','within_budget')) for r in pairs)
    s['parallelism']=2 if passed else 1;write(root/'state.json',s)
    write(root/'parallel-result.json',dict(passed=passed,pairs=pairs,actual_overlap=overlap,
        effect_parallelism=s['parallelism'],capacity_registration=False))


def batch(root,c,s,phase,variants,seeds):
    entries=[]
    for i,seed in enumerate(seeds):
        for v in variants if i%2==0 else variants[::-1]:
            for scene in SCENES if i%2==0 else SCENES[::-1]:
                e=submit(root,c,s,phase,v,scene,seed,s['parallelism'],i);entries.append(e)
    collect(root,c,s,entries)


def verify(root,c,s):
    if protected()!=c['protected']:raise ValueError('Protected production artifacts changed')
    if not (root/'replay-checks.log').read_text().rstrip().endswith('OK'):
        raise ValueError('Replay integration checks did not pass')
    for e in c['sources']:
        src=manage.OPS/'experiments'/e['run_id']
        if any(sha(src/n)!=h for n,h in e['source_hashes'].items()):raise ValueError('Archived evidence changed')
        old=e['baseline_replay'];path=Path(old['output'])
        if any(sha(path/n)!=h for n,h in old['output_hashes'].items()):raise ValueError('Old baseline replay changed')
    for r in s['replays']:
        if any(sha(Path(r['output'])/n)!=h for n,h in r['output_hashes'].items()):raise ValueError('Replay changed')
    for v in c['bundles'].values():verify_bundle(Path(v))
    for r in s['runs']:
        src=manage.OPS/'experiments'/r['run_id'];m=load(src/'manifest.json');meta=load(Path(c['bundles'][r['variant']])/'bundle.json')
        if any(sha(src/n)!=h for n,h in r['source_hashes'].items()):raise ValueError('New trial changed')
        if m['algorithm_binary_sha256']!=c['candidate_binary_sha256'] or m['configuration_sha256']['algorithm.yaml']!=meta['files']['sp_vision_25/configs/simulator.yaml']:
            raise ValueError('Wrong trial algorithm payload')
        expected=dict(scene=r['scene'],sensor_seed=r['sensor_seed'],terrain='flat_range',input_contract='aligned-v1',warmup_frames=30)
        if any(m.get(k)!=value for k,value in expected.items()):raise ValueError('Trial input contract changed')
        expected=dict(mode='lockstep',timing_profile=c['backend_timing_profile'],detector_history='session',clock_domain='simulation')
        if any(m['execution'].get(k)!=value for k,value in expected.items()):raise ValueError('Trial timing/history changed')
        expected=dict(bullet_speed=25.,reported_bullet_speed=25.,physics='no-drag',aim_origin='muzzle',
            gimbal_profile='feedforward',fire_policy='advice',fire_rate_limit_hz=5.,algorithm_profile='tracking_fire_v3')
        if any(m['shooting'].get(k)!=value for k,value in expected.items()):raise ValueError('Trial fire/physics contract changed')
        for role in ('simulator','bridge','evaluator','algorithm'):
            expected=meta['images']['simulator' if role=='bridge' else role]
            if m['containers'][role+'_image_id']!=expected:raise ValueError('Runtime image changed')
        if not r['health']['healthy'] or r['wall_s']>40:raise ValueError('Unhealthy/over-budget effect trial')
    write(root/'validation.json',dict(passed=True,protected_unchanged=True,original_evidence_unchanged=True,
        replays=len(s['replays']),frames=sum(r['metrics']['frames'] for r in s['replays']),
        backend_trials=len(s['runs']),ui_trials=0,algorithm_promoted=False,capacity_registered=False,
        geometry_checks=72,replay_integration_checks=6,
        source_sha256={p.name:sha(p) for p in manage.OPS.glob('perception_repair*.py')}))


def run(root):
    c=initialize(root)
    with (root/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);s=load(root/'state.json')
        if s['state']=='completed':
            if not (root/'report-seal.json').exists():
                from perception_repair_report import finish
                finish(root)
            return
        try:
            s.update(state='running');write(root/'state.json',s)
            replays(root,c,s)
            with (root/'replay-checks.log').open('w') as log:
                subprocess.run([sys.executable,'-B',str(manage.OPS/'perception_repair_checks.py'),
                    '--output',str(root)],check=True,timeout=30,stdout=log,stderr=subprocess.STDOUT)
            if len(s['screen_variants'])>1:
                parallel_probe(root,c,s)
                batch(root,c,s,'screen',s['screen_variants'],c['screen_seeds'])
                rows=[r for r in s['runs'] if r['phase']=='screen']
                results=[effect(rows,v,c) for v in s['screen_variants'] if v!='baseline'];write(root/'screen-result.json',results)
                eligible=[r for r in results if r['passed']]
                chosen=max(eligible,key=lambda r:r['groups']['all']['candidate']['weighted_hit_rate'])['variant'] if eligible else None
                s['selected']=chosen;write(root/'state.json',s)
                if chosen:
                    batch(root,c,s,'confirm',['baseline',chosen],c['confirm_seeds'])
                    result=effect([r for r in s['runs'] if r['phase']=='confirm'],chosen,c,True)
                    write(root/'confirm-result.json',result);s['backend_confirmed']=result['passed']
                    if result['passed']:
                        batch(root,c,s,'repeat',['baseline',chosen],[c['confirm_seeds'][0]])
                        pairs=[]
                        for r in [r for r in s['runs'] if r['phase']=='repeat']:
                            a=next(a for a in s['runs'] if a['phase']=='confirm' and all(a[k]==r[k] for k in ('variant','scene','sensor_seed')))
                            pairs.append(dict(variant=r['variant'],scene=r['scene'],same_scores=all(a[k]==r[k] for k in ('shots','hits','weight')),
                                same_behavior=behavior(manage.OPS/'experiments'/a['run_id'])==behavior(manage.OPS/'experiments'/r['run_id'])))
                        s['ui_eligible']=all(p['same_scores'] and p['same_behavior'] for p in pairs)
                        write(root/'repeatability.json',dict(passed=s['ui_eligible'],pairs=pairs))
            verify(root,c,s);s.update(state='completed',ended=time.time());write(root/'state.json',s)
            from perception_repair_report import finish
            finish(root)
        except BaseException as err:
            s.update(state='failed',error=repr(err));write(root/'state.json',s);raise


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('freeze','run'))
    p.add_argument('--output',required=True,type=Path);a=p.parse_args()
    if a.action=='freeze':initialize(a.output.resolve())
    else:run(a.output.resolve())
