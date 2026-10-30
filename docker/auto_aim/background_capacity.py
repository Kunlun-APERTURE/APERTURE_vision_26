"""Runtime capacity is independent of aiming quality and UI qualification."""
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess

PROFILE='backend-240-v1'


def signature(bundle):
    meta=json.loads((Path(bundle)/'bundle.json').read_text())
    files=meta['files']
    names=('vision_simulator_26/config.toml','docker/auto_aim/background_runtime.py',
        'docker/auto_aim/evaluator.py','docker/auto_aim/fastdds-udp.xml')
    value=dict(profile=PROFILE,gpu=meta['gpu_uuid'],images=meta['images'],
        files={n:files.get(n) for n in names})
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def resource_health(root):
    memory=next(int(l.split()[1]) for l in Path('/proc/meminfo').read_text().splitlines() if l.startswith('MemAvailable:'))*1024
    gpu=subprocess.run(['nvidia-smi','--query-gpu=memory.free','--format=csv,noheader,nounits'],
        capture_output=True,text=True,timeout=3)
    try:free_gpu=min(float(v) for v in gpu.stdout.splitlines())*1024**2
    except ValueError:free_gpu=0
    return dict(healthy=memory>=2*1024**3 and free_gpu>=1024**3 and shutil.disk_usage(root).free>=6*1024**3,
        memory_free_bytes=memory,gpu_free_bytes=free_gpu,disk_free_bytes=shutil.disk_usage(root).free)


def control_hz(folder):
    # Resets create separate monotonic segments. Never divide an entire log by
    # the last scenario clock value (the old diagnostic overcounted sessions).
    folder=Path(folder)
    state=json.loads((folder/'run-state.json').read_text())
    start,end=state.get('measurement_start_stamp_ns'),state.get('measurement_end_stamp_ns')
    if start is None or end is None:return None
    stamps=[]
    with (folder/'telemetry.jsonl').open() as stream:
        for line in stream:
            p=json.loads(line)
            if p['kind']=='gimbal_trace':
                d=p['data'];t=d.get('stamp_ns')
                if t is not None and start<=t<=end and 'control_steps' in d:stamps.append((t,d['control_steps']))
    steps=seconds=0
    for (ta,a),(tb,b) in zip(stamps,stamps[1:]):
        dt=(tb-ta)/1e9;change=b-a
        if 0<dt<=.1 and 0<change<=200:steps+=change;seconds+=dt
    return steps/seconds if seconds else None


def assess(folder,reference=None):
    folder=Path(folder)
    summary=json.loads((folder/'summary.json').read_text());metrics=summary['metrics']
    manifest=json.loads((folder/'manifest.json').read_text())
    lockstep=manifest.get('execution',{}).get('mode')=='lockstep'
    issues=[r['name'] for r in summary['rules'] if r['category']=='validity' and not r['passed']]
    for key,predicate in ([] if lockstep else [('processed_hz',lambda v:v>=20),('age_p95_ms',lambda v:v<=150)]):
        value=metrics.get(key)
        if not isinstance(value,(int,float)) or not math.isfinite(value) or not predicate(value):issues.append(key)
    rate=control_hz(folder)
    if rate is None:issues.append('control_frequency_missing')
    elif not lockstep and reference is not None and not .9*reference<=rate<=1.1*reference:issues.append('control_frequency')
    resources=[]
    if (folder/'resources.jsonl').exists():
        resources=[json.loads(l) for l in (folder/'resources.jsonl').open()]
    if not resources:issues.append('resource_samples_missing')
    for r in resources:
        if r.get('memory_available_kib',0)<2*1024**2:issues.append('memory_guard')
        if r.get('gpu_free_mib',0)<1024:issues.append('vram_guard')
        if r.get('disk_free_bytes',0)<6*1024**3:issues.append('disk_guard')
    return dict(healthy=not issues,issues=sorted(set(issues)),control_hz=rate)


def limit(job,capacity,legacy_qualification):
    spec=json.loads(job['spec'])
    if spec.get('_protocol')!=2:
        import background_jobs
        meta=json.loads((Path(job['bundle'])/'bundle.json').read_text())
        qualified=legacy_qualification.get('parallel_qualified') and legacy_qualification.get('runtime_signature')==background_jobs.stack_signature(meta)
        return min(2,job['parallelism'] or 1) if job['exploratory'] or qualified else 1
    requested=job['parallelism'] or MAX_LEVEL
    if job['exploratory']:return requested
    settings=json.loads(job['settings']) if 'settings' in job.keys() else {}
    if settings.get('detector_history')=='session':return 1
    if settings.get('execution_mode')=='lockstep':
        key=synchronous_signature(job['bundle'],settings['timing_profile'])
        entry=capacity.get(key,{})
        scope=entry.get('qualified_settings')
        if not scope or any(settings.get(name)!=value for name,value in scope.items()):return 1
        return min(requested,entry.get('healthy_parallelism',1),entry.get('runtime_ceiling',MAX_LEVEL))
    # Full-session recording is a distinct realtime IO workload. It has no
    # inherited capacity qualification from the ordinary measurement recorder.
    if settings.get('session_evidence')=='on':return 1
    if settings.get('execution_profile',PROFILE)!=PROFILE:return 1
    entry=capacity.get(signature(job['bundle']),{})
    return min(requested,entry.get('healthy_parallelism',1),entry.get('runtime_ceiling',MAX_LEVEL))


MAX_LEVEL=8


def synchronous_signature(bundle,profile):
    bundle=Path(bundle);meta=json.loads((bundle/'bundle.json').read_text())
    names=('bridge.py','sync_bridge.py','sync_timing.py','experiments.py','scenario_catalog.py',
        'highspeed_scenarios.py','focus_scenarios.py','shots.py','impact_quality.py','report.py','session_evidence.py')
    source={name:meta['files'].get('docker/auto_aim/'+name) for name in names}
    experiment={name:sha for name,sha in meta['files'].items() if name.startswith((
        'sp_vision_25/build/simulator/', 'sp_vision_25/configs/', 'sp_vision_25/assets/',
        'docker/auto_aim/scenarios/'))}
    timing=bundle/'docker/auto_aim/timing_profiles'/(profile+'.json')
    key=dict(runtime=signature(bundle),source=source,experiment=experiment,
        timing=hashlib.sha256(timing.read_bytes()).hexdigest())
    return 'lockstep:'+hashlib.sha256(json.dumps(key,sort_keys=True).encode()).hexdigest()
