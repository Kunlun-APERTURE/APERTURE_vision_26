#!/usr/bin/env python3
"""Host-owned durable queue. Docker access stays on the host, behind sim.sh."""
import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import html
import itertools
import json
import math
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

import manage
from report import load, write

OPS=Path(__file__).resolve().parent
STATE=OPS/'.runtime/jobs'
DB=STATE/'queue.sqlite3'
SERVICE='rm26-experiments.service'
FIELDS={'scene','terrain','mode','matrix','bullet_speed','reported_bullet_speed','physics','label',
    'fire_policy','fire_rate_hz','aim_origin','algorithm_profile','gimbal_profile','gimbal_dynamics','seed',
    'projectile_visible','duration','max_fps','algorithm_lifecycle','stage','evidence',
    'execution_mode','timing_profile','record_timing','preview','session_evidence','detector_history',
    'input_contract','sensor_seed','warmup_frames'}
MAX_SLOTS=8


def stack_signature(metadata):
    names=['background_jobs.py','background_runtime.py','experiments.py','manage.py','bridge.py','observer.py',
        'evaluator.py','report.py','shots.py','impact_quality.py','scenario_catalog.py',
        'highspeed_scenarios.py','focus_scenarios.py','movement_scenarios.py','movement_analysis.py','fastdds-udp.xml','session_evidence.py','render_backend.py']
    selected={name:metadata['files'].get('docker/auto_aim/'+name) for name in names}
    return hashlib.sha256(json.dumps(dict(images=metadata['images'],code=selected,
        gpu_uuid=metadata['gpu_uuid'],renderer=metadata.get('renderer',{'backend':'nvidia'})),sort_keys=True).encode()).hexdigest()


def add_parser(commands):
    p=commands.add_parser('jobs',help='Manage durable background experiments')
    s=p.add_subparsers(dest='jobs_action',required=True)
    s.add_parser('list')
    for action in ('status','pause','resume','cancel','report','logs'):
        child=s.add_parser(action);child.add_argument('job_id')
        if action=='logs':
            child.add_argument('--follow',action='store_true');child.add_argument('--tail',type=int,default=100)
    s.add_parser('install',help='Install and start the host systemd scheduler')
    s.add_parser('service-status')
    s.add_parser('daemon',help=argparse.SUPPRESS)
    s.add_parser('cleanup',help=argparse.SUPPRESS)
    media=s.add_parser('render',help='Queue frozen, offline media generation')
    media.add_argument('job_id');media.add_argument('--run',dest='run_id')
    media.add_argument('--all',action='store_true')
    review=s.add_parser('review',help='Record review of explicitly viewed runs')
    review.add_argument('job_id');review.add_argument('--runs',nargs='+',required=True)
    review.add_argument('--decision',choices=['pass','fail'],required=True)
    review.add_argument('--note',required=True)
    benchmark=s.add_parser('benchmark',help='Measure capacity using the standard backend profile')
    benchmark.add_argument('--levels',default='1,2,4,8')
    benchmark.add_argument('--resume',help='Resume/finalize an existing capacity controller without replacing measured runs')


def connect():
    STATE.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(DB,timeout=15,isolation_level=None)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.executescript('''
      CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,state TEXT NOT NULL,created REAL NOT NULL,
        updated REAL NOT NULL,spec TEXT NOT NULL,bundle TEXT NOT NULL,parallelism INTEGER NOT NULL,
        exploratory INTEGER NOT NULL,error TEXT);
      CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY,job_id TEXT NOT NULL,ordinal INTEGER NOT NULL,
        state TEXT NOT NULL,settings TEXT NOT NULL,failures INTEGER NOT NULL DEFAULT 0,
        UNIQUE(job_id,ordinal));
      CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,item_id INTEGER NOT NULL,slot INTEGER NOT NULL,
        state TEXT NOT NULL,started REAL NOT NULL,ended REAL,run_id TEXT,verdict TEXT,error TEXT);
      CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,job_id TEXT,created REAL NOT NULL,message TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS artifacts(run_id TEXT PRIMARY KEY,job_id TEXT NOT NULL,bundle TEXT NOT NULL,
        state TEXT NOT NULL,requested REAL NOT NULL,started REAL,ended REAL,error TEXT);
    ''')
    return db


@contextlib.contextmanager
def transaction(db):
    db.execute('BEGIN IMMEDIATE')
    try:
        yield
    except BaseException:
        db.execute('ROLLBACK');raise
    else:
        db.execute('COMMIT')


def event(db,job_id,message):
    db.execute('INSERT INTO events(job_id,created,message) VALUES(?,?,?)',(job_id,time.time(),message))


def pause_after_invalid(db,job_id,failures):
    spec=json.loads(db.execute('SELECT spec FROM jobs WHERE id=?',(job_id,)).fetchone()[0])
    stop=spec.get('_stop_on_invalid',False)
    if failures>=2 or stop:
        db.execute("UPDATE jobs SET state='paused',error=? WHERE id=?",('Invalid experiment; inspect retained evidence before resuming',job_id))
    if stop and spec.get('study'):
        # Study-local anomaly handling: finish already running evidence, pause
        # all queued cases before a further registered seed is started.
        db.execute("UPDATE jobs SET state=CASE WHEN state='running' THEN 'pausing' ELSE 'paused' END,error=?,updated=? WHERE json_extract(spec,'$.study')=? AND state IN ('queued','running')",
            ('Study halted after invalid experiment; inspect retained evidence',time.time(),spec['study']))


