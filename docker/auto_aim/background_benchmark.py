"""Sequential capacity levels, with frozen workloads and durable progress."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
import uuid

from report import load,write


def benchmark(args):
    import background_jobs as jobs
    if args.resume:
        if '/' in args.resume or args.resume.startswith('.'):raise ValueError('Expected benchmark ID')
        path=jobs.STATE/'benchmarks'/args.resume/'benchmark.json'
        state=load(path)
        if not state:raise ValueError('Unknown benchmark')
        if state['state']=='completed':raise ValueError('Benchmark is already completed')
        folder=path.parent;state.update(state='running',error=None);write(path,state)
        output=(folder/'controller.log').open('a')
        process=subprocess.Popen([sys.executable,'-B','-u',str(Path(__file__)),str(path)],
            stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
        output.close();print(json.dumps(dict(benchmark_id=args.resume,pid=process.pid,status=str(path))))
        return state
    levels=[int(v) for v in args.levels.split(',')]
    if not levels or levels[0]!=1 or levels!=sorted(set(levels)) or any(v not in (1,2,4,8) for v in levels):
        raise ValueError('Levels must start at 1 and increase through a subset of 1,2,4,8')
    if 8 in levels and 4 not in levels:raise ValueError('8 lanes require a healthy 4-lane qualification first')
    if time.time()-load(jobs.STATE/'daemon-status.json',{}).get('heartbeat',0)>5:raise ValueError('Start the scheduler first')
    with jobs.connect() as db:
        if db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running','pausing')").fetchone()[0]:
            raise ValueError('Drain or pause existing tasks before capacity measurement')
    name='capacity_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'_'+uuid.uuid4().hex[:6]
    folder=jobs.STATE/'benchmarks'/name;folder.mkdir(parents=True)
    started=time.time();bundle=jobs.freeze(name)
    state=dict(version=1,id=name,state='running',levels=levels,bundle=str(bundle),created=started,
        provisioning_seconds=time.time()-started,results=[])
    write(folder/'benchmark.json',state)
    output=(folder/'controller.log').open('a')
    process=subprocess.Popen([sys.executable,'-B','-u',str(Path(__file__)),str(folder/'benchmark.json')],
        stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
    output.close();print(json.dumps(dict(benchmark_id=name,pid=process.pid,status=str(folder/'benchmark.json'))))
    return state


def execute(path):
    import background_jobs as jobs
    from background_capacity import assess,signature
    path=Path(path);state=load(path);reference={};best=1;best_throughput=0;serial=None
    for previous in state['results']:
        if previous['level']==1:
            serial=previous['valid_runs_per_hour']
            for scene in {c['scene'] for c in previous['checks']}:
                values=[c['control_hz'] for c in previous['checks'] if c['scene']==scene and c['control_hz'] is not None]
                if values:reference[scene]=statistics.median(values)
        if previous['healthy'] and previous['valid_runs_per_hour']>best_throughput:
            best=previous['level'];best_throughput=previous['valid_runs_per_hour']
    try:
        for level in state['levels']:
            if any(r['level']==level for r in state['results']):continue
            if level==8 and not any(r['level']==4 and r['healthy'] for r in state['results']):break
            job_id=state['id']+'_p'+str(level)
            defaults=dict(terrain='flat_range',mode='shoot',matrix='single',physics='no-drag',
                algorithm_profile='tracking_fire_v3',gimbal_profile='feedforward',fire_policy='advice',
                fire_rate_hz=5,max_fps=240,stage='screen',evidence='auto')
            rows=[]
            for repetition in range(4):
                for scene in ('hf_static_8','hf_uniform_3','hf_turn90_3','hf_spin_neg'):
                    row=jobs.validate(dict(defaults,scene=scene,label=state['id'],seed=2026102201))
                    row.update(repeat_id=repetition,execution_profile='backend-240-v1');rows.append(row)
            with jobs.connect() as db,jobs.transaction(db):
                if not db.execute('SELECT id FROM jobs WHERE id=?',(job_id,)).fetchone():
                    now=time.time();spec=dict(version=1,_protocol=2,_benchmark=True,defaults=defaults,parallelism=level)
                    db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',
                        (job_id,'queued',now,now,json.dumps(spec),state['bundle'],level,1,None))
                    db.executemany('INSERT INTO items(job_id,ordinal,state,settings) VALUES(?,?,?,?)',
                        [(job_id,i,'pending',json.dumps(s)) for i,s in enumerate(rows)])
                    jobs.event(db,job_id,'Capacity measurement; aiming quality is not a capacity gate')
            state['active_job']=job_id;write(path,state)
            deadline=time.monotonic()+max(600,16*65/level)
            while True:
                with jobs.connect() as db:current=jobs.status(db,job_id)
                if current['state']=='completed':break
                if current['state']=='paused' and (current.get('error') or '').startswith('Repeated invalid experiment'):break
                if current['state'] in ('paused','cancelled'):raise RuntimeError('Capacity task stopped: '+job_id+' '+str(current.get('error')))
                if time.monotonic()>deadline:raise RuntimeError('Capacity task did not finish within qualification deadline')
                time.sleep(2)
            elapsed=time.time()-current['created'];checks=[];durations=[];storage=[];runs=[]
            for attempt in current['attempts']:
                if not attempt['run_id']:continue
                folder=jobs.OPS/'experiments'/attempt['run_id'];manifest=load(folder/'manifest.json')
                health=assess(folder,reference.get(manifest['scene']))
                checks.append(dict(run_id=folder.name,scene=manifest['scene'],**health))
                if health['healthy']:
                    runs.append(folder.name);durations.append(load(folder/'timing.json')['wall_seconds'])
                    storage.append(sum(p.stat().st_size for p in folder.iterdir() if p.is_file()))
            # A capacity level cannot hide an invalid attempt behind its retry.
            healthy=current['state']=='completed' and len(checks)==16 and all(c['healthy'] for c in checks)
            throughput=len(runs)/elapsed*3600
            result=dict(level=level,job_id=job_id,healthy=healthy,wall_seconds=elapsed,
                valid_runs=len(runs),valid_runs_per_hour=throughput,checks=checks,
                median_basic_seconds=statistics.median(durations) if durations else None,
                median_run_bytes=statistics.median(storage) if storage else None,
                task_state=current['state'],remaining_items=current['items'].get('pending',0),
                media_policy='No media requested at any capacity level; basic evidence retained using the same policy.')
            if level==1:
                serial=throughput
                for scene in {c['scene'] for c in checks}:
                    values=[c['control_hz'] for c in checks if c['scene']==scene and c['control_hz'] is not None]
                    if values:reference[scene]=statistics.median(values)
            result['speedup_vs_new_serial']=throughput/serial if serial else None
            state['results'].append(result);write(path,state)
            if healthy and throughput>best_throughput:best=level;best_throughput=throughput
            if not healthy:break
        with jobs.connect() as db:
            for result in state['results']:jobs.report(db,result['job_id'])
        capacity=load(jobs.STATE/'capacity.json',{});key=signature(state['bundle'])
        capacity[key]=dict(healthy_parallelism=best,runtime_ceiling=best,
            control_hz_by_scene=reference,control_hz=statistics.median(reference.values()) if reference else None,
            benchmark=str(path),updated=time.time())
        write(jobs.STATE/'capacity.json',capacity)
        state.update(state='completed',recommended_parallelism=best,ended=time.time(),
            speedup=best_throughput/serial if serial else None,
            throughput_target_met=bool(serial and best_throughput/serial>=1.5))
    except BaseException as error:
        state.update(state='failed',ended=time.time(),error=type(error).__name__+': '+str(error))
        active=state.get('active_job')
        if active:
            with jobs.connect() as db:
                db.execute("UPDATE jobs SET state='pausing',error=? WHERE id=? AND state IN ('queued','running')",(state['error'],active))
        raise
    finally:
        write(path,state)
        output=jobs.OPS/'experiments'/state['id'];output.mkdir(exist_ok=True)
        write(output/'benchmark.json',state)
        import html,experiments
        (output/'report.html').write_text('<!doctype html><meta charset="utf-8"><h1>并发容量验收</h1><pre>'+html.escape(json.dumps(state,ensure_ascii=False,indent=2))+'</pre>')
        experiments.publish_desktop(jobs.manage,output)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('state',type=Path)
    execute(parser.parse_args().state)
