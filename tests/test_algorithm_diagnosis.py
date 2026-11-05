import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('algorithm_diagnosis', Path(__file__).resolve().parents[1]/'tools/analyze_algorithm.py')
diagnosis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnosis)


class AlgorithmDiagnosisTests(unittest.TestCase):
    def test_missing_checks_are_unknown_not_a_fire_failure(self):
        result = dict(observations=[{'pose_valid': True}], tracker='tracking', valid=True)
        self.assertEqual(diagnosis.stage(result), 'unknown_fire_check')
        result['fire_check'] = dict(valid=True, pose_valid=True, aligned=False)
        self.assertEqual(diagnosis.stage(result), 'fire_alignment')
        result['fire_check']['aligned'] = True
        result['fire_advice'] = False
        self.assertEqual(diagnosis.stage(result), 'stability_or_other')
        result['fire_advice'] = True
        self.assertEqual(diagnosis.stage(result), 'advice')

    def test_measurement_bounds_and_zero_shots_preserve_denominators(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);folder=root/'run';folder.mkdir()
            def write(name, data): (folder/name).write_text(json.dumps(data))
            write('run-state.json',dict(measurement_start_stamp_ns=10,measurement_end_stamp_ns=30))
            write('manifest.json',dict(scene='static',sensor_seed=1,duration_s=2))
            frames=[dict(stamp_ns=stamp,measurement=stamp!=5) for stamp in (5,10,20,30)]
            (folder/'frames.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in frames))
            packets=[dict(kind='observation',data=dict(frame=dict(stamp_ns=r['stamp_ns'],seq=i),result=dict(observations=[]))) for i,r in enumerate(frames)]
            (folder/'telemetry.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in packets))
            result=dict(metrics=dict(shots_fired=0,target_armor_hits=0,weighted_hit_rate=None,weighted_points_per_s=0,hits_per_s=0))
            summary=diagnosis.analyze_run(folder,result,root/'frames.jsonl')
            self.assertEqual(summary['frames'],2)
            self.assertEqual(summary['stage_counts'],{'no_detection':2})
            self.assertIsNone(summary['exact'])
            self.assertEqual(summary['shots_per_s'],0)
            self.assertEqual(summary['overlapping_fire_blocks']['aligned'],dict(false=0,unknown=2))
            with (folder/'telemetry.jsonl').open('a') as stream:stream.write(json.dumps(packets[1])+'\n')
            with self.assertRaisesRegex(ValueError,'Duplicate'):
                diagnosis.analyze_run(folder,result,root/'duplicate.jsonl')
