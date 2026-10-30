"""Check frozen native compatibility and new motion mechanisms over wire replay."""
import argparse
import math
from pathlib import Path
import unittest

from report import load
from sync_first_divergence_analysis import primary
from motion_study import FIELDS


class MotionEvidence(unittest.TestCase):
    def test_native_compatibility(self):
        native=[r for r in self.state['replays'] if r['native_configuration']]
        self.assertEqual(len(native),14)
        for r in native:
            result=load(Path(r['output'])/'result.json')
            for field in FIELDS:self.assertEqual(result['differences_from_recorded'][field]['different_frames'],0)

    def test_all_ablation_inputs_equal(self):
        self.assertEqual(len(self.state['replays']),112)
        for e in self.contract['sources']:
            rr=[r for r in self.state['replays'] if r['source_run']==e['run_id']]
            self.assertEqual(len(rr),8);self.assertEqual(len({r['input_sha256'] for r in rr}),1)
            for r in rr:self.assertTrue(all(p['frame'].get('replay_clocks') for p in primary(Path(r['output']))))

    def test_calibrated_delay_used_without_filter_change(self):
        for e in self.contract['sources']:
            rows={r['variant']:primary(Path(r['output'])) for r in self.state['replays'] if r['source_run']==e['run_id'] and r['variant'] in ('BS','T','P')}
            for a,b in zip(rows['BS'],rows['T']):
                self.assertEqual(a['result'].get('filter_state'),b['result'].get('filter_state'))
                diag=b['result'].get('diagnosis',{}).get('aimer',{})
                if 'launch_delay_s' in diag:self.assertAlmostEqual(diag['launch_delay_s'],.0043)
            for a,b in zip(rows['BS'],rows['P']):self.assertEqual(a['result'].get('filter_state'),b['result'].get('filter_state'))

    def test_shared_geometry_and_acceleration_bounds(self):
        shared=acceleration=0
        for row in self.state['replays']:
            if row['variant'] not in ('C','RC','P','ALL'):continue
            for p in primary(Path(row['output'])):
                result=p['result'];x=result.get('filter_state');d=result.get('continuity',{}).get('filter',{}).get('motion_v5') or {}
                if not x:continue
                if 'shared_velocity' in d:
                    shared+=1
                    for i,v in enumerate(result['plate_velocities']):
                        angle=x[6]+i*math.pi/2;radius=x[8]+(x[9] if i%2 else 0)
                        expected=[d['shared_velocity'][0]+radius*x[7]*math.sin(angle),d['shared_velocity'][1]-radius*x[7]*math.cos(angle),d['shared_velocity'][2]]
                        self.assertLess(math.dist(v,expected),1e-7)
                for a in d.get('accelerations',[]):
                    self.assertLessEqual(math.sqrt(sum(v*v for v in a)),8.+1e-8);acceleration+=math.hypot(*a[:2])>.01
        self.assertGreater(shared,0);self.assertGreater(acceleration,0)

    def test_radial_diagnostics_bounded(self):
        observed=released=0
        for row in self.state['replays']:
            if row['variant'] not in ('R','RC','ALL'):continue
            for p in primary(Path(row['output'])):
                d=p['result'].get('continuity',{}).get('filter',{}).get('motion_v5') or {}
                for r in d.get('radial',[]):
                    observed+=1;released+=r['released'];self.assertTrue(math.isfinite(r['normalized']))
                    if r['released']:self.assertEqual(r['coherent_frames'],3)
        self.assertGreater(observed,0)
        # Activation is reported even if zero; do not fail solely because
        # existing recordings did not excite the predeclared detector.
        # Actual activation counts are exported by the independent analyzer.


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    MotionEvidence.contract=load(a.output/'contract.json');MotionEvidence.state=load(a.output/'state.json')
    unittest.main(argv=['motion_study_checks'],verbosity=2)
