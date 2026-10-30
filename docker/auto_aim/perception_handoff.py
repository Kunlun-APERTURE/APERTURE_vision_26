"""Bounded handoff repair: native regression replay, scoped parallel backend trial."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import math
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
from perception_study import SOURCE_FILES,protected,quantile,effect
from perception_repair import submit,collect,batch,parallel_probe,metrics_replay,FIELDS
from report import load,write
from sync_first_divergence import run_one
from sync_first_divergence_analysis import primary

PARENT=manage.OPS/'experiments/perception-repair-20261023'
OLD_B=dict(initial_consistency=True,initial_limit=.35,initial_joint_position=True)
VARIANTS=dict(baseline={},Bfix=OLD_B,H3=dict(OLD_B,initial_handoff_guard=True,
    initial_handoff_anchor_only=True,initial_handoff_frames=3))
FILES=(*SOURCE_FILES,'tasks/auto_aim/target.hpp','tasks/auto_aim/target.cpp')


def initialize(root):
    root.mkdir(parents=True,exist_ok=True)
    if (root/'contract.json').exists():
        c=load(root/'contract.json');s=load(root/'state.json')
        if s['contract_sha256']!=sha(root/'contract.json') or protected()!=c['protected']:raise ValueError('Frozen contract changed')
        for b in c['bundles'].values():verify_bundle(Path(b))
        return c
    pc=load(PARENT/'contract.json');ps=load(PARENT/'state.json')
    if ps['state']!='completed':raise ValueError('Complete parent study first')
    sources=[]
    # All baseline and Bfix closed-loop paths from the prior screen; omit A paths.
    # UI recordings are replayed only; no live UI acquisition in this study.
    for r in ps['runs']:
        if r['phase']!='screen' or r['variant'] not in ('baseline','Bfix'):continue
        folder=manage.OPS/'experiments'/r['run_id'];m=load(folder/'manifest.json')
        sources.append(dict(run_id=r['run_id'],variant=r['variant'],engine='backend',scene=r['scene'],
            sensor_seed=r['sensor_seed'],duration_s=m['duration_s'],source_hashes={n:sha(folder/n) for n in
            ('manifest.json','summary.json','run-state.json','telemetry.jsonl','session-evidence/events.jsonl','session-evidence/input.mjpg')}))
    for e in pc['sources']:
        if e['engine']!='ui':continue
        sources.append({k:e[k] for k in ('run_id','variant','engine','scene','sensor_seed','duration_s','source_hashes')})
    log=(root/'cpp-checks.log').read_text();matched=re.search(r'PASSED (\d+) perception checks',log)
    if not matched:raise ValueError('Run C++ checks before freezing')
    binary=root/'build-source/sp_vision_25/build/perception/sim_auto_aim'
    c=dict(version=1,created=time.time(),parent=str(PARENT),protected=protected(),sources=sources,
        variants=VARIANTS,bundles={},base_bundle=pc['base_bundle'],candidate_binary_sha256=sha(binary),
        build_source_sha256={f:sha(root/'build-source/sp_vision_25'/f) for f in FILES},geometry_checks=int(matched[1]),
        runtime_files=pc['runtime_files'],backend_timing_profile=pc['backend_timing_profile'],
        replay_workers=2,maximum_replays=len(sources)+20,
        probe_seed=2291,screen_seeds=list(range(2301,2306)),confirm_seeds=list(range(2401,2411)),
        effect_gate=pc['effect_gate'],bootstrap=pc['bootstrap'],
        maximum_trials=dict(parallel_probe=12,screen=30,conditional_confirmation=40,repeatability=4,ui=0),
        replay_gate=dict(coverage_drop=.02,initial_latency_p90_add_s=.1),
        repair='Only after a successful joint initialization: three update frames restrict associations to anchor id0 and require prior NIS <= existing limit; budget decrements even with rejected observations. No new firing rule.',
        preliminary_diagnosis='Candidate admission can bypass NIS for strong detections, but Target rechecks it at correction; the recorded 90-degree update itself passed that later NIS test. Anchor identity handoff is tested explicitly.',
        posthoc_truth='Only offline analysis reads enemy truth, after replay completion.',automatic_promotion=False,capacity_registration=False)
    for name,opts in VARIANTS.items():
        base=Path(c['base_bundle']);target=jobs.STATE/'bundles'/('handoff_'+name+'_'+sha(binary)[:12])
        if target.exists():raise ValueError('Refuse to replace existing handoff bundle')
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
    src=manage.OPS/'experiments'/e['run_id'];dst=root/'replays'/src.name/variant
    r=load(dst/'result.json') if (dst/'result.json').exists() else run_one(src,dst,Path(c['bundles'][variant]),
        seconds=e['duration_s'],detector_history='session',diagnosis=True)
    native=variant==e['variant']
    if not r['complete']:raise ValueError('Incomplete handoff replay')
    if native and any(r['differences_from_recorded'][k]['different_frames'] for k in FIELDS):
        raise ValueError('Disabled handoff did not reproduce its native source')
    row=dict(variant=variant,source_run=src.name,source_variant=e['variant'],engine=e['engine'],scene=e['scene'],sensor_seed=e['sensor_seed'],
        native_configuration=native,output=str(dst),frames=r['frames'],input_sha256=r['primary_input_sha256'],
        metrics=metrics_replay(dst,src),output_hashes={n:sha(dst/n) for n in ('plan.json','events.jsonl','result.json')})
    write(dst/'metrics.json',row);return row


def replays(root,c,s):
    pending=[(e,v) for e in c['sources'] for v in ('H3',e['variant'])
        if v in VARIANTS and (e['engine']=='backend' or v=='H3') and not any(r['source_run']==e['run_id'] and r['variant']==v for r in s['replays'])]
    with ThreadPoolExecutor(max_workers=c['replay_workers']) as pool:
        for r in pool.map(lambda pair:replay_one(root,c,*pair),pending):
            s['replays'].append(r);write(root/'state.json',s)
            print(json.dumps(dict(stage='replay',completed=len(s['replays']),variant=r['variant'],source=r['source_run'])),flush=True)
    # Every H3 packet is compared to a baseline from the previous replay or to
    # the native replay of its own new backend recording. No synthesized images.
    pc=load(PARENT/'contract.json');ps=load(PARENT/'state.json');comparisons=[]
    for e in c['sources']:
        rr=next(r for r in s['replays'] if r['source_run']==e['run_id'] and r['variant']=='H3')
        if e['engine']=='backend':ref=next(r for r in s['replays'] if r['source_run']==e['run_id'] and r['native_configuration'])
        else:ref=next(r for r in ps['replays'] if r['source_run']==e['run_id'] and r['variant']=='Bfix')
        if rr['input_sha256']!=ref['input_sha256']:raise ValueError('Handoff paired input mismatch')
        comparisons.append(dict(source_run=e['run_id'],engine=e['engine'],source_variant=e['variant'],
            candidate=rr['metrics'],reference=ref['metrics'],reference_output=ref['output']))
    def combine(key,entries):
        mm=[r[key] for r in entries];vv=[v for m in mm for v in m['errors_m']]
        return dict(coverage=sum(m['valid'] for m in mm)/sum(m['frames'] for m in mm),error_p95_m=quantile(vv,.95),
            initial_latency_p90_s=quantile([m['initial_latency_s'] if m['initial_latency_s'] is not None else 99. for m in mm],.9))
    a,b=combine('candidate',comparisons),combine('reference',comparisons)
    passed=a['coverage']>=b['coverage']-.02 and a['initial_latency_p90_s']<=b['initial_latency_p90_s']+.1
    s['screen_variants']=['baseline','Bfix','H3'] if passed else ['baseline'];write(root/'state.json',s)
    write(root/'replay-result.json',dict(passed=passed,candidate=a,reference=b,comparisons=comparisons,
        native_reproductions=sum(r['native_configuration'] for r in s['replays'])))


def verify(root,c,s):
    if protected()!=c['protected']:raise ValueError('Protected artifacts changed')
    if not (root/'replay-checks.log').read_text().rstrip().endswith('OK'):raise ValueError('Handoff replay checks failed')
    for e in c['sources']:
        src=manage.OPS/'experiments'/e['run_id']
        if any(sha(src/n)!=h for n,h in e['source_hashes'].items()):raise ValueError('Original evidence changed')
    for b in c['bundles'].values():verify_bundle(Path(b))
    for r in s['replays']:
        if any(sha(Path(r['output'])/n)!=h for n,h in r['output_hashes'].items()):raise ValueError('Replay changed')
    for r in s['runs']:
        src=manage.OPS/'experiments'/r['run_id'];m=load(src/'manifest.json');meta=load(Path(c['bundles'][r['variant']])/'bundle.json')
        if any(sha(src/n)!=h for n,h in r['source_hashes'].items()):raise ValueError('Closed-loop evidence changed')
        if m['algorithm_binary_sha256']!=c['candidate_binary_sha256'] or m['configuration_sha256']['algorithm.yaml']!=meta['files']['sp_vision_25/configs/simulator.yaml']:
            raise ValueError('Wrong algorithm payload')
        for field,expected in dict(scene=r['scene'],sensor_seed=r['sensor_seed'],terrain='flat_range',input_contract='aligned-v1',warmup_frames=30).items():
            if m[field]!=expected:raise ValueError('Input contract changed')
        for field,expected in dict(mode='lockstep',timing_profile=c['backend_timing_profile'],detector_history='session',clock_domain='simulation').items():
            if m['execution'][field]!=expected:raise ValueError('Runtime timing/history changed')
        for field,expected in dict(bullet_speed=25.,reported_bullet_speed=25.,physics='no-drag',aim_origin='muzzle',gimbal_profile='feedforward',fire_policy='advice',fire_rate_limit_hz=5.,algorithm_profile='tracking_fire_v3').items():
            if m['shooting'][field]!=expected:raise ValueError('Physics/fire contract changed')
        for role in ('simulator','bridge','evaluator','algorithm'):
            if m['containers'][role+'_image_id']!=meta['images']['simulator' if role=='bridge' else role]:raise ValueError('Runtime image changed')
        if not r['health']['healthy'] or r['wall_s']>40:raise ValueError('Unhealthy/over-budget trial')
    write(root/'validation.json',dict(passed=True,replays=len(s['replays']),frames=sum(r['frames'] for r in s['replays']),
        backend_trials=len(s['runs']),ui_trials=0,geometry_checks=c['geometry_checks'],protected_unchanged=True,
        original_evidence_unchanged=True,algorithm_promoted=False,capacity_registered=False,
        source_sha256={p.name:sha(p) for p in manage.OPS.glob('perception_handoff*.py')}))


def run(root):
    c=initialize(root)
    with (root/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);s=load(root/'state.json')
        if s['state']=='completed':
            if not (root/'report-seal.json').exists():
                from perception_handoff_report import finish
                finish(root)
            return
        try:
            s.update(state='running');write(root/'state.json',s);replays(root,c,s)
            with (root/'replay-checks.log').open('w') as log:
                subprocess.run([sys.executable,'-B',str(manage.OPS/'perception_handoff_checks.py'),'--output',str(root)],check=True,timeout=30,stdout=log,stderr=subprocess.STDOUT)
            if len(s['screen_variants'])>1:
                parallel_probe(root,c,s);batch(root,c,s,'screen',s['screen_variants'],c['screen_seeds'])
                rows=[r for r in s['runs'] if r['phase']=='screen'];result=effect(rows,'H3',c)
                write(root/'screen-result.json',result);s['selected']='H3' if result['passed'] else None
                if result['passed']:
                    batch(root,c,s,'confirm',['baseline','H3'],c['confirm_seeds'])
                    result=effect([r for r in s['runs'] if r['phase']=='confirm'],'H3',c,True)
                    write(root/'confirm-result.json',result);s['backend_confirmed']=result['passed']
                    if result['passed']:
                        batch(root,c,s,'repeat',['baseline','H3'],[c['confirm_seeds'][0]])
                        pairs=[]
                        for r in [r for r in s['runs'] if r['phase']=='repeat']:
                            a=next(a for a in s['runs'] if a['phase']=='confirm' and all(a[k]==r[k] for k in ('variant','scene','sensor_seed')))
                            pairs.append(dict(variant=r['variant'],scene=r['scene'],same_scores=all(a[k]==r[k] for k in ('shots','hits','weight')),
                                same_behavior=behavior(manage.OPS/'experiments'/a['run_id'])==behavior(manage.OPS/'experiments'/r['run_id'])))
                        s['ui_eligible']=all(p['same_scores'] and p['same_behavior'] for p in pairs)
                        write(root/'repeatability.json',dict(passed=s['ui_eligible'],pairs=pairs))
            verify(root,c,s);s.update(state='completed',ended=time.time());write(root/'state.json',s)
            from perception_handoff_report import finish
            finish(root)
        except BaseException as error:
            s.update(state='failed',error=repr(error));write(root/'state.json',s);raise


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('freeze','run'))
    p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if a.action=='freeze':initialize(a.output.resolve())
    else:run(a.output.resolve())
