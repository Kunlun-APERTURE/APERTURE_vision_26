#!/usr/bin/env python3
"""Four simultaneous functional trials; never grants statistical qualification."""
import argparse
import json
from pathlib import Path
import time
import uuid

import background_jobs as jobs
from background_capacity import assess
import manage
from report import load,write
from sync_study import DEFAULTS
from sync_timing import SCENES


def check(study):
    state=load(study/'study.json')
    if state['state']!='completed':raise ValueError('Complete statistical study first')
    with jobs.connect() as db:
        if db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running','pausing')").fetchone()[0]:
            raise ValueError('Drain other tasks first')
    job_id='job_sync_functional4_'+uuid.uuid4().hex[:12];settings=[];now=time.time()
    for scene in SCENES:
        value=jobs.validate({k:v for k,v in dict(DEFAULTS,scene=scene,seed=2026102207,
            execution_mode='lockstep',timing_profile='ui-fixed-v1',record_timing='off',
            preview='off',max_fps=240,label='four-lane-functional').items() if k in jobs.FIELDS})
        value.update(repeat_id=0,execution_profile='lockstep-ui-fixed-v1');settings.append(value)
    with jobs.connect() as db,jobs.transaction(db):
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(job_id,'queued',now,now,
            json.dumps(dict(version=1,_protocol=2,_operational_check=True,parallelism=4)),state['bundle'],4,1,None))
        db.executemany('INSERT INTO items(job_id,ordinal,state,settings) VALUES(?,?,?,?)',
            [(job_id,n,'pending',json.dumps(s)) for n,s in enumerate(settings)])
        jobs.event(db,job_id,'Four-lane functional check only; no repeatability or UI certificate')
    output=study/'parallel-functional.json'
    result=dict(job_id=job_id,candidate_bundle=state['bundle'],passed=False,statistical_qualification=False)
    write(output,result);deadline=time.monotonic()+240
    while time.monotonic()<deadline:
        with jobs.connect() as db:status=jobs.status(db,job_id)
        if status['state']=='completed':break
        if status['state'] in ('paused','cancelled'):raise RuntimeError('Functional task stopped: '+str(status.get('error')))
        time.sleep(.5)
    else:raise RuntimeError('Four-lane functional check exceeded deadline')
    rows=[]
    for attempt in status['attempts']:
        if attempt['state']!='finished' or attempt['verdict']=='invalid':continue
        folder=manage.OPS/'experiments'/attempt['run_id'];manifest=load(folder/'manifest.json')
        metrics=load(folder/'summary.json')['metrics'];health=assess(folder)
        measured=load(folder/'run-state.json');walls=[]
        with (folder/'telemetry.jsonl').open() as stream:
            for line in stream:
                packet=json.loads(line)
                if packet['kind']=='observation':
                    observation=packet['data'];stamp=observation['frame']['stamp_ns']
                    if measured['measurement_start_stamp_ns']<=stamp<=measured['measurement_end_stamp_ns']:
                        walls.append(observation['wall_receipt_ns'])
        if not walls:raise RuntimeError('Missing wall observation times for functional concurrency check')
        rows.append(dict(run_id=folder.name,scene=manifest['scene'],slot=manifest['background']['slot'],
            domain=manifest['ros_domain_id'],port=manifest['background']['bridge_port'],
            started=attempt['started'],ended=attempt['ended'],healthy=health['healthy'],
            measurement_wall_start_ns=min(walls),measurement_wall_end_ns=max(walls),
            issues=health['issues'],control_hz=health['control_hz'],
            metrics={k:metrics.get(k) for k in ('processed_hz','truth_coverage','total_wall_s',
                'shots_fired','target_hit_rate','weighted_hit_rate')}))
    overlap=min(r['ended'] for r in rows)-max(r['started'] for r in rows) if rows else 0
    measured_overlap=(min(r['measurement_wall_end_ns'] for r in rows)-max(r['measurement_wall_start_ns'] for r in rows))/1e9 if rows else 0
    result.update(results=rows,attempt_overlap_wall_s=overlap,measurement_overlap_wall_s=measured_overlap,
        passed=len(status['attempts'])==4 and len(rows)==4 and measured_overlap>0 and
            all(r['healthy'] for r in rows) and
            all(len({r[key] for r in rows})==4 for key in ('scene','slot','domain','port')))
    write(output,result)
    with jobs.connect() as db:jobs.report(db,job_id)
    print(json.dumps(result),flush=True)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('study',type=Path)
    args=parser.parse_args()
    if not check(args.study.resolve())['passed']:raise SystemExit(1)
