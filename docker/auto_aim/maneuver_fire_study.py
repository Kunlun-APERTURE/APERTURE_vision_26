"""Frozen closed-loop shooting verification of the unretuned S/I predictors."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import time

import background_jobs as jobs
import manage
from decision_benchmark import sha,verify_bundle
from decision_benchmark_report import aggregate
from maneuver_study import CORE_FIELDS
from motion_study import submit_regression
from perception_repair import parallel_probe,batch,collect
from perception_study import protected,effect
from report import load,write
from sync_first_divergence import run_one
from sync_first_divergence_analysis import primary

ROOT=manage.OPS/'experiments/maneuver-fire-v6-20261026'
PARENT=manage.OPS/'experiments/motion-v5-20261024'
SHADOW=manage.OPS/'experiments/maneuver-v6-20261026-r2'
VARIANTS=('baseline','BS','P','S','I')
FILES=('CMakeLists.txt','tasks/auto_aim/maneuver_filter.hpp','tasks/auto_aim/motion_model.hpp',
       'tasks/auto_aim/target.cpp','tasks/auto_aim/target.hpp','tasks/auto_aim/aimer.cpp',
       'tests/maneuver_filter_test.cpp','tests/motion_test.cpp')


def initialize(root):
    if (root/'contract.json').exists():
        c=load(root/'contract.json');s=load(root/'state.json')
        if sha(root/'contract.json')!=s['contract_sha256'] or protected()!=c['protected']:raise ValueError('Frozen experiment changed')
        for b in c['bundles'].values():verify_bundle(Path(b))
        return c
    pc=load(PARENT/'contract.json');prior=load(SHADOW/'contract.json');ps=load(PARENT/'state.json')
    if protected()!=pc['protected']:raise ValueError('Protected baseline changed')
    checks={}
    for name in ('maneuver','motion','perception'):
        match=re.search(r'PASSED (\d+) .*checks',(root/(name+'-checks.log')).read_text())
        if not match:raise ValueError('Complete checks before freeze')
        checks[name]=int(match[1])
    binary=root/'build-source/sp_vision_25/build/motion/sim_auto_aim'
    seeds=set([3091,*range(3101,3106),*range(3201,3211),*range(3301,3304)])
    with jobs.connect() as db:
        used={json.loads(r[0]).get('sensor_seed') for r in db.execute('SELECT settings FROM items')}
    if used & seeds:raise ValueError('Use genuinely new frozen seeds: '+str(used & seeds))
    sources=[]
    for e in pc['sources'][:2]:
        refs={v:next(r for r in ps['replays'] if r['source_run']==e['run_id'] and r['variant']==v) for v in ('baseline','BS','P')}
        sources.append(dict(e,references={v:dict(output=r['output'],input_sha256=r['input_sha256'],events_sha256=sha(Path(r['output'])/'events.jsonl')) for v,r in refs.items()}))
    c=dict(version=1,created=time.time(),parent=str(PARENT),prior_shadow=str(SHADOW),protected=protected(),
      replay_sources=sources,maximum_replays=10,bundles={},variants=VARIANTS,candidate_binary_sha256=sha(binary),
      source_sha256={f:sha(root/'build-source/sp_vision_25'/f) for f in FILES},
      model_parameters=prior['model_parameters'],runtime_files=pc['runtime_files'],backend_timing_profile=pc['backend_timing_profile'],
      probe_seed=3091,screen_seeds=list(range(3101,3106)),confirm_seeds=list(range(3201,3211)),
      regression_seeds=list(range(3301,3304)),regression_scenes=['hf_static_5','hf_spin_pos','hf_spin_neg','ego_right_centered'],
      effect_gate=pc['effect_gate'],bootstrap=pc['bootstrap'],cpp_checks=checks,
      maximum_trials=dict(probe=20,screen=50,regression=42,confirm=60,repeat=6,ui=0),
      authorization='User explicitly requested shooting experiments despite the prior stationary replay gate failure; no model retuning or revised replay gate.',
      integration='S/I change capture-posterior forecasts only, including XYZ and velocity/covariance on aim copies; '
                  'BS accepted measurements and capture-time tracking remain CV. Frozen capture-time modes propagate without future pseudo-measurements.',
      selection='S/I only; must pass main effect gates vs v3 and BS, and each exploratory regression must lose no more than 2pp exact/weighted or 5% hits/s vs v3. '
                'Choose highest main weighted rate; no candidate means no confirm. Independent confirmation compares v3, BS and selected candidate.',
      automatic_promotion=False,capacity_registration=False,maximum_complete_trial_s=40,
      historical_files={str(p):sha(p) for p in (SHADOW/'report.md',SHADOW/'contract.json',SHADOW/'validation.json',PARENT/'report.md',PARENT/'contract.json')})
    for v in VARIANTS:
        base=Path(pc['bundles'][v if v in ('baseline','BS','P') else 'BS'])
        dst=jobs.STATE/'bundles'/('maneuver_fire_'+v+'_'+sha(binary)[:12])
        if dst.exists():raise ValueError('Refuse to overwrite candidate bundle')
        shutil.copytree(base,dst,copy_function=os.link)
        for f in FILES:
            out=dst/'sp_vision_25'/f;out.parent.mkdir(parents=True,exist_ok=True);out.unlink(missing_ok=True)
            shutil.copy2(root/'build-source/sp_vision_25'/f,out)
        out=dst/'sp_vision_25/build/simulator/sim_auto_aim';out.unlink();shutil.copy2(binary,out)
        cfg=dst/'sp_vision_25/configs/simulator.yaml';text=cfg.read_text();cfg.unlink()
        text+='\ndiagnosis:\n  enabled: true\n'
        if v in ('S','I'):text+='\nmotion_v6:\n  predictor: '+v+'\n'
        cfg.write_text(text)
        meta=load(base/'bundle.json');meta.update(bundle_id=dst.name,created=time.time(),parent_bundle=str(base),
          maneuver_predictor=v if v in ('S','I') else 'off',research_only=True)
        for f in (*FILES,'build/simulator/sim_auto_aim','configs/simulator.yaml'):meta['files']['sp_vision_25/'+f]=sha(dst/'sp_vision_25'/f)
        (dst/'bundle.json').unlink();write(dst/'bundle.json',meta);verify_bundle(dst);c['bundles'][v]=str(dst)
    write(root/'contract.json',c);write(root/'state.json',dict(state='frozen',contract_sha256=sha(root/'contract.json'),
      replays=[],runs=[],jobs=[],screen_variants=list(VARIANTS),parallelism=1))
    return c


def integration_replay(root,c,s):
    def one(entry,v):
        source=manage.OPS/'experiments'/entry['run_id'];out=root/'replays'/source.name/v
        result=load(out/'result.json') if (out/'result.json').exists() else run_one(source,out,Path(c['bundles'][v]),
          seconds=entry['duration_s'],detector_history='session',diagnosis=True)
        ref=entry['references'][v if v in ('baseline','BS','P') else 'BS'];path=Path(ref['output'])
        assert sha(path/'events.jsonl')==ref['events_sha256'] and result['primary_input_sha256']==ref['input_sha256'] and result['complete']
        original=primary(path);current=primary(out);assert len(original)==len(current)
        fields=CORE_FIELDS if v in ('baseline','BS','P') else ('observations','filter_state','tracker','plate_velocities')
        differences={f:0 for f in fields};active=0;prediction_error=0.
        for a,b in zip(original,current):
            assert a['frame']==b['frame'] and a['jpeg_sha256']==b['jpeg_sha256']
            for f in fields:differences[f]+=a['result'].get(f)!=b['result'].get(f)
            if v in ('S','I') and b['result'].get('valid'):
                r=b['result'];sid=r['selected_plate_id'];h=r['prediction_horizon_s']
                bank=r.get('continuity',{}).get('filter',{}).get('motion_v6',{}).get('plates',[])
                if sid<0 or sid>=len(bank) or not bank[sid].get('initialized'):continue
                d=bank[sid]
                def point(state,tau):
                    g=tau*(h+tau*math.expm1(-h/tau))
                    return [state[3*j]+h*state[3*j+1]+g*state[3*j+2] for j in range(3)]
                if v=='S':expected=point(d['single_state'],.15)
                else:expected=[sum(p*point(m['state'],m['tau'])[j] for p,m in zip(d['probability'],d['models'])) for j in range(3)]
                prediction_error=max(prediction_error,math.dist(expected,r['aim_point']));active+=1
        assert not any(differences.values()),(v,differences)
        if v in ('S','I'):assert active>30 and prediction_error<1e-4,(active,prediction_error)
        return dict(source_run=source.name,variant=v,frames=len(current),output=str(out),input_sha256=result['primary_input_sha256'],
          differences=differences,active_forecasts=active,forecast_reconstruction_max_error_m=prediction_error,
          output_sha256=sha(out/'events.jsonl'))
    pending=[(e,v) for e in c['replay_sources'] for v in VARIANTS if not any(r['source_run']==e['run_id'] and r['variant']==v for r in s['replays'])]
    with ThreadPoolExecutor(max_workers=2) as pool:
        for r in pool.map(lambda p:one(*p),pending):
            s['replays'].append(r);write(root/'state.json',s)
            print(json.dumps(dict(stage='integration',completed=len(s['replays']),variant=r['variant'])),flush=True)


def compare(rows,v,c,reference='baseline',independent=False):
    selected=[dict(r,variant='baseline' if r['variant']==reference else v) for r in rows if r['variant'] in (reference,v)]
    result=effect(selected,v,c,independent);result['reference']=reference;return result


def regression_gate(rows,v):
    checks={};groups={}
    for scene in ('hf_static_5','hf_spin_pos','hf_spin_neg','ego_right_centered'):
        a=aggregate([r for r in rows if r['scene']==scene and r['variant']=='baseline'])
        b=aggregate([r for r in rows if r['scene']==scene and r['variant']==v]);groups[scene]=dict(baseline=a,candidate=b)
        enough=a['shots']>=30 and b['shots']>=30
        available=a['shots']>0 and b['shots']>0
        checks[scene+'_shots']=enough
        checks[scene+'_weighted']=available and b['weighted_hit_rate']>=a['weighted_hit_rate']-.02
        checks[scene+'_exact']=available and b['target_hit_rate']>=a['target_hit_rate']-.02
        checks[scene+'_hits_per_s']=b['hits_per_s']>=a['hits_per_s']*.95
    return dict(variant=v,passed=all(checks.values()),checks=checks,groups=groups)


def run(root):
    c=initialize(root)
    with (root/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);s=load(root/'state.json')
        if s['state']=='completed':return
        try:
            s['state']='running';write(root/'state.json',s);integration_replay(root,c,s)
            if not (root/'parallel-result.json').exists():parallel_probe(root,c,s)
            batch(root,c,s,'screen',list(VARIANTS),c['screen_seeds'])
            entries=[]
            for scene in c['regression_scenes']:
                variants=VARIANTS if scene=='hf_static_5' else ('baseline','S','I')
                for seed in c['regression_seeds']:
                    for v in variants:
                        entries.append(submit_regression(root,c,s,v,scene,seed,seed-c['regression_seeds'][0]))
            collect(root,c,s,entries)
            rows=[r for r in s['runs'] if r['phase']=='screen'];reg=[r for r in s['runs'] if r['phase']=='regression']
            effects=[compare(rows,v,c,ref) for v in ('S','I') for ref in ('baseline','BS','P')]
            regressions=[regression_gate(reg,v) for v in ('S','I')]
            write(root/'screen-result.json',effects);write(root/'regression-result.json',regressions)
            eligible=[v for v in ('S','I') if all(x['passed'] for x in effects if x['variant']==v and x['reference'] in ('baseline','BS'))
              and next(x['passed'] for x in regressions if x['variant']==v)]
            chosen=max(eligible,key=lambda v:aggregate([r for r in rows if r['variant']==v])['weighted_hit_rate']) if eligible else None
            s['selected']=chosen;write(root/'state.json',s)
            if chosen:
                batch(root,c,s,'confirm',['baseline','BS',chosen],c['confirm_seeds'])
                confirm=[compare([r for r in s['runs'] if r['phase']=='confirm'],chosen,c,ref,True) for ref in ('baseline','BS')]
                write(root/'confirm-result.json',confirm);s['backend_confirmed']=all(x['passed'] for x in confirm)
                if s['backend_confirmed']:batch(root,c,s,'repeat',['baseline','BS',chosen],[c['confirm_seeds'][0]])
            s.update(state='completed',ended=time.time());write(root/'state.json',s)
            from maneuver_fire_report import finish
            finish(root)
        except BaseException as error:
            s.update(state='failed',error=repr(error));write(root/'state.json',s);raise


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('freeze','run'));parser.add_argument('--output',type=Path,default=ROOT)
    args=parser.parse_args();root=args.output.resolve()
    if args.action=='freeze':initialize(root)
    else:run(root)
