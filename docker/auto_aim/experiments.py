"""Argument-only experiment orchestration, immutable evidence, review/baseline."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid
import statistics
import html

from report import load, write, finalize, render
from shots import baseline_key
from scenario_catalog import SCENES, EXTRA, HIGHSPEED, generated, semantics
from highspeed_scenarios import DURATIONS
from gimbal_dynamics import MODELS as GIMBAL_DYNAMICS, simulator_image


def add_parser(commands):
    from benchmark.scenarios import REGISTRY
    global SCENES
    SCENES=tuple(dict.fromkeys((*SCENES,*REGISTRY)))
    root = commands.add_parser('experiment',help='Record, score and review a complete experiment')
    sub = root.add_subparsers(dest='experiment_action',required=True)
    run = sub.add_parser('run')
    run.add_argument('--scene',choices=list(SCENES)+['advice-motion','ego-all','stability-all','all'],default='all')
    run.add_argument('--repeat',type=int)
    run.add_argument('--terrain',choices=['arena','flat_range'],default='arena')
    run.add_argument('--mode',choices=['track','shoot'],default='track')
    run.add_argument('--matrix',choices=['ballistic-v1','hit-v2','hit-v3','gimbal-ab','single','highspeed-v1'])
    run.add_argument('--resume',help='Reuse valid matching runs from an interrupted batch id')
    run.add_argument('--bullet-speed',type=float,default=25)
    run.add_argument('--reported-bullet-speed',type=float)
    run.add_argument('--physics',choices=['no-drag','drag'],default='no-drag')
    run.add_argument('--label',default='')
    run.add_argument('--fire-policy',choices=['advice','continuous'],default='advice')
    run.add_argument('--fire-rate-hz',type=float,default=5.0)
    run.add_argument('--aim-origin',choices=['muzzle','gimbal'],default='muzzle')
    run.add_argument('--algorithm-profile',choices=['legacy','tracking_v2','tracking_fire_v2','tracking_v3','tracking_fire_v3','tracking_fire_v4m','tracking_fire_v4mt','tracking_fire_v4mtr','image_v1'],default='legacy')
    run.add_argument('--gimbal-dynamics',choices=GIMBAL_DYNAMICS,default='legacy')
    run.add_argument('--gimbal-profile',choices=['legacy','timing','feedforward','combined'],default='legacy')
    run.add_argument('--seed',type=int,default=20261019)
    run.add_argument('--input-contract',choices=['legacy','aligned-v1'],default='legacy')
    run.add_argument('--sensor-seed',type=int,default=None)
    run.add_argument('--warmup-frames',type=int,default=30)
    run.add_argument('--command-script',default='')
    run.add_argument('--projectile-visible',choices=['on','off'],default='on')
    run.add_argument('--duration',type=float,help='Measurement seconds, 1..18; defaults static/spin/move_ref=6, ego=8, line=18')
    run.add_argument('--max-fps',type=float,help='Shared UI/background main-loop cap; 0 uncapped, omitted preserves config')
    run.add_argument('--algorithm-lifecycle',choices=['session','fresh'],default='session',
        help='Reuse the detector process across scene session resets (UI default), or restart each experiment')
    run.add_argument('--detector-history',choices=['process','session'],default='process',
        help='Keep detector search/cache for the process lifetime, or reset it at each fresh input session')
    run.add_argument('--stage',choices=['screen','final'],default='screen')
    run.add_argument('--evidence',choices=['auto','full'],default='auto')
    run.add_argument('--session-evidence',choices=['auto','on','off'],default='auto',
        help='Archive every fresh-session input/result, including warmup; automatic and required for lockstep')
    run.add_argument('--execution-mode',choices=['realtime','lockstep'],default='realtime')
    run.add_argument('--timing-profile',default=None,help='Frozen timing profile name, required by lockstep')
    run.add_argument('--record-timing',choices=['on','off'],default='off')
    run.add_argument('--preview',choices=['on','off'],default='off',help='Read-only desktop viewer for lockstep input')
    submit = sub.add_parser('submit', parents=[run], add_help=False,
        help='Freeze and enqueue experiments independently of the desktop')
    submit.add_argument('--spec', help='Version 1 JSON matrix specification')
    submit.add_argument('--parallelism', choices=['auto']+[str(n) for n in range(1,9)], default=None)
    submit.add_argument('--paused', action='store_true')
    submit.add_argument('--exploratory', action='store_true', help='Allow unqualified parallel execution; recorded in evidence')
    from benchmark.cli import add_parser as add_algorithm_parser
    add_algorithm_parser(sub)
    suite=sub.add_parser('suite',help='Independent basic/advanced benchmark')
    suite.add_argument('--tier',choices=('basic','advanced'),default='basic')
    suite.add_argument('--cases');suite.add_argument('--pressure',action='store_true')
    suite.add_argument('--list',action='store_true');suite.add_argument('--output',type=Path)
    suite.add_argument('--algorithm',default='B1');suite.add_argument('--environment',choices=('E1','legacy'),default='E1')
    suite.add_argument('--bundle',type=Path);suite.add_argument('--artifact',type=Path);suite.add_argument('--sensor-seeds',default='8101,8102,8103')
    suite.add_argument('--trajectory-seed',type=int,default=2026102207);suite.add_argument('--repeats',type=int,default=1)
    suite.add_argument('--parallelism',choices=('1','2'),default='1')
    suite.add_argument('--resume',action='store_true');suite.add_argument('--wait',action='store_true')
    sub.add_parser('list')
    compare = sub.add_parser('compare',help='Compare paired frozen background tasks')
    compare.add_argument('--baseline',required=True)
    compare.add_argument('--candidate',required=True)
    compare.add_argument('--extend',action='store_true',help='Queue missing paired repeats from the original frozen bundles, up to 10 pairs')
    study=sub.add_parser('lockstep-study',help='Calibrate and qualify both synchronous timing candidates against frozen original UI')
    study.add_argument('--output',default='lockstep-study-20261022')
    study.add_argument('--phase',choices=['calibrate','evaluate','all'],default='all')
    study.add_argument('--repeats',type=int,default=5)
    study.add_argument('--max-repeats',type=int,default=30)
    study.add_argument('--levels',default='1,2,4,8')
    study.add_argument('--resume',action='store_true')
    study.add_argument('--new-candidate',action='store_true',help='Archive earlier candidate results and freeze a new version; original UI evidence is retained')
    report = sub.add_parser('report')
    report.add_argument('run_id')
    review = sub.add_parser('review')
    review.add_argument('run_id')
    review.add_argument('--decision',choices=['pass','fail'],required=True)
    review.add_argument('--note',required=True)
    baseline = sub.add_parser('baseline')
    baseline.add_argument('run_id')


def measurement_duration(scene, args):
    if scene in HIGHSPEED:return args.duration if args.duration is not None else DURATIONS[scene]
    from benchmark.scenarios import REGISTRY,RegisteredScenario
    if scene in REGISTRY and not isinstance(REGISTRY[scene],RegisteredScenario):
        plan=REGISTRY[scene].generate(getattr(args,'seed',20261019))
        return plan.metadata['duration_s']
    return args.duration if args.duration is not None else (6.0 if scene.startswith('distance_') else 18.0 if scene in EXTRA else 18.0 if scene in ('ego_sweep','ego_sweep_reverse') else 8.0 if scene.startswith('ego_') else 18.0 if scene=='line' else 6.0)


def short_scenario(manager, scene, duration, seed=20261019):
    """Render only complete/trimmed segments; original 60 s scenario stays intact."""
    if getattr(manager,'metadata',{}).get('benchmark_version'):
        from benchmark.scenarios import get
        plan=get(scene).generate(seed)
        expected=plan.metadata.get('duration_s',plan.metadata.get('duration'))
        if expected is not None and abs(expected-duration)>1e-6:raise ValueError('Benchmark scene duration differs from frozen plan')
        destination=manager.RUNTIME/'scenarios'/f'{scene}.toml';destination.parent.mkdir(exist_ok=True)
        destination.write_text(plan.script);write(destination.with_suffix('.json'),plan.metadata)
        return destination
    if scene in EXTRA or scene in HIGHSPEED:
        text,plan=generated(scene,seed,duration)
        destination=manager.RUNTIME/'scenarios'/f'{scene}.toml';destination.parent.mkdir(exist_ok=True)
        destination.write_text(text);write(destination.with_suffix('.json'),plan);return destination
    text = (manager.OPS/'scenarios'/f'{scene}.toml').read_text()
    chunks = text.split('[[robot]]')
    header = re.sub(r'(?m)^duration\s*=.*$',f'duration = {duration}',chunks[0])
    result = header
    for robot in chunks[1:]:
        parts = robot.split('[[robot.segment]]')
        result += '[[robot]]'+parts[0]
        for segment in parts[1:]:
            t0 = float(re.search(r'(?m)^t0\s*=\s*([0-9.]+)',segment).group(1))
            if t0 >= duration:
                continue
            t1 = float(re.search(r'(?m)^t1\s*=\s*([0-9.]+)',segment).group(1))
            segment = re.sub(r'(?m)^t1\s*=.*$',f't1 = {min(t1,duration)}',segment)
            result += '[[robot.segment]]'+segment
    destination = manager.RUNTIME/'scenarios'/f'{scene}.toml'
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(result)
    return destination


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):
            checksum.update(block)
    return checksum.hexdigest()


def repository(manager, name, destination):
    if getattr(manager, 'FROZEN_REPOSITORIES', None) is not None:
        for suffix in ('.patch', '-sources.json'):
            source = manager.ROOT/'provenance'/(name+suffix)
            shutil.copy2(source, destination/source.name)
        return manager.FROZEN_REPOSITORIES[name]
    root = manager.ROOT/name
    head = manager.run(['git','-C',str(root),'rev-parse','HEAD'],capture=True)
    status = manager.run(['git','-C',str(root),'status','--short'],capture=True)
    patch = subprocess.check_output(['git','-C',str(root),'diff','--binary','HEAD'])
    (destination/(name+'.patch')).write_bytes(patch)
    tracked = subprocess.check_output(['git','-C',str(root),'ls-files','-z','--cached','--others','--exclude-standard'])
    selected = {}
    for relative in tracked.decode().split('\0'):
        path = root/relative
        if (not relative or not path.is_file() or any(part in ('target','build','.git') for part in path.parts)
                or path.suffix not in ('.rs','.cpp','.hpp','.py','.sh','.toml','.yaml','.yml','.msg','.srv','.txt','.lock')):
            continue
        selected[relative] = digest(path)
    write(destination/(name+'-sources.json'),selected)
    return {'head':head,'status':status,'tracked_diff_sha256':hashlib.sha256(patch).hexdigest(),
        'source_files_sha256':hashlib.sha256(json.dumps(selected,sort_keys=True).encode()).hexdigest()}


def snapshot(manager, folder, scene, args):
    if hasattr(manager, 'snapshot'):
        return manager.snapshot(folder, scene, args)
    configs = folder/'configuration'
    configs.mkdir()
    sources = configs/'ops'
    sources.mkdir()
    for source in manager.OPS.iterdir():
        if source.is_file() and source.suffix in ('.py','.yml'):
            shutil.copy2(source,sources/source.name)
    for source,target in [
        (manager.RUNTIME/'config.toml','simulator.toml'),
        (manager.ROOT/'sp_vision_25/configs/simulator.yaml','algorithm.yaml'),
        (manager.RUNTIME/'scenarios'/f'{scene}.toml','scenario.toml'),
        (manager.OPS/'compose.yml','compose.yml')]:
        shutil.copy2(source,configs/target)
    if manager.profile().get('command_script'):shutil.copy2(manager.RUNTIME/'command-script.json',configs/'command-script.json')
    plan=manager.RUNTIME/'scenarios'/f'{scene}.json'
    if (scene in EXTRA or scene in HIGHSPEED) and plan.exists():shutil.copy2(plan,configs/'scenario-plan.json')
    weights = {}
    # All selectable weights are fingerprinted; no ambiguity if profile changes
    # from YOLOv5 to another supported network in a later experiment.
    assets = manager.ROOT/'sp_vision_25/assets'
    for pattern in ('*.xml','*.bin','*.onnx'):
        for path in assets.glob(pattern):
            weights[str(path.relative_to(assets))] = digest(path)
    simulator = manager.inspect(manager.SIMULATOR)
    bridge = manager.inspect(manager.BRIDGE)
    integration_image = simulator['Image']
    settings = manager.profile()
    settings.setdefault('gimbal_dynamics',getattr(args,'gimbal_dynamics','legacy'))
    shooting = {k:settings[k] for k in ('bullet_speed','reported_bullet_speed','physics','aim_origin','gimbal_profile','gimbal_dynamics','fire_policy','algorithm_profile')} if args.mode=='shoot' else None
    if shooting:
        shooting.update(fire_rate_limit_hz=settings['fire_rate_hz'],settlement_s=6,projectile_lifetime_s=5,
            hit_method='swept_actual_segment_v3')
        if semantics(scene)['ego_motion']!='static':
            shooting['launch_speed_frame'] = 'shooter_rest_from_recorded_gt'
    calibration = manager.ROOT/'sp_vision_25/build/sim/calibration.yaml'
    if calibration.exists():
        shutil.copy2(calibration,configs/'runtime-calibration.yaml')
    manifest = {'mode':args.mode,'shooting':shooting,'schema_version':1,'run_id':folder.name,'scene':scene,'duration_s':measurement_duration(scene,args),'wall_budget_s':40,
        'execution':{'max_fps':settings.get('max_fps'),
            'algorithm_lifecycle':getattr(args,'algorithm_lifecycle','session'),
            'detector_history':settings.get('detector_history','process')},
        'input_contract':settings.get('input_contract','legacy'),
        'sensor_seed':settings.get('sensor_seed'),
        'warmup_frames':settings.get('warmup_frames',30),
        'algorithm_binary_sha256':digest(manager.ROOT/'sp_vision_25/build/simulator/sim_auto_aim'),
        'algorithm_profile':settings['algorithm_profile'],'label':args.label,'gimbal_dynamics':settings['gimbal_dynamics'],'gimbal_profile':settings['gimbal_profile'],'created_utc':datetime.now(timezone.utc).isoformat(),
        'ros_domain_id':0,'random_seed':getattr(args,'seed',20261019),'scenario_semantics':semantics(scene),'projectile_visible':settings.get('projectile_visible','on'),'control_source':'fixed_script' if settings.get('command_script') else 'algorithm','command_script_sha256':digest(manager.RUNTIME/'command-script.json') if settings.get('command_script') else None,'algorithm_weights_sha256':weights,
        'configuration_sha256':{str(p.relative_to(configs)):digest(p) for p in configs.rglob('*') if p.is_file()},
        'repositories':{name:repository(manager,name,folder)
            for name in ('sp_vision_25','vision_simulator_26')},
        'containers':{'simulator_image_id':integration_image,'bridge_image_id':bridge['Image'],
            'evaluator_image_id':manager.inspect('daedalus:rm26-evaluator')['Id']},
        'source_files':{p.name:digest(p) for p in manager.OPS.iterdir() if p.suffix in ('.py','.yml')},
        'terrain':settings.get('terrain','arena'),'impact_quality_version':1 if settings.get('terrain')=='flat_range' else None,
        'terrain_geometry':{'size_m':[120,120],'surface_z_m':0,'friction':.5,'material':'neutral_gray_rough'} if settings.get('terrain')=='flat_range' else None,
        'rules_version':4,'warmup_excluded':True,'recording':'all processed input frames and annotations',
        'visibility_model':'physics centre ray + front face + full plate in FOV; convex self occlusion approximation',
        'acceptance_baseline':None}
    if getattr(manager,'PIPELINE_VERSION',1)==2:
        manifest.update(pipeline_version=2,scoring_version=2,stage=getattr(args,'stage','screen'),
            evidence='full' if getattr(args,'stage','screen')=='final' else getattr(args,'evidence','auto'),
            repeat_id=getattr(args,'repeat_id',0),impact_quality_version=1,
            recording='Original JPEG + telemetry + numerical metrics; media generated offline')
        manifest['execution']['profile']=getattr(args,'execution_profile','backend-240-v1')
    from gimbal_dynamics import description
    manifest['gimbal_reference']=description(settings.get('gimbal_dynamics','legacy'))
    manifest['execution']['mode']=getattr(args,'execution_mode','realtime')
    manifest['execution']['preview']=getattr(args,'preview','off')
    if manifest['execution']['mode']=='lockstep':
        source=manager.OPS/'timing_profiles'/(args.timing_profile+'.json')
        shutil.copy2(source,configs/'timing-profile.json')
        manifest.update(wall_budget_s=180)
        manifest['execution'].update(timing_profile=args.timing_profile,timing_sha256=digest(source),
            initialization='Fresh environment, fixed simulated settling/homing and warmup frame count',
            clock_domain='simulation',real_compute_cost_separate=True)
        manifest['configuration_sha256']['timing-profile.json']=digest(source)
    manifest['baseline_key'] = baseline_key(manifest)
    baseline = load(manager.OPS/'baselines'/(manifest['baseline_key']+'.json'))
    if baseline:
        expected = baseline.get('scenario_sha256')
        actual = manifest['configuration_sha256']['scenario.toml']
        if expected != actual or (args.mode=='shoot' and baseline.get('simulator_sha256') != manifest['configuration_sha256']['simulator.toml']):
            manifest['baseline_warning'] = 'Scenario differs from selected baseline; effect gate requires a new reviewed baseline'
        else:
            manifest['acceptance_baseline'] = baseline
    write(folder/'manifest.json',manifest)
    return manifest


def checked_folder(manager, name):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,240}',name):
        raise RuntimeError('Invalid experiment id')
    folder = manager.OPS/'experiments'/name
    if not folder.is_dir():
        raise RuntimeError('Experiment directory does not exist')
    return folder


def record_failure(folder, reason):
    state = load(folder/'run-state.json',{})
    state.setdefault('errors',[]).append(reason)
    state['complete'] = False
    write(folder/'run-state.json',state)
    finalize(folder)


def publish_desktop(manager, folder):
    if not manager.DESKTOP.exists():return
    # Hardlinks expose the same immutable evidence through the desktop's
    # persistent HOME, with no second copy of large videos.
    target = manager.DESKTOP/'.local/share/rm26-sim/experiments'/folder.name
    for source in folder.rglob('*'):
        destination = target/source.relative_to(folder)
        if source.is_dir():
            destination.mkdir(parents=True,exist_ok=True)
        elif source.is_file():
            destination.parent.mkdir(parents=True,exist_ok=True)
            if destination.exists() and os.path.samefile(source,destination):
                continue
            destination.unlink(missing_ok=True)
            os.link(source,destination)
    print('Desktop report: /home/openvino/.local/share/rm26-sim/experiments/'+folder.name+'/report.html')


def batch_report(path, batch):
    columns = ('processed_hz','age_p95_ms','correct_tracking_fraction','center_xy_error_p95_m','longest_loss_s')
    rows = []
    shooting = any(r.get('mode')=='shoot' for r in batch)
    if shooting:
        columns = ('shots_fired','target_armor_hits','target_hit_rate','first_hit_s','flight_time_p50_s')
    groups = {}
    for entry in batch:
        condition = entry.get('shooting') or {}
        name = entry['scene']
        if shooting:
            name += f" / {condition.get('gimbal_profile','legacy')} / {condition.get('aim_origin','legacy')} / {condition.get('physics')} / {condition.get('bullet_speed')}:{condition.get('reported_bullet_speed')} m/s"
        groups.setdefault(name,[]).append(entry)
    for scene,relevant in groups.items():
        if not relevant:
            continue
        valid = [r for r in relevant if r['verdict']!='invalid']
        cells = [scene,f'{len(valid)}/{len(relevant)}',', '.join(r['verdict'] for r in relevant)]
        for key in columns:
            samples = [r['metrics'][key] for r in valid if r.get('metrics') and r['metrics'].get(key) is not None]
            cells.append(f'{statistics.median(samples):.4f} [{min(samples):.4f}, {max(samples):.4f}]' if samples else 'n/a')
        rows.append(cells)
    headings = ['Scene','Valid/total runs','Verdicts',*[key+' median [min,max]' for key in columns]]
    table = '<tr>'+''.join('<th>'+html.escape(c)+'</th>' for c in headings)+'</tr>'
    table += ''.join('<tr>'+''.join('<td>'+html.escape(c)+'</td>' for c in row)+'</tr>' for row in rows)
    links = ''.join('<li>'+html.escape(str(r.get('shooting') or ''))+' '+html.escape(str({k:(r.get('metrics') or {}).get(k) for k in columns}))+' <a href="'+html.escape(r['run_id'])+'/report.html">'+html.escape(r['run_id'])+'</a> — '+r['verdict']+'</li>' for r in batch)
    path.with_suffix('.html').write_text('<!doctype html><meta charset="utf-8"><title>Experiment batch</title>'
        '<style>body{font:15px system-ui;margin:2em}table{border-collapse:collapse}td,th{border:1px solid #aaa;padding:.5em}</style>'
        '<h1>实验批次</h1><p>有效性、性能和效果分别判定；仅有一轮时不能估计重复性。</p><table>'+table+'</table><ul>'+links+'</ul>')


def run_one(manager, scene, args):
    from input_contract import aligned,validate as validate_contract
    validate_contract(vars(args))
    counted_warmup=aligned(vars(args))
    start_mono = getattr(manager,'attempt_started',time.monotonic()) if getattr(args,'execution_mode','realtime')=='lockstep' else time.monotonic()
    deferred=getattr(manager,'PIPELINE_VERSION',1)==2
    lockstep=getattr(args,'execution_mode','realtime')=='lockstep'
    session_option=getattr(args,'session_evidence','auto')
    if lockstep and session_option=='off':raise ValueError('Lockstep requires complete session evidence')
    reset_detector=getattr(args,'detector_history','process')=='session'
    if reset_detector and session_option=='off':raise ValueError('Session detector history requires complete session evidence')
    session_evidence=lockstep or reset_detector or session_option in (True,'on')
    budget=180 if lockstep else 40
    operation_deadline = start_mono+budget-4
    duration = measurement_duration(scene,args)
    short_scenario(manager,scene,duration,getattr(args,'seed',20261019))
    tag = ('_shoot_'+manager.profile()['aim_origin']+'_'+manager.profile()['physics']+'_'+str(manager.profile()['bullet_speed'])+'_'+str(manager.profile()['reported_bullet_speed'])).replace('.','p') if args.mode=='shoot' else ''
    if manager.profile()['gimbal_profile'] != 'legacy':
        tag += '_'+manager.profile()['gimbal_profile']
    if manager.profile().get('fire_policy') == 'continuous':
        tag += '_continuous'
    if manager.profile().get('fire_rate_hz',2) != 2:
        tag += '_'+str(manager.profile()['fire_rate_hz']).replace('.','p')+'hz'
    if manager.profile().get('algorithm_profile','legacy') != 'legacy':
        tag += '_'+manager.profile()['algorithm_profile']
    identifier = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'_'+scene+tag+'_'+uuid.uuid4().hex[:8]
    folder = manager.OPS/'experiments'/identifier
    folder.mkdir(parents=True)
    if hasattr(manager, 'on_run_created'):
        manager.on_run_created(folder)
    write(folder/'manifest.json',{'run_id':identifier,'scene':scene,'mode':args.mode,'duration_s':measurement_duration(scene,args),'wall_budget_s':budget,'pipeline_version':2 if deferred else 1,'created_utc':datetime.now(timezone.utc).isoformat()})
    container = 'rm26-eval-'+uuid.uuid4().hex[:12]
    capture_started = None
    component_ids = {}
    print('Experiment: '+identifier,flush=True)
    try:
        manager.rpc('prepare',name=scene,control='on',duration=duration,seed=getattr(args,'seed',20261019),
            record_session=session_evidence,input_contract=getattr(args,'input_contract','legacy'),
            warmup_frames=getattr(args,'warmup_frames',30))
        deadline = min(time.monotonic()+5,operation_deadline)
        while True:
            health = manager.rpc('status')
            result = health.get('last_result') or {}
            if counted_warmup and health.get('connected'):break
            if (health.get('connected') and health.get('frame_age_ms') is not None
                    and health['frame_age_ms'] < 250 and result.get('diagnostics_version',0) >= 1):
                break
            if time.monotonic()>deadline:
                raise RuntimeError('Fresh algorithm diagnostics were not ready after preparation')
            time.sleep(.2)
        # This confirms input readiness, not successful target detection. A
        # detector failure must remain a measured failure, not prevent the run.
        manifest = snapshot(manager,folder,scene,args)
        if session_evidence:
            manifest['session_evidence']=dict(version=1,required=True,archive='session-evidence',
                scope='All inputs and results since the final fresh session reset, including unscored warmup')
            write(folder/'manifest.json',manifest)
        for name in (manager.SIMULATOR,manager.BRIDGE,manager.DESKTOP_CONTAINER):
            info = manager.inspect(name)
            component_ids[name] = (info['Id'],info['State']['StartedAt'])
        if shutil.disk_usage(folder).free < 3*1024**3:
            raise RuntimeError('Need at least 3 GiB free for a complete evidence recording')
        manager.compose('--profile','experiment','run','--detach','--no-deps','--name',container,
            *(['--label','aperture.pr='+os.environ['RM26_PR_TASK']] if os.environ.get('RM26_PR_TASK') else []),
            'evaluator','python3','-u','/opt/rm26/evaluator.py','--output','/experiments/'+identifier,
            '--scene',scene,'--duration',str(duration),'--mode',args.mode,'--settlement','6',
            '--wall-budget',str(max(1,operation_deadline-time.monotonic())),
            '--input-contract',getattr(args,'input_contract','legacy'),
            *(['--defer-media'] if deferred else []))
        capture_started = time.monotonic()
        if lockstep:
            # Subscriptions must exist before the deterministic priming ticks.
            deadline=min(time.monotonic()+5,operation_deadline)
            while not load(folder/'recorder-status.json',{}).get('stream_ready'):
                if time.monotonic()>deadline:raise RuntimeError('Observer failed to subscribe')
                time.sleep(.05)
            manager.rpc('prime')
        deadline = min(time.monotonic()+5,operation_deadline)
        while True:
            recorder = load(folder/'recorder-status.json',{})
            if recorder.get('stream_ready') and recorder.get('truth_ready'):
                break
            if lockstep:manager.rpc('prime')
            info = manager.inspect(container)
            if not info or not info['State']['Running']:
                raise RuntimeError('Recorder exited before it was ready')
            if time.monotonic()>deadline:
                raise RuntimeError('Observer stream was not ready')
            time.sleep(.2)
        if counted_warmup:
            write(folder/'warmup.json',manager.rpc('warm'))
        if getattr(args,'diagnosis_reset',False):
            reset=manager.rpc('diagnosis_reset')
            write(folder/'diagnosis-reset.json',{'reset_status':reset,'requested_ns':time.time_ns(),
                'purpose':'Recorder is already ready; include the full fresh algorithm session before measurement.'})
            time.sleep(1.)
        manager.rpc('begin',fire=args.mode=='shoot')
        write(folder/'begin.json',{'requested_utc':datetime.now(timezone.utc).isoformat(),
            'bridge_status':manager.rpc('status')})
        deadline = operation_deadline
        while True:
            info = manager.inspect(container)
            if not info or not info['State']['Running']:
                if info and info['State']['ExitCode']:
                    raise RuntimeError('Recorder process failed; see evaluator.log')
                break
            for name,identity in component_ids.items():
                component = manager.inspect(name)
                if (not component or not component['State']['Running'] or
                    (component['Id'],component['State']['StartedAt']) != identity):
                    raise RuntimeError('Environment stopped or restarted during experiment: '+name)
            if time.monotonic()>deadline:
                raise RuntimeError('Experiment or report finalization exceeded deadline')
            time.sleep(.5)
        if not (folder/'summary.json').is_file():
            raise RuntimeError('Recorder did not produce an experiment report')
    except BaseException as error:
        info = manager.inspect(container)
        if info and info['State']['Running']:
            manager.run(['docker','stop','--timeout','1',container],check=False)
        record_failure(folder,type(error).__name__+': '+str(error))
        if isinstance(error,KeyboardInterrupt):
            raise
        print('Experiment invalid: '+str(error),flush=True)
    finally:
        try:
            manager.rpc('control',value='off')
            saved = manager.profile()
            saved.update(scene=scene,control='off')
            manager.save_profile(saved)
        except (OSError,RuntimeError):
            pass
        session_audit=None
        if session_evidence:
            try:
                manager.rpc('seal_session')
                shutil.copytree(manager.RUNTIME/'session-evidence',folder/'session-evidence',dirs_exist_ok=True)
                from session_evidence import audit
                run_state=load(folder/'run-state.json',{})
                session_audit=audit(folder/'session-evidence',run_state.get('measurement_start_stamp_ns'),
                    run_state.get('measurement_end_stamp_ns'),folder)
                first=load(folder/'begin.json',{}).get('bridge_status',{}).get('last_result') or {}
                if first.get('session')!=session_audit['session']:
                    session_audit['errors'].append('Archived session differs from measurement session')
                    session_audit['passed']=False
                if reset_detector:
                    initial={}
                    with (folder/'session-evidence/events.jsonl').open() as events:
                        for line in events:
                            packet=json.loads(line)
                            if packet['kind']=='result':initial=packet['data']['result'];break
                    reset_ok=initial.get('detector_history')=='session' and initial.get('detector_reset') is True
                    session_audit['detector_session_reset_verified']=reset_ok
                    if not reset_ok:
                        session_audit['errors'].append('Detector did not acknowledge fresh-session cache reset')
                        session_audit['passed']=False
            except (OSError,RuntimeError,ValueError,KeyError) as error:
                session_audit=dict(passed=False,errors=[type(error).__name__+': '+str(error)])
            write(folder/'session-evidence-audit.json',session_audit)
        for name,label in [(container,'evaluator'),(manager.SIMULATOR,'simulator'),(manager.BRIDGE,'bridge')]:
            if manager.inspect(name):
                logs = subprocess.run(['docker','logs','--timestamps',name],capture_output=True,text=True)
                (folder/(label+'.log')).write_text(logs.stdout+logs.stderr)
        control_timing=manager.RUNTIME/'control-timing.jsonl'
        if control_timing.exists():shutil.copy2(control_timing,folder/'control-timing.jsonl')
        if lockstep:write(folder/'synchronization.json',manager.rpc('status').get('synchronization'))
        if deferred:
            from background_pipeline import compact
            if (folder/'input.mjpg').exists():compact(folder)
            write(folder/'media-status.json',{'state':'not_requested','basic_results_available':True})
        evidence = {}
        for name in ('input.mjpg','input.avi','annotated.avi','annotated.mp4','frames.jsonl','telemetry.jsonl',
                'manifest.json','run-state.json','shots.jsonl','shots.csv','motion-analysis.json','impact-quality.png','impact-quality.svg','motion.png','motion.svg','quality-shots.jsonl','quality-shots.csv',
                'retained.mjpg','retained-index.json','evidence-policy.json','control-timing.jsonl','synchronization.json'):
            artifact = folder/name
            if artifact.exists():
                evidence[name] = digest(artifact)
        if session_evidence:
            for artifact in sorted((folder/'session-evidence').glob('*')):
                if artifact.is_file():evidence[str(artifact.relative_to(folder))]=digest(artifact)
            evidence['session-evidence-audit.json']=digest(folder/'session-evidence-audit.json')
        write(folder/'evidence-sha256.json',evidence)
        if manager.inspect(container):
            manager.run(['docker','rm',container],check=False)
        summary = load(folder/'summary.json',{})
        if session_audit is not None:
            summary.setdefault('rules',[]).append(dict(name='complete_session_evidence',category='validity',
                passed=session_audit['passed'],actual=session_audit,expected='Complete warmup and measurement input/result history'))
            if not session_audit['passed']:summary['verdict']='invalid'
        if not deferred:publish_desktop(manager,folder)
        elapsed = time.monotonic()-start_mono
        write(folder/'timing.json',{'wall_seconds':elapsed,'budget_seconds':budget,'within_budget':elapsed<=budget,
            'measurement_seconds':duration,'settlement_seconds':load(folder/'run-state.json',{}).get('settlement_wall_s',6),
            'completion_boundary':'basic_results' if deferred else 'all_artifacts',
            'media_seconds':None if deferred else 0})
        summary.setdefault('metrics',{})['total_wall_s'] = elapsed
        summary.setdefault('rules',[]).append({'name':'wall_time_budget','category':'validity',
            'passed':elapsed<=budget,'actual':elapsed,'expected':f'<= {budget} s including preparation, settlement and report'})
        if elapsed>budget:
            summary['verdict']='invalid'
        write(folder/'summary.json',summary)
        render(folder,summary)
        publish_desktop(manager,folder)
        print('Report: '+str(folder/'report.html')+' — '+summary.get('verdict','invalid')+f' ({elapsed:.2f}s total)',flush=True)
    return folder


def execute(manager, args):
    action = args.experiment_action
    if action=='algorithm':
        from benchmark.cli import execute
        return execute(args)
    if action=='suite':
        from benchmark.runner import execute
        return execute(args)
    if action=='run' and getattr(args,'execution_mode','realtime')=='lockstep':
        import background_jobs as jobs
        argv=['experiment','submit','--parallelism','1']
        for key,value in vars(args).items():
            if key in jobs.FIELDS and value is not None:argv+=['--'+key.replace('_','-'),str(value)]
        if args.repeat is not None:argv+=['--repeat',str(args.repeat)]
        task=jobs.submit(manager.parser().parse_args(argv))
        if not task['scheduler_available']:raise RuntimeError('Frozen task queued; start the scheduler to execute it: '+task['job_id'])
        while True:
            with jobs.connect() as db:state=jobs.status(db,task['job_id'])
            if state['state'] in ('completed','paused','cancelled'):
                with jobs.connect() as db:print(jobs.report(db,task['job_id']))
                if state['state']!='completed':raise RuntimeError('Task stopped: '+state['state'])
                return
            time.sleep(.5)
    if action=='compare':
        from background_compare import compare
        return compare(args)
    if action=='lockstep-study':
        from sync_study import launch
        return launch(args)
    root = manager.OPS/'experiments'
    root.mkdir(exist_ok=True)
    if action == 'list':
        for folder in sorted(root.iterdir()):
            if folder.is_dir():
                result = load(folder/'summary.json',{})
                print(folder.name+'\t'+result.get('verdict','unfinished'))
        return
    if action == 'report':
        folder = checked_folder(manager,args.run_id)
        publish_desktop(manager,folder)
        print((folder/'summary.json').read_text())
        print('HTML: '+str(folder/'report.html'))
        return
    if action == 'review':
        folder = checked_folder(manager,args.run_id)
        if not args.note.strip():
            raise RuntimeError('Visual review needs a concrete observation note')
        summary = load(folder/'summary.json')
        if not summary:
            raise RuntimeError('No completed report to review')
        review = {'decision':args.decision,'note':args.note,
            'reviewed_utc':datetime.now(timezone.utc).isoformat(),
            'checklist':['target identity','detection/GT projection alignment',
                'loss and reacquisition','aim/actuator behaviour','video continuity'],
            'reviewer':os.environ.get('SUDO_USER') or os.environ.get('USER','rm')}
        audit = folder/'reviews'
        audit.mkdir(exist_ok=True)
        write(audit/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'_'+uuid.uuid4().hex[:6]+'.json'),review)
        write(folder/'visual-review.json',review)
        summary = finalize(folder)
        publish_desktop(manager,folder)
        print('Review saved: '+summary['verdict'])
        return
    if action == 'baseline':
        folder = checked_folder(manager,args.run_id)
        summary = load(folder/'summary.json',{})
        if (summary.get('visual_review') or {}).get('decision') != 'pass':
            raise RuntimeError('Review the complete evidence and record a visual pass before selecting a baseline')
        if any(not r['passed'] and r['category'] in ('validity','performance','safety') for r in summary['rules']):
            raise RuntimeError('Invalid or failed basic rules cannot become a baseline')
        if summary.get('mode')=='shoot' and (summary.get('shooting') or {}).get('hit_method') != 'swept_actual_segment_v3':
            raise RuntimeError('Legacy collision/manifold-only results cannot become a precision baseline; record a hit-v3 experiment')
        metrics = summary['metrics']
        limits = {}
        names = ('target_hit_rate',) if summary.get('mode')=='shoot' else ('correct_tracking_fraction','center_xy_error_p95_m','velocity_error_p95_mps','first_correct_lock_s','longest_loss_s')
        for name in names:
            value = metrics.get(name)
            if value is None:
                raise RuntimeError('Baseline metric missing: '+name)
            if name in ('correct_tracking_fraction','target_hit_rate'):
                limits[name] = {'direction':'min','value':max(0,value-.05)}
            else:
                slack = .2 if name.endswith('_s') else .02
                limits[name] = {'direction':'max','value':value*1.2+slack}
        manifest = load(folder/'manifest.json')
        if manifest.get('control_source')=='fixed_script':raise RuntimeError('Fixed command replay is a renderer diagnostic and cannot become an algorithm baseline')
        baseline = {'run_id':folder.name,'scene':summary['scene'],'limits':limits,
            'selected_utc':datetime.now(timezone.utc).isoformat(),
            'scenario_sha256':manifest['configuration_sha256']['scenario.toml'],
            'simulator_sha256':manifest['configuration_sha256']['simulator.toml'],
            'reference_metrics':metrics,'rule':'Coverage may decrease by 5 percentage points; error/time limits reference*1.2 + 0.02 units (0.2 seconds for time).'}
        location = manager.OPS/'baselines'
        location.mkdir(exist_ok=True)
        write(location/(baseline_key(manifest)+'.json'),baseline)
        write(folder/'selected-as-baseline.json',baseline)
        print('Selected baseline: '+summary['scene']+' / '+folder.name)
        return
    if args.matrix=='highspeed-v1':
        if args.gimbal_dynamics!='legacy':
            raise RuntimeError('Legacy highspeed-v1 study has fixed gimbal dynamics; use a concrete --scene with --matrix single, or experiment submit')
        if args.max_fps is not None or args.algorithm_lifecycle!='session':
            raise RuntimeError('Legacy highspeed-v1 study has fixed execution settings; use a concrete --scene with --matrix single, or experiment submit for timing/lifecycle comparisons')
        from highspeed_study import run
        return run(manager,manager.OPS/'experiments'/('highspeed-v1-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')),locked=True)
    if isinstance(args.seed,bool) or not 0<=args.seed<=4294967295:raise RuntimeError('seed must be an unsigned 32-bit integer')
    args.matrix = args.matrix or ('hit-v3' if args.mode=='shoot' else None)
    args.repeat = args.repeat if args.repeat is not None else (1 if args.mode=='shoot' else 3)
    if args.mode=='track' and args.matrix:
        raise RuntimeError('--matrix requires --mode shoot')
    if args.duration is not None and not 1 <= args.duration <= 18:
        raise RuntimeError('Measurement duration must be 1..18 seconds to preserve 40 s total budget')
    if args.max_fps is not None and (not math.isfinite(args.max_fps) or not (args.max_fps==0 or 1<=args.max_fps<=1000)):
        raise RuntimeError('max-fps must be 0 (uncapped) or 1..1000')
    if not 1 <= args.repeat <= 30 or len(args.label)>100:
        raise RuntimeError('repeat must be 1..30; label at most 100 characters')
    image = manager.inspect('daedalus:rm26-evaluator')
    integration = manager.inspect(manager.IMAGE)
    if not image or not integration:
        raise RuntimeError('Build simulator and evaluator images with sim.sh build first')
    if image['Config'].get('Labels',{}).get('rm26.integration_image_id') != integration['Id']:
        raise RuntimeError('Evaluator image does not match integration interfaces; run sim.sh build --component evaluator')
    selected_simulator=manager.inspect(os.environ.get('RM26_SIMULATOR_IMAGE') or simulator_image(args.gimbal_dynamics,manager.IMAGE))
    if not selected_simulator:
        raise RuntimeError('Selected gimbal dynamics simulator image is missing')
    desktop = manager.inspect(manager.DESKTOP_CONTAINER)
    if not desktop or not desktop['State']['Running']:
        raise RuntimeError('Sunshine desktop must be running; no experiment started')
    scenes = ['static','line','spin'] if args.scene=='all' else ['move_ref','line','ego_sweep','ego_sweep_reverse'] if args.scene=='advice-motion' else ['move_ref','line','ego_left','ego_right'] if args.scene=='stability-all' else ['move_ref','ego_left','ego_right'] if args.scene=='ego-all' else [args.scene]
    if args.mode=='shoot' and args.matrix=='gimbal-ab':
        configurations = [(physics,args.bullet_speed,args.reported_bullet_speed or args.bullet_speed,args.aim_origin,profile)
            for profile in manager.GIMBAL_PROFILES for physics in ('no-drag','drag')]
    elif args.mode=='shoot' and args.matrix in ('hit-v2','hit-v3'):
        configurations = [(physics,args.bullet_speed,args.reported_bullet_speed or args.bullet_speed,origin,args.gimbal_profile)
            for origin in ('muzzle','gimbal') for physics in ('no-drag','drag')]
    elif args.mode=='shoot' and args.matrix != 'single':
        configurations = [(physics,actual,reported,args.aim_origin,args.gimbal_profile) for physics in ('no-drag','drag')
            for actual,reported in ((25.,25.),(20.,20.),(28.,28.),(25.,23.),(25.,27.))]
    else:
        configurations = [(args.physics,args.bullet_speed,
            args.reported_bullet_speed if args.reported_bullet_speed is not None else args.bullet_speed,args.aim_origin,args.gimbal_profile)]
    batch = []
    if args.resume:
        if not re.fullmatch(r'batch_[a-zA-Z0-9_-]{1,90}',args.resume):
            raise RuntimeError('Invalid batch id')
        previous = load(root/(args.resume+'.json'))
        if not previous or previous.get('mode','track') != args.mode:
            raise RuntimeError('Resume batch missing or mode differs')
        for row in previous['runs']:
            settings = row.get('shooting') or {}
            if settings.get('algorithm_profile','legacy') != args.algorithm_profile: continue
            if settings.get('gimbal_dynamics','legacy') != args.gimbal_dynamics:continue
            if settings.get('fire_policy','advice') != args.fire_policy or settings.get('fire_rate_limit_hz',2) != args.fire_rate_hz: continue
            condition = (settings.get('physics'),settings.get('bullet_speed'),settings.get('reported_bullet_speed'),settings.get('aim_origin','gimbal'),settings.get('gimbal_profile','legacy'))
            if row['scene'] not in scenes or row['repeat'] > args.repeat or (args.mode=='shoot' and condition not in configurations):
                continue
            saved = load(checked_folder(manager,row['run_id'])/'summary.json',{})
            if saved.get('verdict') in ('invalid',None):
                continue
            manifest = load(checked_folder(manager,row['run_id'])/'manifest.json',{})
            if manifest.get('terrain','arena')!=args.terrain:continue
            if manifest.get('execution',{}).get('max_fps')!=args.max_fps:continue
            if manifest.get('execution',{}).get('algorithm_lifecycle','session')!=args.algorithm_lifecycle:continue
            if manifest.get('execution',{}).get('detector_history','process')!=args.detector_history:continue
            if manifest.get('input_contract','legacy')!=args.input_contract or manifest.get('sensor_seed')!=args.sensor_seed or manifest.get('warmup_frames',30)!=args.warmup_frames:continue
            if manifest.get('command_script_sha256')!=(digest(Path(args.command_script)) if args.command_script else None):continue
            if manifest.get('random_seed',20261019)!=args.seed or manifest.get('projectile_visible','on')!=args.projectile_visible:continue
            if manifest.get('algorithm_binary_sha256') != digest(manager.ROOT/'sp_vision_25/build/simulator/sim_auto_aim'):
                raise RuntimeError('Resume algorithm binary differs; start a new batch')
            if manifest.get('containers',{}).get('simulator_image_id') != selected_simulator['Id']:
                raise RuntimeError('Resume simulator image differs; start a new batch')
            if manifest.get('configuration_sha256',{}).get('scenario.toml') != digest(short_scenario(manager,row['scene'],measurement_duration(row['scene'],args),getattr(args,'seed',20261019))):
                raise RuntimeError('Resume scenario differs; start a new batch')
            if manifest.get('configuration_sha256',{}).get('algorithm.yaml') != digest(manager.ROOT/'sp_vision_25/configs/simulator.yaml'):
                raise RuntimeError('Resume algorithm config differs; start a new batch')
            for name,checksum in manifest.get('algorithm_weights_sha256',{}).items():
                if digest(manager.ROOT/'sp_vision_25/assets'/name) != checksum:
                    raise RuntimeError('Resume model weights differ; start a new batch')
            expected = manager.render_config({'terrain':args.terrain,'physics':settings.get('physics',args.physics),
                'bullet_speed':settings.get('bullet_speed',args.bullet_speed),'gimbal_profile':settings.get('gimbal_profile','legacy'),'gimbal_dynamics':settings.get('gimbal_dynamics','legacy'),
                'max_fps':args.max_fps,'input_contract':args.input_contract,'sensor_seed':args.sensor_seed,
                'warmup_frames':args.warmup_frames,'detector_history':args.detector_history,
                'session_evidence':args.session_evidence})
            frozen = (checked_folder(manager,row['run_id'])/'configuration/simulator.toml').read_text()
            if manager.tomllib.loads(expected) != manager.tomllib.loads(frozen):
                raise RuntimeError('Resume simulator parameters differ; start a new batch')
            batch.append(row)
    batch_path = root/('batch_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'_'+uuid.uuid4().hex[:6]+'.json')
    write(batch_path,{'mode':args.mode,'resumed_from':args.resume,'runs':batch,'complete':False})
    print(f'Batch: {batch_path} ({len(configurations)*len(scenes)*args.repeat} runs)',flush=True)
    try:
        for repeat in range(args.repeat):
            for physics,actual,reported,origin,gimbal_profile in configurations:
                def already_done(scene):
                    for row in batch:
                        settings = row.get('shooting') or {}
                        if (row['scene']==scene and row['repeat']==repeat+1
                            and (args.mode=='track' or (settings.get('physics')==physics
                                and settings.get('bullet_speed')==actual
                                and settings.get('reported_bullet_speed')==reported and settings.get('aim_origin')==origin
                                and settings.get('gimbal_profile','legacy')==gimbal_profile
                                and settings.get('gimbal_dynamics','legacy')==args.gimbal_dynamics
                                and settings.get('algorithm_profile','legacy')==args.algorithm_profile
                                and settings.get('fire_policy','advice')==args.fire_policy
                                and settings.get('fire_rate_limit_hz',2)==args.fire_rate_hz))):
                            return True
                    return False
                pending = [scene for scene in scenes if not already_done(scene)]
                if not pending:
                    continue
                manager.stop()
                manager.start(argparse.Namespace(terrain=args.terrain,scene='static',control='off',view='off',replace=False,
                    input_contract=args.input_contract,sensor_seed=args.sensor_seed,warmup_frames=args.warmup_frames,
                    fire='auto' if args.mode=='shoot' else 'off',physics=physics,max_fps=args.max_fps,detector_history=args.detector_history,
                    bullet_speed=actual,reported_bullet_speed=reported,aim_origin=origin,gimbal_profile=gimbal_profile,gimbal_dynamics=args.gimbal_dynamics,fire_policy=args.fire_policy,fire_rate_hz=args.fire_rate_hz,algorithm_profile=args.algorithm_profile,projectile_visible=args.projectile_visible,command_script=args.command_script))
                for scene in pending:
                    if args.algorithm_lifecycle=='fresh':
                        manager.algorithm('stop')
                        manager.algorithm('start',view='off',fire='auto' if args.mode=='shoot' else 'off',
                            aim_origin=origin,algorithm_profile=args.algorithm_profile,detector_history=args.detector_history,detach=True)
                    folder = run_one(manager,scene,args)
                    result = load(folder/'summary.json',{})
                    batch.append({'run_id':folder.name,'scene':scene,'repeat':repeat+1,'mode':args.mode,
                        'shooting':result.get('shooting'),'verdict':result.get('verdict','invalid'),
                        'metrics':result.get('metrics')})
                    write(batch_path,{'mode':args.mode,'resumed_from':args.resume,'runs':batch,'complete':False})
                    batch_report(batch_path,batch)
                    if result.get('verdict') == 'invalid':
                        raise RuntimeError('Batch stopped because experiment environment/evidence was invalid: '+folder.name)
        write(batch_path,{'mode':args.mode,'resumed_from':args.resume,'runs':batch,'complete':True})
    finally:
        try:
            manager.rpc('control',value='off')
        except (OSError,RuntimeError):
            pass
        batch_report(batch_path,batch)
        # Publish the summary and each evidence directory into the same desktop folder.
        desktop_root = manager.DESKTOP/'.local/share/rm26-sim/experiments'
        desktop_root.mkdir(parents=True,exist_ok=True)
        for artifact in (batch_path,batch_path.with_suffix('.html')):
            if artifact.exists():
                shutil.copy2(artifact,desktop_root/artifact.name)
        print('Batch record: '+str(batch_path),flush=True)
