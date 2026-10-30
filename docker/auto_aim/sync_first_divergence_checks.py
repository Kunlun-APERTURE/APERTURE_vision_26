"""Contract checks for exact-input causal diagnostics and detector lifecycle."""
import copy
import json
import unittest
from unittest.mock import patch

import background_jobs as jobs
from background_capacity import limit
import manage
from shots import baseline_key
from sync_first_divergence import logical_frame,tiles


class ContractChecks(unittest.TestCase):
    def test_simulated_input_is_not_changed(self):
        frame=dict(clock_mode='simulation',stamp_ns=100,logical_now_ns=130,calibration={'id':'a'})
        result=logical_frame(frame,{})
        self.assertEqual(frame,result);result['calibration']['id']='b'
        self.assertEqual(frame['calibration']['id'],'a')

    def test_ui_clock_conversion_uses_own_pose_only(self):
        frame=dict(stamp_ns=1_000_000_000,latest_muzzle_pose=dict(stamp_ns=1_025_000_000,yaw=0))
        old=copy.deepcopy(frame)
        header=logical_frame(frame,dict(result=dict(fire_check=dict(pose_age_s=.012))))
        self.assertEqual(header['logical_now_ns'],1_037_000_000)
        self.assertEqual(frame,old)
        self.assertNotIn('truth',header)
        with self.assertRaises(ValueError):logical_frame(frame,dict(result=dict(fire_check={})))

    def test_declared_scan_order_matches_detector(self):
        self.assertEqual(tiles(1440,1080)[0],[400,220,640,640])
        self.assertEqual(tiles(1440,1080)[3],[400,0,640,640])
        self.assertEqual(tiles(1440,1080)[6],[400,440,640,640])
        self.assertEqual(tiles(640,640),[[0,0,640,640]])

    def test_explicit_history_contract_roundtrips_queue_validation(self):
        ordinary=jobs.validate(dict(scene='static'))
        isolated=jobs.validate(dict(scene='static',detector_history='session'))
        self.assertEqual(ordinary['detector_history'],'process')
        self.assertEqual(isolated['detector_history'],'session')
        with self.assertRaises(ValueError):jobs.validate(dict(scene='static',detector_history='session',session_evidence='off'))

    def test_manual_legacy_binary_receives_no_new_flag(self):
        with patch.object(manage,'run',return_value='{}') as run:
            manage.algorithm('start',detector_history='process')
            self.assertNotIn('--detector-history',run.call_args.args[0])
            manage.algorithm('start',detector_history='session')
            self.assertIn('--detector-history',run.call_args.args[0])

    def test_history_condition_is_separate_baseline(self):
        ordinary=dict(scene='static',mode='track',execution=dict(detector_history='process'))
        historical=dict(scene='static',mode='track')
        isolated=dict(ordinary,execution=dict(detector_history='session'))
        self.assertEqual(baseline_key(ordinary),baseline_key(historical))
        self.assertNotEqual(baseline_key(ordinary),baseline_key(isolated))

    def test_history_contract_does_not_inherit_capacity(self):
        job=dict(spec=json.dumps({'_protocol':2}),parallelism=0,exploratory=0,bundle='unused',
            settings=json.dumps(dict(detector_history='session')))
        self.assertEqual(limit(job,{},{}),1)


if __name__=='__main__':unittest.main(verbosity=2)
