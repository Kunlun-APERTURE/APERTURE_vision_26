#!/usr/bin/env python3
"""Host-side lifecycle manager behind sim.sh. No user-supplied shell code."""
import argparse
import base64
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import tomllib

OPS = Path(__file__).resolve().parent
ROOT = OPS.parent.parent
RUNTIME = OPS / '.runtime'
LOGS = OPS / 'logs'
DESKTOP = ROOT / 'docker/sunshine/home'
DESKTOP_CONTAINER = 'sp_vision_sunshine'
SIMULATOR = 'vision_simulator_26'
BRIDGE = 'sp_vision_ros_bridge'
IMAGE = 'daedalus:rm26-integration'
ALGORITHM_PROFILES = ('legacy','tracking_v2','tracking_fire_v2','tracking_v3','tracking_fire_v3')
GIMBAL_PROFILES = ('legacy','timing','feedforward','combined')
from scenario_catalog import SCENES
RUNNER = '/home/openvino/.local/share/rm26-sim/algorithm_runner.py'


def run(command, *, capture=False, log=None, check=True, env=None):
    if log:
        with Path(log).open('w') as output:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, env=env)
            for line in process.stdout:
                output.write(line)
                output.flush()
                print(line, end='', flush=True)
            code = process.wait()
            if check and code:
                raise RuntimeError(f'Command failed ({code}): {command[0]} — see {log}')
            return ''
    result = subprocess.run(command, check=check, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None, env=env)
    return result.stdout.strip() if capture else result.returncode


def inspect(name):
    result = subprocess.run(['docker','inspect',name], capture_output=True, text=True)
    return json.loads(result.stdout)[0] if result.returncode == 0 else None


def compose(*args, capture=False):
    env = os.environ.copy()
    env['RM26_RUN_UID'] = str(os.getuid())
    env['RM26_RUN_GID'] = str(os.getgid())
    evaluator = inspect('daedalus:rm26-evaluator')
    if evaluator and 'ROS_DISTRO=jazzy' in evaluator['Config'].get('Env',[]):
        env.setdefault('RM26_EVALUATOR_BASE','daedalus:rm26-evaluator')
    if 'RM26_INTEGRATION_IMAGE_ID' not in env:
        image = inspect(IMAGE)
        if image:
            env['RM26_INTEGRATION_IMAGE_ID'] = image['Id']
    if 'RM26_GPU_UUID' not in env:
        env['RM26_GPU_UUID'] = run(['nvidia-smi','--query-gpu=uuid','--format=csv,noheader'], capture=True).splitlines()[0]
    config = RUNTIME / 'config.toml'
    if config.exists():
        env['RM26_BULLET_SPEED'] = str(tomllib.loads(config.read_text())['projectile']['speed'])
    launch = json.loads((RUNTIME/'launch.json').read_text()) if (RUNTIME/'launch.json').exists() else {}
    env['RM26_REPORTED_BULLET_SPEED'] = str(launch.get('reported_bullet_speed',env.get('RM26_BULLET_SPEED',25)))
    env['RM26_COMMAND_SCRIPT'] = '/run/rm26-sim/command-script.json' if launch.get('command_script') else ''
    env['RM26_PROJECTILE_VISIBLE'] = launch.get('projectile_visible','on')
    env['RM26_FIRE_MODE'] = launch.get('fire','off')
    env['RM26_ALGORITHM_PROFILE'] = launch.get('algorithm_profile','legacy')
    env['RM26_FIRE_POLICY'] = launch.get('fire_policy','advice')
    env['RM26_FIRE_RATE_HZ'] = str(launch.get('fire_rate_hz',5.0))
    env['RM26_PHYSICS'] = launch.get('physics','no-drag')
    return run(['docker','compose','--file',str(OPS/'compose.yml'),*args], capture=capture, env=env)


def rpc(op, **fields):
    body = json.dumps(dict(version=1, type='admin', op=op, **fields)).encode()
    with socket.create_connection(('127.0.0.1',18726), timeout=3) as sock:
        sock.settimeout(30)
        sock.sendall(struct.pack('!II',len(body),0)+body)
        def exact(size):
            data = bytearray()
            while len(data) < size:
                chunk = sock.recv(size-len(data))
                if not chunk:
                    raise RuntimeError('Bridge disconnected')
                data.extend(chunk)
            return bytes(data)
        size, payload = struct.unpack('!II',exact(8))
        if not 0 < size <= 65536 or payload:
            raise RuntimeError('Invalid bridge response')
        response = json.loads(exact(size))
        if not response.get('ok'):
            raise RuntimeError(response.get('error','Bridge rejected operation'))
        return response['result']