def validate(settings):
    # Validate through the existing argument parser, including all enum choices.
    argv=['experiment','run']
    for key,value in settings.items():
        if key not in FIELDS:raise ValueError('Unsupported experiment field: '+key)
        if value is not None:argv+=['--'+key.replace('_','-'),str(value)]
    parsed=vars(manage.parser().parse_args(argv))
    s={k:v for k,v in parsed.items() if k in FIELDS}
    from input_contract import validate as validate_contract
    validate_contract(s)
    if s['detector_history']=='session' and s['session_evidence']=='off':
        raise ValueError('Session detector history requires complete session evidence')
    if s['max_fps'] is None:s['max_fps']=240.0
    if s['execution_mode']=='lockstep':
        if s['session_evidence']=='off':raise ValueError('Lockstep requires complete session evidence')
        import re
        from sync_timing import load_profile,SCENES
        if s['scene'] not in SCENES:
            from benchmark.scenarios import get
            get(s['scene'])
        if s['physics']!='no-drag' or s['algorithm_profile'] not in ('tracking_fire_v3','image_v1') or s['fire_policy']!='advice':
            raise ValueError('Lockstep v1 requires no-drag, tracking_fire_v3 and advice')
        if not s['timing_profile'] or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',s['timing_profile']):
            raise ValueError('Expected timing profile name')
        timing=load_profile(OPS/'timing_profiles'/(s['timing_profile']+'.json'))
        if timing['kind']=='trace' and s['scene'] not in timing['scenes']:raise ValueError('Trace timing missing scene')
        s['algorithm_lifecycle']='fresh'
    for key in ('duration','bullet_speed','reported_bullet_speed','fire_rate_hz','max_fps'):
        if s[key] is not None and (isinstance(s[key],bool) or not math.isfinite(s[key])):
            raise ValueError(key+' must be finite')
    if s['duration'] is not None and not 1<=s['duration']<=18:raise ValueError('duration must be 1..18 seconds')
    if s['max_fps'] is not None and not (s['max_fps']==0 or 1<=s['max_fps']<=1000):
        raise ValueError('max_fps must be 0 (uncapped) or 1..1000')
    if not 14<=s['bullet_speed']<=40:raise ValueError('bullet_speed must be 14..40 m/s')
    if s['reported_bullet_speed'] is not None and not 14<=s['reported_bullet_speed']<=40:
        raise ValueError('reported_bullet_speed must be 14..40 m/s')
    if not 0<s['fire_rate_hz']<=5:raise ValueError('Background fire rate must be >0 and <=5 Hz')
    if s['fire_policy']=='continuous' and s['fire_rate_hz']!=2:
        raise ValueError('Legacy continuous bench requires 2 Hz; use advice for configurable firing')
    if s['scene'] in manage.HIGHSPEED and s['terrain']!='flat_range':
        raise ValueError('High-speed scenes require terrain=flat_range')
    if s['mode']=='track' and s['matrix']:
        raise ValueError('Experiment matrices require mode=shoot')
    if not 0<=s['seed']<=4294967295:raise ValueError('seed must be uint32')
    if len(s['label'])>100:raise ValueError('label is limited to 100 characters')
    s['reported_bullet_speed']=s['reported_bullet_speed'] or s['bullet_speed']
    s.update(control='off',fire='auto' if s['mode']=='shoot' else 'off',view='off',command_script='')
    return s


