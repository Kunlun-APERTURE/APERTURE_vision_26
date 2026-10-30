"""Frozen independent radial/shared/prediction/timing ablations and backend results."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

import background_jobs as jobs
import manage
from decision_benchmark import sha,verify_bundle
from decision_benchmark_report import behavior
from perception_study import protected,quantile,effect
from perception_association import FILES as PREVIOUS_FILES
from perception_handoff import verify as common_verify
from perception_repair import batch,parallel_probe,submit,collect,metrics_replay,FIELDS
from report import load,write
from sync_first_divergence import run_one
from sync_contract_study import settings
from sync_timing import load_profile

PARENT=manage.OPS/'experiments/perception-association-20261023'
OPTIONS=dict(baseline={},BS={},R=dict(radial_adaptation=True),C=dict(shared_translation=True),
    P=dict(acceleration_prediction=True),T=dict(calibrated_delay=True),
    RC=dict(radial_adaptation=True,shared_translation=True),
    ALL=dict(radial_adaptation=True,shared_translation=True,acceleration_prediction=True,calibrated_delay=True))
FILES=tuple(dict.fromkeys((*PREVIOUS_FILES,'tasks/auto_aim/motion_model.hpp','tasks/auto_aim/plate_filter.hpp',
    'tasks/auto_aim/aimer.hpp','tasks/auto_aim/aimer.cpp','tests/motion_test.cpp')))


def initialize(root):
    root.mkdir(parents=True,exist_ok=True)
    if (root/'contract.json').exists():
        c=load(root/'contract.json');s=load(root/'state.json')
        if sha(root/'contract.json')!=s['contract_sha256'] or protected()!=c['protected']:raise ValueError('Frozen motion contract changed')
        for b in c['bundles'].values():verify_bundle(Path(b))
        return c
    pc=load(PARENT/'contract.json');ps=load(PARENT/'state.json')
    if ps['state']!='completed':raise ValueError('Parent study incomplete')
    sources=[]
    for e in ps['runs']:
        if e['phase']!='confirm' or e['variant']!='BS' or e['sensor_seed']>2605:continue
        folder=manage.OPS/'experiments'/e['run_id'];m=load(folder/'manifest.json')
        sources.append(dict(run_id=e['run_id'],variant='BS',engine='backend',scene=e['scene'],sensor_seed=e['sensor_seed'],duration_s=m['duration_s'],
            source_hashes={n:sha(folder/n) for n in ('manifest.json','summary.json','run-state.json','telemetry.jsonl','session-evidence/events.jsonl','session-evidence/input.mjpg')}))
    sources.extend(e for e in pc['sources'] if e['engine']=='ui')
    if len(sources)!=14:raise ValueError('Expected ten backend BS + four UI baseline paths')
    checks={}
    for name,pattern in (('motion',r'PASSED (\d+) motion checks'),('cpp',r'PASSED (\d+) perception checks')):
        match=re.search(pattern,(root/(name+'-checks.log')).read_text())
        if not match:raise ValueError('Run all C++ checks before freezing')
        checks[name]=int(match[1])
    binary=root/'build-source/sp_vision_25/build/motion/sim_auto_aim'
    c=dict(version=1,created=time.time(),parent=str(PARENT),parent_contract_sha256=sha(PARENT/'contract.json'),
        protected=protected(),sources=sources,options=OPTIONS,bundles={},base_bundle=pc['base_bundle'],BS_options=pc['options']['BS'],
        candidate_binary_sha256=sha(binary),build_source_sha256={f:sha(root/'build-source/sp_vision_25'/f) for f in FILES},
        geometry_checks=sum(checks.values()),cpp_checks=checks,runtime_files=pc['runtime_files'],backend_timing_profile=pc['backend_timing_profile'],
        replay_workers=2,maximum_replays=112,probe_seed=2691,screen_seeds=list(range(2701,2706)),confirm_seeds=list(range(2801,2811)),
        regression_seeds=list(range(2901,2904)),regression_scenes=['hf_static_5','hf_spin_pos','hf_spin_neg','ego_right_centered'],
        effect_gate=pc['effect_gate'],bootstrap=pc['bootstrap'],
        replay_gate=dict(coverage_drop=.02,initial_latency_p90_add_s=.1,center_error_p95_ratio=1.25),
        maximum_trials=dict(probe=32,screen=80,confirm=40,repeat=4,regression=24,ui=0),
        design='All variants compared with v3; R/C/P/T and combinations retain BS sensing. Delay fixed to .0043 from all previously recorded BS launches, treated as backend-only calibration. No score-selected delay scan.',
        no_truth_input=True,automatic_promotion=False,capacity_registration=False)
    for name,motion in OPTIONS.items():
        base=Path(c['base_bundle']);target=jobs.STATE/'bundles'/('motion_'+name+'_'+sha(binary)[:12])
        if target.exists():raise ValueError('Refuse overwrite of candidate bundle')
        shutil.copytree(base,target,copy_function=os.link)
        for f in FILES:
            dst=target/'sp_vision_25'/f;dst.parent.mkdir(parents=True,exist_ok=True);dst.unlink(missing_ok=True)
            shutil.copy2(root/'build-source/sp_vision_25'/f,dst)
        dst=target/'sp_vision_25/build/simulator/sim_auto_aim';dst.unlink();shutil.copy2(binary,dst)
        config=target/'sp_vision_25/configs/simulator.yaml';text=config.read_text();config.unlink()
        if name!='baseline':text+='\nperception_v1:\n'+''.join('  '+k+': '+str(v).lower()+'\n' for k,v in c['BS_options'].items())
        if motion:text+='\nmotion_v5:\n'+''.join('  '+k+': '+str(v).lower()+'\n' for k,v in motion.items())
        config.write_text(text)
        meta=load(base/'bundle.json');meta.update(bundle_id=target.name,created=time.time(),parent_bundle=str(base),motion_options=motion)
        for f in FILES:meta['files']['sp_vision_25/'+f]=sha(target/'sp_vision_25'/f)
        for f in ('build/simulator/sim_auto_aim','configs/simulator.yaml'):meta['files']['sp_vision_25/'+f]=sha(target/'sp_vision_25'/f)
        (target/'bundle.json').unlink();write(target/'bundle.json',meta);verify_bundle(target);c['bundles'][name]=str(target)
    write(root/'contract.json',c);write(root/'state.json',dict(state='frozen',contract_sha256=sha(root/'contract.json'),replays=[],runs=[],jobs=[]))
    return c


def replay_one(root,c,e,v):
    source=manage.OPS/'experiments'/e['run_id'];out=root/'replays'/source.name/v
    r=load(out/'result.json') if (out/'result.json').exists() else run_one(source,out,Path(c['bundles'][v]),seconds=e['duration_s'],detector_history='session',diagnosis=True)
    native=v==e['variant']
    if not r['complete'] or native and any(r['differences_from_recorded'][k]['different_frames'] for k in FIELDS):raise ValueError('Native motion reproduction failed')
    return dict(variant=v,source_run=source.name,source_variant=e['variant'],engine=e['engine'],scene=e['scene'],sensor_seed=e['sensor_seed'],
        native_configuration=native,output=str(out),frames=r['frames'],input_sha256=r['primary_input_sha256'],metrics=metrics_replay(out,source),
        output_hashes={n:sha(out/n) for n in ('plan.json','events.jsonl','result.json')})


def replays(root,c,s):
    pending=[(e,v) for e in c['sources'] for v in OPTIONS if not any(r['source_run']==e['run_id'] and r['variant']==v for r in s['replays'])]
    with ThreadPoolExecutor(max_workers=c['replay_workers']) as pool:
        for r in pool.map(lambda x:replay_one(root,c,*x),pending):
            s['replays'].append(r);write(root/'state.json',s)
            print(json.dumps(dict(stage='replay',completed=len(s['replays']),variant=r['variant'])),flush=True)
    for e in c['sources']:
        if len({r['input_sha256'] for r in s['replays'] if r['source_run']==e['run_id']})!=1:raise ValueError('Replay input pairing broken')
    def summarize(v):
        rr=[r['metrics'] for r in s['replays'] if r['variant']==v];errors=[e for r in rr for e in r['errors_m']]
        return dict(coverage=sum(r['valid'] for r in rr)/sum(r['frames'] for r in rr),error_p95_m=quantile(errors,.95),
            initial_latency_p90_s=quantile([r['initial_latency_s'] if r['initial_latency_s'] is not None else 99 for r in rr],.9))
    scores={v:summarize(v) for v in OPTIONS};base=scores['BS'];eligible=['baseline','BS']
    for v in OPTIONS:
        if v in eligible:continue
        a=scores[v];a['eligible']=a['coverage']>=base['coverage']-.02 and a['error_p95_m']<=base['error_p95_m']*1.25 and a['initial_latency_p90_s']<=base['initial_latency_p90_s']+.1
        if a['eligible']:eligible.append(v)
    s['screen_variants']=eligible;write(root/'state.json',s);write(root/'replay-result.json',dict(scores=scores,eligible=eligible))


def verify(root,c,s):
    common_verify(root,c,s)
    validation=load(root/'validation.json');validation.update(source_sha256={p.name:sha(p) for p in manage.OPS.glob('motion_study*.py')},
        helpers_sha256={name:sha(manage.OPS/name) for name in ('perception_repair.py','perception_handoff.py','perception_study.py')})
    write(root/'validation.json',validation)


def regression_settings(c,scene,seed,variant):
    if scene not in c['regression_scenes']:raise ValueError('Regression scene outside frozen study')
    # The coordinator supports catalog scenes with fixed timing; the ordinary
    # submit parser restricts historical qualification to four focus scenes.
    # This local exploratory extension leaves that global policy unchanged.
    opts=dict(settings(scene,seed),execution_mode='realtime',timing_profile=None,max_fps=240,
        stage='screen',evidence='full',label='motion-regression-'+variant)
    row=jobs.validate({k:v for k,v in opts.items() if k in jobs.FIELDS})
    if row['physics']!='no-drag' or row['algorithm_profile']!='tracking_fire_v3' or row['fire_policy']!='advice':
        raise ValueError('Regression requires original lockstep physics/algorithm/fire contract')
    profile=load_profile(manage.OPS/'timing_profiles'/(c['backend_timing_profile']+'.json'))
    if profile['kind']!='fixed':raise ValueError('No scene-specific timing trace available for extra regression scenes')
    row.update(execution_mode='lockstep',timing_profile=c['backend_timing_profile'],algorithm_lifecycle='fresh',
        execution_profile='lockstep-'+c['backend_timing_profile'])
    return row


def submit_regression(root,c,s,variant,scene,seed,repeat):
    existing=next((e for e in s['jobs'] if e['phase']=='regression' and e['variant']==variant and e['scene']==scene and e['sensor_seed']==seed),None)
    if existing:return existing
    row=regression_settings(c,scene,seed,variant);row['repeat_id']=repeat
    identifier='job_motion_regression_'+uuid.uuid4().hex[:12];now=time.time();level=s['parallelism']
    with jobs.connect() as db,jobs.transaction(db):
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(identifier,'queued',now,now,json.dumps(dict(version=1,_protocol=2,
            _motion_regression=True,parallelism=level,study=str(root),scope='Exploratory catalog scenes with frozen fixed timing; no global qualification')),
            c['bundles'][variant],level,1,None))
        db.execute('INSERT INTO items(job_id,ordinal,state,settings) VALUES(?,?,?,?)',(identifier,0,'pending',json.dumps(row)))
        jobs.event(db,identifier,'Study-local fixed-timing regression; original global four-scene validation unchanged')
    entry=dict(phase='regression',variant=variant,scene=scene,sensor_seed=seed,repeat_id=repeat,id=identifier,level=level)
    s['jobs'].append(entry);write(root/'state.json',s);return entry


def run(root):
    c=initialize(root)
    with (root/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);s=load(root/'state.json')
        if s['state']=='completed':
            if not (root/'report-seal.json').exists():
                from motion_study_report import finish
                finish(root)
            return
        try:
            s['state']='running';write(root/'state.json',s);replays(root,c,s)
            with (root/'replay-checks.log').open('w') as log:
                subprocess.run([sys.executable,'-B',str(manage.OPS/'motion_study_checks.py'),'--output',str(root)],check=True,timeout=45,stdout=log,stderr=subprocess.STDOUT)
            parallel_probe(root,c,s);batch(root,c,s,'screen',s['screen_variants'],c['screen_seeds'])
            rows=[r for r in s['runs'] if r['phase']=='screen'];effects=[effect(rows,v,c) for v in s['screen_variants'] if v not in ('baseline','BS')]
            write(root/'screen-result.json',effects);eligible=[r for r in effects if r['passed']]
            chosen=max(eligible,key=lambda r:r['groups']['all']['candidate']['weighted_hit_rate'])['variant'] if eligible else None
            s['selected']=chosen;write(root/'state.json',s)
            # Regression is mandatory for the full 1-3 implementation even if
            # the efficacy gate rejects it. It is a diagnosis, not a promotion.
            variants=['baseline',chosen or 'ALL'];entries=[]
            write(root/'regression-extension.json',dict(contract_sha256=sha(root/'contract.json'),
                source_sha256=sha(Path(__file__)),scenes=c['regression_scenes'],profile=c['backend_timing_profile'],
                fixed_timing_only=True,original_global_scene_validation_unchanged=True,capacity_registered=False,
                global_validation_sha256=sha(manage.OPS/'background_jobs.py'),
                reason='Ordinary queue parser limits historical lockstep qualification to four scenes; frozen coordinator supports generic catalog scenes with fixed timing. This exploratory study validates actual scene via existing parser/input rules before applying same fixed clock.'))
            for i,seed in enumerate(c['regression_seeds']):
                for scene in c['regression_scenes']:
                    for v in variants:entries.append(submit_regression(root,c,s,v,scene,seed,i))
            collect(root,c,s,entries)
            if chosen:
                batch(root,c,s,'confirm',['baseline',chosen],c['confirm_seeds'])
                result=effect([r for r in s['runs'] if r['phase']=='confirm'],chosen,c,True);write(root/'confirm-result.json',result)
                s['backend_confirmed']=result['passed']
                if result['passed']:
                    batch(root,c,s,'repeat',['baseline',chosen],[c['confirm_seeds'][0]])
                    pairs=[]
                    for r in [r for r in s['runs'] if r['phase']=='repeat']:
                        a=next(a for a in s['runs'] if a['phase']=='confirm' and all(a[k]==r[k] for k in ('variant','scene','sensor_seed')))
                        pairs.append(dict(variant=r['variant'],scene=r['scene'],same_scores=all(a[k]==r[k] for k in ('shots','hits','weight')),
                            same_behavior=behavior(manage.OPS/'experiments'/a['run_id'])==behavior(manage.OPS/'experiments'/r['run_id'])))
                    s['ui_eligible']=all(r['same_scores'] and r['same_behavior'] for r in pairs)
                    write(root/'repeatability.json',dict(passed=s['ui_eligible'],pairs=pairs))
            verify(root,c,s);s.update(state='completed',ended=time.time());write(root/'state.json',s)
            from motion_study_report import finish
            finish(root)
        except BaseException as error:
            s.update(state='failed',error=repr(error));write(root/'state.json',s);raise


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('freeze','run'));p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if a.action=='freeze':initialize(a.output.resolve())
    else:run(a.output.resolve())