def algorithm(action, *, view='off', fire='off', aim_origin='muzzle', algorithm_profile='legacy', detach=False):
    command = ['docker','exec']
    if detach:
        command.append('--detach')
    command += ['--user','1000:1000','--env','DISPLAY=:20',
        '--env','XAUTHORITY=/run/user/1000/Xauthority',
        DESKTOP_CONTAINER,'python3',RUNNER,action]
    if action == 'start':
        command += ['--view',view,'--fire',fire,'--aim-origin',aim_origin,'--algorithm-profile',algorithm_profile]
    result = run(command, capture=not detach)
    return json.loads(result) if result and not detach else None


def install():
    (DESKTOP/'.local/share/rm26-sim').mkdir(parents=True, exist_ok=True)
    (DESKTOP/'.local/bin').mkdir(parents=True, exist_ok=True)
    shutil.copy2(OPS/'algorithm_runner.py',DESKTOP/'.local/share/rm26-sim/algorithm_runner.py')
    shutil.copy2(ROOT/'sim.sh',DESKTOP/'.local/bin/rm26-sim')
    (DESKTOP/'.local/bin/rm26-sim').chmod(0o755)
    link = DESKTOP/'Desktop/项目脚本/sim.sh'
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        link.unlink()
    if not link.exists():
        link.symlink_to('/home/openvino/.local/bin/rm26-sim')
    (DESKTOP/'.local/share/rm26-sim/experiments').mkdir(parents=True,exist_ok=True)
    reports = DESKTOP/'Desktop/仿真实验报告'
    if not reports.exists() and not reports.is_symlink():
        reports.symlink_to('/home/openvino/.local/share/rm26-sim/experiments')
    print('Desktop command: bash ~/Desktop/项目脚本/sim.sh --help')


def override(text, section, key, value):
    pattern = re.compile(r'(?ms)(^\['+re.escape(section)+r'\]\s*\n)(.*?)(?=^\[|\Z)')
    found = pattern.search(text)
    if not found:
        return text + f'\n[{section}]\n{key} = {value}\n'
    body = found.group(2)
    entry = re.compile(r'(?m)^'+re.escape(key)+r'\s*=.*$')
    body = entry.sub(f'{key} = {value}',body) if entry.search(body) else body+f'\n{key} = {value}\n'
    return text[:found.start(2)]+body+text[found.end(2):]


def render_config(settings):
    text = (ROOT/'vision_simulator_26/config.toml').read_text()
    text = override(text,'bench','enabled','true')
    # bench requires a valid boot scenario even when runtime services will load
    # the user's chosen scene a moment later. Boot parked, then reset via ROS.
    text = override(text,'bench','scenario_path','"/scenarios/static.toml"')
    text = override(text,'capture.color','publish_hz','30')
    text = override(text,'projectile','speed',str(settings['bullet_speed']))
    text = override(text,'projectile.aerodynamics','enabled','true' if settings['physics']=='drag' else 'false')
    text = override(text,'projectile.aerodynamics','wind','[0.0, 0.0, 0.0]')
    mode = settings.get('gimbal_profile','legacy')
    if mode not in GIMBAL_PROFILES:
        raise RuntimeError('Unknown gimbal profile')
    fields = {'fixed_update':'true' if mode in ('timing','combined') else 'false',
        'feedforward_gain':'1.0' if mode in ('feedforward','combined') else '0.0',
        'feedforward_tau_s':'0.06','feedforward_max_rate':'0.6',
        'feedforward_reset_gap_s':'0.15','feedforward_max_step_rad':'0.06'}
    for key,value in fields.items():
        text = override(text,'vehicle.gimbal_pid',key,value)
    return text


def prepare_config(settings):
    text = render_config(settings)
    (RUNTIME/'launch.json').write_text(json.dumps(settings,indent=2))
    # Dedicated runtime copy; original simulator config and real gun calibration
    # are never edited by start/scene/control operations.
    temporary = RUNTIME/'config.tmp'
    temporary.write_text(text)
    temporary.replace(RUNTIME/'config.toml')