def expand(args):
    if args.resume:raise ValueError('Use jobs resume JOB_ID for durable tasks')
    if args.command_script:
        raise ValueError('Fixed command playback is available through experiment run; queued v1 jobs use algorithm control')
    base={k:v for k,v in vars(args).items() if k in FIELDS}
    repeat=args.repeat if args.repeat is not None else (1 if args.mode=='shoot' or args.matrix=='highspeed-v1' else 3)
    spec={'version':1,'defaults':base,'matrix':{},'repeat':repeat,'parallelism':args.parallelism}
    if args.spec:
        raw=Path(args.spec)
        if raw.is_relative_to('/home/openvino'):
            raw=manage.DESKTOP/raw.relative_to('/home/openvino')
        elif raw.is_relative_to('/workspace/sp_vision_25'):
            raw=manage.ROOT/'sp_vision_25'/raw.relative_to('/workspace/sp_vision_25')
        elif not raw.is_absolute() and not raw.exists():
            raw=manage.ROOT/raw
        path=raw.resolve()
        if not path.is_relative_to(manage.ROOT):raise ValueError('Specification must be inside this workspace')
        if path.stat().st_size>1024*1024:raise ValueError('Specification exceeds 1 MiB')
        spec=load(path)
        if not isinstance(spec,dict) or spec.get('version')!=1:raise ValueError('Expected version 1 matrix specification')
        if set(spec)-{'version','defaults','matrix','cases','repeat','parallelism'}:raise ValueError('Unknown matrix specification key')
        base.update(spec.get('defaults',{}));repeat=spec.get('repeat',1)
    if isinstance(repeat,bool) or not isinstance(repeat,int) or not 1<=repeat<=30:raise ValueError('repeat must be 1..30')
    parallelism=args.parallelism if args.parallelism is not None else spec.get('parallelism')
    if parallelism is None:parallelism='auto'
    if args.stage=='final':base.update(stage='final',evidence='full')
    elif args.evidence=='full':base['evidence']='full'
    if parallelism=='auto':parallelism=0
    elif isinstance(parallelism,str) and parallelism.isdigit():parallelism=int(parallelism)
    if type(parallelism) is not int or not 0<=parallelism<=MAX_SLOTS:
        raise ValueError('parallelism must be auto or 1..8')
    if 'cases' in spec:
        cases=spec['cases']
        if spec.get('matrix') or repeat!=1:raise ValueError('Ordered cases require no matrix and repeat=1')
        if not isinstance(cases,list) or not 1<=len(cases)<=10000:
            raise ValueError('Expected 1..10000 ordered cases')
        if any(not isinstance(case,dict) or set(case)-FIELDS for case in cases):
            raise ValueError('Unsupported ordered case field')
        rows=[validate(dict(base,**case)) for case in cases]
        if any(row['scene'] not in manage.SCENES or row['matrix'] not in (None,'single') for row in rows):
            raise ValueError('Ordered cases require concrete scenes and matrix=single')
        return rows,parallelism,spec
    matrix=spec.get('matrix',{})
    if not isinstance(matrix,dict) or set(matrix)-FIELDS:raise ValueError('Unsupported matrix field')
    if any(not isinstance(v,list) or not v or len(v)>100 for v in matrix.values()):
        raise ValueError('Matrix values must be nonempty arrays with <=100 entries')
    if math.prod(len(v) for v in matrix.values())*repeat>10000:raise ValueError('Matrix exceeds 10000 items')
    rows=[]
    for values in itertools.product(*matrix.values()):
        settings=dict(base,**dict(zip(matrix,values)))
        scenes={'all':['static','line','spin'],'advice-motion':['move_ref','line','ego_sweep','ego_sweep_reverse'],
            'ego-all':['move_ref','ego_left','ego_right'],'stability-all':['move_ref','line','ego_left','ego_right']}.get(settings['scene'],[settings['scene']])
        if settings.get('matrix')=='highspeed-v1':
            scenes=[s for s in manage.HIGHSPEED if s.startswith('hs_')]
            settings.update(terrain='flat_range',mode='shoot',algorithm_profile='tracking_fire_v3',gimbal_profile='feedforward')
            physics=['no-drag','drag']
        else:physics=[settings['physics']]
        name=settings.get('matrix')
        variants=[{}]
        if name in ('hit-v2','hit-v3'):variants=[dict(physics=p,aim_origin=o) for p in ('no-drag','drag') for o in ('muzzle','gimbal')]
        elif name=='gimbal-ab':variants=[dict(physics=p,gimbal_profile=g) for p in ('no-drag','drag') for g in manage.GIMBAL_PROFILES]
        elif name=='ballistic-v1':variants=[dict(physics=p,bullet_speed=a,reported_bullet_speed=r) for p in ('no-drag','drag') for a,r in ((25,25),(20,20),(28,28),(25,23),(25,27))]
        for repetition in range(repeat):
            for scene in (scenes if repetition%2==0 else list(reversed(scenes))):
                for p in physics:
                    for variant in variants:
                        rows.append(validate(dict(settings,scene=scene,physics=p,**{k:v for k,v in variant.items() if k!='physics'}) | ({'physics':variant['physics']} if 'physics' in variant else {})))
    if not rows or len(rows)>10000:raise ValueError('Expected 1..10000 expanded experiment items')
    # Adjacent conditions interleave rather than completing one algorithm first.
    rows.sort(key=lambda s:(s['seed'],s['scene'],s['physics']))
    if args.matrix=='highspeed-v1' and not args.spec and args.seed==20261019:
        rows=[dict(row,seed=seed) for seed in (2026102104,2026102105) for row in rows]
    return rows,parallelism,spec


