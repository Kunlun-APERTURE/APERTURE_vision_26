#!/usr/bin/env python3
"""Exercise the frozen simulator's real Unix step server in an isolated container."""
import argparse
import json
import os
import re
from pathlib import Path
import socket
import subprocess
import tempfile
import time

import manage
from report import load, write


def check(study):
    state = load(study/'study.json') or load(study/'diagnosis-state.json') or load(study/'continuity-state.json')
    bundle = Path(state['bundle'])
    image = load(bundle/'bundle.json')['images']['simulator']
    name = 'rm26-lockstep-protocol-check-'+str(os.getpid())
    results = {}
    with tempfile.TemporaryDirectory(prefix='rm26-lockstep-protocol-') as root:
        root = Path(root)
        sample=next(iter(state['modes'].values()))['runs'][0]['run_id']
        config = (manage.OPS/'experiments'/sample/'configuration/simulator.toml').read_text()
        # Explicitly park this fixture in Idle. The production boot scenario
        # uses on_load; a reset while Running must be rejected by the protocol.
        scenario=(bundle/'docker/auto_aim/scenarios/static.toml').read_text()
        scenario,count=re.subn(r'(?m)^trigger\s*=\s*"[^"]+"', 'trigger = "on_first_gimbal_cmd"',scenario)
        if count!=1:raise RuntimeError('Protocol fixture needs an explicit idle trigger')
        (root/'scenario.toml').write_text(scenario)
        config=manage.override(config,'bench','scenario_path','"/probe/scenario.toml"')
        config=manage.override(config,'capture.effect','sensor_seed','17')
        for section, key, value in [('window', 'headless', 'true'), ('preview', 'enabled', 'false'),
                                    ('debug', 'egui', 'false'), ('debug', 'inspector', 'false'),
                                    ('camera', 'pip_enabled', 'false')]:
            config = manage.override(config, section, key, value)
        (root/'config.toml').write_text(config)
        command = ['docker', 'run', '--detach', '--name', name, '--gpus', 'all', '--network', 'none',
                   '-e', 'DAEDALUS_EXECUTION_MODE=lockstep', '-e', 'DAEDALUS_STEP_SOCKET=/probe/step.sock',
                   '-e', 'DAEDALUS_INTEGRATION=1', '-e', 'ROS_DOMAIN_ID=160',
                   '-e', 'HOME=/tmp/probe-home', '-e', 'XDG_RUNTIME_DIR=/tmp/probe-runtime',
                   '-e', 'NVIDIA_DRIVER_CAPABILITIES=graphics,utility,compute',
                   '-v', str(root)+':/probe', '-v', str(root/'config.toml')+':/app/config.toml:ro',
                   '-v', str(bundle/'docker/auto_aim/scenarios')+':/scenarios:ro',
                   image, 'bash', '-c', 'mkdir -p "$HOME" "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"; exec /app/daedalus']
        subprocess.run(command, check=True, capture_output=True, text=True)
        try:
            client = socket.socket(socket.AF_UNIX)
            client.settimeout(30)
            deadline = time.monotonic()+60
            while True:
                try:
                    client.connect(str(root/'step.sock'))
                    break
                except PermissionError:
                    # The simulator runs as root, like the production image;
                    # permit this host client only inside its private temp dir.
                    subprocess.run(['docker','exec',name,'chmod','666','/probe/step.sock'],check=True,capture_output=True)
                except (FileNotFoundError, ConnectionRefusedError):
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(.05)
            with client, client.makefile('rb') as stream:
                def request(value):
                    client.sendall((json.dumps(value, allow_nan=False)+'\n').encode())
                    return json.loads(stream.readline())
                boot_id=0;ready_deadline=time.monotonic()+60
                while True:
                    for _ in range(20):
                        boot_id+=1
                        ack=request(dict(id=boot_id,delta_ns=1_000_000,capture=False))
                        assert ack['ok'],ack
                    logs=subprocess.run(['docker','logs',name],capture_output=True,text=True)
                    if 'scene loaded' in logs.stdout+logs.stderr:break
                    if time.monotonic()>ready_deadline:raise RuntimeError('Protocol fixture scene did not load')
                    time.sleep(.01)
                first = dict(id=boot_id+1, delta_ns=1_000_000, capture=False)
                a = request(first)
                assert a['ok'], a
                results['duplicate_ack_no_advance'] = request(first) == a
                next_id=first['id']+1
                results['out_of_order_rejected'] = not request(dict(first, id=next_id+1))['ok']
                results['changed_duplicate_rejected'] = not request(dict(first, delta_ns=2_000_000))['ok']
                results['negative_delta_rejected'] = not request(dict(first, id=next_id, delta_ns=-1))['ok']
                results['excessive_delta_rejected'] = not request(dict(first, id=next_id, delta_ns=100_000_001))['ok']
                results['wrong_generation_rejected'] = not request(dict(first, id=next_id, generation=a['generation']+1))['ok']
                results['malformed_command_rejected'] = not request(dict(first, id=next_id, command={'yaw': 'invalid'}))['ok']
                b = request(dict(first, id=next_id))
                results['rejections_do_not_advance'] = b['ok'] and b['stamp_ns']-a['stamp_ns'] == 1_000_000
                snapshot = request(dict(id=next_id+1, delta_ns=0, observe_only=True))
                results['readiness_snapshot_no_advance'] = snapshot['ok'] and snapshot['stamp_ns'] == b['stamp_ns']
                physical=snapshot.get('scenario_boundary')
                if physical:
                    results['physical_idle_snapshot']=physical['phase']==0 and physical['measurement_start_stamp_ns'] is None
                n=next_id+2
                results['observation_mutation_rejected'] = not request(dict(id=n,delta_ns=0,observe_only=True,align_epoch=True))['ok']
                results['reset_and_begin_rejected'] = not request(dict(id=n,delta_ns=1_000_000,reset_scene=True,begin_scene=True))['ok']
                reset_request=dict(id=n,delta_ns=1_000_000,reset_scene=True,align_epoch=True)
                if physical:reset_request['sensor_reset']='protocol-seed'
                reset=request(reset_request)
                results['idle_preparation_reset_acknowledged'] = reset.get('reset_scene') is True and reset['stamp_ns']-snapshot['stamp_ns']==1_000_000
                results['duplicate_reset_no_advance'] = request(reset_request)==reset
                if physical:results['native_sensor_reset_acknowledged']=reset.get('sensor_reset_id')=='protocol-seed'
                settled=request(dict(id=n+1,delta_ns=10_000_000))
                results['reset_crosses_physics_boundary'] = settled['ok'] and settled['stamp_ns']-reset['stamp_ns']==10_000_000
                running=request(dict(id=n+2,delta_ns=10_000_000,begin_scene=True))
                results['begin_acknowledged_after_reset'] = running['ok']
                if physical:
                    boundary=running['scenario_boundary']
                    results['physical_begin_in_same_step_ack']=boundary['phase']==1 and boundary['measurement_start_stamp_ns']==running['stamp_ns']
                    results['running_sensor_reset_rejected']=not request(dict(id=n+3,delta_ns=1_000_000,sensor_reset='forbidden'))['ok']
                results['running_reset_rejected'] = not request(dict(id=n+3,delta_ns=1_000_000,reset_scene=True))['ok']
                parked=request(dict(id=n+3,delta_ns=0,observe_only=True))
                results['rejected_reset_no_advance'] = parked['ok'] and parked['stamp_ns']==running['stamp_ns']
                if physical:results['physical_start_survives_observe_only']=parked['scenario_boundary']['measurement_start_stamp_ns']==boundary['measurement_start_stamp_ns']
            result = dict(passed=all(results.values()), checks=results, simulator_image=image,
                          candidate_bundle=str(bundle))
            write(study/'protocol-checks.json', result)
            print(json.dumps(result), flush=True)
            return result
        finally:
            logs = subprocess.run(['docker', 'logs', name], capture_output=True, text=True)
            (study/'protocol-checks.log').write_text(logs.stdout+logs.stderr)
            subprocess.run(['docker', 'rm', '--force', name], check=True, capture_output=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('study', type=Path)
    args = parser.parse_args()
    if not check(args.study.resolve())['passed']:
        raise SystemExit(1)
