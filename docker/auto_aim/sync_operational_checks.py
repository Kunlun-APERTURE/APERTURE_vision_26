#!/usr/bin/env python3
"""Isolated preview, compute-pause and scheduler-recovery experiments.

These are labelled interventions and never enter statistical qualification.
Run only after the study and capacity measurements have drained.
"""
import argparse
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time
import uuid

import background_jobs as jobs
import manage
from report import load, write
from sync_study import DEFAULTS


def rpc(slot):
    body=json.dumps(dict(version=1,type='admin',op='status')).encode()
    with socket.create_connection(('127.0.0.1',18826+slot),timeout=2) as connection:
        connection.sendall(struct.pack('!II',len(body),0)+body)
        def exact(size):
            value=b''
            while len(value)<size:
                part=connection.recv(size-len(value))
                if not part:raise RuntimeError('Closed status connection')
                value+=part
            return value
        size,payload=struct.unpack('!II',exact(8))
        if not 0<size<=65536 or payload:raise RuntimeError('Malformed status response')
        result=json.loads(exact(size))
        if not result['ok']:raise RuntimeError(str(result))
        return result['result']


def check(study):
    state=load(study/'study.json')
    if state['state']!='completed':raise ValueError('Complete statistical qualification first')
    with jobs.connect() as db:
        if db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running','pausing')").fetchone()[0]:
            raise ValueError('Drain existing tasks first')
    job_id='job_sync_operations_'+uuid.uuid4().hex[:12]
    rows=[]
    for repeat in range(4):
        settings=jobs.validate({k:v for k,v in dict(DEFAULTS,scene='hf_uniform_3',seed=2026102207,
            execution_mode='lockstep',timing_profile='ui-fixed-v1',record_timing='off',
            preview='on' if repeat==1 else 'off',max_fps=240,label='sync-operational-'+str(repeat)).items() if k in jobs.FIELDS})
        settings.update(repeat_id=repeat,execution_profile='lockstep-ui-fixed-v1');rows.append(settings)
    now=time.time()
    with jobs.connect() as db,jobs.transaction(db):
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(job_id,'queued',now,now,
            json.dumps(dict(version=1,_protocol=2,_operational_check=True,parallelism=1)),state['bundle'],1,1,None))
        db.executemany('INSERT INTO items(job_id,ordinal,state,settings) VALUES(?,?,?,?)',
            [(job_id,i,'pending',json.dumps(settings)) for i,settings in enumerate(rows)])
        jobs.event(db,job_id,'Separate operational checks: preview; paused computation; scheduler restart and explicit resume')
    result=dict(job_id=job_id,passed=False,interventions=[],statistical_qualification=False)
    target=study/'operational-checks.json';write(target,result)
    paused=False;restarted=False;preview_verified=False;deadline=time.monotonic()+360
    while time.monotonic()<deadline:
        with jobs.connect() as db:
            status=jobs.status(db,job_id)
            active=db.execute("SELECT a.*,i.ordinal FROM attempts a JOIN items i ON a.item_id=i.id "
                "WHERE i.job_id=? AND a.state='running'",(job_id,)).fetchone()
        if status['state']=='completed':break
        if status['state'] in ('paused','cancelled'):raise RuntimeError('Operational task stopped: '+str(status.get('error')))
        if active and ((active['ordinal']==1 and not preview_verified) or
            (active['ordinal']==2 and not paused) or (active['ordinal']==3 and not restarted)):
            try:
                snapshot=rpc(active['slot'])
            except (OSError,RuntimeError):
                time.sleep(.1);continue
            synchronization=snapshot.get('synchronization') or {}
            if (synchronization.get('mode')=='measure' and synchronization.get('delivered_frames',0)>=32
                and snapshot.get('scenario',{}).get('id')=='rm26_hf_uniform_3'
                and snapshot.get('scenario',{}).get('phase')==1):
                if active['ordinal']==1:
                    windows=subprocess.run(['docker','exec','--user','1000:1000','--env','DISPLAY=:20',
                        '--env','XAUTHORITY=/run/user/1000/Xauthority',manage.DESKTOP_CONTAINER,
                        'xwininfo','-root','-tree'],capture_output=True,text=True,timeout=5)
                    preview_verified=windows.returncode==0 and 'RM26 synchronous input' in windows.stdout
                    if preview_verified:
                        result['preview_window']=dict(verified=True,display=':20',title='RM26 synchronous input')
                        write(target,result)
                elif active['ordinal']==2:
                    name='rm26-worker-'+str(active['slot'])+'-algorithm'
                    subprocess.run(['docker','pause',name],check=True,capture_output=True)
                    try:
                        time.sleep(.3);first=rpc(active['slot'])['synchronization']
                        started=time.monotonic();time.sleep(1.0)
                        second=rpc(active['slot'])['synchronization']
                    finally:subprocess.run(['docker','unpause',name],check=True,capture_output=True)
                    result['interventions'].append(dict(kind='compute_pause',attempt=active['id'],
                        wall_seconds=time.monotonic()-started,first=first,second=second,
                        logical_clock_held=first['stamp_ns']==second['stamp_ns']))
                    paused=True;write(target,result)
                else:
                    subprocess.run([sys.executable,str(manage.OPS/'manage.py'),'jobs','install'],check=True,capture_output=True)
                    recovery_deadline=time.monotonic()+30
                    while True:
                        with jobs.connect() as db:recovery=jobs.status(db,job_id)
                        if recovery['state']=='paused':break
                        if time.monotonic()>recovery_deadline:raise RuntimeError('Scheduler recovery did not pause task')
                        time.sleep(.2)
                    interrupted=[a for a in recovery['attempts'] if a['id']==active['id']]
                    result['interventions'].append(dict(kind='scheduler_restart',attempt=active['id'],
                        task_paused=True,interrupted=bool(interrupted and interrupted[0]['state']=='interrupted'),
                        interrupted_run=interrupted[0]['run_id'] if interrupted else None))
                    write(target,result)
                    subprocess.run([sys.executable,str(manage.OPS/'manage.py'),'jobs','resume',job_id],check=True,capture_output=True)
                    restarted=True
        time.sleep(.1)
    else:raise RuntimeError('Operational checks exceeded deadline')
    with jobs.connect() as db:
        final=jobs.status(db,job_id);jobs.report(db,job_id)
        valid=[]
        for item in db.execute('SELECT * FROM items WHERE job_id=? ORDER BY ordinal',(job_id,)):
            attempts=db.execute("SELECT * FROM attempts WHERE item_id=? AND state='finished' AND verdict!='invalid'",(item['id'],)).fetchall()
            if len(attempts)!=1:raise RuntimeError('Expected one valid result per operational condition')
            run_id=attempts[0]['run_id'];summary=load(manage.OPS/'experiments'/run_id/'summary.json')
            valid.append(dict(condition=('preview_off','preview_on','compute_pause','recovered')[item['ordinal']],
                run_id=run_id,verdict=summary['verdict'],metrics={k:summary['metrics'].get(k) for k in (
                    'target_hit_rate','weighted_hit_rate','shots_fired','correct_tracking_fraction','longest_loss_s')}))
    baseline=valid[0]['metrics']
    result.update(results=valid,preview_rate_differences={k:valid[1]['metrics'][k]-baseline[k] for k in ('target_hit_rate','weighted_hit_rate')},
        pause_rate_differences={k:valid[2]['metrics'][k]-baseline[k] for k in ('target_hit_rate','weighted_hit_rate')},
        interrupted_attempts=sum(a['state']=='interrupted' for a in final['attempts']))
    result['passed']=(preview_verified and paused and restarted and final['state']=='completed' and
        all(v['verdict']!='invalid' for v in valid) and
        all(abs(v)<=.02 for values in (result['preview_rate_differences'],result['pause_rate_differences']) for v in values.values()) and
        all(i.get('logical_clock_held',True) and i.get('interrupted',True) for i in result['interventions']))
    write(target,result);print(json.dumps(result),flush=True)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('study',type=Path)
    args=parser.parse_args()
    if not check(args.study.resolve())['passed']:raise SystemExit(1)
