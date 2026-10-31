"""Explicit old/new frozen payload comparison; reference supplied by caller."""
import json
import time
import uuid
from pathlib import Path
from .core import sha
from .evaluation import behavior
from .artifacts import verify
from report import load,write

SCENES=('hf_static_5','hf_uniform_3','hf_spin_pos','hf_spin_neg','adv_variable_pos','adv_pursuit_pos')


def run(reference,candidate,output):
    import background_jobs as jobs
    import manage
    from .core import EnvironmentSpec
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    registration=load(output/'state.json',dict(jobs={}))
    for bundle in (reference,candidate):verify(bundle)
    conditions={scene:jobs.validate(dict(EnvironmentSpec('legacy','legacy').settings(scene,8301,2026102207),algorithm_profile='tracking_fire_v3')) for scene in SCENES}
    for variant,bundle in [('reference',reference),('candidate',candidate)]:
        if variant in registration['jobs']:continue
        job_id='job_migration_'+uuid.uuid4().hex[:12];now=time.time()
        with jobs.connect() as db,jobs.transaction(db):
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(job_id,'queued',now,now,json.dumps(dict(version=1,_protocol=2,_stop_on_invalid=True)),str(Path(bundle).resolve()),1,0,None))
            db.executemany('INSERT INTO items(job_id,ordinal,state,settings) VALUES(?,?,?,?)',[(job_id,i,'pending',json.dumps(dict(s,repeat_id=0,execution_profile='lockstep-ui-aligned-v7-clock'))) for i,s in enumerate(conditions.values())])
        registration['jobs'][variant]=job_id;write(output/'state.json',registration)
    rows={}
    for variant,job_id in registration['jobs'].items():
        while True:
            with jobs.connect() as db:status=jobs.status(db,job_id)
            if status['state']=='completed':break
            if status['state'] in ('paused','cancelled'):raise RuntimeError('Invalid retained migration evidence: '+job_id)
            time.sleep(1)
        rows[variant]={scene:manage.OPS/'experiments'/a['run_id'] for scene,a in zip(SCENES,status['attempts'])}
    pairs=[]
    for scene in SCENES:
        a,b=rows['reference'][scene],rows['candidate'][scene]
        ma,mb=[load(p/'summary.json')['metrics'] for p in (a,b)]
        metrics=('shots_fired','target_armor_hits','weighted_hit_score_sum')
        from .diagnostics import first_difference,relative_events,relative_shots
        pairs.append(dict(relative_behavior_equal=relative_events(a)==relative_events(b),relative_shots_equal=relative_shots(a)==relative_shots(b),first_difference=first_difference(a,b),scene=scene,reference=a.name,candidate=b.name,same_behavior=behavior(a)==behavior(b),same_score=all(ma[k]==mb[k] for k in metrics),same_shots=(a/'shots.jsonl').read_bytes()==(b/'shots.jsonl').read_bytes()))
    result=dict(comparison_rule='session-relative-v1',raw_comparison_retained=True,pairs=pairs,passed=all(all(p[k] for k in ('relative_behavior_equal','same_score','relative_shots_equal')) for p in pairs))
    write(output/'migration.json',result);return result
