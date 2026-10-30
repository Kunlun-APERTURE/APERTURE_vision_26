"""Protect pairing, statistical interpretation and immutable algorithm snapshots."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from decision_benchmark import freeze_variant, sha, verify_bundle
from decision_benchmark_replay import centroid, hybrid, match_observations
from decision_benchmark_report import aggregate, behavior, compare, direction, same_contract, unified_score


def contract():
    return dict(variants=['v3', 'v3_no_refine'], bootstrap=dict(random_seed=7, samples=300),
                decision=dict(primary='weighted_hit_rate', weighted_practical_margin=.03,
                              hits_per_s_practical_margin=.1, interaction_margin=.05,
                              minimum_pairs=5, minimum_shots_per_cell=30))


def trials(backend_effect=10):
    return [dict(engine=e, variant=v, sensor_seed=seed, shots=100, hits=50,
                 duration_s=10, weight=30 + (10 if e == 'ui' else backend_effect) * (v != 'v3'),
                 comparison_contract=dict(scene='same', scoring_version=2))
            for e in ('ui', 'backend') for v in ('v3', 'v3_no_refine') for seed in range(5)]


class BenchmarkChecks(unittest.TestCase):
    def test_paired_gain_transfers(self):
        result = compare(trials(), contract())
        self.assertEqual(result['ranking_transfer'], 'supported')
        self.assertEqual(result['directions']['ui']['weighted_hit_rate'], 'gain')
        self.assertTrue(result['effect_magnitude_agreement'])

    def test_clear_reversal_is_not_accepted(self):
        self.assertEqual(compare(trials(-10), contract())['ranking_transfer'], 'reversed')

    def test_same_point_sign_with_wide_intervals_is_uncertain(self):
        self.assertEqual(direction([-.2, .3], .03), 'uncertain')
        data = trials()
        for r in data:
            if r['variant'] != 'v3':
                r['weight'] = 30 + [-20, -15, 0, 20, 30][r['sensor_seed']]
        self.assertEqual(compare(data, contract())['ranking_transfer'], 'inconclusive')

    def test_unequal_seed_sets_are_not_pooled(self):
        data = trials(); data[0]['sensor_seed'] = 99
        with self.assertRaises(ValueError): same_contract(data)

    def test_duplicate_seed_does_not_raise_sample_size(self):
        data = trials(); data.append(copy.deepcopy(data[0]))
        with self.assertRaises(ValueError): same_contract(data)

    def test_changed_scene_or_scoring_refuses_comparison(self):
        data = trials(); data[0]['comparison_contract']['scoring_version'] = 1
        with self.assertRaises(ValueError): same_contract(data)

    def test_zero_shots_and_undefined_bootstrap_are_inconclusive(self):
        data = trials()
        for r in data: r.update(shots=0, hits=0, weight=0)
        self.assertIsNone(aggregate(data)['weighted_hit_rate'])
        result = compare(data, contract())
        self.assertEqual(result['ranking_transfer'], 'inconclusive')
        self.assertIsNone(result['intervals_95']['ui']['weighted_hit_rate'])

    def test_shot_weighted_aggregation_not_mean_of_run_rates(self):
        result = aggregate([dict(shots=1, hits=1, weight=1., duration_s=5),
                            dict(shots=9, hits=0, weight=0., duration_s=5)])
        self.assertEqual(result['weighted_hit_rate'], .1)
        self.assertEqual(result['hits_per_s'], .1)

    def test_freeze_ablation_does_not_mutate_hardlinked_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / 'base'; config = base / 'sp_vision_25/configs/simulator.yaml'
            config.parent.mkdir(parents=True); config.write_text('robustness_v3:\n  color_refinement: true\n')
            (base / 'bundle.json').write_text(json.dumps(dict(files={str(config.relative_to(base)): sha(config)})))
            before = sha(config)
            with patch('decision_benchmark.jobs.STATE', Path(directory) / 'state'):
                variant = freeze_variant(base, 'v3_no_refine')
            self.assertEqual(sha(config), before)
            self.assertIn('color_refinement: false', (variant / config.relative_to(base)).read_text())
            verify_bundle(base); verify_bundle(variant)

    def test_correspondence_ignores_confidence_order(self):
        def detection(x, conf):
            return dict(name='three', type='small', color='blue', confidence=conf,
                        corners=[[x, 0], [x+10, 0], [x+10, 5], [x, 5]])
        a = [detection(0, .9), detection(60, .8)]
        b = [detection(61, .95), detection(1, .85)]
        paired, order = match_observations(a, b)
        self.assertEqual(order, [1, 0])
        changed = hybrid(a, paired, confidence=True)
        self.assertEqual(changed[0]['corners'], a[0]['corners'])
        self.assertEqual(changed[0]['confidence'], .85)

    def test_shape_and_translation_interventions_are_separate(self):
        a = [dict(name='three', type='small', color='blue', confidence=.9,
                  corners=[[0, 0], [10, 0], [10, 5], [0, 5]])]
        b = copy.deepcopy(a); b[0]['corners'] = [[2, 1], [14, 1], [14, 8], [2, 8]]
        translated = hybrid(a, b, geometry='translation')[0]
        shape = hybrid(a, b, geometry='shape')[0]
        self.assertEqual(centroid(translated), centroid(b[0]))
        self.assertEqual(centroid(shape), centroid(a[0]))
        self.assertEqual(translated['corners'][1][0]-translated['corners'][0][0], 10)
        self.assertEqual(shape['corners'][1][0]-shape['corners'][0][0], 12)

    def test_mixed_native_scoring_is_normalized_without_modifying_sources(self):
        from decision_benchmark import manage
        from shots import score
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); folder = root / 'native'; folder.mkdir()
            (folder / 'telemetry.jsonl').write_text(json.dumps(dict(kind='armors', data=dict(
                armors=[dict(id=10, robot_id=1, slot=0)]))) + '\n')
            events = [dict(kind=0, shot_seq=1, stamp_ns=1_000_000_000, shooter_id=0,
                           world_point=[0, 0, 1], velocity=[25, 0, 0]),
                      dict(kind=1, shot_seq=1, stamp_ns=1_100_000_000, target_id=1, armor_id=11,
                           contact_point=[2, 0, 1], flight_time_s=.1, impact_geometry_valid=True,
                           plate_center=[2, 0, 1], plate_right=[0, 1, 0], plate_up=[0, 0, 1], plate_size=[.2, .1])]
            m = dict(mode='shoot', duration_s=2, impact_quality_version=1, scoring_version=1,
                     scenario_semantics=dict(hit_evaluation='front_armor'), shooting={})
            state = dict(shot_events=events, measurement_start_stamp_ns=0, measurement_end_stamp_ns=2_000_000_000)
            (folder / 'manifest.json').write_text(json.dumps(m))
            (folder / 'run-state.json').write_text(json.dumps(state))
            raw, _ = score(folder, m, state)
            (folder / 'summary.json').write_text(json.dumps(dict(metrics=raw)))
            originals = {p.name: sha(p) for p in folder.iterdir()}
            c = dict(runtime_files={'docker/auto_aim/' + p: sha(manage.OPS / p)
                                    for p in ('shots.py', 'impact_quality.py')})
            actual = unified_score(folder, root / 'scored', c)
            self.assertEqual(raw['weighted_hit_rate'], 0)
            self.assertEqual(actual['metrics']['weighted_hit_rate'], 1)
            self.assertEqual(actual['metrics']['target_hit_rate'], raw['target_hit_rate'])
            self.assertEqual(originals, {p.name: sha(p) for p in folder.iterdir()})
            with self.assertRaises(ValueError): unified_score(folder, folder, c)

    def test_teardown_tail_is_not_mistaken_for_measurement_nondeterminism(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fingerprints = []
            for name, tail in [('one', [110]), ('two', [110, 120, 130])]:
                folder = root / name; (folder / 'session-evidence').mkdir(parents=True)
                (folder / 'run-state.json').write_text(json.dumps(dict(measurement_end_stamp_ns=100)))
                rows = [dict(kind='input', jpeg_sha256=str(stamp), data=dict(stamp_ns=stamp,
                    sensor=dict(index=i, phase=1, seed=1801))) for i, stamp in enumerate([90] + tail)]
                (folder / 'session-evidence/events.jsonl').write_text(''.join(json.dumps(p)+'\n' for p in rows))
                fingerprints.append(behavior(folder))
            self.assertEqual(*fingerprints)


if __name__ == '__main__':
    unittest.main()
