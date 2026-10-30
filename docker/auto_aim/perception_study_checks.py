import unittest
from perception_study import effect,quantile


def data(weight=4.,hits=9,delay=0.):
    return [dict(variant=variant,scene=scene,sensor_seed=seed,shots=10,hits=8 if variant=='baseline' else hits,
                 weight=3. if variant=='baseline' else weight,duration_s=5.,initial_latency_s=0. if variant=='baseline' else delay,
                 first_hit_s=.3,first_hit_censored=False)
            for variant in ('baseline','A') for scene in ('hf_uniform_3','hf_turn90_3') for seed in range(10)]


class StudyChecks(unittest.TestCase):
    def config(self):return dict(effect_gate=dict(minimum_shots=30),bootstrap=dict(samples=300,random_seed=42))
    def test_backend_gate_requires_gain(self):
        self.assertTrue(effect(data(),'A',self.config(),True)['passed'])
        self.assertFalse(effect(data(3.),'A',self.config(),True)['passed'])
    def test_slow_initialization_cannot_buy_hit_rate(self):
        self.assertFalse(effect(data(delay=.2),'A',self.config())['passed'])
    def test_fewer_hits_per_second_cannot_buy_weight(self):
        self.assertFalse(effect(data(hits=4),'A',self.config())['passed'])
    def test_scene_regression_is_not_hidden_by_pooling(self):
        rows=data()
        for r in rows:
            if r['variant']=='A' and r['scene']=='hf_turn90_3':r['weight']=2.
        self.assertFalse(effect(rows,'A',self.config())['passed'])
    def test_independent_unpaired_seeds_rejected(self):
        rows=data();rows[-1]['sensor_seed']=99
        with self.assertRaises(ValueError):effect(rows,'A',self.config(),True)
    def test_quantile_keeps_unrecovered_upper_tail(self):
        self.assertEqual(quantile([0]*8+[2]*2,.9),2)
    def test_zero_shots_not_success(self):
        rows=data()
        for r in rows:
            if r['variant']=='A':r.update(shots=0,hits=0,weight=0)
        self.assertFalse(effect(rows,'A',self.config(),True)['passed'])

if __name__=='__main__':unittest.main()
