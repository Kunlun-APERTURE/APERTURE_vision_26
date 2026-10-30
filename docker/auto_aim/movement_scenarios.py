"""Versioned basic/advanced motion suites; world-frame velocity scripts only."""
import math

ADVANCED_CASES=('variable','turn08','turn04','spin_startstop','pursuit')
ADVANCED=tuple('adv_'+case+'_'+sign for case in ADVANCED_CASES for sign in ('pos','neg'))
PRESSURE=tuple('adv_'+case+'_fast_'+sign for case in ('variable','pursuit') for sign in ('pos','neg'))
BASIC_COMBINED=tuple('basic_move'+str(speed)+'_spin_'+sign for speed in (3,4) for sign in ('pos','neg'))
NEW_SCENES=BASIC_COMBINED+ADVANCED+PRESSURE
BASIC=('hf_static_5','hf_uniform_3','hf_turn90_3','hf_spin_pos','hf_spin_neg','ego_right_centered')+BASIC_COMBINED
DURATION=9.
DT=.1

def select(tier='basic',cases=None,pressure=False):
    if tier not in ('basic','advanced'):raise ValueError('Unknown test tier')
    if tier=='basic':
        if pressure:raise ValueError('Pressure cases belong to advanced tier')
        available=list(BASIC);allowed=set(available)
        chosen=available if not cases else cases.split(',') if isinstance(cases,str) else list(cases)
        if not set(chosen)<=allowed:raise ValueError('Unknown basic case')
        return list(dict.fromkeys(chosen))
    chosen=list(ADVANCED_CASES) if not cases else cases.split(',') if isinstance(cases,str) else list(cases)
    if not set(chosen)<=set(ADVANCED_CASES):raise ValueError('Unknown advanced case')
    scenes=['adv_'+case+'_'+sign for case in chosen for sign in ('pos','neg')]
    if pressure:scenes += ['adv_'+case+'_fast_'+sign for case in chosen if case in ('variable','pursuit') for sign in ('pos','neg')]
    return scenes

def definition(scene):
    if scene not in NEW_SCENES:raise ValueError('Unknown movement scene')
    kind=next((k for k in ADVANCED_CASES if scene.startswith('adv_'+k+'_')),'combined')
    speed=float(scene[len('basic_move')]) if kind=='combined' else 4.
    return dict(scene=scene,tier='basic' if kind=='combined' else 'advanced',case=kind,
        pressure='_fast_' in scene,sign=-1 if scene.endswith('_neg') else 1,
        speed=speed,omega=(6. if '_fast_' in scene else 3.)*(-1 if scene.endswith('_neg') else 1),
        ego_speed=4. if kind=='pursuit' else 0.,position=[6.5,0.],homing_yaw_deg=0.,
        change_times=[3.,5.,7.] if kind in ('variable','spin_startstop','pursuit') else
            [round(1+i*(.8 if kind=='turn08' else .4),6) for i in range(1,20 if kind=='turn04' else 10)] if kind.startswith('turn') else [])

def semantics(scene):
    p=definition(scene)
    return dict(homing_yaw_deg=0.,ego_motion='translation' if p['ego_speed'] else 'static',
        target_motion='translation_rotation',estimate_frame='gimbal_origin_world_aligned',
        velocity_reference='relative_to_translating_ego_origin',hit_evaluation='target_armor',stress=True,
        requested_target_speed_mps=p['speed'],requested_target_yaw_rate_radps=p['omega'],
        requested_ego_speed_mps=p['ego_speed'],test_tier=p['tier'],test_case=p['case'],pressure=p['pressure'])

