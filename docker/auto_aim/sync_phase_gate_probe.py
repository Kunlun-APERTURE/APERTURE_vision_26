"""Exercise the unmodified frozen Bridge.result with only phase changed.

No ROS scheduling is imitated. Captured inputs/results and receipt timestamps
are fixed. This is a gate unit intervention, never a new hit-rate measurement.
"""
import argparse
import ast
import collections
import copy
import json
import math
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from report import load, write
from sync_first_divergence import sha


class Command:
    def __init__(self):
        self.header = SimpleNamespace(stamp=SimpleNamespace(sec=0, nanosec=0), frame_id='')


def method(path):
    tree = ast.parse(path.read_text())
    bridge = next(p for p in tree.body if isinstance(p, ast.ClassDef) and p.name == 'Bridge')
    function = next(p for p in bridge.body if isinstance(p, ast.FunctionDef) and p.name == 'result')
    scope = dict(math=math, time=time, GimbalCmd=Command)
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), 'exec'), scope)
    return scope['result']


def probe(function, packet, phase):
    frame, result = packet['frame'], packet['result']
    received = packet['receipt_ns']
    observed = []
    state = SimpleNamespace(lock=threading.RLock(), session=frame['session'],
        counts=collections.defaultdict(int), ages=[], pipeline_times=[], command_script=None,
        clock_ns=lambda:received, control=packet['control_requested'], fire_mode='auto',
        fire_armed=True, connected=True, fire_rate_hz=5., fire_policy='advice', last_fire=0,
        scenario=dict(phase=phase), sync=SimpleNamespace(command=lambda cmd, eligible:(cmd, eligible)),
        publish_eligibility=lambda *args:None, archive=lambda *args:None,
        observer=SimpleNamespace(publish=lambda p,jpeg:observed.append(p)), hold=lambda:None)
    function(state, copy.deepcopy(result), copy.deepcopy(frame), b'')
    assert len(observed) == 1
    return observed[0]['fire_forwarded']


def run(parent, output):
    live = load(parent/'history-live.json')
    bundle = Path(live['bundle'])
    source = bundle/'docker/auto_aim/bridge.py'
    function = method(source)
    rows = []
    for entry in live['modes']['lockstep_session']['runs']:
        folder = parent.parent/entry['run_id']
        events = [json.loads(line) for line in (folder/'session-evidence/events.jsonl').open()]
        ack = next(p['data']['stamp_ns'] for p in events if p['kind'] == 'measurement_ack')
        results = [p['data'] for p in events if p['kind'] == 'result' and p['data']['receipt_ns'] >= ack]
        original = next(p for p in results if p['fire_forwarded'])
        suppressed = []
        for p in results:
            if p['receipt_ns'] >= original['receipt_ns']:
                break
            idle, running = probe(function, p, 0), probe(function, p, 1)
            if running and not idle and not p['fire_forwarded']:
                suppressed.append(dict(seq=p['frame']['seq'], stamp_ns=p['frame']['stamp_ns'],
                    capture_to_ack_ms=(p['frame']['stamp_ns']-ack)*1e-6,
                    receipt_to_ack_ms=(p['receipt_ns']-ack)*1e-6,
                    phase0_fire=idle, phase1_fire=running,
                    actual_fire=p['fire_forwarded'], valid=p['result']['valid'],
                    advice=p['result']['fire_advice'], control=p['control_requested']))
        earliest = next((p for p in results if probe(function, p, 1)), None)
        rows.append(dict(**entry, measurement_ack_ns=ack,
            inferred_start_offset_to_ack_ms=(ack-load(folder/'run-state.json')['measurement_start_stamp_ns'])*1e-6,
            first_actual_pulse_ms=(original['receipt_ns']-ack)*1e-6,
            first_phase1_eligible_ms=(earliest['receipt_ns']-ack)*1e-6 if earliest else None,
            status_gate_suppressed=suppressed,
            first_pulse_delay_due_to_status_gate_ms=(original['receipt_ns']-earliest['receipt_ns'])*1e-6
                if earliest else None))
    value = dict(source=str(source), source_sha256=sha(source), results=rows,
        intervention='Actual frozen Bridge.result; same recorded result/own pose/receipt/control, '
            'only scenario.phase=0 versus1, no previous measured pulse. Start from acknowledged physical begin.',
        limitation='First-pulse gating only. No projectile simulation or closed-loop hit-rate counterfactual.')
    write(output/'phase-gate.json', value)
    return value


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--parent', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    print(json.dumps(run(args.parent.resolve(), args.output.resolve()), indent=2))
