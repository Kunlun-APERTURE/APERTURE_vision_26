"""Frozen, short physical-motion scenarios for the optional flat range."""
import math

from focus_scenarios import FOCUS, DURATIONS as FOCUS_DURATIONS
from movement_scenarios import NEW_SCENES, DURATION

HIGHSPEED=('hs_static','hs_cross_1','hs_cross_2','hs_cross_3','hs_reverse_3','hs_turn90_3',
           'hs_stop_start_3','hs_spin_pos','hs_spin_neg','hs_spin_move_pos','hs_spin_move_neg','hs_both_2')
HIGHSPEED=HIGHSPEED+FOCUS+NEW_SCENES
DURATIONS={s:(6 if s=='hs_static' else 8 if s in ('hs_spin_pos','hs_spin_neg') else 10 if s.startswith('hs_cross_') else 12) for s in HIGHSPEED if s not in FOCUS}
DURATIONS.update(FOCUS_DURATIONS)
DURATIONS.update({s:DURATION for s in NEW_SCENES})


def properties(scene):
    if scene in NEW_SCENES:
        from movement_scenarios import definition
        return definition(scene)
    if scene in FOCUS:
        from focus_scenarios import properties as focus_properties
        return focus_properties(scene)
    if scene not in HIGHSPEED:raise ValueError(scene)
    spin='spin' in scene
    moving=scene not in ('hs_static','hs_spin_pos','hs_spin_neg')
    speed=float(scene[-1]) if scene.startswith('hs_cross_') else 2. if scene in ('hs_both_2','hs_spin_move_pos','hs_spin_move_neg') else 3. if moving else 0.
    omega=(-3. if scene.endswith('_neg') else 3.) if spin else 0.
    # Establish the track at 5 m before accelerating into a lateral pass.
    # Preflight B's 12 m cold-start never acquired; retain it as design evidence.
    y=0. if scene.startswith('hs_cross_') else -3. if moving else 0.
    return dict(speed=speed,omega=omega,position=[5.,y],homing_yaw_deg=math.degrees(math.atan2(y,5.)),
                ego_speed=2. if scene=='hs_both_2' else 0.,change_times=[6.,8.] if scene=='hs_stop_start_3' else [6.] if scene in ('hs_reverse_3','hs_turn90_3') else [])


def semantics(scene):
    if scene in NEW_SCENES:
        from movement_scenarios import semantics as movement_semantics
        return movement_semantics(scene)
    if scene in FOCUS:
        from focus_scenarios import semantics as focus_semantics
        return focus_semantics(scene)
    p=properties(scene)
    return dict(homing_yaw_deg=p['homing_yaw_deg'],ego_motion='translation' if p['ego_speed'] else 'static',
        target_motion='translation_rotation' if p['speed'] and p['omega'] else 'rotation' if p['omega'] else 'translation' if p['speed'] else 'static',
        estimate_frame='gimbal_origin_world_aligned',velocity_reference='relative_to_translating_ego_origin',
        hit_evaluation='target_armor' if p['omega'] else 'front_armor',stress=True,
        requested_target_speed_mps=p['speed'],requested_target_yaw_rate_radps=p['omega'],requested_ego_speed_mps=p['ego_speed'])


def generated(scene,seed,duration=None):
    if scene in NEW_SCENES:
        from movement_scenarios import generated as movement_generated
        return movement_generated(scene,seed,duration)
    if scene in FOCUS:
        from focus_scenarios import generated as focus_generated
        return focus_generated(scene,seed,duration)
    p=properties(scene);duration=DURATIONS[scene] if duration is None else duration
    legs=[[],[]]
    def add(robot,t0,t1,v=0.,direction=90.,omega=0.):
        t1=min(t1,duration)
        if t1<=t0:return
        legs[robot].append(dict(t0=t0,t1=t1,type='strafe_spin' if omega else 'strafe' if v else 'static',v=v,dir_deg=direction,omega=omega))
    add(0,0,1);add(0,1,duration,p['ego_speed'],270.)
    add(1,0,1)
    if scene=='hs_reverse_3':
        add(1,1,6,3.);add(1,6,duration,3.,270.)
    elif scene=='hs_turn90_3':
        add(1,1,6,3.);add(1,6,duration,3.,0.)
    elif scene=='hs_stop_start_3':
        add(1,1,6,3.);add(1,6,8);add(1,8,duration,3.)
    else:add(1,1,duration,p['speed'],90.,p['omega'])
    text=f'id = "rm26_{scene}"\nseed = {seed}\nduration = {duration}\ntrigger = "on_load"\n'
    for robot,(x,y,yaw) in enumerate([(0.,0.,0.),(*p['position'],180.)]):
        text+=f'\n[[robot]]\nid = {robot}\nspawn = {{ x = {x}, y = {y}, z = 0.0, yaw_deg = {yaw} }}\n'
        for leg in legs[robot]:
            text+='[[robot.segment]]\n'
            for k,v in leg.items():
                if leg['type']=='static' and k in ('v','dir_deg','omega') or k=='omega' and not v:continue
                text+=f'{k} = "{v}"\n' if isinstance(v,str) else f'{k} = {v}\n'
    return text,dict(scene=scene,seed=seed,terrain='flat_range',duration_s=duration,semantics=semantics(scene),segments=legs,
        motion_change_times_s=p['change_times'],steady_windows_s=[[3.,6.]] if p['change_times'] else [[3.,duration]],
        intensity_tolerance_fraction=.15,intensity_minimum_hold_s=1.,bounds_m=[-60,60])
