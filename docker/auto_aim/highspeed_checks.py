#!/usr/bin/env python3
"""Focused checks for impact quality, terrain isolation and short scenarios."""
import json,math,tomllib
from pathlib import Path
from impact_quality import impact_coordinates,summarize
from highspeed_scenarios import HIGHSPEED,DURATIONS,generated
import manage


def check():
    checks=[]
    geometry=dict(impact_geometry_valid=True,plate_center=[2,3,4],plate_right=[1,0,0],plate_up=[0,0,1],plate_size=[.2,.1])
    for point,weight in [([2,3,4],1.),([2.05,3,4],.5),([2,3,4.025],.5),([2.1,3,4],0.),([2.15,3,4],0.)]:
        assert abs(impact_coordinates(point,geometry)['impact_center_proximity']-weight)<1e-12
    checks.append('center/halfway/edge/outside')
    rotated=dict(geometry,plate_right=[0,1,0],plate_center=[8,-7,5])
    assert abs(impact_coordinates([8,-6.95,5],rotated)['impact_center_proximity']-.5)<1e-12
    larger=dict(geometry,plate_size=[.4,.2]);assert abs(impact_coordinates([2.1,3,4],larger)['impact_center_proximity']-.5)<1e-12
    checks.append('rotated/translated/large armor')
    assert impact_coordinates([0,0,0],dict(geometry,impact_geometry_valid=False)) is None
    assert impact_coordinates([0,0,0],dict(geometry,plate_right=[2,0,0])) is None
    assert impact_coordinates([0,0,0],dict(geometry,plate_up=[1,0,0])) is None
    assert impact_coordinates([float('nan'),0,0],geometry) is None
    assert summarize([])['weighted_hit_rate'] is None
    rows=[dict(outcome='target_armor',useful_hit=True,**impact_coordinates([2,3,4],geometry)),dict(outcome='no_hit_event',useful_hit=False)]
    assert summarize(rows)['weighted_hit_rate']==.5
    assert summarize([dict(outcome='target_armor',useful_hit=True)])['weighted_hit_rate'] is None
    assert summarize(rows,False)['weighted_hit_rate'] is None
    checks.append('miss denominator/missing evidence/legacy/zero shots')
    for scene in HIGHSPEED:
        text,plan=generated(scene,2026102104,DURATIONS[scene]);d=tomllib.loads(text)
        assert d['duration']==DURATIONS[scene]<=12
        for robot in d['robot']:
            assert robot['spawn']['z']==0
            legs=robot['segment'];assert legs[0]['t0']==0 and legs[-1]['t1']==d['duration']
            assert all(a['t1']==b['t0'] for a,b in zip(legs,legs[1:]))
            assert all(s['t0']<s['t1'] for s in legs)
        assert plan['terrain']=='flat_range'
    checks.append('12 deterministic contiguous bounded scenarios')
    settings=dict(physics='no-drag',bullet_speed=25.,gimbal_profile='feedforward')
    arena=tomllib.loads(manage.render_config(settings));flat=tomllib.loads(manage.render_config(dict(settings,terrain='flat_range')))
    assert arena['scene']['terrain']=='arena' and flat['scene']['terrain']=='flat_range'
    for key in ('vehicle','physics','projectile'):assert arena[key]==flat[key]
    checks.append('terrain defaults/isolation/preserved physics')
    frozen=json.loads((manage.OPS/'experiments/highspeed-v1-20261021/algorithm-frozen.json').read_text())
    import hashlib
    assert all(hashlib.sha256((manage.ROOT/name).read_bytes()).hexdigest()==digest for name,digest in frozen.items())
    checks.append('algorithm/config/models/user test unchanged')
    return dict(passed=True,checks=checks)


if __name__=='__main__':print(json.dumps(check(),ensure_ascii=False,indent=2))
