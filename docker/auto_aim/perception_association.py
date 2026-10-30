"""One image assignment shared by pose hints and updates; bounded backend study."""
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

import background_jobs as jobs
import manage
from decision_benchmark import sha,verify_bundle
from decision_benchmark_report import behavior
from perception_study import protected,quantile,effect
from perception_handoff import FILES as PREVIOUS_FILES,OLD_B,verify as prior_verify
from perception_repair import batch,parallel_probe,metrics_replay,FIELDS
from report import load,write
from sync_first_divergence import run_one

PARENT=manage.OPS/'experiments/perception-handoff-20261023'
H3=dict(OLD_B,initial_handoff_guard=True,initial_handoff_anchor_only=True,initial_handoff_frames=3)
OPTIONS=dict(baseline={},S=dict(image_association=True),BS=dict(OLD_B,image_association=True),H3=H3)
CANDIDATES=('S','BS')
FILES=(*PREVIOUS_FILES,'tasks/auto_aim/image_association.hpp','tasks/auto_aim/solver.hpp')


def initialize(root):
    root.mkdir(parents=True,exist_ok=True)
    if (root/'contract.json').exists():
        c=load(root/'contract.json');s=load(root/'state.json')
        if sha(root/'contract.json')!=s['contract_sha256'] or protected()!=c['protected']:raise ValueError('Frozen study changed')
        for b in c['bundles'].values():verify_bundle(Path(b))
        return c
    pc=load(PARENT/'contract.json');ps=load(PARENT/'state.json')
    if ps['state']!='completed':raise ValueError('Complete previous handoff study first')
    sources=[]
    for r in ps['runs']:
        if r['phase']!='screen' or r['variant'] not in ('baseline','H3'):continue
        folder=manage.OPS/'experiments'/r['run_id'];m=load(folder/'manifest.json')
        sources.append(dict(run_id=r['run_id'],variant=r['variant'],engine='backend',scene=r['scene'],sensor_seed=r['sensor_seed'],
            duration_s=m['duration_s'],source_hashes={n:sha(folder/n) for n in
            ('manifest.json','summary.json','run-state.json','telemetry.jsonl','session-evidence/events.jsonl','session-evidence/input.mjpg')}))
    sources.extend(e for e in pc['sources'] if e['engine']=='ui' and e['variant']=='baseline')
    if len(sources)!=24:raise ValueError('Expected all 20 backend and four native UI baseline recordings')
    binary=root/'build-source/sp_vision_25/build/perception/sim_auto_aim'
    checked=re.search(r'PASSED (\d+) perception checks',(root/'cpp-checks.log').read_text())
    if not checked:raise ValueError('C++ checks required')
    c=dict(version=1,created=time.time(),parent=str(PARENT),parent_contract_sha256=sha(PARENT/'contract.json'),
        protected=protected(),sources=sources,candidates=CANDIDATES,options=OPTIONS,bundles={},base_bundle=pc['base_bundle'],
        candidate_binary_sha256=sha(binary),build_source_sha256={f:sha(root/'build-source/sp_vision_25'/f) for f in FILES},
        geometry_checks=int(checked[1]),runtime_files=pc['runtime_files'],backend_timing_profile=pc['backend_timing_profile'],
        replay_workers=2,maximum_replays=82,probe_seed=2491,screen_seeds=list(range(2501,2506)),confirm_seeds=list(range(2601,2611)),
        effect_gate=pc['effect_gate'],bootstrap=pc['bootstrap'],
        replay_gate=dict(coverage_drop=.02,initial_latency_p90_add_s=.1,center_error_p95_max_ratio=1.10),
        maximum_trials=dict(parallel_probe=12,screen=30,conditional_confirmation=40,repeatability=4,ui=0),
        algorithm='Exact partial image assignment with four plate masks, normalized four-corner squared residual, unmatched cost 4, center gate 80px inherited from yaw hints, calibrated camera visibility. Same selected id supplies yaw hint and is assessed for correction; unmatched observations do not update this target.',
        defaults_unchanged=True,automatic_promotion=False,capacity_registration=False,
        truth_policy='Enemy truth is used only by post-inference replay metrics and independent scoring.',
        unchanged='No refinement guard, no H3 handoff guard in candidates, no fire/physics/noise/timing changes.')
    for name,opts in OPTIONS.items():
        base=Path(c['base_bundle']);target=jobs.STATE/'bundles'/('association_'+name+'_'+sha(binary)[:12])
        if target.exists():raise ValueError('Refuse existing candidate bundle')
        shutil.copytree(base,target,copy_function=os.link)
        for f in FILES:
            dst=target/'sp_vision_25'/f;dst.parent.mkdir(parents=True,exist_ok=True);dst.unlink(missing_ok=True)
            shutil.copy2(root/'build-source/sp_vision_25'/f,dst)
        dst=target/'sp_vision_25/build/simulator/sim_auto_aim';dst.unlink();shutil.copy2(binary,dst)
        dst=target/'sp_vision_25/configs/simulator.yaml';txt=dst.read_text();dst.unlink()
        dst.write_text(txt+('\nperception_v1:\n'+''.join('  '+k+': '+str(v).lower()+'\n' for k,v in opts.items()) if opts else ''))
        meta=load(base/'bundle.json');meta.update(bundle_id=target.name,created=time.time(),parent_bundle=str(base),perception_options=opts)
        for f in FILES:meta['files']['sp_vision_25/'+f]=sha(target/'sp_vision_25'/f)
        for f in ('build/simulator/sim_auto_aim','configs/simulator.yaml'):meta['files']['sp_vision_25/'+f]=sha(target/'sp_vision_25'/f)
        (target/'bundle.json').unlink();write(target/'bundle.json',meta);verify_bundle(target);c['bundles'][name]=str(target)
    write(root/'contract.json',c);write(root/'state.json',dict(state='frozen',contract_sha256=sha(root/'contract.json'),replays=[],runs=[],jobs=[]))
    return c