def freeze(job_id,gimbal_dynamics="legacy",algorithm_binary=None,image_tags=None,renderer='auto'):
    import experiments
    from gimbal_dynamics import simulator_image
    image_tags=image_tags or {}
    from render_backend import select
    rendering=select(renderer)
    requested=image_tags.get("simulator",manage.IMAGE)
    selected=manage.inspect(requested)
    unified=bool(selected and selected['Config'].get('Labels',{}).get('aperture.unified')=='1')
    if unified:
        from gimbal_dynamics import FORMULA_SHA256, MODELS
        models=(gimbal_dynamics,) if isinstance(gimbal_dynamics,str) else tuple(gimbal_dynamics)
        if any(model not in MODELS for model in models) or selected['Config']['Labels'].get('rm26.reference_yaw_formula_sha256')!=FORMULA_SHA256:
            raise RuntimeError('Unified runtime yaw provenance differs')
    simulator_tag=requested if unified else simulator_image(gimbal_dynamics,requested)
    bundle=STATE/'bundles'/job_id
    bundle.mkdir(parents=True)
    provenance=bundle/'provenance';provenance.mkdir()
    repositories={name:experiments.repository(manage,name,provenance) for name in ('sp_vision_25','vision_simulator_26')}
    source_ops=bundle/'docker/auto_aim';source_ops.mkdir(parents=True)
    for source in OPS.iterdir():
        if source.is_file() and source.suffix in ('.py','.yml','.json','.toml','.Dockerfile','.xml'):
            shutil.copy2(source,source_ops/source.name)
    for name in ('scenarios','baselines','timing_profiles','benchmark'):
        if (OPS/name).exists():shutil.copytree(OPS/name,source_ops/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for name in ('sp_vision_25','vision_simulator_26'):
        root=manage.ROOT/name
        paths=subprocess.check_output(['git','-C',str(root),'ls-files','-z','--cached','--others','--exclude-standard']).decode().split('\0')
        for relative in paths:
            source=root/relative
            if not relative or not source.is_file() or any(p in ('build','target','.git') for p in Path(relative).parts):continue
            if source.suffix not in ('.rs','.cpp','.hpp','.h','.py','.sh','.toml','.yaml','.yml','.msg','.srv','.txt','.lock','.cmake'):continue
            target=bundle/name/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
    shutil.copytree(manage.ROOT/'sp_vision_25/assets',bundle/'sp_vision_25/assets',dirs_exist_ok=True)
    binary=bundle/'sp_vision_25/build/simulator/sim_auto_aim';binary.parent.mkdir(parents=True)
    shutil.copy2(algorithm_binary or manage.ROOT/'sp_vision_25/build/simulator/sim_auto_aim',binary)
    images={};retained_images={}
    for key,tag in (('simulator',simulator_tag),('evaluator',image_tags.get('evaluator','daedalus:rm26-evaluator')),('algorithm',image_tags.get('algorithm','daedalus:rm26-algorithm'))):
        info=manage.inspect(tag)
        if not info:raise RuntimeError('Missing image '+tag+'; build background prerequisites first')
        images[key]=info['Id']
        retained='daedalus:rm26-frozen-'+key+'-'+job_id
        manage.run(['docker','image','tag',info['Id'],retained])
        if manage.inspect(retained)['Id']!=info['Id']:
            raise RuntimeError('Image changed while freezing '+tag+'; submit again')
        retained_images[key]=retained
    evaluator=manage.inspect(images['evaluator'])
    same_runtime=unified and len(set(images.values()))==1
    if not same_runtime and evaluator['Config'].get('Labels',{}).get('rm26.integration_image_id')!=manage.inspect(image_tags.get('simulator',manage.IMAGE))['Id']:
        raise RuntimeError('Evaluator interfaces differ; rebuild evaluator')
    # Reject an inconsistent source snapshot if an editor saved during copying.
    for name,before in repositories.items():
        after=experiments.repository(manage,name,provenance)
        if after!=before:raise RuntimeError('Sources changed while freezing '+name+'; submit again')
    files={str(p.relative_to(bundle)):experiments.digest(p) for p in sorted(bundle.rglob('*')) if p.is_file()}
    metadata=dict(bundle_id=job_id,created=time.time(),repositories=repositories,images=images,retained_images=retained_images,files=files,
        gpu_uuid=rendering['gpu_uuid'],renderer=rendering,
        source_workspace=str(manage.ROOT))
    write(bundle/'bundle.json',metadata)
    # Read-only payload files; scheduler logs and per-slot state live elsewhere.
    for path in bundle.rglob('*'):
        if path.is_file():path.chmod(path.stat().st_mode & ~0o222)
    return bundle


def submit(args):
    rows,parallelism,spec=expand(args)
    spec=dict(spec,_protocol=2)
    repetitions={}
    for row in rows:
        if args.stage=='final':row.update(stage='final',evidence='full')
        elif args.evidence=='full':row['evidence']='full'
        key=json.dumps({k:v for k,v in row.items() if k not in ('label','stage','evidence')},sort_keys=True)
        row['repeat_id']=repetitions.get(key,0)
        repetitions[key]=row['repeat_id']+1
        row['execution_profile']=('lockstep-'+row['timing_profile'] if row['execution_mode']=='lockstep' else
            'backend-240-v1' if row['max_fps']==240 else 'backend-custom-v1')
    if shutil.disk_usage(OPS).free<6*1024**3:
        raise RuntimeError('Need at least 6 GiB free before freezing another task')
    job_id='job_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'_'+uuid.uuid4().hex[:8]
    from input_contract import runtime,aligned
    if any(aligned(row) for row in rows) and not all(aligned(row) for row in rows):
        raise ValueError('Use separate jobs for legacy and aligned input contracts')
    with runtime(rows[0],manage):bundle=freeze(job_id,[r.get('gimbal_dynamics','legacy') for r in rows])
    with connect() as db,transaction(db):
        now=time.time()
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(job_id,'paused' if args.paused else 'queued',now,now,json.dumps(spec),str(bundle),parallelism,int(args.exploratory),None))
        db.executemany('INSERT INTO items(job_id,ordinal,state,settings) VALUES(?,?,?,?)',
            [(job_id,i,'pending',json.dumps(s)) for i,s in enumerate(rows)])
        event(db,job_id,f'Submitted {len(rows)} frozen experiment items')
    heartbeat=load(STATE/'daemon-status.json',{}).get('heartbeat',0)
    result=dict(job_id=job_id,items=len(rows),parallelism=parallelism or 'auto',bundle=str(bundle),scheduler_available=time.time()-heartbeat<5)
    print(json.dumps(result,ensure_ascii=False))
    return result


def get_job(db,job_id):
    job=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not job:raise ValueError('Unknown job: '+job_id)
    return job


def status(db,job_id):
    job=dict(get_job(db,job_id))
    job['spec']=json.loads(job['spec'])
    job['requested_parallelism']=job['parallelism'] or 'auto'
    job['items']={r['state']:r['n'] for r in db.execute('SELECT state,count(*) n FROM items WHERE job_id=? GROUP BY state',(job_id,))}
    job['attempts']=[dict(r) for r in db.execute('SELECT a.* FROM attempts a JOIN items i ON i.id=a.item_id WHERE i.job_id=? ORDER BY a.started',(job_id,))]
    job['artifacts']=[dict(r) for r in db.execute('SELECT * FROM artifacts WHERE job_id=?',(job_id,))]
    return job


