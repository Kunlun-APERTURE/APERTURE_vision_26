"""Host checks for physical boundaries, counted warmup and replay clocks."""
import ast
import collections
import copy
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace as N
import unittest

from input_contract import accept_boundary,boundary,validate
from sync_first_divergence import logical_frame

ROOT=Path(__file__).parent


def methods(file,cls,names,scope):
    tree=ast.parse((ROOT/file).read_text())
    node=next(p for p in tree.body if isinstance(p,ast.ClassDef) and p.name==cls)
    selected=[p for p in node.body if isinstance(p,ast.FunctionDef) and p.name in names]
    exec(compile(ast.Module(body=selected,type_ignores=[]),file,'exec'),scope)
    return scope


def phase(n=0,stamp=100,generation=1):
    return dict(version=1,id='rm26_scene',generation=generation,phase=n,stamp_ns=stamp,
        measurement_start_stamp_ns=90 if n else None,measurement_end_stamp_ns=99 if n==2 else None)


class ContractChecks(unittest.TestCase):
    def test_explicit_seed_and_history_required(self):
        baseline=dict(input_contract='aligned-v1',sensor_seed=17,detector_history='session',warmup_frames=30)
        validate(baseline)
        for change in (dict(sensor_seed=None),dict(sensor_seed=True),dict(sensor_seed=-1),
                dict(detector_history='process'),dict(warmup_frames=0),dict(session_evidence='off')):
            with self.subTest(change=change),self.assertRaises(ValueError):validate(dict(baseline,**change))

    def test_stale_status_cannot_rewind_phase(self):
        self.assertFalse(accept_boundary(phase(1),phase(0,110)))
        self.assertFalse(accept_boundary(phase(1,generation=2),phase(1,110,generation=1)))
        self.assertTrue(accept_boundary(phase(2),phase(0,110,generation=2)))

    def test_boundary_requires_valid_start_end(self):
        for change in (dict(measurement_start_stamp_ns=None),dict(measurement_start_stamp_ns=101),
                dict(measurement_end_stamp_ns=89),dict(measurement_end_stamp_ns=101)):
            with self.subTest(change=change),self.assertRaises(ValueError):boundary(dict(phase(2),**change))

    def test_start_cannot_drift_within_generation(self):
        with self.assertRaises(ValueError):accept_boundary(phase(1),dict(phase(1,110),measurement_start_stamp_ns=91))

    def test_legacy_settings_keep_no_sensor_override(self):
        validate(dict(input_contract='legacy'))
        with self.assertRaises(ValueError):validate(dict(input_contract='legacy',sensor_seed=1))

    def test_exact_replay_preserves_each_sampled_clock(self):
        frame=dict(stamp_ns=100,clock_mode='wall',latest_muzzle_pose=dict(stamp_ns=120))
        clocks=dict(aim_now_ns=130,fire_now_ns=141,pose_now_ns=145)
        result=logical_frame(frame,dict(result=dict(evaluation_clocks=clocks)))
        self.assertEqual(result['replay_clocks'],clocks)
        self.assertEqual(result['logical_now_ns'],141)
        self.assertEqual(frame['clock_mode'],'wall')

    def test_legacy_clock_reconstruction_remains_available(self):
        frame=dict(stamp_ns=1,latest_muzzle_pose=dict(stamp_ns=100))
        result=logical_frame(frame,dict(result=dict(fire_check=dict(pose_age_s=.000001))))
        self.assertEqual(result['logical_now_ns'],1100)

    def test_count_warmup_includes_failed_detections(self):
        scope=methods('bridge.py','Bridge',{'count_warmup'},{})
        fake=N(input_contract='aligned-v1',warmup_state='warming',warmup_processed=[],warmup_frames=3,
            warmup_done=threading.Event(),sync=None)
        for seq in range(3):scope['count_warmup'](fake,dict(seq=seq,stamp_ns=100+seq,sensor=dict(index=seq)))
        self.assertTrue(fake.warmup_done.is_set());self.assertEqual(fake.warmup_state,'parked')
        scope['count_warmup'](fake,dict(seq=4,stamp_ns=104));self.assertEqual(len(fake.warmup_processed),3)

    def test_authoritative_start_replaces_status_time_inference(self):
        scope=methods('evaluator.py','Evaluator',{'on_boundary'},dict(accept_boundary=accept_boundary,time=time))
        fake=N(args=N(scene='scene'),lock=threading.RLock(),scenario_boundary=None,scenario_started_stamp=None,
            require_boundary=True,started_received=None,end_stamp=None,end_received=None,
            fail=lambda reason:self.fail(reason),log=lambda *args:None)
        scope['on_boundary'](fake,phase(1));self.assertEqual(fake.scenario_started_stamp,90)
        scope['on_boundary'](fake,phase(0,110));self.assertEqual(fake.scenario_boundary['phase'],1)
        scope['on_boundary'](fake,phase(2,120));self.assertEqual(fake.end_stamp,99)

    def test_profile_renders_native_seed_without_disabling_noise(self):
        import manage,tomllib
        config=tomllib.loads(manage.render_config(dict(input_contract='aligned-v1',sensor_seed=17,
            detector_history='session',bullet_speed=25,physics='no-drag')))
        self.assertEqual(config['capture']['effect']['sensor_seed'],17)
        self.assertEqual(config['capture']['effect']['grain'],.05)

    def test_legacy_baselines_are_separate(self):
        from shots import baseline_key
        old=dict(scene='scene',mode='track',execution=dict(detector_history='session'))
        new=dict(old,input_contract='aligned-v1',sensor_seed=17,warmup_frames=30)
        self.assertNotEqual(baseline_key(old),baseline_key(new))

    def test_warmup_queue_keeps_prefix_and_reports_overflow(self):
        scope=methods('bridge.py','Bridge',{'on_image'},dict(stamp_ns=lambda x:x,MAX_JPEG=100))
        fake=N(lock=threading.RLock(),counts=collections.Counter(),latest=None,seq=0,
            input_contract='aligned-v1',sync=None,warmup_state='warming',warmup_images=collections.deque(),
            warmup_error=None,warmup_done=threading.Event())
        for stamp in range(1,66):scope['on_image'](fake,N(header=N(stamp=stamp),data=b'jpeg'))
        self.assertEqual(len(fake.warmup_images),64)
        self.assertEqual(fake.warmup_images[0][0],1)
        self.assertEqual(fake.warmup_state,'failed')
        self.assertTrue(fake.warmup_done.is_set())
        self.assertIn('overflow',fake.warmup_error)


if __name__=='__main__':unittest.main()