def replay_one(root,c,e,variant):
    src=manage.OPS/'experiments'/e['run_id'];out=root/'replays'/src.name/variant
    result=load(out/'result.json') if (out/'result.json').exists() else run_one(src,out,Path(c['bundles'][variant]),
        seconds=e['duration_s'],detector_history='session',diagnosis=True)
    native=e['variant']==variant
    if not result['complete']:raise ValueError('Incomplete association replay')
    if native and any(result['differences_from_recorded'][k]['different_frames'] for k in FIELDS):raise ValueError('Disabled association did not reproduce native source')
    return dict(variant=variant,source_run=src.name,source_variant=e['variant'],engine=e['engine'],scene=e['scene'],sensor_seed=e['sensor_seed'],
        native_configuration=native,output=str(out),frames=result['frames'],input_sha256=result['primary_input_sha256'],metrics=metrics_replay(out,src),
        output_hashes={n:sha(out/n) for n in ('plan.json','events.jsonl','result.json')})


def replays(root,c,s):
    pending=[(e,v) for e in c['sources'] for v in ('baseline',*CANDIDATES,*(['H3'] if e['variant']=='H3' else []))
        if not any(r['source_run']==e['run_id'] and r['variant']==v for r in s['replays'])]
    with ThreadPoolExecutor(max_workers=c['replay_workers']) as pool:
        for r in pool.map(lambda pair:replay_one(root,c,*pair),pending):
            s['replays'].append(r);write(root/'state.json',s)
            print(json.dumps(dict(stage='replay',completed=len(s['replays']),variant=r['variant'],source=r['source_run'])),flush=True)
    for e in c['sources']:
        rr=[r for r in s['replays'] if r['source_run']==e['run_id']]
        if len({r['input_sha256'] for r in rr})!=1:raise ValueError('Unequal archived inputs')
    def summary(variant):
        mm=[r['metrics'] for r in s['replays'] if r['variant']==variant];errors=[x for m in mm for x in m['errors_m']]
        return dict(coverage=sum(m['valid'] for m in mm)/sum(m['frames'] for m in mm),error_p95_m=quantile(errors,.95),
            initial_latency_p90_s=quantile([m['initial_latency_s'] if m['initial_latency_s'] is not None else 99. for m in mm],.9))
    base=summary('baseline');scores={};eligible=[]
    for v in CANDIDATES:
        a=summary(v);checks=dict(coverage=a['coverage']>=base['coverage']-.02,
            latency=a['initial_latency_p90_s']<=base['initial_latency_p90_s']+.1,
            center_p95=a['error_p95_m'] is not None and a['error_p95_m']<=base['error_p95_m']*1.10)
        scores[v]=dict(metrics=a,checks=checks,eligible=all(checks.values()))
        if all(checks.values()):eligible.append(v)
    s['screen_variants']=['baseline',*eligible];write(root/'state.json',s)
    write(root/'replay-result.json',dict(baseline=base,candidates=scores,eligible=eligible))


def verify(root,c,s):
    if sha(PARENT/'contract.json')!=c['parent_contract_sha256']:raise ValueError('Parent definition changed')
    prior_verify(root,c,s)
    v=load(root/'validation.json');v.update(source_sha256={p.name:sha(p) for p in manage.OPS.glob('perception_association*.py')},
        helpers_sha256={name:sha(manage.OPS/name) for name in ('perception_handoff.py','perception_repair.py','perception_study.py')})
    write(root/'validation.json',v)


def run(root):
    c=initialize(root)
    with (root/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);s=load(root/'state.json')
        if s['state']=='completed':
            if not (root/'report-seal.json').exists():
                from perception_association_report import finish
                finish(root)
            return
        try:
            s.update(state='running');write(root/'state.json',s);replays(root,c,s)
            with (root/'replay-checks.log').open('w') as log:
                subprocess.run([sys.executable,'-B',str(manage.OPS/'perception_association_checks.py'),'--output',str(root)],check=True,timeout=45,stdout=log,stderr=subprocess.STDOUT)
            if len(s['screen_variants'])>1:
                parallel_probe(root,c,s);batch(root,c,s,'screen',s['screen_variants'],c['screen_seeds'])
                rows=[r for r in s['runs'] if r['phase']=='screen'];results=[effect(rows,v,c) for v in s['screen_variants'] if v!='baseline']
                write(root/'screen-result.json',results);eligible=[r for r in results if r['passed']]
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
                        s['ui_eligible']=all(r['same_scores'] and r['same_behavior'] for r in pairs)
                        write(root/'repeatability.json',dict(passed=s['ui_eligible'],pairs=pairs))
            verify(root,c,s);s.update(state='completed',ended=time.time());write(root/'state.json',s)
            from perception_association_report import finish
            finish(root)
        except BaseException as error:
            s.update(state='failed',error=repr(error));write(root/'state.json',s);raise


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('freeze','run'));p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if a.action=='freeze':initialize(a.output.resolve())
    else:run(a.output.resolve())
