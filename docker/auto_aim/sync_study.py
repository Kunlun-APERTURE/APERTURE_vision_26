"""Frozen original-UI comparison, independent calibration and repeatability gates."""
import argparse
import contextlib
from datetime import datetime,timezone
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path
import random
import re
import shutil
import statistics
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

import background_jobs as jobs
import experiments
import manage
from report import load,write
from shots import score
from sync_timing import SCENES,calibrate,digest

DEFAULTS=dict(terrain='flat_range',mode='shoot',matrix='single',algorithm_profile='tracking_fire_v3',
    gimbal_profile='feedforward',physics='no-drag',fire_policy='advice',fire_rate_hz=5.,bullet_speed=25.,
    reported_bullet_speed=25.,aim_origin='muzzle',projectile_visible='on',duration=None,
    command_script='',max_fps=None,algorithm_lifecycle='session',stage='final',evidence='full',
    execution_mode='realtime',timing_profile=None,record_timing='on')

QUALIFIED_SETTINGS={name:DEFAULTS[name] for name in ('terrain','mode','algorithm_profile','gimbal_profile',
    'physics','fire_policy','fire_rate_hz','bullet_speed','reported_bullet_speed','aim_origin','projectile_visible','duration')}


def launch(args):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',args.output):raise ValueError('Expected study name')
    folder=manage.OPS/'experiments'/args.output;folder.mkdir(exist_ok=True)
    with (folder/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return launch_locked(args)


def launch_locked(args):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',args.output):raise ValueError('Expected study name')
    if not 5<=args.repeats<=args.max_repeats<=30:raise ValueError('Expected 5 <= repeats <= max-repeats <= 30')
    levels=[int(v) for v in args.levels.split(',')]
    if not levels or levels[0]!=1 or levels!=sorted(set(levels)) or any(v not in (1,2,4,8) for v in levels):raise ValueError('Invalid levels')
    if 8 in levels and 4 not in levels:raise ValueError('8 lanes require 4-lane acceptance')
    folder=manage.OPS/'experiments'/args.output;folder.mkdir(exist_ok=True)
    path=folder/'study.json';state=load(path)
    if state and not args.resume:raise ValueError('Study exists; use --resume')
    if not state:
        state=dict(version=1,name=args.output,phase=args.phase,repeats=args.repeats,max_repeats=args.max_repeats,
            levels=levels,created=time.time(),state='queued',calibration=[],reference=[],modes={},checks=[])
        write(path,state)
    else:state.update(phase=args.phase,state='queued',error=None);write(path,state)
    if args.new_candidate:
        # Keep every previous version and its runs. Cross-version results are
        # never pooled to make a favourable acceptance interval.
        with jobs.connect() as db:
            for entry in state['modes'].values():
                if any(jobs.get_job(db,j)['state'] in ('queued','running','pausing') for j in entry['jobs']):raise ValueError('Drain or pause candidate tasks first')
        if state.get('bundle'):
            state.setdefault('candidate_history',[]).append(dict(bundle=state.pop('bundle'),modes=state['modes'],qualification=state.get('qualification'),archived=time.time()))
        state['modes']={};state.pop('qualification',None);write(path,state)
    output=(folder/'controller.log').open('a',buffering=1)
    process=subprocess.Popen([sys.executable,'-B','-u',str(Path(__file__)),str(path)],
        stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
    output.close();print(json.dumps(dict(study=args.output,pid=process.pid,status=str(path))))
    return state


def saved(path,state):
    write(path,state)
    folder=path.parent
    (folder/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>同步实验验收</title>'
        '<style>body{font:15px system-ui;max-width:1200px;margin:2em auto}pre{white-space:pre-wrap}</style>'
        '<h1>非实时同步实验验收</h1><p>仿真时间与现实耗时分别记录；未通过不切换默认。</p><pre>'+
        html.escape(json.dumps(state,ensure_ascii=False,indent=2))+'</pre>')


@contextlib.contextmanager
def original_ui(state):
    alignment=load(manage.OPS/'experiments/background-alignment-20261022-warm/alignment.json')
    with jobs.connect() as db:bundle=Path(jobs.get_job(db,alignment['modes']['background']['job_id'])['bundle'])
    meta=load(bundle/'bundle.json');reference=manage.DESKTOP/'.local/share/rm26-sim/original-ui-reference/sp_vision_25'
    reference.mkdir(parents=True,exist_ok=True)
    def frozen_copy(source,target):
        target=Path(target)
        if target.exists():
            if digest(source)==digest(target):return str(target)
            target.unlink()
        return shutil.copy2(source,target)
    for name in ('assets','configs'):
        shutil.copytree(bundle/'sp_vision_25'/name,reference/name,dirs_exist_ok=True,copy_function=frozen_copy)
    binary=reference/'build/simulator/sim_auto_aim';binary.parent.mkdir(parents=True,exist_ok=True)
    frozen_copy(bundle/'sp_vision_25/build/simulator/sim_auto_aim',binary)
    state['original_ui']=dict(bundle=str(bundle),images=meta['images'],algorithm_sha256=digest(binary))
    names=('RM26_SIMULATOR_IMAGE','RM26_BRIDGE_IMAGE','RM26_ALGORITHM_ROOT')
    previous={k:os.environ.get(k) for k in names}
    os.environ.update(RM26_SIMULATOR_IMAGE=meta['images']['simulator'],RM26_BRIDGE_IMAGE=meta['images']['simulator'],
        RM26_ALGORITHM_ROOT='/home/openvino/.local/share/rm26-sim/original-ui-reference/sp_vision_25')
    def snapshot(folder,scene,args):
        proxy=SimpleNamespace(ROOT=bundle,OPS=manage.OPS,RUNTIME=manage.RUNTIME,inspect=manage.inspect,
            profile=manage.profile,run=manage.run,SIMULATOR=manage.SIMULATOR,BRIDGE=manage.BRIDGE,
            FROZEN_REPOSITORIES=meta['repositories'])
        result=experiments.snapshot(proxy,folder,scene,args)
        runtime=reference/'build/sim/calibration.yaml'
        if runtime.exists():shutil.copy2(runtime,folder/'configuration/runtime-calibration.yaml')
        result['reference_kind']='original_ui_frozen'
        result['configuration_sha256']={str(p.relative_to(folder/'configuration')):experiments.digest(p)
            for p in (folder/'configuration').rglob('*') if p.is_file()}
        write(folder/'manifest.json',result);return result
    manage.snapshot=snapshot
    try:yield
    finally:
        if hasattr(manage,'snapshot'):delattr(manage,'snapshot')
        for name,value in previous.items():
            if value is None:os.environ.pop(name,None)
            else:os.environ[name]=value


def ui_runs(path,state,key,target,scenes=SCENES):
    entries=state[key]
    if all(any(r['scene']==scene and r['repeat_id']==repeat for r in entries)
        for repeat in range(target) for scene in scenes):return
    with original_ui(state),(manage.RUNTIME/'operation.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            manage.stop()
            manage.start(argparse.Namespace(**dict(DEFAULTS,scene='static',control='off',view='off',replace=False,fire='auto')))
            for repeat in range(target):
                for scene in (scenes if repeat%2==0 else tuple(reversed(scenes))):
                    if any(r['scene']==scene and r['repeat_id']==repeat for r in entries):continue
                    opts=argparse.Namespace(**DEFAULTS,scene=scene,seed=2026102207+repeat%3,
                        repeat_id=repeat,label='sync-'+key)
                    folder=experiments.run_one(manage,scene,opts)
                    result=load(folder/'summary.json')
                    entries.append(dict(run_id=folder.name,scene=scene,repeat_id=repeat,seed=opts.seed))
                    saved(path,state)
                    if result['verdict']=='invalid':raise RuntimeError('Invalid original UI trial: '+folder.name)
        finally:
            manage.stop()
            profile=manage.profile();profile.update(scene='static',terrain='arena',control='off',fire='off',view='off',
                algorithm_profile='legacy',gimbal_profile='legacy',max_fps=None,record_timing='off')
            manage.save_profile(profile)


def row(folder,output,repeat):
    manifest=load(folder/'manifest.json');summary=load(folder/'summary.json');state=load(folder/'run-state.json')
    output.mkdir(parents=True,exist_ok=True)
    copied=dict(manifest,scoring_version=2,impact_quality_version=1)
    metrics,errors=score(folder,copied,state,output_folder=output)
    values=dict(summary['metrics'],**metrics)
    value=dict(run_id=folder.name,repeat_id=repeat,scene=manifest['scene'],metrics=values,
        invalid=summary['verdict']=='invalid' or bool(errors),errors=errors,
        source_manifest_sha256=digest(folder/'manifest.json'),duration=manifest['duration_s'])
    write(output/'rescore.json',value);return value


def bootstrap(left,right,margin,auxiliary=False):
    common=sorted(set(left)&set(right));pairs=[(left[i],right[i]) for i in common]
    if len(pairs)<5:return dict(conclusion='insufficient_samples',pairs=len(pairs))
    if any(r['invalid'] for pair in pairs for r in pair):return dict(conclusion='invalid',pairs=len(pairs))
    if any(r['metrics']['shots_fired']<=0 for pair in pairs for r in pair):
        return dict(conclusion='invalid',pairs=len(pairs),reason='A paired trial has no shooting samples')
    names=['target_hit_rate','weighted_hit_rate']+(['fire_frequency','correct_tracking_fraction','longest_loss_s'] if auxiliary else [])
    def aggregate(rows):
        shots=sum(r['metrics']['shots_fired'] for r in rows);duration=sum(r['duration'] for r in rows)
        return dict(target_hit_rate=sum(r['metrics']['target_armor_hits'] for r in rows)/shots if shots else None,
            weighted_hit_rate=sum(r['metrics']['weighted_hit_score_sum'] or 0 for r in rows)/shots if shots else None,
            fire_frequency=shots/duration if duration else 0,
            correct_tracking_fraction=statistics.mean(r['metrics']['correct_tracking_fraction'] or 0 for r in rows),
            longest_loss_s=statistics.mean(r['metrics']['longest_loss_s'] for r in rows),shots=shots)
    a,b=aggregate([p[0] for p in pairs]),aggregate([p[1] for p in pairs])
    result=dict(pairs=len(pairs),baseline=a,candidate=b)
    if min(a['shots'],b['shots'])<30:return dict(result,conclusion='insufficient_samples')
    rng=random.Random(20261022);samples={n:[] for n in names}
    for _ in range(5000):
        selected=[pairs[rng.randrange(len(pairs))] for _ in pairs]
        x,y=aggregate([p[0] for p in selected]),aggregate([p[1] for p in selected])
        for name in names:
            value=(y[name]/x[name]-1 if name=='fire_frequency' and x[name] else y[name]-x[name])
            samples[name].append(value)
    intervals={n:[sorted(v)[124],sorted(v)[4874]] for n,v in samples.items()}
    limits={n:(.05 if n in ('fire_frequency','correct_tracking_fraction') else .2 if n=='longest_loss_s' else margin) for n in names}
    passed=all(-limits[n]<=bounds[0] and bounds[1]<=limits[n] for n,bounds in intervals.items())
    different=any(bounds[1]<-limits[n] or bounds[0]>limits[n] for n,bounds in intervals.items())
    return dict(result,intervals_95=intervals,margins=limits,conclusion='passed' if passed else 'different' if different else 'insufficient_samples')


def ingest(db,job_id,entry):
    """Recover a completed batch even if the controller died before saving it."""
    changed=False
    for item in db.execute('SELECT * FROM items WHERE job_id=? ORDER BY ordinal',(job_id,)):
        attempts=db.execute("SELECT * FROM attempts WHERE item_id=? AND verdict!='invalid' AND run_id IS NOT NULL ORDER BY started",(item['id'],)).fetchall()
        if not attempts:raise RuntimeError('No valid synchronous evidence')
        if len(attempts)>1:raise RuntimeError('Multiple valid attempts cannot replace measured scores')
        opts=json.loads(item['settings']);run_id=attempts[0]['run_id']
        previous=next((r for r in entry['runs'] if r['scene']==opts['scene'] and r['repeat_id']==opts['repeat_id']),None)
        if previous:
            if previous['run_id']!=run_id:raise RuntimeError('A completed pair cannot be replaced by a rerun')
            continue
        entry['runs'].append(dict(run_id=run_id,scene=opts['scene'],repeat_id=opts['repeat_id'],seed=opts['seed']))
        changed=True
    return changed


def background(path,state,profile,level,repeats,repeat_ids=None,scenes=SCENES):
    key=profile+'_p'+str(level);entry=state['modes'].setdefault(key,dict(profile=profile,level=level,jobs=[],runs=[]))
    if entry['jobs']:
        changed=False
        with jobs.connect() as db:
            for job_id in entry['jobs']:
                if jobs.get_job(db,job_id)['state']=='completed':changed=ingest(db,job_id,entry) or changed
        if changed:saved(path,state)
    missing=[(n,scene) for n in (range(repeats) if repeat_ids is None else repeat_ids) for scene in scenes
        if not any(r['repeat_id']==n and r['scene']==scene for r in entry['runs'])]
    if not missing:return entry
    with jobs.connect() as db:
        active=[jid for jid in entry['jobs'] if jobs.get_job(db,jid)['state'] not in ('completed','cancelled')]
    if not active:
        job_id='job_sync_'+uuid.uuid4().hex[:12];now=time.time()
        settings=[]
        for n,scene in missing:
            s=jobs.validate({k:v for k,v in dict(DEFAULTS,scene=scene,max_fps=240,execution_mode='lockstep',timing_profile=profile,
                seed=2026102207+n%3,label=key,record_timing='off').items() if k in jobs.FIELDS})
            s.update(repeat_id=n,execution_profile='lockstep-'+profile);settings.append(s)
        with jobs.connect() as db,jobs.transaction(db):
            spec=dict(version=1,_protocol=2,_sync_study=state['name'],parallelism=level)
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(job_id,'queued',now,now,json.dumps(spec),state['bundle'],level,1,None))
            db.executemany('INSERT INTO items(job_id,ordinal,state,settings) VALUES(?,?,?,?)',
                [(job_id,i,'pending',json.dumps(s)) for i,s in enumerate(settings)])
            jobs.event(db,job_id,'Synchronous timing qualification; no real-time frequency gate')
        entry['jobs'].append(job_id);active=[job_id];saved(path,state)
    for job_id in active:
        while True:
            with jobs.connect() as db:status=jobs.status(db,job_id)
            if status['state']=='completed':break
            if status['state'] in ('paused','cancelled'):raise RuntimeError('Synchronous task requires inspection/resume: '+job_id)
            time.sleep(2)
        with jobs.connect() as db:
            ingest(db,job_id,entry)
            jobs.report(db,job_id)
        saved(path,state)
    return entry


def compare_entries(path,state,left,right,margin,auxiliary=False):
    results={}
    for scene in SCENES:
        def collect(entries):
            return {e['repeat_id']:row(manage.OPS/'experiments'/e['run_id'],path.parent/'rescore'/e['run_id'],e['repeat_id'])
                for e in entries if e['scene']==scene}
        results[scene]=bootstrap(collect(left),collect(right),margin,auxiliary)
    return results


def all_scenes_pass(results):
    return set(results)==set(SCENES) and all(r['conclusion']=='passed' for r in results.values())


def recommend(qualification):
    for profile in ('ui-fixed-v1','ui-trace-v1'):
        checks=qualification.get(profile,{})
        stability=checks.get('repeatability',{})
        if all_scenes_pass(checks.get('ui_equivalence',{})) and all_scenes_pass(stability.get('1',{})) and all_scenes_pass(stability.get('4',{})):
            return profile
    return None


def balanced_performance(state,profile,level):
    expected={(scene,n) for scene in SCENES for n in range(state['repeats'])}
    entry=state['modes'][profile+'_p'+str(level)]
    with jobs.connect() as db:
        for job_id in entry['jobs']:
            settings=[json.loads(r['settings']) for r in db.execute('SELECT settings FROM items WHERE job_id=?',(job_id,))]
            if len(settings)!=len(expected) or {(s['scene'],s['repeat_id']) for s in settings}!=expected:continue
            status=jobs.status(db,job_id)
            if status['state']!='completed':continue
            seconds=max(a['ended'] for a in status['attempts'])-status['created']
            valid=sum(a['verdict']!='invalid' and a['state']=='finished' for a in status['attempts'])
            if valid!=len(expected):raise RuntimeError('Balanced capacity batch lacks complete valid results')
            return dict(job_id=job_id,valid_runs=valid,wall_seconds=seconds,valid_runs_per_hour=valid/seconds*3600,
                workload='Same first paired repeats in all four scenes; includes provisioning and retries')
    return None


def drain_profile(path,state,profile):
    """Finish already-dispatched evidence before changing the tested mode."""
    for entry in state['modes'].values():
        if entry['profile']!=profile:continue
        for job_id in entry['jobs']:
            while True:
                with jobs.connect() as db:status=jobs.status(db,job_id)
                if status['state']=='completed':break
                if status['state'] in ('paused','cancelled'):raise RuntimeError('Inspect stopped qualification task: '+job_id)
                time.sleep(2)
            with jobs.connect() as db:ingest(db,job_id,entry)
    saved(path,state)


def execute(path):
    path=Path(path);state=load(path)
    with (path.parent/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        controller_sha=digest(Path(__file__))
        archive=path.parent/('controller-'+controller_sha+'.py')
        if not archive.exists():shutil.copy2(__file__,archive)
        state.setdefault('controller_history',[]).append(dict(started=time.time(),sha256=controller_sha,source=archive.name))
        state.update(state='running',pid=os.getpid());saved(path,state)
        try:
            if state['phase'] in ('calibrate','all'):
                ui_runs(path,state,'calibration',3)
                profiles=calibrate([manage.OPS/'experiments'/e['run_id'] for e in state['calibration']],manage.OPS/'timing_profiles')
                state['profiles']={p.stem:dict(path=str(p),sha256=digest(p)) for p in profiles};saved(path,state)
            if state['phase'] in ('evaluate','all'):
                if not state.get('profiles'):raise ValueError('Calibrate first')
                if not state.get('bundle'):state['bundle']=str(jobs.freeze('sync_'+uuid.uuid4().hex[:12]));saved(path,state)
                ui_runs(path,state,'reference',state['repeats'])
                for profile in ('ui-trace-v1','ui-fixed-v1'):
                    checks=state.setdefault('qualification',{}).setdefault(profile,dict(ui_equivalence={},repeatability={}))
                    count=state['repeats']
                    pending=SCENES
                    while True:
                        baseline=background(path,state,profile,1,count,scenes=pending)
                        ui=compare_entries(path,state,state['reference'],baseline['runs'],.05,True)
                        checks['ui_equivalence']=ui;saved(path,state)
                        if any(r['conclusion'] in ('invalid','different') for r in ui.values()) or not any(r['conclusion']=='insufficient_samples' for r in ui.values()) or count>=state['max_repeats']:break
                        pending=tuple(scene for scene,r in ui.items() if r['conclusion']=='insufficient_samples')
                        count=min(count+5,state['max_repeats'])
                        ui_runs(path,state,'reference',count,scenes=pending)
                    count=state['repeats']
                    pending=tuple(scene for scene in SCENES if checks['repeatability'].get('1',{}).get(scene,{}).get('conclusion')!='passed')
                    while True:
                        baseline=background(path,state,profile,1,count,scenes=pending)
                        background(path,state,profile,1,count,repeat_ids=range(30,30+count),scenes=pending)
                        first=[r for r in baseline['runs'] if r['repeat_id']<30]
                        second=[dict(r,repeat_id=r['repeat_id']-30) for r in baseline['runs'] if r['repeat_id']>=30]
                        checks['repeatability']['1']=compare_entries(path,state,first,second,.02)
                        state.setdefault('qualification',{})[profile]=checks;saved(path,state)
                        results=checks['repeatability']['1'].values()
                        if any(r['conclusion'] in ('invalid','different') for r in results) or not any(r['conclusion']=='insufficient_samples' for r in checks['repeatability']['1'].values()) or count>=state['max_repeats']:break
                        pending=tuple(scene for scene,r in checks['repeatability']['1'].items() if r['conclusion']=='insufficient_samples')
                        count=min(count+5,state['max_repeats'])
                    if not all_scenes_pass(checks['repeatability']['1']):
                        # A baseline that cannot establish its own stability
                        # cannot grant any parallel level a certificate.
                        # Retain/drain earlier exploratory batches on resume.
                        checks['expansion_gate']='not_qualified_single_baseline'
                        drain_profile(path,state,profile)
                        for key,entry in state['modes'].items():
                            if entry['profile']==profile and entry['level']>1:
                                checks['repeatability'][str(entry['level'])]=compare_entries(path,state,
                                    state['modes'][profile+'_p1']['runs'],entry['runs'],.02)
                        saved(path,state);continue
                    checks['expansion_gate']='passed_single_baseline'
                    for level in state['levels'][1:]:
                        if level==8 and not all_scenes_pass(checks['repeatability'].get('4',{})):break
                        count=state['repeats']
                        pending=tuple(scene for scene in SCENES if checks['repeatability'].get(str(level),{}).get(scene,{}).get('conclusion')!='passed')
                        while True:
                            baseline=background(path,state,profile,1,count,scenes=pending)
                            entry=background(path,state,profile,level,count,scenes=pending)
                            checks['repeatability'][str(level)]=compare_entries(path,state,baseline['runs'],entry['runs'],.02)
                            state.setdefault('qualification',{})[profile]=checks;saved(path,state)
                            if any(r['conclusion'] in ('invalid','different') for r in checks['repeatability'][str(level)].values()) or not any(r['conclusion']=='insufficient_samples' for r in checks['repeatability'][str(level)].values()) or count>=state['max_repeats']:break
                            pending=tuple(scene for scene,r in checks['repeatability'][str(level)].items() if r['conclusion']=='insufficient_samples')
                            count=min(count+5,state['max_repeats'])
                        state.setdefault('qualification',{})[profile]=checks;saved(path,state)
                        if any(r['conclusion'] in ('invalid','different') for r in checks['repeatability'][str(level)].values()):break
                    state.setdefault('qualification',{})[profile]=checks;saved(path,state)
                # An unsuccessful 8-lane exploration cannot revoke a completed
                # 4-lane qualification. Dispatch chooses only accepted levels.
                state['recommended_profile']=recommend(state['qualification'])
                state['certified']=state['recommended_profile'] is not None
                state['default_execution_mode']='realtime'
                state['certified_scope']=dict(scenes=list(SCENES),settings=QUALIFIED_SETTINGS)
                state['observed_performance']={key:balanced_performance(state,entry['profile'],entry['level'])
                    for key,entry in state['modes'].items()}
                for profile in ('ui-trace-v1','ui-fixed-v1'):
                    q=state['qualification'][profile]
                    accepted=[int(level) for level,checks in q['repeatability'].items() if all_scenes_pass(checks)]
                    if 1 not in accepted:accepted=[]
                    if not accepted:
                        state.setdefault('capacity',{})[profile]=dict(accepted_levels=[],recommended_parallelism=None)
                        continue
                    # Capacity certification is separate from UI equivalence.
                    # Supplemental scenes have an unequal workload mix. Use
                    # only the common balanced batch when selecting capacity.
                    throughputs={level:state['observed_performance'][profile+'_p'+str(level)]['valid_runs_per_hour']
                        for level in accepted}
                    best=max(throughputs,key=throughputs.get)
                    state.setdefault('capacity',{})[profile]=dict(accepted_levels=accepted,
                        valid_runs_per_hour=throughputs,recommended_parallelism=best)
                    from background_capacity import synchronous_signature
                    key=synchronous_signature(state['bundle'],profile)
                    capacity=load(jobs.STATE/'capacity.json',{})
                    capacity[key]=dict(healthy_parallelism=best,runtime_ceiling=best,study=str(path),
                        qualified_settings=QUALIFIED_SETTINGS,
                        ui_equivalent=all(c['conclusion']=='passed' for c in q['ui_equivalence'].values()),updated=time.time())
                    write(jobs.STATE/'capacity.json',capacity)
            state.update(state='completed',ended=time.time());saved(path,state)
        except BaseException as error:
            state.update(state='failed',ended=time.time(),error=type(error).__name__+': '+str(error));saved(path,state);raise
        finally:experiments.publish_desktop(manage,path.parent)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('state',type=Path);execute(parser.parse_args().state)
