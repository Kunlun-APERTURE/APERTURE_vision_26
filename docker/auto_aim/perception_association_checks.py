"""Wire-level checks for a shared pose-hint / correction association."""
import argparse
import math
from pathlib import Path
import unittest

from report import load
from sync_first_divergence_analysis import primary
from perception_association import FIELDS


class AssociationEvidence(unittest.TestCase):
    def test_native_reproduction(self):
        rr=[r for r in self.state['replays'] if r['native_configuration']]
        self.assertEqual(len(rr),24)
        for r in rr:
            result=load(Path(r['output'])/'result.json')
            for field in FIELDS:self.assertEqual(result['differences_from_recorded'][field]['different_frames'],0)

    def test_identical_image_pose_clock_inputs(self):
        self.assertEqual(len(self.state['replays']),82)
        for e in self.contract['sources']:
            rr=[r for r in self.state['replays'] if r['source_run']==e['run_id']]
            self.assertEqual(len({r['input_sha256'] for r in rr}),1)
            self.assertTrue({'baseline','S','BS'}.issubset({r['variant'] for r in rr}))
            for r in rr:
                self.assertTrue(all(p['frame'].get('replay_clocks') for p in primary(Path(r['output']))))

    def test_shared_identity_for_hint_and_updates(self):
        checked=0;active=0
        for r in self.state['replays']:
            if r['variant'] not in ('S','BS'):continue
            for p in primary(Path(r['output'])):
                d=p['result'].get('continuity',{});links=d.get('image_association')
                if links is None:continue
                active+=1;visible=set(links['visible_plate_ids'])
                assignment={tuple(o['image_center']):o['plate_id'] for o in links['observations'] if o['plate_id']>=0}
                self.assertEqual(len(assignment.values()),len(set(assignment.values())))
                filt=d.get('filter') or {};selected=d.get('selected_associations',[])
                for row in selected:
                    checked+=1;plate=row['plate_id']
                    self.assertIn(plate,visible)
                    self.assertEqual(assignment[tuple(row['image_center'])],plate)
                    self.assertIsNotNone(row['supplied_hint'])
                    expected=filt['state_before'][6]+plate*math.pi/2
                    error=math.remainder(row['supplied_hint']-expected,2*math.pi)
                    self.assertLess(abs(error),1e-8)
                for update in filt.get('updates',[]):
                    self.assertIn(update['plate_id'],{row['plate_id'] for row in selected})
        self.assertGreater(active,0);self.assertGreater(checked,0)

    def test_s_preserves_original_initialization(self):
        for e in self.contract['sources']:
            rows={r['variant']:r for r in self.state['replays'] if r['source_run']==e['run_id']}
            a=primary(Path(rows['baseline']['output']))[0]['result']
            b=primary(Path(rows['S']['output']))[0]['result']
            self.assertEqual(a['filter_state'],b['filter_state'])

    def test_no_old_guards_in_candidates(self):
        for variant in ('S','BS'):
            meta=load(Path(self.contract['bundles'][variant])/'bundle.json')['perception_options']
            self.assertTrue(meta['image_association'])
            self.assertFalse(meta.get('initial_handoff_guard',False))
            self.assertFalse(meta.get('refinement_guard',False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    AssociationEvidence.contract=load(a.output/'contract.json');AssociationEvidence.state=load(a.output/'state.json')
    unittest.main(argv=['perception_association_checks'],verbosity=2)