def report(db,job_id):
    result=status(db,job_id)
    directory=OPS/'experiments'/job_id;directory.mkdir(parents=True,exist_ok=True)
    write(directory/'job.json',result)
    rows=[]
    for attempt in result['attempts']:
        run=attempt['run_id'];link=f'<a href="../{html.escape(run)}/report.html">{html.escape(run)}</a>' if run else '—'
        if attempt['state']!='running':
            logs=directory/'attempts'/attempt['id'];logs.mkdir(parents=True,exist_ok=True)
            for source in (STATE/'attempts'/attempt['id']).glob('*.log'):
                target=logs/source.name
                if not target.exists():os.link(source,target)
            if not run and (logs/'worker.log').exists():
                link='<a href="attempts/'+attempt['id']+'/worker.log">启动日志</a>'
        rows.append('<tr>'+''.join('<td>'+html.escape(str(attempt.get(k)))+'</td>' for k in ('slot','state','verdict'))+'<td>'+link+'</td></tr>')
    artifact_rows=''.join('<tr>'+''.join('<td>'+html.escape(str(a.get(k)))+'</td>' for k in ('run_id','state','requested','started','ended','error'))+'</tr>' for a in result['artifacts'])
    latencies=[]
    for a in result['artifacts']:
        if a['ended']:
            run=next((r for r in result['attempts'] if r['run_id']==a['run_id']),None)
            if run:latencies.append(a['ended']-run['started'])
    result['requested_artifact_latency_seconds']=latencies
    write(directory/'job.json',result)
    (directory/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>后台实验任务</title>'
        '<style>body{font:15px system-ui;margin:2em}td,th{padding:.5em;border:1px solid #aaa}table{border-collapse:collapse}</style>'
        '<h1>'+html.escape(job_id)+'</h1><p>'+html.escape(result['state'])+' '+html.escape(json.dumps(result['items']))+'</p>'
        '<p>任务执行状态与实验有效性分别记录；中断和无效尝试均保留。完成表示基础结果可用，媒体状态另列；自动通过不代表人工视觉通过。</p>'
        '<p>从实验启动到所请求媒体完成的延迟（秒）：'+html.escape(str(latencies))+'</p>'
        '<table><tr><th>工作槽</th><th>执行</th><th>实验判定</th><th>报告</th></tr>'+''.join(rows)+'</table>'
        '<h2>独立媒体队列</h2><table><tr><th>运行</th><th>状态</th><th>请求</th><th>开始</th><th>结束</th><th>错误</th></tr>'+artifact_rows+'</table>')
    import experiments
    experiments.publish_desktop(manage,directory)
    return directory/'report.html'


def cleanup_slot(slot):
    names=subprocess.run(['docker','ps','-aq','--filter','label=rm26.background=true',
        *(['--filter','label=rm26.slot='+str(slot)] if slot is not None else [])],capture_output=True,text=True,check=True).stdout.split()
    if not names:return
    inspected=json.loads(subprocess.check_output(['docker','inspect',*names]))
    owned=[]
    for info in inspected:
        label=info['Config'].get('Labels',{}).get('rm26.workspace')
        legacy_owned=label is None and any(Path(m['Source']).is_relative_to(STATE/'workers') for m in info.get('Mounts',[]))
        if label==str(manage.ROOT) or legacy_owned:owned.append(info['Id'])
    if owned:subprocess.run(['docker','rm','-f',*owned],check=False,stdout=subprocess.DEVNULL)


def interrupt_evidence(run_id,reason):
    if not run_id:return
    import experiments
    folder=OPS/'experiments'/run_id
    if not folder.is_dir():return
    try:experiments.record_failure(folder,reason)
    except (OSError,ValueError,KeyError) as error:
        # A torn last JSONL record must never trap the scheduler in a restart
        # loop. Retain it and the failure instead of rewriting raw evidence.
        write(folder/'recovery-error.json',dict(reason=reason,error=str(error)))
    evidence=load(folder/'evidence-sha256.json',{})
    for name in ('run-state.json','manifest.json'):
        if (folder/name).exists():evidence[name]=experiments.digest(folder/name)
    write(folder/'evidence-sha256.json',evidence)
    experiments.publish_desktop(manage,folder)


def reconcile(db):
    cleanup_slot(None)
    media_names=subprocess.run(['docker','ps','-aq','--filter','label=rm26.media=true',
        '--filter','label=rm26.workspace='+str(manage.ROOT)],capture_output=True,text=True,check=True).stdout.split()
    if media_names:subprocess.run(['docker','rm','-f',*media_names],check=False,stdout=subprocess.DEVNULL)
    db.execute("UPDATE artifacts SET state='queued',error='Interrupted; retrying offline' WHERE state='running'")
    with transaction(db):
        for attempt in db.execute("SELECT * FROM attempts WHERE state='running'").fetchall():
            result=load(STATE/'attempts'/attempt['id']/'result.json',{})
            run_id=result.get('run_id') or attempt['run_id']
            interrupt_evidence(run_id,'Scheduler stopped or host restarted; this attempt is interrupted')
            db.execute("UPDATE attempts SET state='interrupted',ended=?,run_id=?,verdict='invalid',error=? WHERE id=?",
                (time.time(),run_id,'scheduler restart',attempt['id']))
        db.execute("UPDATE items SET state='pending' WHERE state='running'")
        db.execute("UPDATE jobs SET state='paused',error='Scheduler restarted; use jobs resume',updated=? WHERE state IN ('running','queued','pausing')",(time.time(),))


