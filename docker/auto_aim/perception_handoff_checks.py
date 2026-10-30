"""Check real wire replay handoff behavior without using target truth."""
import argparse
import math
from pathlib import Path
import unittest

from report import load
from sync_first_divergence_analysis import primary
from perception_handoff import FIELDS


class HandoffEvidence(unittest.TestCase):
    def test_native_configuration_unchanged(self):
        rows=[r for r in self.state['replays'] if r['native_configuration']]
        self.assertEqual(len(rows),20)
        for r in rows:
            data=load(Path(r['output'])/'result.json')
            for k in FIELDS:self.assertEqual(data['differences_from_recorded'][k]['different_frames'],0,(r['source_run'],k))

    def test_same_input_clocks(self):
        self.assertEqual(len(self.state['replays']),52)
        comparisons=load(self.root/'replay-result.json')['comparisons']
        for r in self.state['replays']:
            packets=primary(Path(r['output']))
            self.assertTrue(all(p['frame'].get('replay_clocks') for p in packets))
            if r['variant']=='H3':
                comparison=next(c for c in comparisons if c['source_run']==r['source_run'])
                ref=load(Path(comparison['reference_output'])/'result.json')
                self.assertEqual(r['input_sha256'],ref['primary_input_sha256'])

    def test_bounded_handoff_and_anchor_updates(self):
        active=0;blocked=0
        for r in self.state['replays']:
            if r['variant']!='H3':continue
            remaining=0
            for p in primary(Path(r['output'])):
                result=p['result'];d=result.get('continuity',{});initial=d.get('initial_consistency') or {}
                if d.get('initialized'):
                    remaining=3 if initial.get('status')=='joint_position' else 0
                    continue
                filt=d.get('filter') or {}
                if d.get('state')=='lost' and not filt:continue
                observed=filt.get('initial_handoff_remaining',0)
                self.assertEqual(observed,remaining,(r['source_run'],p['frame']['seq']))
                if observed:
                    active+=1
                    for update in filt.get('updates',[]):
                        self.assertEqual(update['plate_id'],0)
                        if update['accepted']:self.assertLessEqual(update['nis_prior'],13.276704135987622)
                    remaining-=1
                blocked+=len(d.get('handoff_rejections',[]))
        self.assertGreater(active,0);self.assertGreater(blocked,0)

    def test_preserves_joint_first_state(self):
        rows=[r for r in self.state['replays'] if r['source_variant']=='Bfix' and r['engine']=='backend']
        sources=sorted({r['source_run'] for r in rows})
        self.assertEqual(len(sources),10)
        for source in sources:
            rr={r['variant']:primary(Path(r['output'])) for r in rows if r['source_run']==source}
            self.assertEqual(rr['H3'][0]['result']['filter_state'],rr['Bfix'][0]['result']['filter_state'])

    def test_actual_failure_second_frame_excludes_opposite_slot(self):
        r=next(r for r in self.state['replays'] if r['variant']=='H3' and r['source_variant']=='Bfix'
            and r['scene']=='hf_uniform_3' and r['sensor_seed']==2104)
        rr=primary(Path(r['output']));second=rr[1]['result']['continuity']
        self.assertEqual(second['filter']['initial_handoff_remaining'],3)
        self.assertTrue(all(u['plate_id']==0 for u in second['filter']['updates']))
        self.assertTrue(any(p['plate_id']==2 and p['anchor_only'] for p in second['handoff_rejections']))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    HandoffEvidence.root=a.output;HandoffEvidence.contract=load(a.output/'contract.json');HandoffEvidence.state=load(a.output/'state.json')
    unittest.main(argv=['perception_handoff_checks'],verbosity=2)