def generated(scene,seed,duration=None):
    p=definition(scene);duration=DURATION if duration is None else duration
    if abs(duration-DURATION)>1e-6:raise ValueError('Movement v1 scenarios require frozen 9 second measurement')
    legs=[[],[]];reference=[[0.,0.],[6.5,0.]];knots=[];theta=0.
    for robot in (0,1):legs[robot].append(dict(t0=0.,t1=1.,type='static'))
    # Short straight segments approximate the same precomputed reference arc for both algorithms.
    # No actual pose or truth feeds back into the script.
    for i in range(80):
        t=round(1+i*DT,6);end=round(t+DT,6);stage=min(3,int((t-1+1e-6)//2));kind=p['case']
        target_speed=[3.,4.,3.,4.][stage] if kind=='variable' else [3.5,4.,3.5,4.][stage] if kind=='pursuit' else p['speed']
        omega=([0.,p['omega'],0.,p['omega']][stage] if kind=='spin_startstop' else p['omega'])
        ego_speed=[4.,3.5,4.,3.5][stage] if kind=='pursuit' else 0.
        if kind=='pursuit':
            direction=[0.,45.,90.,45.][stage]
        else:
            direction=math.degrees((math.atan2(reference[1][1],reference[1][0]) if kind.startswith('turn') else theta)+target_speed*DT/(2*6.5))+90.
            if kind.startswith('turn'):
                interval=.8 if kind=='turn08' else .4
                index=int((t-1+1e-6)/interval);angle=22.5 if t<5 else 45.
                direction+=angle*(1 if index%2==0 else -1)
            theta+=target_speed*DT/6.5
        for robot,speed in ((0,ego_speed),(1,target_speed)):
            leg=dict(t0=t,t1=end,type='strafe_spin' if robot==1 and omega else 'strafe' if speed else 'static')
            if speed or (robot==1 and omega):leg.update(v=speed,dir_deg=direction)
            if robot==1 and omega:leg['omega']=omega
            legs[robot].append(leg)
            reference[robot][0]+=speed*DT*math.cos(math.radians(direction));reference[robot][1]+=speed*DT*math.sin(math.radians(direction))
        knots.append(dict(t=t,t1=end,stage=stage,target_speed=target_speed,target_heading_deg=direction,
            target_omega=omega,ego_speed=ego_speed,ego_heading_deg=direction,target_position=list(reference[1]),ego_position=list(reference[0]),
            ideal_range_m=math.dist(reference[0],reference[1])))
    phases=[dict(name='initial',t0=0.,t1=1.,target_speed=0.,target_omega=0.,ego_speed=0.)]
    for stage in range(4):
        knot=knots[stage*20]
        phases.append(dict(name='stage_'+str(stage+1),t0=1.+stage*2,t1=3.+stage*2,
            target_speed=knot['target_speed'],target_omega=knot['target_omega'],ego_speed=knot['ego_speed']))
    plan=dict(version=1,scene=scene,seed=seed,terrain='flat_range',duration_s=duration,semantics=semantics(scene),
        suite='movement-v1',tier=p['tier'],case=p['case'],pressure=p['pressure'],segments=legs,reference_knots=knots,phases=phases,
        motion_change_times_s=p['change_times'],steady_windows_s=[[1.,9.]],joint_tolerance_fraction=.15,
        joint_minimum_hold_s=1.,zero_omega_tolerance_radps=.3,range_focus_m=[5.,8.],bounds_m=[-60.,60.],
        speed_servo_min_flip_interval_s=.5,turn_command_interval_s=.8 if p['case']=='turn08' else .4 if p['case']=='turn04' else None,
        turn_targets_deg=[45.,90.] if p['case'].startswith('turn') else [],physical_model='Existing vehicle/servo unchanged; not a swerve drivetrain validation')
    text=f'id = "rm26_{scene}"\nseed = {seed}\nduration = {duration}\ntrigger = "on_load"\n'
    for robot,(x,y) in enumerate(((0.,0.),(6.5,0.))):
        text+=f'\n[[robot]]\nid = {robot}\nspawn = {{ x = {x}, y = {y}, z = 0.0, yaw_deg = {0 if robot==0 else 180}.0 }}\n'
        for leg in legs[robot]:
            text+='[[robot.segment]]\n'+''.join(f'{k} = "{v}"\n' if isinstance(v,str) else f'{k} = {v}\n' for k,v in leg.items())
    return text,plan
