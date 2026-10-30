"""Checks of review coverage, oracle ceiling identities and source immutability."""
import argparse
import json
import math
from pathlib import Path
import unittest

from decision_benchmark import sha
from perception_study import protected
from report import load
import manage


class ReviewEvidence(unittest.TestCase):
    def test_coverage_accounting_and_strict_subset(self):
        a=load(self.root/'result.json');b=load(self.root/'sensitivity-result.json')
        x=a['counts'];y=b['counts']
        self.assertEqual(sum(x.values()),2*x['measured_frames']) # measured count plus exclusive categories.
        self.assertEqual(y['accepted']+y['selected_not_observed']+y['future_beyond_measurement']+y['invalid_forecast'],y['measured_frames'])
        def keys(name):return {(r['run_id'],r['seq']) for r in (json.loads(l) for l in (self.root/name).open())}
        self.assertTrue(keys('frame-budget.jsonl')<keys('sensitivity-frame-budget.jsonl'))
        self.assertEqual(len(keys('frame-budget.jsonl')),x['accepted'])

    def test_perfect_stationary_state_has_no_cv_error(self):
        for file in ('result.json','sensitivity-result.json'):
            r=load(self.root/file)
            for g in r['groups']:
                if g['phase']=='stationary':
                    self.assertLess(g['errors']['true_position_and_velocity']['p95'],.002)

    def test_finite_budget_and_all_paths(self):
        rows=[json.loads(l) for l in (self.root/'sensitivity-frame-budget.jsonl').open()]
        self.assertEqual(len({r['run_id'] for r in rows}),20)
        self.assertTrue(all(0<r['horizon_s']<1 and all(math.isfinite(v) and v>=0 for v in r['errors_m'].values()) for r in rows))

    def test_sources_and_production_unchanged(self):
        c=load(self.root/'contract.json');self.assertEqual(protected(),c['protected_before'])
        for e in c['sources']:
            folder=manage.OPS/'experiments'/e['run_id']
            self.assertTrue(all(sha(folder/n)==h for n,h in e['source_hashes'].items()))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    ReviewEvidence.root=a.output;unittest.main(argv=['prediction_review_checks'],verbosity=2)