def save_profile(profile):
    temporary = RUNTIME/'profile.tmp'
    temporary.write_text(json.dumps(profile,indent=2))
    temporary.replace(RUNTIME/'profile.json')


def profile():
    try:
        value = json.loads((RUNTIME/'profile.json').read_text())
        value.setdefault('projectile_visible','on')
        value.setdefault('algorithm_profile','legacy')
        value.setdefault('gimbal_profile','legacy')
        value.setdefault('fire_policy','advice')
        value.setdefault('fire_rate_hz',2.0)
        return value
    except (OSError,ValueError):
        return {}


def assert_simulator_owned(info):
    if info and info['Config'].get('Labels',{}).get('rm26.workspace') != str(ROOT/'vision_simulator_26'):
        raise RuntimeError('Simulator container name belongs to another workspace')


def assert_bridge_owned():
    info = inspect(BRIDGE)
    if info and info['Config'].get('Labels',{}).get('rm26.integration') != 'true':
        raise RuntimeError('Bridge container name belongs to another workspace')


def stop():
    info = inspect(SIMULATOR)
    assert_simulator_owned(info)
    assert_bridge_owned()
    try:
        rpc('control',value='off')
    except (OSError,RuntimeError):
        pass
    desktop = inspect(DESKTOP_CONTAINER)
    if desktop and desktop['State']['Running'] and (DESKTOP/'.local/share/rm26-sim/algorithm_runner.py').exists():
        algorithm('stop')
    if inspect(BRIDGE):
        run(['docker','stop','--timeout','5',BRIDGE])
    if info and info['Config'].get('Labels',{}).get('rm26.integration') == 'true':
        run(['docker','stop','--timeout','15',SIMULATOR])
    print('Integration stopped; Sunshine desktop is still running.')


def status():
    output = {'containers':{},'profile':profile()}
    for name in (DESKTOP_CONTAINER,SIMULATOR,BRIDGE):
        info = inspect(name)
        output['containers'][name] = {
            'status':info['State']['Status'],
            'restart':info['HostConfig']['RestartPolicy']['Name'],
            'integration':info['Config'].get('Labels',{}).get('rm26.integration') == 'true'
        } if info else {'status':'absent'}
    try:
        output['bridge'] = rpc('status')
    except (OSError,RuntimeError) as error:
        output['bridge'] = {'available':False,'error':str(error)}
    try:
        output['algorithm'] = algorithm('status')
    except (RuntimeError,subprocess.CalledProcessError,ValueError):
        output['algorithm'] = {'running':False}
    print(json.dumps(output,ensure_ascii=False,indent=2))


