#!/usr/bin/env python3
"""Own only this integration's algorithm process inside the desktop container."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time

ROOT = Path('/workspace/sp_vision_25')
STATE = ROOT / 'build/sim/algorithm.json'
LOG = ROOT / 'build/sim/algorithm.log'
EXE = ROOT / 'build/simulator/sim_auto_aim'


def identity(pid):
    try:
        # starttime disambiguates a reused PID. Executable guards unrelated jobs.
        start = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
        executable = os.readlink(f'/proc/{pid}/exe')
        return start if executable.removesuffix(' (deleted)') == str(EXE) else None
    except (OSError, IndexError):
        return None


def state():
    try:
        result = json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {'running': False}
    result['running'] = identity(result.get('pid', 0)) == result.get('starttime') and bool(result.get('starttime'))
    return result


def write(value):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(value))
    tmp.replace(STATE)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start', 'stop', 'status'])
    parser.add_argument('--algorithm-profile',choices=['legacy','tracking_v2','tracking_fire_v2','tracking_v3','tracking_fire_v3'],default='legacy')
    parser.add_argument('--aim-origin',choices=['muzzle','gimbal'],default='muzzle')
    parser.add_argument('--fire',choices=['off','auto'],default='off')
    parser.add_argument('--view', choices=['on', 'off'], default='off')
    args = parser.parse_args()
    current = state()
    if args.action == 'status':
        print(json.dumps(current))
        return
    if args.action == 'stop':
        if current['running']:
            os.killpg(current['pid'], signal.SIGTERM)
            deadline = time.monotonic()+5
            while identity(current['pid']) == current['starttime'] and time.monotonic() < deadline:
                time.sleep(.05)
            if identity(current['pid']) == current['starttime']:
                os.killpg(current['pid'], signal.SIGKILL)
        print(json.dumps(state()))
        return
    if current['running']:
        raise SystemExit('Algorithm already running')
    if not EXE.is_file():
        raise SystemExit('Build sim_auto_aim first')
    STATE.parent.mkdir(parents=True, exist_ok=True)
    # Bounded rotation; keep output independent of the lifetime of docker exec.
    if LOG.exists() and LOG.stat().st_size > 20*1024*1024:
        LOG.replace(LOG.with_suffix('.log.1'))
    with LOG.open('a', buffering=1) as output:
        command = [str(EXE), '--config=configs/simulator.yaml',
                   '--runtime=build/sim/calibration.yaml', '--port=18726',
                   '--view=' + ('true' if args.view == 'on' else 'false'), '--fire='+args.fire,'--aim-origin='+args.aim_origin,'--algorithm-profile='+args.algorithm_profile]
        child = subprocess.Popen(command, cwd=ROOT, stdout=output,
            stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic()+2
        start = identity(child.pid)
        while start is None and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
            start = identity(child.pid)
        write({'pid':child.pid, 'starttime':start, 'view':args.view,
               'algorithm_profile':args.algorithm_profile,'started_at':time.time(), 'running':child.poll() is None})
        code = child.wait()
        if state().get('pid') == child.pid:
            write({'pid':child.pid, 'starttime':start, 'view':args.view,
                   'running':False, 'exit_code':code, 'stopped_at':time.time()})


if __name__ == '__main__':
    main()
