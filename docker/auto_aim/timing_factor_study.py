"""Frozen model-by-launch-offset shooting experiment; config changes only."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import time

import background_jobs as jobs
import manage
from decision_benchmark import sha,verify_bundle
from decision_benchmark_report import aggregate,behavior
from maneuver_study import CORE_FIELDS
from maneuver_fire_study import compare,regression_gate
from motion_study import submit_regression
from perception_repair import parallel_probe,batch,collect
from perception_study import protected
from report import load,write
from sync_first_divergence import run_one
from sync_first_divergence_analysis import primary

ROOT=manage.OPS/'experiments/timing-factor-20261026'
PARENT=manage.OPS/'experiments/maneuver-fire-v6-20261026'
DIAGNOSIS=manage.OPS/'experiments/maneuver-shot-diagnosis-20261026'
VARIANTS=('baseline','BS15','BS4','I15','I4')
BASE_VARIANT=dict(baseline='baseline',BS15='BS',BS4='BS',I15='I',I4='I')
DELAY_VARIANTS=('BS4','I4')


def initialize(root):
    root.mkdir(parents=True,exist_ok=True)
    if (root/'contract.json').exists():
        c=load(root/'contract.json');s=load(root/'state.json')
        if sha(root/'contract.json')!=s['contract_sha256'] or protected()!=c['protected']:
            raise ValueError('Frozen contract or protected baseline changed')
        for b in c['bundles'].values():verify_bundle(Path(b))
        return c
    pc=load(PARENT/'contract.json');ps=load(PARENT/'state.json');assert ps['state']=='completed'
    dc=load(DIAGNOSIS/'contract.json');dv=load(DIAGNOSIS/'validation.json');assert dv['passed']
    ledger=load(DIAGNOSIS/'shot-ledger.json')
    delays=[(x['launch_stamp_ns']-x['decision_stamp_ns'])*1e-9 for x in ledger]
    delay=statistics.median(delays);assert len(delays)==461 and .004<delay<.005
    assert protected()==pc['protected']
    seeds={3391,*range(3401,3406),*range(3501,3511),*range(3601,3604)}
    with jobs.connect() as db:used={json.loads(x[0]).get('sensor_seed') for x in db.execute('SELECT settings FROM items')}
    assert not used & seeds,('seed already used',used & seeds)
    sources=[]
    for e in pc['replay_sources']:
        refs={v:next(x for x in ps['replays'] if x['source_run']==e['run_id'] and x['variant']==v) for v in ('baseline','BS','I')}
        sources.append(dict(e,references={v:dict(output=x['output'],events_sha256=x['output_sha256'],input_sha256=x['input_sha256']) for v,x in refs.items()}))
    history={}
    for folder in (PARENT,DIAGNOSIS):
        for name,h in load(folder/'report-seal.json').items():assert sha(folder/name)==h;history[str(folder/name)]=h
        history[str(folder/'report-seal.json')]=sha(folder/'report-seal.json')
    c=dict(version=1,created=time.time(),parent=str(PARENT),diagnosis=str(DIAGNOSIS),variants=VARIANTS,
      short_delay_s=delay,delay_provenance=dict(shots=len(ledger),statistic='median direct decision-to-launch on unique pulse/gimbal-accepted shots',
        minimum_s=min(delays),maximum_s=max(delays),source_sha256=sha(DIAGNOSIS/'shot-ledger.json'),hardware_transfer=False),
      protected=pc['protected'],candidate_binary_sha256=pc['candidate_binary_sha256'],model_parameters=pc['model_parameters'],
      runtime_files=pc['runtime_files'],backend_timing_profile=pc['backend_timing_profile'],replay_sources=sources,maximum_replays=10,
      bundles={},probe_seed=3391,screen_seeds=list(range(3401,3406)),confirm_seeds=list(range(3501,3511)),
      regression_seeds=list(range(3601,3604)),regression_scenes=['hf_static_5','hf_spin_pos','hf_spin_neg','ego_right_centered'],
      effect_gate=pc['effect_gate'],bootstrap=pc['bootstrap'],
      maximum_trials=dict(probe=20,screen=50,regression=60,confirm=60,repeat=6,ui=0),
      maximum_complete_trial_s=40,automatic_promotion=False,capacity_registration=False,
      design='Full 2x2: BS/I x nominal 15ms/directly measured launch offset, plus unchanged v3. Same binary and model parameters; only the two short-delay configs change. '
             'No score-based delay scan, no widened fire window, no physical/scoring changes.',
      selection='Only BS4/I4 can be selected. Each must pass main effect gates vs v3 and BS15 and all regression noninferiority gates vs v3. '
                'Choose highest main weighted hit rate. Then confirm on unseen seeds versus v3 and BS15 with paired bootstrap lower bounds >0.',
      hypothesis='Does measured launch offset improve shots within either estimator, and is its effect different for IMM? '
                 'The original 15ms could include continuous tracking compensation, so measurement alone does not establish correctness.',
      historical_files=history)
    for v in VARIANTS:
        base=Path(pc['bundles'][BASE_VARIANT[v]])
        target=jobs.STATE/'bundles'/('timing_factor_'+v+'_'+sha(DIAGNOSIS/'shot-ledger.json')[:12])
        if target.exists():raise ValueError('Refuse to overwrite frozen bundle')
        shutil.copytree(base,target,copy_function=os.link)
        if v in DELAY_VARIANTS:
            p=target/'sp_vision_25/configs/simulator.yaml';text=p.read_text();p.unlink()
            text+='\nmotion_v5:\n  calibrated_delay: true\n  launch_delay_s: '+repr(delay)+'\n';p.write_text(text)
        meta=load(base/'bundle.json');meta.update(bundle_id=target.name,created=time.time(),parent_bundle=str(base),
          timing_factor=dict(model='I' if v.startswith('I') else 'BS' if v!='baseline' else 'v3',delay_s=delay if v in DELAY_VARIANTS else .015))
        meta['files']['sp_vision_25/configs/simulator.yaml']=sha(target/'sp_vision_25/configs/simulator.yaml')
        (target/'bundle.json').unlink();write(target/'bundle.json',meta);verify_bundle(target)
        assert sha(target/'sp_vision_25/build/simulator/sim_auto_aim')==c['candidate_binary_sha256']
        old=load(base/'bundle.json')
        changes=[p for p,h in old['files'].items() if meta['files'][p]!=h]
        assert changes==(['sp_vision_25/configs/simulator.yaml'] if v in DELAY_VARIANTS else [])
        c['bundles'][v]=str(target)
    write(root/'contract.json',c);write(root/'state.json',dict(state='frozen',contract_sha256=sha(root/'contract.json'),
      replays=[],runs=[],jobs=[],screen_variants=list(VARIANTS),parallelism=1))
    return c


def replays(root,c,s):
    def one(e,v):
        source=manage.OPS/'experiments'/e['run_id'];out=root/'replays'/source.name/v
        result=load(out/'result.json') if (out/'result.json').exists() else run_one(source,out,Path(c['bundles'][v]),
          seconds=e['duration_s'],detector_history='session',diagnosis=True)
        reference=e['references'][BASE_VARIANT[v]];original=primary(Path(reference['output']));current=primary(out)
        assert result['complete'] and result['primary_input_sha256']==reference['input_sha256']
        assert sha(Path(reference['output'])/'events.jsonl')==reference['events_sha256'] and len(original)==len(current)
        fields=CORE_FIELDS if v not in DELAY_VARIANTS else ('observations','filter_state','tracker','plate_velocities')
        differences={k:0 for k in fields};active=0;max_error=0.;seen_delay=[]
        for a,b in zip(original,current):
            assert a['frame']==b['frame'] and a['jpeg_sha256']==b['jpeg_sha256']
            for k in fields:differences[k]+=a['result'].get(k)!=b['result'].get(k)
            r=b['result'];diag=(r.get('diagnosis') or {}).get('aimer') or {}
            if 'launch_delay_s' in diag:seen_delay.append(diag['launch_delay_s'])
            if v.startswith('I') and r.get('valid'):
                banks=r.get('continuity',{}).get('filter',{}).get('motion_v6',{}).get('plates',[]);sid=r['selected_plate_id'];h=r['prediction_horizon_s']
                if 0<=sid<len(banks) and banks[sid].get('initialized'):
                    bank=banks[sid]
                    def point(m,j):
                        x=m['state'];tau=m['tau'];g=tau*(h+tau*math.expm1(-h/tau));return x[3*j]+h*x[3*j+1]+g*x[3*j+2]
                    expected=[sum(p*point(m,j) for p,m in zip(bank['probability'],bank['models'])) for j in range(3)]
                    max_error=max(max_error,math.dist(expected,r['aim_point']));active+=1
        assert not any(differences.values()),(v,differences)
        expected=c['short_delay_s'] if v in DELAY_VARIANTS else .015
        assert len(seen_delay)>30 and all(abs(x-expected)<1e-12 for x in seen_delay)
        if v.startswith('I'):assert active>30 and max_error<1e-4
        return dict(variant=v,source_run=source.name,output=str(out),frames=len(current),differences=differences,
          launch_delay_s=expected,active_forecasts=active,forecast_reconstruction_max_error_m=max_error,
          input_sha256=result['primary_input_sha256'],output_sha256=sha(out/'events.jsonl'))
    pending=[(e,v) for e in c['replay_sources'] for v in VARIANTS if not any(x['source_run']==e['run_id'] and x['variant']==v for x in s['replays'])]
    with ThreadPoolExecutor(max_workers=2) as pool:
        for row in pool.map(lambda p:one(*p),pending):
            s['replays'].append(row);write(root/'state.json',s)
            print(json.dumps(dict(stage='replay',completed=len(s['replays']),variant=row['variant'])),flush=True)


def run(root):
    c=initialize(root)
    with (root/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);s=load(root/'state.json')
        if s['state']=='completed':return
        try:
            s['state']='running';s.pop('error',None);write(root/'state.json',s);replays(root,c,s)
            if not (root/'parallel-result.json').exists():parallel_probe(root,c,s)
            batch(root,c,s,'screen',list(VARIANTS),c['screen_seeds'])
            entries=[]
            for scene in c['regression_scenes']:
                for seed in c['regression_seeds']:
                    for v in VARIANTS:entries.append(submit_regression(root,c,s,v,scene,seed,seed-c['regression_seeds'][0]))
            collect(root,c,s,entries)
            rows=[r for r in s['runs'] if r['phase']=='screen'];rr=[r for r in s['runs'] if r['phase']=='regression']
            effects=[compare(rows,v,c,reference=ref) for v in DELAY_VARIANTS for ref in ('baseline','BS15')]
            regressions=[regression_gate(rr,v) for v in DELAY_VARIANTS]
            write(root/'screen-result.json',effects);write(root/'regression-result.json',regressions)
            eligible=[v for v in DELAY_VARIANTS if all(x['passed'] for x in effects if x['variant']==v)
                      and next(x['passed'] for x in regressions if x['variant']==v)]
            selected=max(eligible,key=lambda v:aggregate([r for r in rows if r['variant']==v])['weighted_hit_rate']) if eligible else None
            s['selected']=selected;write(root/'state.json',s)
            if selected:
                batch(root,c,s,'confirm',['baseline','BS15',selected],c['confirm_seeds'])
                confirm=[compare([r for r in s['runs'] if r['phase']=='confirm'],selected,c,ref,True) for ref in ('baseline','BS15')]
                write(root/'confirm-result.json',confirm);s['backend_confirmed']=all(x['passed'] for x in confirm)
                if s['backend_confirmed']:
                    batch(root,c,s,'repeat',['baseline','BS15',selected],[c['confirm_seeds'][0]])
                    pairs=[]
                    for r in [r for r in s['runs'] if r['phase']=='repeat']:
                        a=next(a for a in s['runs'] if a['phase']=='confirm' and all(a[k]==r[k] for k in ('variant','scene','sensor_seed')))
                        pairs.append(dict(variant=r['variant'],scene=r['scene'],same_scores=all(a[k]==r[k] for k in ('shots','hits','weight')),
                          same_behavior=behavior(manage.OPS/'experiments'/a['run_id'])==behavior(manage.OPS/'experiments'/r['run_id'])))
                    s['ui_eligible']=len(pairs)==6 and all(x['same_scores'] and x['same_behavior'] for x in pairs)
                    write(root/'repeatability.json',dict(passed=s['ui_eligible'],pairs=pairs))
            s.update(state='completed',ended=time.time());write(root/'state.json',s)
            from timing_factor_report import finish
            finish(root)
        except BaseException as error:
            s.update(state='failed',error=repr(error));write(root/'state.json',s);raise


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('freeze','run'));parser.add_argument('--output',type=Path,default=ROOT)
    args=parser.parse_args();root=args.output.resolve()
    if args.action=='freeze':initialize(root)
    else:run(root)
