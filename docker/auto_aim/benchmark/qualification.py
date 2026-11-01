"""Explicit per-scene repeated/parallel validation; failures never become scores."""
import json
import time
import uuid
from pathlib import Path
from .core import identity
from .artifacts import verify
from .evaluation import behavior
from report import load,write


def signature(bundle,settings):
    meta=load(Path(bundle)/'bundle.json')
    return identity(dict(files=meta['files'],images=meta['images'],gpu=meta['gpu_uuid'],renderer=meta.get('renderer',{'backend':'nvidia'}),settings={k:v for k,v in settings.items() if k not in ('repeat_id','label','sensor_seed')}))


def limit(job):
    import background_jobs as jobs
    settings=json.loads(job['settings']);key=signature(job['bundle'],settings)
    entry=load(jobs.STATE/'benchmark-capacity.json',{}).get(key,{})
    return min(job['parallelism'] or 1,entry.get('parallelism',1))


def qualify(root):
    import background_jobs as jobs
    import manage
    root=Path(root);state=load(root/'state.json');contract=load(root/'contract.json')
    if identity(contract)!=state['contract_sha256']:raise ValueError('Changed contract')
    verify(contract['bundle'],contract.get('payload_identity'));status=None
    with jobs.connect() as db:status=jobs.status(db,state['job_id'])
    if status['state']!='completed':raise ValueError('Complete serial reference first')
    references={}
    with jobs.connect() as db:
        for row in db.execute('SELECT i.settings,a.* FROM items i JOIN attempts a ON a.item_id=i.id WHERE i.job_id=? ORDER BY i.ordinal',(state['job_id'],)):
            s=json.loads(row['settings']);references.setdefault(s['scene'],(s,dict(row)))
    registration=root/'qualification-state.json';registered=load(registration,{})
    results=[]
    for scene,(settings,reference) in references.items():
        for level in (1,2):
            key=scene+':'+str(level)
            job_id=registered.get(key)
            if not job_id:
                job_id='job_qualification_'+uuid.uuid4().hex[:12];now=time.time()
                from .jobs import JobService
                JobService().submit(job_id,contract['bundle'],[settings]*level,level,probe=True)
                registered[key]=job_id;write(registration,registered)
            while True:
                with jobs.connect() as db:status=jobs.status(db,job_id)
                if status['state']=='completed':break
                if status['state'] in ('paused','cancelled'):raise RuntimeError('Qualification evidence invalid: '+job_id)
                time.sleep(1)
            ref=manage.OPS/'experiments'/reference['run_id'];ref_score=(ref/'shots.jsonl').read_bytes()
            attempts=status['attempts'];checks=[]
            for trial in attempts:
                folder=manage.OPS/'experiments'/trial['run_id']
                from .diagnostics import first_difference,relative_events,relative_shots
                checks.append(dict(relative_behavior_equal=relative_events(ref)==relative_events(folder),relative_shots_equal=relative_shots(ref)==relative_shots(folder),first_difference=first_difference(ref,folder),run_id=folder.name,same_behavior=behavior(ref)==behavior(folder),
                    same_shots=ref_score==(folder/'shots.jsonl').read_bytes(),within_budget=load(folder/'timing.json')['wall_seconds']<=40))
            overlap=level==1 or (len(attempts)==2 and attempts[0]['slot']!=attempts[1]['slot'] and max(a['started'] for a in attempts)<min(a['ended'] for a in attempts))
            passed=overlap and len(attempts)==level and all(all(c[k] for k in ('relative_behavior_equal','relative_shots_equal','within_budget')) for c in checks)
            results.append(dict(scene=scene,level=level,passed=passed,overlap=overlap,checks=checks))
            write(root/'qualification.json',dict(results=results,global_qualification=False,complete=False))
        scoped=[r for r in results if r['scene']==scene]
        if all(r['passed'] for r in scoped):
            path=jobs.STATE/'benchmark-capacity.json';registry=load(path,{})
            registry[signature(contract['bundle'],settings)]=dict(parallelism=2,evidence=str(root/'qualification.json'))
            write(path,registry)
        write(root/'qualification.json',dict(results=results,global_qualification=False))
    result=dict(comparison_rule='session-relative-v1',raw_comparison_retained=True,results=results,passed=all(r['passed'] for r in results),global_qualification=False,complete=True)
    write(root/'qualification.json',result)
    return result
