#!/usr/bin/env python3
"""Exercise the real evaluator settlement loop without publishing ROS traffic.

Run inside the evaluator image with ROS setup sourced. No simulator is started.
"""
import collections
import io
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from evaluator import Evaluator


class Executor:
    def add_node(self,node):pass
    def spin(self):pass
    def shutdown(self,**kwargs):pass


class SettlementChecks(unittest.TestCase):
    def fake(self,path,snapshot,events):
        obj=object.__new__(Evaluator)
        now=time.monotonic()
        obj.args=SimpleNamespace(mode='shoot',wall_budget=30,settlement=0)
        obj.lockstep=False;obj.last_settlement_stamp=None
        obj.output=path;obj.deferred=True;obj.settlement_snapshot=snapshot
        obj.settlement_generation=None;obj.last_shot_activity=now-1
        obj.created_mono=now;obj.started_received=now-6;obj.end_received=now-.5
        obj.last_observation_wall=now;obj.end_stamp=10;obj.scenario_started_stamp=0
        obj.errors=[];obj.scenario={'phase':2};obj.scenario_history=collections.deque()
        obj.abort=threading.Event();obj.stopped=threading.Event();obj.lock=threading.RLock()
        obj.observations=queue.Queue();obj.raw_log=io.StringIO();obj.samples_log=io.StringIO();obj.original_jpegs=io.BytesIO()
        obj.raw_video=obj.annotated_video=None;obj.shot_events=events;obj.projectile_traces=[]
        obj.observer_gaps=obj.queue_drops=obj.fire_events=obj.fire_advice=obj.control_messages=obj.algorithm_fire_suggestions=0
        obj.fail=lambda message:obj.errors.append(message)
        obj.record_status=lambda *a,**kw:None
        obj.log=lambda *a,**kw:None
        return obj

    def run_loop(self,snapshot,events,lockstep=False):
        with tempfile.TemporaryDirectory() as root:
            folder=Path(root);obj=self.fake(folder,snapshot,events)
            obj.lockstep=lockstep
            if lockstep:obj.last_settlement_stamp=100_000_000
            with patch('evaluator.rclpy.executors.SingleThreadedExecutor',Executor),patch('evaluator.finalize'):
                obj.run()
            return json.loads((folder/'run-state.json').read_text())

    def test_expiry_settles_without_hit(self):
        s=self.run_loop(dict(allocated=1,terminals=[dict(shot_seq=1,reason='expired')]),[dict(kind=0,shot_seq=1)])
        self.assertFalse(s['errors']);self.assertTrue(s['complete'])

    def test_collision_requires_received_hit(self):
        snapshot=dict(allocated=1,terminals=[dict(shot_seq=1,reason='collision')])
        s=self.run_loop(snapshot,[dict(kind=0,shot_seq=1)])
        self.assertIn('Missing projectile settlement or collision events',s['errors'])
        s=self.run_loop(snapshot,[dict(kind=0,shot_seq=1),dict(kind=1,shot_seq=1)])
        self.assertFalse(s['errors'])

    def test_missing_fire_and_unresolved_are_invalid(self):
        for snapshot,events in [(dict(allocated=1,terminals=[dict(shot_seq=1,reason='expired')]),[]),
                (dict(allocated=1,terminals=[]),[dict(kind=0,shot_seq=1)])]:
            with self.subTest(snapshot=snapshot):
                self.assertTrue(self.run_loop(snapshot,events)['errors'])

    def test_legacy_fallback(self):
        self.assertFalse(self.run_loop(None,[dict(kind=0,shot_seq=1)])['errors'])

    def test_generation_reset_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            obj=self.fake(Path(root),None,[])
            for generation in (3,4):
                Evaluator.on_settlement(obj,SimpleNamespace(data=json.dumps(dict(version=1,
                    method='swept_actual_segment_v3',generation=generation,allocated=0,terminals=[],stamp_ns=100))))
            self.assertIn('Settlement generation changed during capture',obj.errors)

    def test_lockstep_settlement_uses_logical_quiet_period(self):
        snapshot=dict(allocated=1,terminals=[dict(shot_seq=1,reason='expired')],stamp_ns=1_000_000_000)
        result=self.run_loop(snapshot,[dict(kind=0,shot_seq=1)],lockstep=True)
        self.assertTrue(result['complete']);self.assertFalse(result['errors'])

    def test_lockstep_missing_terminal_stays_invalid(self):
        snapshot=dict(allocated=1,terminals=[],stamp_ns=7_000_000_000)
        result=self.run_loop(snapshot,[dict(kind=0,shot_seq=1)],lockstep=True)
        self.assertIn('Missing projectile settlement or collision events',result['errors'])


if __name__=='__main__':unittest.main()
