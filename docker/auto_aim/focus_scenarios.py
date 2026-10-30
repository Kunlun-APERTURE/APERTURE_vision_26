"""Near-range physical high-speed diagnosis; historical highspeed-v1 stays intact."""
import math

FOCUS=('hf_static_5','hf_static_8','hf_uniform_3','hf_reverse_3','hf_turn90_3','hf_stop_start_3',
       'hf_spin_pos','hf_spin_neg','hf_spin_move_pos','hf_spin_move_neg','hf_stop_start_v4')
DURATIONS={s:6.2 if s in ('hf_stop_start_3','hf_stop_start_v4') else 5.5 if s in ('hf_reverse_3','hf_turn90_3') else 5. if s=='hf_uniform_3' else 6. for s in FOCUS}


def properties(scene):
    if scene not in FOCUS:raise ValueError(scene)
    spin='spin' in scene;moving=scene in ('hf_uniform_3','hf_reverse_3','hf_turn90_3','hf_stop_start_3','hf_stop_start_v4') or 'spin_move' in scene
    speed=2. if 'spin_move' in scene else 3. if moving else 0.
    position=[8.,0.] if scene=='hf_static_8' else [3.8,-3.8] if scene=='hf_turn90_3' else [5.,-4.4] if moving else [5.,0.]
    changes=[3.2,4.2] if scene in ('hf_stop_start_3','hf_stop_start_v4') else [3.2] if scene in ('hf_reverse_3','hf_turn90_3') else []
    return dict(speed=speed,omega=(-3. if scene.endswith('_neg') else 3.) if spin else 0.,ego_speed=0.,position=position,
        homing_yaw_deg=math.degrees(math.atan2(position[1],position[0])),change_times=changes)


def semantics(scene):
    p=properties(scene)
    return dict(homing_yaw_deg=p['homing_yaw_deg'],ego_motion='static',
        target_motion='translation_rotation' if p['speed'] and p['omega'] else 'rotation' if p['omega'] else 'translation' if p['speed'] else 'static',
        estimate_frame='gimbal_origin_world_aligned',velocity_reference='relative_to_translating_ego_origin',
        hit_evaluation='target_armor' if p['omega'] else 'front_armor',stress=True,
        requested_target_speed_mps=p['speed'],requested_target_yaw_rate_radps=p['omega'],requested_ego_speed_mps=0.,focus_range_m=[5.,8.])


def generated(scene,seed,duration=None):
    p=properties(scene);duration=DURATIONS[scene] if duration is None else duration;legs=[[],[]]
    def add(robot,a,b,v=0.,direction=90.,omega=0.):
        b=min(b,duration)
        if b>a:legs[robot].append(dict(t0=a,t1=b,type='strafe_spin' if omega else 'strafe' if v else 'static',v=v,dir_deg=direction,omega=omega))
    add(0,0,duration);add(1,0,1)
    if scene=='hf_reverse_3':add(1,1,3.2,3.);add(1,3.2,duration,3.,270.)
    elif scene=='hf_turn90_3':add(1,1,3.2,3.,45.);add(1,3.2,duration,3.,135.)
    elif scene in ('hf_stop_start_3','hf_stop_start_v4'):add(1,1,3.2,3.);add(1,3.2,4.2);add(1,4.2,duration,3.,270. if scene=='hf_stop_start_v4' else 90.)
    else:add(1,1,duration,p['speed'],90.,p['omega'])
    text=f'id = "rm26_{scene}"\nseed = {seed}\nduration = {duration}\ntrigger = "on_load"\n'
    for robot,(x,y,yaw) in enumerate([(0.,0.,0.),(*p['position'],180.)]):
        text+=f'\n[[robot]]\nid = {robot}\nspawn = {{ x = {x}, y = {y}, z = 0.0, yaw_deg = {yaw} }}\n'
        for leg in legs[robot]:
            text+='[[robot.segment]]\n'
            for key,value in leg.items():
                if leg['type']=='static' and key in ('v','dir_deg','omega') or key=='omega' and not value:continue
                text+=f'{key} = "{value}"\n' if isinstance(value,str) else f'{key} = {value}\n'
    return text,dict(scene=scene,seed=seed,terrain='flat_range',duration_s=duration,semantics=semantics(scene),segments=legs,
        motion_change_times_s=p['change_times'],steady_windows_s=[[2.,3.2]] if p['change_times'] else [[2.,duration]],
        focus_windows_s=[[2.,duration]] if not p['change_times'] else [[t-.5,min(t+2,duration)] for t in p['change_times']],
        focus_range_m=[5.,8.],pre_maneuver_speed_hold_s=.5,intensity_tolerance_fraction=.15,intensity_minimum_hold_s=1.,bounds_m=[-60,60])