def daemon():
    STATE.mkdir(parents=True,exist_ok=True)
    with (STATE/'daemon.lock').open('a') as lock, (OPS/'.runtime/pr-resource.lock').open('a') as resource_lease:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        db=connect();reconcile(db)
        active={};active_limits={};stopping=False;idle_cleaned=False
        media=None;health_checked=0;resources={'healthy':True}
        def stop(*_):
            nonlocal stopping
            stopping=True
        signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
        try:
            while not stopping:
                write(STATE/'daemon-status.json',dict(pid=os.getpid(),heartbeat=time.time(),slots=list(active),
                    resource_health=resources,media_run=media[1] if media else None))
                from background_pipeline import pump_media
                media=pump_media(db,media)
                for slot,entry in list(active.items()):
                    process,attempt_id,item_id,job_id,output=entry
                    job=get_job(db,job_id)
                    if job['state']=='cancelled' and process.poll() is None:
                        os.killpg(process.pid,signal.SIGTERM)
                        cleanup_slot(slot)
                        try:process.wait(timeout=8)
                        except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL)
                    if process.poll() is None:continue
                    output.close();result=load(STATE/'attempts'/attempt_id/'result.json',{})
                    valid=result.get('state')=='finished' and result.get('verdict')!='invalid' and process.returncode==0
                    cancelled=job['state']=='cancelled'
                    with transaction(db):
                        db.execute('UPDATE attempts SET state=?,ended=?,run_id=?,verdict=?,error=? WHERE id=?',
                            ('cancelled' if cancelled else 'finished' if process.returncode==0 else 'failed',time.time(),result.get('run_id'),result.get('verdict','invalid'),result.get('error'),attempt_id))
                        if valid:db.execute("UPDATE items SET state='completed' WHERE id=?",(item_id,))
                        elif cancelled:db.execute("UPDATE items SET state='cancelled' WHERE id=?",(item_id,))
                        else:
                            db.execute("UPDATE items SET state='pending',failures=failures+1 WHERE id=?",(item_id,))
                            failures=db.execute('SELECT failures FROM items WHERE id=?',(item_id,)).fetchone()[0]
                            pause_after_invalid(db,job_id,failures)
                        event(db,job_id,f'Attempt {attempt_id} ended: '+json.dumps(result))
                    if result.get('run_id') and load(OPS/'experiments'/result['run_id']/'manifest.json',{}).get('pipeline_version')==2:
                        from background_pipeline import enqueue_final,final_selection
                        enqueue_final(db,job_id,row_bundle=job['bundle'])
                        if result.get('state')=='finished':
                            from background_capacity import assess,signature,synchronous_signature
                            capacity=load(STATE/'capacity.json',{})
                            run_folder=OPS/'experiments'/result['run_id']
                            run_manifest=load(run_folder/'manifest.json');execution=run_manifest.get('execution',{})
                            key=(synchronous_signature(job['bundle'],execution['timing_profile'])
                                if execution.get('mode')=='lockstep' else signature(job['bundle']))
                            entry=capacity.get(key,{})
                            scene=run_manifest['scene']
                            check=assess(run_folder,entry.get('control_hz_by_scene',{}).get(scene,entry.get('control_hz')))
                            if not check['healthy']:
                                entry['runtime_ceiling']=max(1,active_limits.get(slot,1)//2)
                                entry['last_health_failure']=dict(run_id=result['run_id'],**check)
                                capacity[key]=entry;write(STATE/'capacity.json',capacity)
                                # A throttled but idle renderer still consumes
                                # GPU/CPU. Remove this failed environment so
                                # reducing dispatch also reduces actual load.
                                if not job['exploratory']:cleanup_slot(slot)
                    if not valid:
                        attempt_dir=STATE/'attempts'/attempt_id
                        for label in ('simulator','bridge','algorithm'):
                            component=subprocess.run(['docker','logs','--timestamps','rm26-worker-'+str(slot)+'-'+label],capture_output=True,text=True,timeout=5)
                            (attempt_dir/(label+'.log')).write_text(component.stdout+component.stderr)
                        cleanup_slot(slot)
                    del active[slot]
                    active_limits.pop(slot,None)
                    report(db,job_id)
                changed=[]
                with transaction(db):
                    for job in db.execute("SELECT * FROM jobs WHERE state IN ('queued','running','pausing')").fetchall():
                        running=db.execute("SELECT count(*) FROM items WHERE job_id=? AND state='running'",(job['id'],)).fetchone()[0]
                        pending=db.execute("SELECT count(*) FROM items WHERE job_id=? AND state='pending'",(job['id'],)).fetchone()[0]
                        if not running and (job['state']=='pausing' or not pending):
                            db.execute('UPDATE jobs SET state=?,updated=? WHERE id=?',('paused' if pending else 'completed',time.time(),job['id']))
                            changed.append(job['id'])
                for job_id in changed:report(db,job_id)
                qualification=load(STATE/'qualification.json',{})
                capacity=load(STATE/'capacity.json',{})
                from background_capacity import resource_health,limit as capacity_limit
                if time.monotonic()-health_checked>5:
                    try:resources=resource_health(STATE)
                    except (OSError,subprocess.TimeoutExpired):resources={'healthy':False}
                    health_checked=time.monotonic()
                for slot in range(1,MAX_SLOTS+1):
                    if slot in active:continue
                    if not resources['healthy']:continue
                    if shutil.disk_usage(STATE).free<6*1024**3:
                        db.execute("UPDATE jobs SET state='pausing',error='Less than 6 GiB free' WHERE state IN ('queued','running')")
                        continue
                    with transaction(db):
                        candidates=db.execute("SELECT i.*,j.bundle,j.parallelism,j.exploratory,j.spec FROM items i JOIN jobs j ON j.id=i.job_id WHERE i.state='pending' AND j.state IN ('queued','running') ORDER BY j.created,i.ordinal").fetchall()
                        chosen=None
                        for row in candidates:
                            from render_backend import frozen
                            renderer=frozen(load(Path(row['bundle'])/'bundle.json'))
                            if renderer['backend']=='nvidia' and not resources.get('nvidia_healthy',resources['healthy']):continue
                            limit=capacity_limit(row,capacity,qualification)
                            # A serial task reserves the machine; don't mix it with another job.
                            if len(active)>=limit:continue
                            if active and len(active)>=min(active_limits.values()):continue
                            chosen=row;break
                        if chosen is None:continue
                        try:fcntl.flock(resource_lease,fcntl.LOCK_SH|fcntl.LOCK_NB)
                        except BlockingIOError:continue
                        row=chosen;job_id=row['job_id'];item_id=row['id'];attempt_id='attempt_'+uuid.uuid4().hex[:12]
                        db.execute("UPDATE items SET state='running' WHERE id=?",(item_id,))
                        db.execute("UPDATE jobs SET state='running',updated=? WHERE id=?",(time.time(),job_id))
                        db.execute('INSERT INTO attempts(id,item_id,slot,state,started) VALUES(?,?,?,?,?)',(attempt_id,item_id,slot,'running',time.time()))
                    folder=STATE/'attempts'/attempt_id;folder.mkdir(parents=True)
                    request=dict(job_id=job_id,item_id=item_id,attempt=attempt_id,slot=slot,
                        settings=json.loads(row['settings']),bundle=row['bundle'],ops=str(OPS),desktop=str(manage.DESKTOP),
                        runtime=str(STATE/'workers'/str(slot)),result=str(folder/'result.json'),
                        parallelism=row['parallelism'] or 'auto',effective_parallelism=limit,exploratory=bool(row['exploratory']))
                    request['protocol']=json.loads(row['spec']).get('_protocol',1)
                    write(folder/'request.json',request)
                    output=(folder/'worker.log').open('a',buffering=1)
                    process=subprocess.Popen([sys.executable,'-B','-u',str(Path(row['bundle'])/'docker/auto_aim/background_runtime.py'),str(folder/'request.json')],
                        stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
                    active[slot]=(process,attempt_id,item_id,job_id,output)
                    active_limits[slot]=limit
                    idle_cleaned=False
                    event(db,job_id,f'Started {attempt_id} in slot {slot}')
                if not active:fcntl.flock(resource_lease,fcntl.LOCK_UN)
                if not active and not idle_cleaned:
                    cleanup_slot(None)
                    idle_cleaned=True
                time.sleep(.5)
        finally:
            for process,*_ in active.values():
                if process.poll() is None:os.killpg(process.pid,signal.SIGTERM)
            if media:
                process,_,output=media
                if process.poll() is None:os.killpg(process.pid,signal.SIGTERM)
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
                output.close()
            cleanup_slot(None)
            for process,*_ in active.values():
                try:process.wait(timeout=8)
                except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL)
            reconcile(db)
            for entry in active.values():entry[-1].close()
            (STATE/'daemon-status.json').unlink(missing_ok=True)
            db.close()


def install():
    unit=f'''[Unit]
Description=RM26 durable background experiment scheduler
Requires=docker.service
After=docker.service

[Service]
Type=simple
User={os.getuid()}
Group={os.getgid()}
SupplementaryGroups=docker
WorkingDirectory={manage.ROOT}
ExecStart=/usr/bin/python3 -u {OPS}/manage.py jobs daemon
ExecStopPost=/usr/bin/python3 {OPS}/manage.py jobs cleanup
Restart=on-failure
RestartSec=3
TimeoutStopSec=30
KillMode=mixed

[Install]
WantedBy=multi-user.target
'''
    # The host user already owns Docker. This short-lived helper only installs
    # this reviewed unit and asks the host systemd manager to load/start it.
    image=manage.inspect('daedalus:rm26-algorithm') or manage.inspect('sp_vision_dev:2024.6.0')
    if not image:raise RuntimeError('Build the independent algorithm environment before installing the service')
    common=['docker','run','--rm','--user','0:0','--network','host','--pid','host',
        '--volume','/etc/systemd/system:/host-units', '--volume','/run/systemd:/run/systemd',
        '--entrypoint','/usr/bin/python3',image['Id']]
    subprocess.run([*common,'-c','import pathlib,sys; root=pathlib.Path("/host-units"); (root/"rm26-experiments.service").write_text(sys.argv[1]); wants=root/"multi-user.target.wants"; wants.mkdir(exist_ok=True); link=wants/"rm26-experiments.service"; link.symlink_to("../rm26-experiments.service") if not link.exists() else None',unit],check=True)
    common[common.index('/usr/bin/python3')]='/bin/systemctl'
    for command in (['daemon-reload'],['restart',SERVICE]):subprocess.run([*common,*command],check=True)
    install_desktop()
    print('Installed '+SERVICE+'; restart recovery requires jobs resume JOB_ID')


def install_desktop():
    manage.install()
    script=manage.DESKTOP/'Desktop/项目脚本/实验任务.bash'
    script.write_text('#!/usr/bin/env bash\n"$HOME/.local/bin/rm26-sim" jobs list\nprintf "\\n查看任务：rm26-sim jobs status 任务ID\\n查看报告：rm26-sim jobs report 任务ID\\n"\nexec bash\n')
    script.chmod(0o755)
    desktop=manage.DESKTOP/'Desktop/实验任务.desktop'
    desktop.write_text('[Desktop Entry]\nType=Application\nName=实验任务\nTerminal=true\nExec=/home/openvino/Desktop/项目脚本/实验任务.bash\nIcon=utilities-terminal\n')
    desktop.chmod(0o755)
    info=manage.inspect(manage.DESKTOP_CONTAINER)
    if info and info['State']['Running'] and not info['State']['Paused']:
        discover='''from pathlib import Path
for p in Path('/proc').iterdir():
 try:
  if (p/'cmdline').read_bytes().split(b'\\0')[0]!=b'/usr/libexec/gnome-session-binary':continue
  for v in (p/'environ').read_bytes().split(b'\\0'):
   if v.startswith(b'DBUS_SESSION_BUS_ADDRESS='):print(v.split(b'=',1)[1].decode());raise SystemExit
 except (OSError,ValueError):pass
'''
        result=subprocess.run(['docker','exec','--user','1000:1000',manage.DESKTOP_CONTAINER,'python3','-c',discover],capture_output=True,text=True,timeout=5)
        if result.stdout.strip():
            subprocess.run(['docker','exec','--user','1000:1000','--env','DBUS_SESSION_BUS_ADDRESS='+result.stdout.strip(),
                manage.DESKTOP_CONTAINER,'gio','set','/home/openvino/Desktop/实验任务.desktop','metadata::trusted','true'],check=True,timeout=5)


def execute(args):
    if args.action=='experiment':return submit(args)
    action=args.jobs_action
    if action=='daemon':return daemon()
    if action=='cleanup':
        cleanup_slot(None)
        return
    if action=='benchmark':
        from background_benchmark import benchmark
        return benchmark(args)
    if action=='install':return install()
    if action=='service-status':
        subprocess.run(['systemctl','status',SERVICE,'--no-pager'],check=False)
        print(json.dumps(load(STATE/'daemon-status.json',{})));return
    with connect() as db:
        if action=='list':
            print('任务 ID\t状态\t请求并行数\t实验项进度\t说明')
            for row in db.execute('SELECT id,state,parallelism,error FROM jobs ORDER BY created DESC'):
                counts=dict((r['state'],r['n']) for r in db.execute('SELECT state,count(*) n FROM items WHERE job_id=? GROUP BY state',(row['id'],)))
                print(row['id']+'\t'+row['state']+'\t'+str(row['parallelism'] or 'auto')+'\t'+json.dumps(counts)+'\t'+str(row['error'] or ''))
            return
        job=get_job(db,args.job_id)
        if action in ('render','review'):
            from background_pipeline import command
            return command(db,args)
        if action=='status':print(json.dumps(status(db,args.job_id),ensure_ascii=False,indent=2));return
        if action=='report':print(report(db,args.job_id));return
        if action=='logs':
            if not 0<=args.tail<=10000:raise ValueError('tail must be 0..10000')
            last=max(0,db.execute('SELECT coalesce(max(id),0) FROM events WHERE job_id=?',(args.job_id,)).fetchone()[0]-args.tail)
            positions={}
            while True:
                for row in db.execute('SELECT * FROM events WHERE job_id=? AND id>? ORDER BY id',(args.job_id,last)):
                    print(datetime.fromtimestamp(row['created'],timezone.utc).isoformat(),row['message'],flush=True);last=row['id']
                for row in db.execute('SELECT a.id FROM attempts a JOIN items i ON i.id=a.item_id WHERE i.job_id=?',(args.job_id,)):
                    path=STATE/'attempts'/row['id']/'worker.log'
                    if path.exists():
                        with path.open() as source:
                            if str(path) not in positions:
                                lines=source.readlines();data=''.join(lines[-args.tail:]) if args.tail else ''
                            else:
                                source.seek(positions[str(path)]);data=source.read()
                            positions[str(path)]=source.tell()
                        if data:print(data,end='',flush=True)
                if not args.follow or get_job(db,args.job_id)['state'] in ('completed','cancelled'):break
                time.sleep(.5)
            return
        with transaction(db):
            pending_media=db.execute("SELECT count(*) FROM artifacts WHERE job_id=? AND state IN ('queued','running')",(args.job_id,)).fetchone()[0]
            if job['state'] in ('completed','cancelled') and not (action=='cancel' and pending_media):raise ValueError('Task is already terminal')
            if action=='pause':db.execute("UPDATE jobs SET state='pausing',updated=? WHERE id=?",(time.time(),args.job_id))
            elif action=='resume':
                if job['state'] not in ('paused','pausing'):raise ValueError('Task is not paused')
                db.execute("UPDATE jobs SET state='queued',error=NULL,updated=? WHERE id=?",(time.time(),args.job_id))
                db.execute("UPDATE items SET failures=0 WHERE job_id=? AND state='pending'",(args.job_id,))
            elif action=='cancel':
                db.execute("UPDATE jobs SET state='cancelled',updated=? WHERE id=?",(time.time(),args.job_id))
                db.execute("UPDATE items SET state='cancelled' WHERE job_id=? AND state='pending'",(args.job_id,))
                db.execute("UPDATE artifacts SET state='cancelled',ended=? WHERE job_id=? AND state='queued'",(time.time(),args.job_id))
            event(db,args.job_id,action)
        print(json.dumps(dict(job_id=args.job_id,state=get_job(db,args.job_id)['state'])))


if __name__=='__main__':
    execute(manage.parser().parse_args())
