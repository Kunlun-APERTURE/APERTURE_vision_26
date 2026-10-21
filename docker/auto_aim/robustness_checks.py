#!/usr/bin/env python3
"""Boundary checks for generated experiments, including independence and scoring units."""
import json,math,tomllib,tempfile
from pathlib import Path
from command_script import CommandScript
from robustness_prediction import evaluate as evaluate_prediction
from robustness_report import command_change_times
from scenario_catalog import generated,EXTRA,semantics
from report import quantile


def check():
    tests=[]
    for scene in EXTRA:
        for seed in (2026102001,2026102101):
            duration=6 if scene.startswith('distance_') else 18
            text,plan=generated(scene,seed,duration);value=tomllib.loads(text)
            assert generated(scene,seed,duration)[0]==text
            for robot in value['robot']:
                segments=robot['segment'];assert segments[0]['t0']==0 and segments[-1]['t1']==duration
                assert all(a['t1']==b['t0'] for a,b in zip(segments,segments[1:]));assert all(a['t1']>a['t0'] for a in segments)
                if scene.startswith(('random_both','spin_move')) and (robot['id']==1 or 'random_both' in scene or 'ego' in scene):
                    x=y=0
                    for s in segments:
                        if s['type']=='static':continue
                        dt=s['t1']-s['t0'];d=math.radians(s['dir_deg']);x+=s['v']*dt*math.cos(d);y+=s['v']*dt*math.sin(d)
                        assert abs(x)<=.751 and abs(y)<=1.251,(scene,seed,x,y)
            tests.append(scene+':'+str(seed))
    _,plan=generated('random_both_10',2026102101)
    assert plan['segments'][0]!=plan['segments'][1]
    assert generated('random_both_10',2026102101)[0]!=generated('random_both_10',2026102102)[0]
    assert semantics('random_both_10')['ego_motion']=='translation'
    assert semantics('spin_move_ego_15')['hit_evaluation']=='target_armor'
    assert quantile([.1,.8],.9)==.8
    assert command_change_times(generated('distance_3',2026102001,6)[1])==[]
    assert command_change_times(generated('range_in_out',2026102001)[1])==[2.,9.]
    assert command_change_times(generated('spin_move_ego_15',2026102001)[1])[0]==2.
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'commands.json';path.write_text(json.dumps(dict(commands=[dict(time_s=0.,valid=True,yaw=.1,pitch=.01,distance=4.)],fire_times_s=[.2,.4])))
        script=CommandScript(path)
        assert not script.at(.19)['fire_advice'] and script.at(.2)['fire_advice']
        script.fired();assert not script.at(.3)['fire_advice'] and script.at(.4)['fire_advice']
        script.reset();assert script.at(.51)['fire_advice']==False and script.expired_fires==2
        script.reset();assert script.next_fire==0
    with tempfile.TemporaryDirectory() as tmp:
        folder=Path(tmp);stamp=1_000_000_000;future=1_100_000_000
        packets=[]
        for tick,point,own in [(stamp,[14.,0.,0.],[9.9,0.,0.]),(future,[14.4,0.,0.],[10.1,0.,0.])]:
            packets.append(dict(kind='armors',data=dict(stamp_ns=tick,armors=[dict(id=10,robot_id=1,position=point)])))
            packets.append(dict(kind='robots',data=dict(stamp_ns=tick,robots=[dict(id=0,position=own)])))
        (folder/'telemetry.jsonl').write_text(''.join(json.dumps(p)+'\n' for p in packets))
        observations=[dict(frame=dict(stamp_ns=stamp,seq=1,gimbal_position_map=[10.,0.,0.]),
            result=dict(track_estimate=dict(plates=[[4.,0.,0.]]),selected_plate_id=0,aim_point=[4.2,0.,0.],prediction_horizon_s=.1))]
        result=evaluate_prediction(folder,observations,dict(start=stamp,end=stamp))
        assert result['matched_prediction_frames']==1 and result['future_point_error_p95_m']<1e-10
        for packet in packets:
            if packet['kind']=='armors':packet['data']['armors'].append(dict(id=11,robot_id=1,position=[v+.01 if i==0 else v for i,v in enumerate(packet['data']['armors'][0]['position'])]))
        (folder/'telemetry.jsonl').write_text(''.join(json.dumps(p)+'\n' for p in packets))
        result=evaluate_prediction(folder,observations,dict(start=stamp,end=stamp))
        assert result['matched_prediction_frames']==0 and result['matching_coverage']==0
    return dict(passed=True,generated_cases=len(tests),checks=['determinism','seed sensitivity','actor independence','continuous timelines','nominal bounds','explicit semantics','nearest rank tail','fixed-script pulse consumption, expiry, reset','future moving-origin compensation','ambiguous physical-plate exclusion'])

if __name__=='__main__':print(json.dumps(check(),ensure_ascii=False))
