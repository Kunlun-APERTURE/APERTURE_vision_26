"""Scenario/analysis invariants, including joint-rate signs and unattained commands."""
import math
import tomllib
from movement_scenarios import *
from movement_analysis import joint_good,interval_metrics

def main():
    for scene in NEW_SCENES:
        text,plan=generated(scene,121);raw=tomllib.loads(text);assert len(raw['robot'])==2
        assert generated(scene,121)==(text,plan)
        for legs in plan['segments']:
            assert legs[0]['t0']==0 and legs[-1]['t1']==9
            assert all(abs(a['t1']-b['t0'])<1e-7 for a,b in zip(legs,legs[1:]))
        assert all(5<=k['ideal_range_m']<=8 for k in plan['reference_knots'])
        assert max(abs(x) for k in plan['reference_knots'] for pos in ('target_position','ego_position') for x in k[pos])<59
        if scene.endswith('_neg'):assert all(k['target_omega']<=0 for k in plan['reference_knots'])
        if 'startstop' in scene:assert [p['target_omega'] for p in plan['phases'][1:]]==[0,definition(scene)['omega'],0,definition(scene)['omega']]
    assert len(select('advanced'))==10 and len(select('advanced',pressure=True))==14 and len(select())==10
    assert select('advanced','turn04')==['adv_turn04_pos','adv_turn04_neg']
    try:select('basic',pressure=True)
    except ValueError:pass
    else:raise AssertionError('Pressure joined basic')
    desired=dict(target_speed=4.,target_omega=3.,ego_speed=0.)
    assert joint_good(dict(speed=4.,omega=3.,ego_speed=0.),desired)
    assert not joint_good(dict(speed=4.,omega=-3.,ego_speed=0.),desired)
    alternating=[dict(t=i*.04,speed=4. if i<30 else 1.,omega=0. if i<30 else 3.,ego_speed=0.) for i in range(60)]
    joint=interval_metrics(alternating,lambda r:joint_good(r,desired),0,2.4)
    assert joint['good_s']==0 and joint['longest_hold_s']==0,'Separate speed/rate episodes incorrectly qualify'
    stable=[dict(t=i*.04,speed=4.,omega=3.,ego_speed=0.) for i in range(60)]
    assert interval_metrics(stable,lambda r:joint_good(r,desired),0,2.4)['longest_hold_s']>2
    gapped=[dict(t=t) for t in (0,.04,.08,1.,1.04,1.08)]
    assert interval_metrics(gapped,lambda r:True,0,2)['longest_hold_s']<.1
    from highspeed_scenarios import HIGHSPEED,DURATIONS,generated as legacy
    assert all(s in HIGHSPEED and DURATIONS[s]==9 for s in NEW_SCENES)
    assert legacy('hf_uniform_3',121,5)[1]['duration_s']==5
    print('All 18 scripts, tier selection, deterministic fixed pursuit, time/distance bounds, signed joint intensity and gap checks passed')
if __name__=='__main__':main()
