"""Integration checks against frozen, completed replay evidence."""
import argparse
import math
from pathlib import Path
import unittest

from report import load
from sync_first_divergence_analysis import primary
from perception_repair import FIELDS


class RepairEvidence(unittest.TestCase):
    def test_disabled_native_behavior(self):
        rows=[r for r in self.state['replays'] if r['variant']=='baseline']
        self.assertEqual(len(rows),9)
        for row in rows:
            result=load(Path(row['output'])/'result.json')
            self.assertTrue(all(result['differences_from_recorded'][k]['different_frames']==0 for k in FIELDS))

    def test_same_image_pose_and_clock_inputs(self):
        self.assertEqual(len(self.state['replays']),61)
        for row in self.state['replays']:
            source=next(e for e in self.contract['sources'] if e['run_id']==row['source_run'])
            self.assertEqual(row['input_sha256'],source['baseline_replay']['input_sha256'])
            for p in primary(Path(row['output'])):
                self.assertTrue(p['frame'].get('replay_clocks'))

    def test_joint_position_survives_target_initialization(self):
        checked=0
        for row in self.state['replays']:
            if row['variant']!='Bfix':continue
            for p in primary(Path(row['output'])):
                r=p['result'];d=r.get('continuity',{}).get('initial_consistency') or {}
                if d.get('status')!='joint_position':continue
                self.assertTrue(r['continuity']['initialized'])
                x=r['filter_state'];self.assertIsNotNone(x)
                self.assertLess(math.dist([x[0],x[2],x[4]],d['center']),1e-8)
                self.assertLessEqual(d['anchor_residual'],.35)
                self.assertLessEqual(d['other_residual'],.35)
                self.assertLessEqual(d['position_shift_m'],d['position_bound_m']+1e-9)
                self.assertNotEqual(d['selected_observation'],d['other_observation'])
                self.assertIn(d['other_slot'],(1,2,3));checked+=1
        self.assertGreater(checked,0)

    def test_bfix_never_blocks_for_inconsistency(self):
        for row in self.state['replays']:
            if row['variant']!='Bfix':continue
            self.assertNotIn('pending_inconsistent',row['metrics']['statuses'])
            self.assertEqual(row['metrics']['initial_latency_s'],row['baseline']['initial_latency_s'])

    def test_actual_longest_a_regression(self):
        source=next(e for e in self.contract['sources'] if e['sensor_seed']==1904 and e['variant']=='A35')
        base=primary(Path(source['baseline_replay']['output']))[0]
        old=primary(Path(source['baseline_replay']['output']).parent/'A35')[0]
        row=next(r for r in self.state['replays'] if r['source_run']==source['run_id'] and r['variant']=='Afix')
        fixed=primary(Path(row['output']))[0]
        self.assertEqual(old['result']['observations'][0]['refinement']['reason'],'side_ratio_change')
        self.assertTrue(fixed['result']['observations'][0]['refinement']['accepted'])
        self.assertEqual(fixed['result']['observations'][0]['corners'],base['result']['observations'][0]['corners'])
        self.assertEqual(fixed['result']['filter_state'],base['result']['filter_state'])

    def test_actual_longest_b_wait(self):
        source=next(e for e in self.contract['sources'] if e['engine']=='ui' and e['scene']=='hf_turn90_3' and e['sensor_seed']==1901 and e['variant']=='B35')
        row=next(r for r in self.state['replays'] if r['source_run']==source['run_id'] and r['variant']=='Bfix')
        fixed=primary(Path(row['output']))[0]
        self.assertIsNotNone(fixed['result']['filter_state'])
        self.assertEqual(fixed['result']['continuity']['initial_consistency']['status'],'joint_position')
        self.assertEqual(row['metrics']['initial_latency_s'],0.)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    RepairEvidence.contract=load(a.output/'contract.json');RepairEvidence.state=load(a.output/'state.json')
    unittest.main(argv=['perception_repair_checks'],verbosity=2)