def start(args):
    desktop = inspect(DESKTOP_CONTAINER)
    if not desktop or not desktop['State']['Running']:
        raise RuntimeError('Sunshine desktop must be running before start')
    info = inspect(SIMULATOR)
    assert_simulator_owned(info)
    assert_bridge_owned()
    current = profile()
    running = info and info['State']['Running']
    integration = info and info['Config'].get('Labels',{}).get('rm26.integration') == 'true'
    # A fresh manual start always defaults to preview, including after reboot.
    # For a running instance, omitted args preserve idempotent start behaviour.
    defaults = current if running and integration else {}
    desired = {key:(getattr(args,key,None) if getattr(args,key,None) is not None else defaults.get(key,default))
               for key,default in [('scene','static'),('control','off'),('view','off'),
                   ('fire','off'),('fire_policy','advice'),('fire_rate_hz',5.0),('aim_origin','muzzle'),('gimbal_profile','legacy'),('algorithm_profile','legacy'),('bullet_speed',25.0),('reported_bullet_speed',None),('physics','no-drag'),('projectile_visible','on'),('command_script','')]}
    if not math.isfinite(desired['fire_rate_hz']) or not 0 < desired['fire_rate_hz'] <= 5:
        raise RuntimeError('fire-rate-hz must be within (0,5] Hz')
    if desired['fire_policy'] == 'continuous' and desired['fire_rate_hz'] != 2:
        raise RuntimeError('Legacy independent continuous bench supports 2 Hz only; use advice for configurable firing')
    if desired['reported_bullet_speed'] is None:
        desired['reported_bullet_speed'] = desired['bullet_speed']
    for key in ('bullet_speed','reported_bullet_speed'):
        if not math.isfinite(desired[key]) or not 14 <= desired[key] <= 40:
            raise RuntimeError(key+' must be finite and within 14..40 m/s')
    if running and not integration and not args.replace:
        raise RuntimeError('Standalone simulator is running. Use start --replace to switch to integration.')
    if running and integration:
        if desired != current:
            raise RuntimeError('Integration configuration differs. Use restart or scene/control commands.')
        try:
            healthy = rpc('status')
            process = algorithm('status')
            if process['running'] and healthy['connected']:
                print('Integration already running with this configuration.')
                return
        except (OSError,RuntimeError,subprocess.CalledProcessError):
            pass
        raise RuntimeError('Integration is incomplete; use restart to recover it.')
    if not (ROOT/'sp_vision_25/build/simulator/sim_auto_aim').is_file():
        raise RuntimeError('Algorithm executable missing. Run sim.sh build first.')
    run(['docker','image','inspect',IMAGE],capture=True)
    install()
    (RUNTIME/'scenarios').mkdir(exist_ok=True)
    if desired.get('command_script'):
        from command_script import CommandScript
        CommandScript(desired['command_script'])
        shutil.copy2(desired['command_script'],RUNTIME/'command-script.json')
    prepare_config(desired)
    run(['python3',str(ROOT/'docker/daedalus/display_auth.py')])
    try:
        compose('up','--detach','--no-build','--force-recreate','daedalus','bridge')
        deadline = time.monotonic()+60
        while True:
            health = None
            try:
                health = rpc('status')
            except (OSError,RuntimeError):
                pass
            for name in (SIMULATOR,BRIDGE):
                component = inspect(name)
                if not component or not component['State']['Running']:
                    raise RuntimeError(name+' exited during startup; inspect its logs')
            if health and health.get('services_ready'):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('Simulator scenario services did not become ready')
            time.sleep(.3)
        rpc('scene',name=desired['scene'])
        algorithm('start',view=desired['view'],fire=desired['fire'],aim_origin=desired['aim_origin'],algorithm_profile=desired['algorithm_profile'],detach=True)
        deadline = time.monotonic()+45
        while True:
            health = rpc('status')
            if health['connected'] and health['frame_age_ms'] is not None and health['frame_age_ms'] < 250:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('Algorithm/image stream not ready; inspect algorithm and simulator logs')
            time.sleep(.25)
        rpc('control',value=desired['control'])
        save_profile(desired)
        print('Integration ready: '+json.dumps(desired,ensure_ascii=False))
    except BaseException:
        stop()
        raise


def build(args):
    desktop = inspect(DESKTOP_CONTAINER)
    if not desktop or not desktop['State']['Running']:
        raise RuntimeError('Start the Sunshine desktop before building the algorithm')
    if args.component in ('all','algorithm'):
        command = ['docker','exec','--user','1000:1000','--workdir','/workspace/sp_vision_25',
            DESKTOP_CONTAINER,'bash','-c',
            'cmake -S . -B build/simulator -DBUILD_SIMULATOR_ADAPTER=ON -DCMAKE_BUILD_TYPE=Release '
            '&& cmake --build build/simulator --target sim_auto_aim standard -j 4']
        run(command,log=LOGS/'algorithm-build.log')
    if args.component in ('all','simulator'):
        os.environ.setdefault('RM26_BUILD_PROXY','http://127.0.0.1:7890')
        definition = compose('build','--print','daedalus',capture=True)
        bake = LOGS/'build-bake.json'
        bake.write_text(definition)
        run(['docker','buildx','bake','--file',str(bake),'--allow=network.host',
             '--progress','plain','--load','daedalus'],log=LOGS/'build.log')
    if args.component in ('all','evaluator'):
        os.environ.setdefault('RM26_BUILD_PROXY','http://127.0.0.1:7890')
        definition = compose('--profile','experiment','build','--print','evaluator',capture=True)
        bake = LOGS/'evaluator-bake.json'
        bake.write_text(definition)
        run(['docker','buildx','bake','--file',str(bake),'--allow=network.host',
            '--progress','plain','--load','evaluator'],log=LOGS/'evaluator-build.log')
    install()


