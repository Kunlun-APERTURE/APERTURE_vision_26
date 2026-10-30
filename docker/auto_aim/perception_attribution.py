"""Cross replay of existing native sessions; no simulator, commands or new shots."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
from pathlib import Path
import time

import manage
from decision_benchmark import sha,verify_bundle
from perception_study import protected
from report import load,write
from sync_first_divergence import run_one

PARENT=manage.OPS/'experiments/perception-ui-probe-20261023'
VARIANTS=('baseline','A35','B35')


def initialize(root):
    root.mkdir(parents=True,exist_ok=True)
    if (root/'contract.json').exists():
        c=load(root/'contract.json')
        if sha(root/'contract.json')!=load(root/'state.json')['contract_sha256']:raise ValueError('Frozen attribution contract changed')
        if protected()!=c['protected']:raise ValueError('Protected production artifacts changed')
        return c
    source=load(PARENT/'contract.json');ui=load(PARENT/'state.json')
    if ui['state']!='completed':raise ValueError('Complete original UI acquisition first')
    entries=[*ui['runs'],*source['backend']]
    if len(entries)!=24:raise ValueError('Expected all 24 original UI/backend trajectories')
    sources=[]
    for entry in entries:
        f=manage.OPS/'experiments'/entry['run_id'];m=load(f/'manifest.json')
        sources.append(dict(entry,duration_s=m['duration_s'],source_hashes={n:sha(f/n) for n in
            ('manifest.json','run-state.json','summary.json','telemetry.jsonl',
             'session-evidence/events.jsonl','session-evidence/input.mjpg')}))
    for b in source['bundles'].values():verify_bundle(Path(b))
    c=dict(version=1,created=time.time(),parent=str(PARENT),parent_contract_sha256=sha(PARENT/'contract.json'),
        variants=VARIANTS,bundles=source['bundles'],sources=sources,maximum_replays=72,
        workers=2,protected=protected(),
        coverage='All recorded warmup and full physical measurement interval, excluding acquisition teardown tail.',
        pairing='Three frozen algorithm configurations consume identical original JPEG bytes, own-pose metadata and recorded aim/fire/pose clocks per source.',
        intervention='Only the frozen algorithm configuration changes. Source camera/control trajectories remain recorded and fixed.',
        posthoc_truth='Only the analysis process reads target ground truth after replay processes exit.',
        diagnostics='No new closed-loop trials, no counterfactual hit-rate estimates, no algorithm repair or parameter selection.',
        reproducibility_fields=['observations','filter_state','selected_plate_id','tracker','yaw','pitch','fire_advice'])
    write(root/'contract.json',c)
    write(root/'state.json',dict(state='frozen',contract_sha256=sha(root/'contract.json'),replays=[],new_closed_loop_trials=0))
    return c


def replay(root,c,entry,variant):
    source=manage.OPS/'experiments'/entry['run_id'];output=root/'replays'/source.name/variant
    r=load(output/'result.json') if (output/'result.json').exists() else run_one(
        source,output,Path(c['bundles'][variant]),seconds=entry['duration_s'],detector_history='session',diagnosis=True)
    if not r['complete']:raise ValueError('Incomplete archived-input replay')
    actual=(entry['variant']==variant)
    differences={k:r['differences_from_recorded'][k] for k in c['reproducibility_fields']}
    if actual and any(v['different_frames'] for v in differences.values()):
        write(output/'reproduction-failure.json',differences)
        raise ValueError('Native configuration did not reproduce its source: '+str(output))
    result=dict(source_run=source.name,source_engine=entry['engine'],source_variant=entry['variant'],
        scene=entry['scene'],sensor_seed=entry['sensor_seed'],variant=variant,
        output=str(output),frames=r['frames'],wall_s=r['wall_s'],
        input_sha256=r['primary_input_sha256'],native_configuration=actual,
        source_reproduction=differences if actual else None,
        output_hashes={n:sha(output/n) for n in ('plan.json','events.jsonl','result.json')})
    return result


def run(root):
    c=initialize(root)
    with (root/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        state=load(root/'state.json')
        if state['state']=='completed':return
        try:
            state.update(state='running');write(root/'state.json',state)
            pending=[(e,v) for e in c['sources'] for v in VARIANTS if not any(
                p['source_run']==e['run_id'] and p['variant']==v for p in state['replays'])]
            with ThreadPoolExecutor(max_workers=c['workers']) as pool:
                for r in pool.map(lambda pair:replay(root,c,*pair),pending):
                    state['replays'].append(r);write(root/'state.json',state)
                    print(json.dumps(dict(completed=len(state['replays']),maximum=72,source=r['source_run'],variant=r['variant'])),flush=True)
            if len(state['replays'])!=72:raise ValueError('Missing replay cases')
            for entry in c['sources']:
                rr=[r for r in state['replays'] if r['source_run']==entry['run_id']]
                if len(rr)!=3 or len({r['input_sha256'] for r in rr})!=1:raise ValueError('A/B input mismatch')
                f=manage.OPS/'experiments'/entry['run_id']
                if any(sha(f/n)!=h for n,h in entry['source_hashes'].items()):raise ValueError('Source evidence changed')
            if protected()!=c['protected']:raise ValueError('Production artifacts changed')
            state.update(state='completed',ended=time.time());write(root/'state.json',state)
        except BaseException as error:
            state.update(state='failed',error=repr(error));write(root/'state.json',state);raise


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();run(a.output.resolve())
