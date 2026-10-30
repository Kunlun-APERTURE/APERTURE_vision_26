#!/usr/bin/env python3
"""Explicit live acceptance: pause/resume, cancellation, worker and daemon faults."""
import argparse
import json
import html
import os
from pathlib import Path
import subprocess
import time

import background_jobs as jobs
import experiments
import manage
from report import load, write


def systemctl(*args):
    image=manage.inspect('daedalus:rm26-algorithm')['Id']
    subprocess.run(['docker','run','--rm','--user','0:0','--network','host','--pid','host',
        '--volume','/run/systemd:/run/systemd','--entrypoint','/bin/systemctl',image,*args],check=True)


def task(job_id):
    with jobs.connect() as db:return jobs.status(db,job_id)


def wait(job_id,predicate,timeout=150):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        state=task(job_id)
        if predicate(state):return state
        time.sleep(.4)
    raise RuntimeError('Timed out waiting for '+job_id+': '+json.dumps(task(job_id)))


def recording(state,count=1):
    active=[a for a in state['attempts'] if a['state']=='running']
    return len(active)>=count and all(
        (result:=load(jobs.STATE/'attempts'/a['id']/'result.json',{})).get('run_id') and
        (manage.OPS/'experiments'/result['run_id']/'begin.json').exists() for a in active)


def control(action,job_id):
    jobs.execute(manage.parser().parse_args(['jobs',action,job_id]))


def submit(output,label,repeat=1,parallelism=1,paused=False,exploratory=True,mode='shoot',duration=None):
    spec=output/(label+'.json')
    write(spec,dict(version=1,parallelism=parallelism,repeat=repeat,defaults=dict(
        terrain='flat_range',mode=mode,matrix='single' if mode=='shoot' else None,duration=duration,algorithm_profile='tracking_fire_v3',
        gimbal_profile='feedforward',physics='no-drag',label='background-functional-'+label),
        matrix=dict(scene=['hf_static_5'])))
    args=['experiment','submit','--spec',str(spec)]+(['--exploratory'] if exploratory else [])+(['--paused'] if paused else [])
    return jobs.submit(manage.parser().parse_args(args))['job_id']


def recovery(output,check,created):
    job_id=submit(output,'scheduler-crash',repeat=2);created.append(job_id)
    wait(job_id,recording)
    systemctl('kill','--signal=SIGKILL','--kill-who=main',jobs.SERVICE)
    paused=wait(job_id,lambda s:s['state']=='paused')
    check('scheduler_restart_requires_manual_resume',any(a['state']=='interrupted' and a['verdict']=='invalid' for a in paused['attempts']),job_id=job_id)
    attempts=len(paused['attempts']);time.sleep(2)
    check('restart_does_not_automatically_claim',len(task(job_id)['attempts'])==attempts,job_id=job_id)
    control('resume',job_id)
    finished=wait(job_id,lambda s:s['state'] in ('completed','paused'))
    check('restart_resume_creates_new_attempts',finished['state']=='completed' and len(finished['attempts'])==3,job_id=job_id)


