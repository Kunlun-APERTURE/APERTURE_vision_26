"""Backend-first, bounded perception optimization. UI is conditional on holdout gain."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import random
import shutil
import statistics
import subprocess
import time
import uuid

import background_jobs as jobs
import manage
from decision_benchmark import sha, verify_bundle
from decision_benchmark_report import aggregate, behavior, unified_score
from report import load, write
from sync_contract_study import settings
from sync_first_divergence import run_one
from sync_first_divergence_analysis import primary, truth_centers

SCENES=('hf_uniform_3','hf_turn90_3')
LIMITS=(.15,.25,.35)
PRIOR=manage.OPS/'experiments/decision-benchmark-20261023'
SOURCE_FILES=('CMakeLists.txt','src/sim_auto_aim.cpp','tasks/auto_aim/perception_guard.hpp',
 'tasks/auto_aim/initial_consistency.hpp','tasks/auto_aim/armor.hpp','tasks/auto_aim/detector.hpp',
 'tasks/auto_aim/detector.cpp','tasks/auto_aim/continuity.hpp','tasks/auto_aim/tracker.hpp',
 'tasks/auto_aim/tracker.cpp','tests/perception_test.cpp')


def protected():
    names=('sp_vision_25/build/simulator/sim_auto_aim','sp_vision_25/configs/simulator.yaml',
           'sp_vision_25/tests/auto_aim_test.cpp','docker/auto_aim/input-contract-runtime.json')
    return {n:sha(manage.ROOT/n) for n in names}


def freeze(root):
    root.mkdir(parents=True,exist_ok=True)
    path=root/'contract.json'
    if path.exists():
        c=load(path)
        if sha(path)!=load(root/'state.json')['contract_sha256']:raise ValueError('Frozen contract changed')
        if protected()!=c['protected']:raise ValueError('Production binary, config, test or selector changed')
        return c
    prior=load(PRIOR/'contract.json');base=Path(prior['bundles']['v3']);meta=verify_bundle(base)
    binary=root/'build-source/sp_vision_25/build/perception/sim_auto_aim'
    if not binary.exists():raise ValueError('Build the isolated candidate first')
    c=dict(version=1,created=time.time(),parent=str(PRIOR),base_bundle=str(base),
        original_binary_sha256=meta['files']['sp_vision_25/build/simulator/sim_auto_aim'],
        candidate_binary_sha256=sha(binary),build_source_sha256={p:sha(root/'build-source/sp_vision_25'/p) for p in SOURCE_FILES},
        protected=protected(),sources=prior['replay_sources'],runtime_files=prior['runtime_files'],
        scenes=SCENES,thresholds=LIMITS,screen_seeds=list(range(1901,1906)),confirm_seeds=list(range(2001,2011)),
        backend_timing_profile='ui-aligned-v7-clock',replay_workers=2,closed_loop_parallelism=1,
        replay_gate=dict(coverage_drop=.02,initial_latency_p90_add_s=.1),
        effect_gate=dict(weighted_gain=.03,scene_weighted_loss=.02,scene_exact_loss=.02,
                         hits_per_s_ratio=.95,latency_p90_add_s=.1,minimum_shots=30),
        bootstrap=dict(samples=10000,random_seed=20261023,unit='paired sensor seed across both scenes'),
        maximum_trials=dict(screen=40,confirm=40,repeatability=4,conditional_ui=40),
        defaults=dict(input_contract='aligned-v1',warmup_frames=30,algorithm_profile='tracking_fire_v3',
                      fire_policy='advice',fire_rate_hz=5,terrain='flat_range',physics='no-drag'),
        ui_policy='No UI acquisition unless independent backend confirmation and repeatability pass. '
                  'No score-based reruns, seed extensions or default promotion.',
        scoring_version=2,original_profile=manage.profile(),capacity_registration=False)
    write(path,c)
    write(root/'state.json',dict(state='frozen',contract_sha256=sha(path),bundles={},replays=[],
                                runs=[],jobs=[],selection={},ui_scheduled=False))
    return c


def bundle(root,state,c,name,options):
    if name in state['bundles']:
        target=Path(state['bundles'][name]);verify_bundle(target);return target
    base=Path(c['base_bundle']);target=jobs.STATE/'bundles'/('perception_'+name+'_'+c['candidate_binary_sha256'][:12])
    if target.exists():
        verify_bundle(target)
        expected=load(target/'bundle.json').get('perception_options')
        if expected!=options:raise ValueError('Refuse to replace existing algorithm bundle')
    else:
        shutil.copytree(base,target,copy_function=os.link)
        for relative in SOURCE_FILES:
            dst=target/'sp_vision_25'/relative;dst.parent.mkdir(parents=True,exist_ok=True)
            dst.unlink(missing_ok=True);shutil.copy2(root/'build-source/sp_vision_25'/relative,dst)
        dst=target/'sp_vision_25/build/simulator/sim_auto_aim';dst.unlink()
        shutil.copy2(root/'build-source/sp_vision_25/build/perception/sim_auto_aim',dst)
        config=target/'sp_vision_25/configs/simulator.yaml';text=config.read_text();config.unlink()
        config.write_text(text+'\nperception_v1:\n'+''.join('  '+k+': '+str(v).lower()+'\n' for k,v in options.items()))
        meta=load(base/'bundle.json');meta.update(bundle_id=target.name,created=time.time(),parent_bundle=str(base),perception_options=options)
        for relative in SOURCE_FILES:
            meta['files']['sp_vision_25/'+relative]=sha(target/'sp_vision_25'/relative)
        for relative in ('sp_vision_25/build/simulator/sim_auto_aim','sp_vision_25/configs/simulator.yaml'):
            meta['files'][relative]=sha(target/relative)
        (target/'bundle.json').unlink();write(target/'bundle.json',meta);verify_bundle(target)
    state['bundles'][name]=str(target);write(root/'state.json',state)
    return target


def quantile(values,p):
    values=sorted(values)
    return values[max(0,math.ceil(len(values)*p)-1)] if values else None


def replay_one(root,c,name,target,entry):
    source=manage.OPS/'experiments'/entry['run_id'];dest=root/'replays'/name/source.name
    if not (dest/'result.json').exists():
        run_one(source,dest,target,seconds=2.,detector_history='session',diagnosis=True)
    rr=primary(dest);start=load(source/'run-state.json')['measurement_start_stamp_ns']
    first=rr[0]['frame']['stamp_ns'];end=start+2_000_000_000
    # Ground truth is opened only after inference has finished.
    truth=truth_centers(source);errors=[];valid=0;init=None;guard={};pending=0
    for p in rr:
        f=p['frame'];r=p['result'];x=r.get('filter_state')
        if x is not None and init is None:init=(f['stamp_ns']-first)*1e-9
        if r.get('continuity',{}).get('initial_consistency',{}).get('status')=='pending_inconsistent':pending+=1
        for a in r.get('observations',[]):
            reason=(a.get('refinement') or {}).get('reason')
            if reason:guard[reason]=guard.get(reason,0)+1
        gt=truth(f['stamp_ns'])
        if x is not None and gt is not None:
            xy=[x[0]+f['gimbal_position_map'][0],x[2]+f['gimbal_position_map'][1]]
            errors.append(math.dist(xy,gt[:2]));valid+=1
    result=dict(variant=name,run_id=source.name,scene=entry['scene'],frames=len(rr),valid=valid,
                errors_m=errors,coverage=valid/len(rr),initial_latency_s=init if init is not None else (end-first)*1e-9,
                initialized=init is not None,input_sha256=load(dest/'result.json')['primary_input_sha256'],
                guard_reasons=guard,pending_frames=pending,replay=str(dest),
                baseline_differences=load(dest/'result.json')['differences_from_recorded'] if name=='baseline' else None)
    write(dest/'metrics.json',result)
    return result


def summarize_replays(rows):
    errors=[x for r in rows for x in r['errors_m']]
    return dict(error_p95_m=quantile(errors,.95),coverage=sum(r['valid'] for r in rows)/sum(r['frames'] for r in rows),
                initial_latency_p90_s=quantile([r['initial_latency_s'] for r in rows],.9),
                initialized_runs=sum(r['initialized'] for r in rows))


def prescreen(root,state,c):
    definitions=[('baseline',{})]+[(f'A{int(t*100)}',dict(refinement_guard=True,refinement_limit=t)) for t in LIMITS]+[
        (f'B{int(t*100)}',dict(initial_consistency=True,initial_limit=t)) for t in LIMITS]
    def acquire(name,options):
        target=bundle(root,state,c,name,options)
        pending=[e for e in c['sources'] if not any(r['variant']==name and r['run_id']==e['run_id'] for r in state['replays'])]
        with ThreadPoolExecutor(max_workers=c['replay_workers']) as pool:
            for result in pool.map(lambda e:replay_one(root,c,name,target,e),pending):
                state['replays'].append(result);write(root/'state.json',state)
                print(json.dumps(dict(stage='replay',completed=len(state['replays']),variant=name,run_id=result['run_id'])),flush=True)
        return summarize_replays([r for r in state['replays'] if r['variant']==name])
    summaries={name:acquire(name,opts) for name,opts in definitions}
    for r in state['replays']:
        base=next(x for x in state['replays'] if x['variant']=='baseline' and x['run_id']==r['run_id'])
        if r['input_sha256']!=base['input_sha256']:raise ValueError('Replay A/B input bytes differ')
        if r['variant']=='baseline' and any(r['baseline_differences'][k]['different_frames'] for k in
                                           ('observations','filter_state','selected_plate_id')):
            raise ValueError('Disabled candidate did not reproduce old v3')
    base=summaries['baseline'];selection={}
    for family in ('A','B'):
        candidates=[]
        for name,score in summaries.items():
            if not name.startswith(family):continue
            eligible=(score['coverage']>=base['coverage']-c['replay_gate']['coverage_drop'] and
                      score['initial_latency_p90_s']<=base['initial_latency_p90_s']+c['replay_gate']['initial_latency_p90_add_s'])
            score['eligible']=eligible
            if eligible:candidates.append(name)
        selection[family]=min(candidates,key=lambda n:(summaries[n]['error_p95_m'] if summaries[n]['error_p95_m'] is not None else math.inf,
            -summaries[n]['coverage'],summaries[n]['initial_latency_p90_s'],n)) if candidates else None
    if all(selection.values()):
        options={}
        for name in selection.values():options.update(load(Path(state['bundles'][name])/'bundle.json')['perception_options'])
        score=acquire('AB',options);summaries['AB']=score
        score['eligible']=score['coverage']>=base['coverage']-.02 and score['initial_latency_p90_s']<=base['initial_latency_p90_s']+.1
    state['selection']['replay']=selection
    state['replay_summaries']=summaries
    state['screen_variants']=['baseline']+[n for n in selection.values() if n]+(['AB'] if summaries.get('AB',{}).get('eligible') else [])
    write(root/'state.json',state);write(root/'replay-result.json',dict(summaries=summaries,selected=selection,screen_variants=state['screen_variants']))


def trial_metrics(root,entry,c):
    folder=manage.OPS/'experiments'/entry['run_id'];m=load(folder/'manifest.json');summary=load(folder/'summary.json')
    if summary['verdict']=='invalid' or not load(folder/'session-evidence-audit.json')['passed']:raise ValueError('Invalid trial retained')
    scored=unified_score(folder,root/'normalized-scores'/folder.name,c)['metrics']
    records=[json.loads(line)['data'] for line in (folder/'session-evidence/events.jsonl').open() if json.loads(line)['kind']=='result']
    first=records[0]['frame']['stamp_ns'];initial=next((p for p in records if p['result'].get('filter_state') is not None),None)
    state=load(folder/'run-state.json');duration=m['duration_s']
    return dict(entry,shots=scored['shots_fired'],hits=scored['target_armor_hits'],weight=scored['weighted_hit_score_sum'],
                duration_s=duration,initial_latency_s=(initial['frame']['stamp_ns']-first)*1e-9 if initial else (state['measurement_end_stamp_ns']-first)*1e-9,
                initial_censored=initial is None,first_hit_s=scored['first_hit_s'] if scored['first_hit_s'] is not None else duration,
                first_hit_censored=scored['first_hit_s'] is None,wall_s=load(folder/'timing.json')['wall_seconds'],
                source_hashes={n:sha(folder/n) for n in ('manifest.json','summary.json','run-state.json','session-evidence/events.jsonl')})


def background_case(root,state,c,phase,variant,scene,seed,repeat):
    key=(phase,variant,scene,seed,repeat)
    if any(tuple(r[k] for k in ('phase','variant','scene','sensor_seed','repeat_id'))==key for r in state['runs']):return
    existing=next((j for j in state['jobs'] if tuple(j[k] for k in ('phase','variant','scene','sensor_seed','repeat_id'))==key),None)
    if existing is None:
        opts=dict(settings(scene,seed),execution_mode='lockstep',timing_profile=c['backend_timing_profile'],
                  max_fps=240,label='perception-'+phase+'-'+variant)
        row=jobs.validate({k:v for k,v in opts.items() if k in jobs.FIELDS});row.update(repeat_id=repeat,execution_profile='lockstep-'+c['backend_timing_profile'])
        identifier='job_perception_'+uuid.uuid4().hex[:12];now=time.time()
        with jobs.connect() as db,jobs.transaction(db):
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(identifier,'queued',now,now,json.dumps(dict(version=1,_protocol=2,
                _perception_study=True)),state['bundles'][variant],1,0,None))
            db.execute('INSERT INTO items(job_id,ordinal,state,settings) VALUES(?,?,?,?)',(identifier,0,'pending',json.dumps(row)))
            jobs.event(db,identifier,'Frozen backend-first perception comparison; UI conditional; no capacity registration')
        existing=dict(phase=phase,variant=variant,scene=scene,sensor_seed=seed,repeat_id=repeat,id=identifier)
        state['jobs'].append(existing);write(root/'state.json',state)
    identifier=existing['id'];deadline=time.monotonic()+300
    while True:
        with jobs.connect() as db:status=jobs.status(db,identifier)
        if status['state']=='completed':break
        if status['state'] in ('paused','cancelled') or time.monotonic()>deadline:raise RuntimeError('Inspect retained job: '+identifier)
        time.sleep(1)
    with jobs.connect() as db:
        attempts=[dict(p) for p in db.execute('SELECT a.* FROM attempts a JOIN items i ON i.id=a.item_id WHERE i.job_id=?',(identifier,))]
    existing['attempts']=attempts;write(root/'state.json',state)
    if len(attempts)!=1 or attempts[0]['verdict']=='invalid' or not attempts[0]['run_id']:raise ValueError('Invalid/duplicate attempt retained')
    entry=dict(phase=phase,variant=variant,scene=scene,sensor_seed=seed,repeat_id=repeat,run_id=attempts[0]['run_id'],job_id=identifier,engine='backend')
    metrics=trial_metrics(root,entry,c)
    if metrics['wall_s']>40:raise ValueError('Trial exceeded 40-second budget')
    state['runs'].append(entry);write(root/'state.json',state)
    print(json.dumps(dict(stage=phase,completed=len(state['runs']),**entry)),flush=True)


def batch(root,state,c,phase,variants,seeds):
    for i,seed in enumerate(seeds):
        for scene in SCENES if i%2==0 else SCENES[::-1]:
            for variant in variants if i%2==0 else variants[::-1]:
                background_case(root,state,c,phase,variant,scene,seed,i)


def effect(rows,variant,c,independent=False):
    base=[r for r in rows if r['variant']=='baseline'];candidate=[r for r in rows if r['variant']==variant]
    checks={};groups={}
    for scene in ('all',*SCENES):
        b=[r for r in base if scene=='all' or r['scene']==scene];v=[r for r in candidate if scene=='all' or r['scene']==scene]
        a,z=aggregate(b),aggregate(v);groups[scene]=dict(baseline=a,candidate=z)
        if not a['shots'] or not z['shots']:
            checks[scene+'_shots']=False;continue
        checks[scene+'_shot_count']=a['shots']>=c['effect_gate']['minimum_shots'] and z['shots']>=c['effect_gate']['minimum_shots']
        checks[scene+'_weighted']=z['weighted_hit_rate']>=a['weighted_hit_rate']+(.03 if scene=='all' else -.02)
        checks[scene+'_exact']=z['target_hit_rate']>=a['target_hit_rate']-.02
        checks[scene+'_hit_rate_per_s']=z['hits_per_s']>=a['hits_per_s']*.95
        checks[scene+'_init_latency']=quantile([r['initial_latency_s'] for r in v],.9)<=quantile([r['initial_latency_s'] for r in b],.9)+.1
        checks[scene+'_first_hit_latency']=quantile([r['first_hit_s'] for r in v],.9)<=quantile([r['first_hit_s'] for r in b],.9)+.1
        checks[scene+'_no_hit_censor']=sum(r['first_hit_censored'] for r in v)/len(v)<=sum(r['first_hit_censored'] for r in b)/len(b)
    interval=None
    if independent:
        seeds=sorted({r['sensor_seed'] for r in base});rng=random.Random(c['bootstrap']['random_seed']);values=[]
        if {(r['scene'],r['sensor_seed']) for r in base}!={(r['scene'],r['sensor_seed']) for r in candidate}:raise ValueError('Unpaired holdout')
        for _ in range(c['bootstrap']['samples']):
            draw=rng.choices(seeds,k=len(seeds));bb=[r for seed in draw for r in base if r['sensor_seed']==seed]
            vv=[r for seed in draw for r in candidate if r['sensor_seed']==seed]
            a,z=aggregate(bb),aggregate(vv)
            if a['weighted_hit_rate'] is None or z['weighted_hit_rate'] is None:values=[];break
            values.append(z['weighted_hit_rate']-a['weighted_hit_rate'])
        if values:interval=[quantile(values,.025),quantile(values,.975)]
        checks['independent_weighted_lower_above_zero']=interval is not None and interval[0]>0
    return dict(variant=variant,passed=all(checks.values()),checks=checks,groups=groups,weighted_gain_95=interval)


def run(root):
    c=freeze(root)
    with (root/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);state=load(root/'state.json')
        if state['state']=='completed':return
        try:
            state['state']='running';write(root/'state.json',state)
            prescreen(root,state,c)
            variants=state['screen_variants']
            if len(variants)>1:
                batch(root,state,c,'screen',variants,c['screen_seeds'])
                rows=[trial_metrics(root,e,c) for e in state['runs'] if e['phase']=='screen']
                results=[effect(rows,v,c) for v in variants if v!='baseline'];write(root/'screen-result.json',results)
                eligible=[r for r in results if r['passed']]
                selected=min(eligible,key=lambda r:(-r['groups']['all']['candidate']['weighted_hit_rate'],
                    -r['groups']['all']['candidate']['hits_per_s'],2 if r['variant']=='AB' else 1,r['variant']))['variant'] if eligible else None
                state['selection']['screen']=selected;write(root/'state.json',state)
                if selected:
                    batch(root,state,c,'confirm',['baseline',selected],c['confirm_seeds'])
                    rows=[trial_metrics(root,e,c) for e in state['runs'] if e['phase']=='confirm']
                    confirmed=effect(rows,selected,c,True);write(root/'confirm-result.json',confirmed)
                    for variant in ('baseline',selected):
                        for scene in SCENES:background_case(root,state,c,'repeat',variant,scene,c['confirm_seeds'][0],10)
                    repeats=[]
                    for r in [p for p in state['runs'] if p['phase']=='repeat']:
                        original=next(p for p in state['runs'] if p['phase']=='confirm' and all(p[k]==r[k] for k in ('variant','scene','sensor_seed')))
                        folders=[manage.OPS/'experiments'/p['run_id'] for p in (original,r)]
                        metrics=[trial_metrics(root,p,c) for p in (original,r)]
                        repeats.append(dict(scene=r['scene'],variant=r['variant'],input_behavior_equal=behavior(folders[0])==behavior(folders[1]),
                            scores_equal=all(metrics[0][k]==metrics[1][k] for k in ('shots','hits','weight'))))
                    write(root/'repeatability.json',dict(passed=all(p['input_behavior_equal'] and p['scores_equal'] for p in repeats),pairs=repeats))
                    state['ui_eligible']=confirmed['passed'] and load(root/'repeatability.json')['passed'];write(root/'state.json',state)
                    if state['ui_eligible']:
                        from perception_study_ui import run_ui
                        run_ui(root,state,c,selected)
            state.update(state='completed',ended=time.time());write(root/'state.json',state)
        except BaseException as error:
            state.update(state='failed',error=repr(error));write(root/'state.json',state);raise


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['freeze','run'])
    p.add_argument('--output',type=Path,required=True);a=p.parse_args();root=a.output.resolve()
    if a.action=='freeze':print(json.dumps(freeze(root),indent=2))
    else:run(root)