def parser():
    result = argparse.ArgumentParser(description='RM26 simulator + ROS bridge + original auto-aim algorithm')
    commands = result.add_subparsers(dest='action',required=True)
    p = commands.add_parser('build')
    p.add_argument('--component',choices=['all','algorithm','simulator','evaluator'],default='all')
    for name in ('start','restart'):
        p = commands.add_parser(name)
        p.add_argument('--scene',choices=SCENES)
        p.add_argument('--control',choices=['on','off'])
        p.add_argument('--view',choices=['on','off'])
        p.add_argument('--fire',choices=['off','auto'])
        p.add_argument('--fire-policy',choices=['advice','continuous'])
        p.add_argument('--fire-rate-hz',type=float)
        p.add_argument('--aim-origin',choices=['muzzle','gimbal'])
        p.add_argument('--gimbal-profile',choices=GIMBAL_PROFILES)
        p.add_argument('--algorithm-profile',choices=ALGORITHM_PROFILES)
        p.add_argument('--bullet-speed',type=float)
        p.add_argument('--reported-bullet-speed',type=float)
        p.add_argument('--physics',choices=['no-drag','drag'])
        p.add_argument('--projectile-visible',choices=['on','off'])
        p.add_argument('--command-script',help='Fixed control playback JSON for renderer diagnostics')
        p.add_argument('--replace',action='store_true',help='Replace a running standalone simulator')
    p = commands.add_parser('control')
    p.add_argument('value',choices=['on','off'])
    p = commands.add_parser('scene')
    p.add_argument('name',choices=SCENES)
    p = commands.add_parser('logs')
    p.add_argument('--component',choices=['algorithm','bridge','simulator'],default='algorithm')
    p.add_argument('--follow',action='store_true')
    p.add_argument('--tail',type=int,default=100)
    for name in ('stop','status','install'):
        commands.add_parser(name)
    from experiments import add_parser
    add_parser(commands)
    return result


def main():
    sys.stdout.reconfigure(line_buffering=True)
    arguments = sys.argv[1:]
    if arguments[:1] == ['--desktop-payload']:
        if len(arguments) != 2 or len(arguments[1]) > 4096:
            raise RuntimeError('Invalid desktop request')
        arguments = json.loads(base64.b64decode(arguments[1],altchars=b'-_',validate=True))
        if not isinstance(arguments,list) or not all(isinstance(v,str) and len(v)<256 for v in arguments):
            raise RuntimeError('Invalid desktop argument list')
    args = parser().parse_args(arguments)
    RUNTIME.mkdir(parents=True,exist_ok=True,mode=0o700)
    LOGS.mkdir(parents=True,exist_ok=True,mode=0o700)
    if args.action == 'logs':
        if not 0 <= args.tail <= 10000:
            raise RuntimeError('tail must be between 0 and 10000')
        if args.component == 'algorithm':
            command = ['docker','exec',DESKTOP_CONTAINER,'tail','-n',str(args.tail)]
            if args.follow:
                command.append('-F')
            command.append('/workspace/sp_vision_25/build/sim/algorithm.log')
        else:
            command = ['docker','logs','--timestamps','--tail',str(args.tail)]
            if args.follow:
                command.append('--follow')
            command.append(BRIDGE if args.component == 'bridge' else SIMULATOR)
        run(command)
        return
    if args.action == 'status':
        status()
        return
    with (RUNTIME/'operation.lock').open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another integration operation is still running')
        if args.action == 'experiment':
            import experiments
            experiments.execute(sys.modules[__name__],args)
        elif args.action == 'install':
            install()
        elif args.action == 'build':
            build(args)
        elif args.action == 'stop':
            stop()
        elif args.action == 'start':
            start(args)
        elif args.action == 'restart':
            saved = profile()
            stop()
            for key in ('scene','control','view','fire','aim_origin','gimbal_profile','bullet_speed','reported_bullet_speed','physics'):
                if getattr(args,key) is None:
                    setattr(args,key,saved.get(key))
            start(args)
        elif args.action == 'control':
            rpc('control',value=args.value)
            saved = profile()
            saved['control'] = args.value
            save_profile(saved)
            print('Control '+args.value)
        elif args.action == 'scene':
            rpc('scene',name=args.name)
            saved = profile()
            saved['scene'] = args.name
            save_profile(saved)
            print('Scene ready: '+args.name)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError,OSError,ValueError,subprocess.CalledProcessError) as error:
        print('sim.sh: '+str(error),file=sys.stderr)
        sys.exit(1)