def fallback(output,check,created):
    if load(jobs.STATE/'qualification.json',{}).get('parallel_qualified'):
        check('unqualified_formal_task_runs_serially',True,skipped='Current stack is already qualified')
        return
    job_id=submit(output,'formal-fallback',repeat=2,parallelism=2,exploratory=False,mode='track',duration=1)
    created.append(job_id)
    finished=wait(job_id,lambda s:s['state'] in ('completed','paused'))
    attempts=sorted(finished['attempts'],key=lambda a:a['started'])
    nonoverlapping=len(attempts)==2 and attempts[0]['ended']<=attempts[1]['started']
    effective=[load(manage.OPS/'experiments'/a['run_id']/'manifest.json').get('background',{}).get('effective_parallelism')
        for a in attempts if a['run_id']]
    check('unqualified_formal_task_runs_serially',finished['state']=='completed' and nonoverlapping and effective==[1,1],job_id=job_id)


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    p.add_argument('--recovery-only',action='store_true',help='Resume recovery checks after the first eight checks passed')
    p.add_argument('--fallback-only',action='store_true',help='Verify formal serial fallback after the other checks passed')
    args=p.parse_args()
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    results=load(output/'functional.json',{}).get('checks',[]) if args.recovery_only or args.fallback_only else []
    if args.recovery_only and (len(results)<8 or not all(r['passed'] for r in results[:8])):
        raise RuntimeError('Run the full functional acceptance before resuming recovery checks')
    results=results[:8] if args.recovery_only else results
    if args.fallback_only:
        if len(results)<11 or not all(r['passed'] for r in results[:11]):raise RuntimeError('Complete functional checks before testing fallback')
        results=results[:11]
    created=list(dict.fromkeys(r['job_id'] for r in results))
    def check(name,passed,**details):
        row=dict(name=name,passed=bool(passed),**details);results.append(row)
        write(output/'functional.json',dict(checks=results,jobs=created,complete=False));print(json.dumps(row),flush=True)
        if not passed:raise RuntimeError('Functional acceptance failed: '+name)
    # First isolate the user-workspace edit to a new disposable configuration file.
    probe=manage.ROOT/'sp_vision_25/configs/background_freeze_probe.yaml'
    if probe.exists():raise RuntimeError('Refusing to replace an existing probe file')
    desktop_was_running=manage.inspect(manage.DESKTOP_CONTAINER)['State']['Running']
    desktop_was_paused=manage.inspect(manage.DESKTOP_CONTAINER)['State']['Paused']
    try:
        if args.fallback_only:
            fallback(output,check,created)
            write(output/'functional.json',dict(checks=results,complete=True,jobs=created))
            return
        if args.recovery_only:
            recovery(output,check,created)
            write(output/'functional.json',dict(checks=results,complete=True,jobs=created))
            return
        probe.write_text('probe: frozen\n')
        job_id=submit(output,'freeze-independent',paused=True);created.append(job_id)
        frozen=Path(task(job_id)['bundle'])/'sp_vision_25/configs/background_freeze_probe.yaml'
        probe.write_text('probe: edited-after-submit\n')
        check('workspace_edit_does_not_change_bundle',frozen.read_text()=='probe: frozen\n' and experiments.digest(frozen)!=experiments.digest(probe),job_id=job_id)
        # Suspend the entire desktop cgroup without discarding editor/session
        # state. The worker has no DISPLAY or display authentication mounts.
        if desktop_was_running and not desktop_was_paused:subprocess.run(['docker','pause',manage.DESKTOP_CONTAINER],check=True)
        control('resume',job_id)
        finished=wait(job_id,lambda s:s['state'] in ('completed','paused'))
        desktop=manage.inspect(manage.DESKTOP_CONTAINER)['State']
        check('runs_with_sunshine_suspended',finished['state']=='completed' and (desktop['Paused'] or not desktop['Running']),job_id=job_id)
        if desktop_was_running and not desktop_was_paused:subprocess.run(['docker','unpause',manage.DESKTOP_CONTAINER],check=True)
        probe.unlink()

        job_id=submit(output,'pause-resume',repeat=3);created.append(job_id)
        wait(job_id,recording);control('pause',job_id)
        paused=wait(job_id,lambda s:s['state']=='paused')
        completed=paused['items'].get('completed',0)
        time.sleep(2)
        check('pause_finishes_current_and_stops_claiming',completed==1 and task(job_id)['items'].get('completed')==completed,job_id=job_id)
        control('resume',job_id)
        finished=wait(job_id,lambda s:s['state'] in ('completed','paused'))
        check('resume_only_runs_remaining',finished['state']=='completed' and len(finished['attempts'])==3,job_id=job_id)

        job_id=submit(output,'cancel',repeat=3);created.append(job_id)
        wait(job_id,recording);control('cancel',job_id)
        finished=wait(job_id,lambda s:all(a['state']!='running' for a in s['attempts']))
        check('cancel_preserves_invalid_attempt',finished['state']=='cancelled' and any(a['run_id'] and a['verdict']=='invalid' for a in finished['attempts']),job_id=job_id)

        job_id=submit(output,'worker-fault',repeat=2,parallelism=2);created.append(job_id)
        running=wait(job_id,lambda s:recording(s,2))
        victim,survivor=sorted([a for a in running['attempts'] if a['state']=='running'],key=lambda a:a['slot'])
        survivor_name='rm26-worker-'+str(survivor['slot'])+'-simulator'
        survivor_id=manage.inspect(survivor_name)['Id']
        subprocess.run(['docker','kill','rm26-worker-'+str(victim['slot'])+'-simulator'],check=True)
        finished=wait(job_id,lambda s:s['state'] in ('completed','paused'))
        surviving=next(a for a in finished['attempts'] if a['id']==survivor['id'])
        check('worker_fault_does_not_interrupt_peer',surviving['state']=='finished' and surviving['verdict']!='invalid',job_id=job_id,survivor_container_id=survivor_id)
        check('environment_failure_retried_once',finished['state']=='completed' and len(finished['attempts'])==3 and sum(a['verdict']=='invalid' for a in finished['attempts'])==1,job_id=job_id)

        job_id=submit(output,'repeated-fault');created.append(job_id)
        for attempt_number in (1,2):
            running=wait(job_id,lambda s:len(s['attempts'])>=attempt_number and recording(s))
            victim=next(a for a in running['attempts'] if a['state']=='running')
            subprocess.run(['docker','kill','rm26-worker-'+str(victim['slot'])+'-simulator'],check=True)
        paused=wait(job_id,lambda s:s['state']=='paused');time.sleep(2)
        check('second_environment_failure_pauses_task',len(task(job_id)['attempts'])==2 and paused['items'].get('pending')==1,job_id=job_id)
        control('cancel',job_id)

        recovery(output,check,created)
        fallback(output,check,created)
        write(output/'functional.json',dict(checks=results,complete=True,jobs=created))
    finally:
        probe.unlink(missing_ok=True)
        if desktop_was_running and not desktop_was_paused and manage.inspect(manage.DESKTOP_CONTAINER)['State']['Paused']:
            subprocess.run(['docker','unpause',manage.DESKTOP_CONTAINER],check=True)
        for job_id in created:
            if task(job_id)['state'] not in ('completed','cancelled'):control('cancel',job_id)
        with jobs.connect() as db:
            for job_id in created:jobs.report(db,job_id)
        (output/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>后台功能验收</title><h1>后台功能验收</h1><pre>'+
            html.escape(json.dumps(load(output/'functional.json'),ensure_ascii=False,indent=2))+'</pre><ul>'+''.join(
                '<li><a href="../'+job_id+'/report.html">'+job_id+'</a></li>' for job_id in created)+'</ul>')
        experiments.publish_desktop(manage,output)


if __name__=='__main__':main()
