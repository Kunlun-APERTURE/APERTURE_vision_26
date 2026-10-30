"""Backend-only interval calibration and acknowledged preparation-release checks."""
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace as N
import unittest
from unittest.mock import patch

from sync_timing import Schedule,validate
from sync_bridge import Coordinator


def profile():
    return dict(version=1,kind='fixed',name='timing-test',control_hz=480,
        input_delay_ns=28_000_000,processing_delay_ns=9_000_000,warmup_frames=30)


class TimingAlignmentChecks(unittest.TestCase):
    def test_default_capture_cadence_unchanged(self):
        s=Schedule(profile(),'hf_uniform_3')
        self.assertEqual(sum(s.image_interval() for _ in range(300)),10_000_000_000)

    def test_explicit_native_ui_capture_interval(self):
        s=Schedule(dict(profile(),capture_interval_ns=34_634_124),'hf_uniform_3')
        self.assertEqual([s.image_interval() for _ in range(3)],[34_634_124]*3)

    def test_reject_invalid_capture_intervals(self):
        for value in (True,0,1e7,1_000_000,100_000_001):
            with self.subTest(value=value),self.assertRaises(ValueError):validate(dict(profile(),capture_interval_ns=value))

    def test_release_is_explicit_boolean(self):
        validate(dict(profile(),preparation_release_home=True))
        with self.assertRaises(ValueError):validate(dict(profile(),preparation_release_home=1))

    def test_release_enters_only_as_serialized_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'profile.json';path.write_text(json.dumps(profile()))
            with patch('threading.Thread.start'):
                driver=Coordinator(N(archive=lambda *a:None),Path(directory)/'step.sock',path,'hf_uniform_3')
            request=dict(mode='release_home',seconds=1/480,scene=None,entered=threading.Event(),done=threading.Event())
            driver.transitions.put(request);driver.enter_transitions()
            self.assertTrue(request['entered'].is_set());self.assertTrue(driver.release_home_pending)
            self.assertEqual(driver.mode,'release_home');self.assertEqual(driver.phase_end-driver.now_ns,2_083_333)

    def test_release_uses_acknowledged_step_and_no_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'profile.json';path.write_text(json.dumps(profile()))
            with patch('threading.Thread.start'):
                driver=Coordinator(N(archive=lambda *a:None),Path(directory)/'step.sock',path,'hf_uniform_3')
            old=driver.now_ns;calls=[];driver.release_home_pending=True;driver.mode='release_home'
            def command():return N(header=N(stamp=N(sec=0,nanosec=0)),distance=0,fire_advice=True)
            driver.command=lambda cmd,gate:(dict(distance=cmd.distance,fire_advice=cmd.fire_advice),dict(enabled=gate))
            def step(delta,capture,cmd,gate,**kwargs):
                calls.append((delta,capture,cmd,gate));return dict(stamp_ns=old+delta,generation=1)
            driver.client=N(step=step)
            with patch.dict('sys.modules',{'rm_interfaces.msg':N(GimbalCmd=command)}):driver.tick(False)
            self.assertEqual(len(calls),1);self.assertFalse(calls[0][1])
            self.assertEqual(calls[0][2],dict(distance=-1.,fire_advice=False))
            self.assertFalse(calls[0][3]['enabled']);self.assertFalse(driver.release_home_pending)
            self.assertEqual(driver.now_ns,old+calls[0][0])


if __name__=='__main__':unittest.main()
