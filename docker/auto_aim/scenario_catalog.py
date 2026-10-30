"""Explicit scenario semantics shared by orchestration and the independent scorer."""
import math, random
from highspeed_scenarios import HIGHSPEED

BASE=('static','line','spin','move_ref','ego_left','ego_right','ego_right_centered','ego_sweep','ego_sweep_reverse')
EXTRA=('random_both_05','random_both_10','random_both_20','distance_3','distance_5','distance_8',
       'range_in_out','range_diagonal','spin_move_05_pos','spin_move_05_neg','spin_move_15_pos','spin_move_15_neg',
       'spin_move_ego_05','spin_move_ego_15')
SCENES=BASE+EXTRA+HIGHSPEED


def semantics(scene):
    if scene in HIGHSPEED:
        from highspeed_scenarios import semantics as hs_semantics
        return hs_semantics(scene)
    moving_ego=scene.startswith('ego_') or scene.startswith('random_both') or scene.startswith('spin_move_ego')
    rotating=scene=='spin' or scene.startswith('spin_move')
    target_moving=scene in ('line','range_in_out','range_diagonal') or scene.startswith(('random_both','spin_move'))
    home=-156. if scene in ('ego_right_centered','ego_sweep_reverse') else 180. if scene in ('move_ref','ego_left','ego_right','ego_sweep') else 90. if scene.startswith(('distance_','range_')) else 0.
    return dict(homing_yaw_deg=home,ego_motion='translation' if moving_ego else 'static',target_motion='translation_rotation' if rotating and target_moving else 'rotation' if rotating else 'translation' if target_moving else 'static',
        estimate_frame='gimbal_origin_world_aligned',velocity_reference='relative_to_translating_ego_origin',
        hit_evaluation='target_armor' if rotating else 'front_armor',stress=scene=='random_both_20')


def generated(scene,seed,duration=18):
    if scene in HIGHSPEED:
        from highspeed_scenarios import generated as hs_generated
        return hs_generated(scene,seed,duration)
    if scene not in EXTRA:raise ValueError('Not a generated scenario')
    distance=int(scene.rsplit('_',1)[1]) if scene.startswith('distance_') else 4
    if scene.startswith('range_'):distance=3
    range_lane=scene.startswith(('distance_','range_'))
    positions=[(-2.5,5.),(-2.5,5.+distance)] if range_lane else [(-6.5,11.3),(-6.5+distance,11.3)]
    segments=[[],[]];constraints=[0,0]
    def add(robot,a,b,v=0,direction=0,omega=0):
        if b>a:segments[robot].append(dict(t0=round(a,6),t1=round(b,6),type='strafe_spin' if omega else 'strafe' if v else 'static',v=v,dir_deg=direction,omega=omega))
    def random_motion(robot,speed,low,high,omega=0):
        rng=random.Random((seed<<2)^robot);t=2.;x=y=0.;previous=None
        while t<min(16,duration):
            dt=min(rng.uniform(low,high),min(16,duration)-t)
            choices=[]
            for direction in range(0,360,45):
                if direction==previous:continue
                a=math.radians(direction);nx=x+speed*dt*math.cos(a);ny=y+speed*dt*math.sin(a)
                if abs(nx)<=.75 and abs(ny)<=1.25:choices.append((direction,nx,ny))
            if not choices:
                # A short final step can use a reflected direction toward the box centre.
                direction=math.degrees(math.atan2(-y,-x));a=math.radians(direction)
                choices=[(direction,x+speed*dt*math.cos(a),y+speed*dt*math.sin(a))];constraints[robot]+=1
            if len(choices)<7:constraints[robot]+=1
            direction,nx,ny=rng.choice(choices);add(robot,t,t+dt,speed,direction,omega);x,y=nx,ny;t+=dt;previous=direction
    for robot in (0,1):add(robot,0,min(2,duration))
    if scene.startswith('random_both'):
        speed={'05':.5,'10':1.,'20':2.}[scene.rsplit('_',1)[1]]
        for r in (0,1):random_motion(r,speed,.2 if speed==2 else .4,.4 if speed==2 else .8)
    elif scene.startswith('distance_'):
        for r in (0,1):add(r,2,min(16,duration))
    elif scene.startswith('range_'):
        add(0,2,min(16,duration))
        if scene=='range_in_out':
            add(1,2,min(9,duration),.7,90);add(1,9,min(16,duration),.7,270)
        else:
            for a,b,d in [(2,5.5,100),(5.5,9,80),(9,12.5,260),(12.5,16,280)]:add(1,a,min(b,duration),.7,d)
    else:
        omega=(1.5 if '_15' in scene else .5)*(-1 if scene.endswith('_neg') else 1)
        random_motion(1,1.,.4,.8,omega)
        if scene.startswith('spin_move_ego'):random_motion(0,.5,.4,.8)
        else:add(0,2,min(16,duration))
    for r in (0,1):add(r,min(16,duration),duration)
    text=f'id = "rm26_{scene}"\nseed = {seed}\nduration = {duration}\ntrigger = "on_load"\n'
    for r in (0,1):
        x,y=positions[r];text+=f'\n[[robot]]\nid = {r}\nspawn = {{ x = {x}, y = {y}, z = -0.2465, yaw_deg = {(90 if r==0 else -90) if range_lane else (0 if r==0 else 180)}.0 }}\n'
        for leg in segments[r]:
            text+='[[robot.segment]]\n'+''.join(f'{k} = "{v}"\n' if isinstance(v,str) else f'{k} = {v}\n' for k,v in leg.items() if k not in ('v','dir_deg','omega') or leg['type']!='static' and (k!='omega' or leg['omega']!=0))
    return text,dict(seed=seed,scene=scene,semantics=semantics(scene),segments=segments,boundary_constraint_decisions=constraints,
                     bounds_relative_to_spawn_m={'x':[-.75,.75],'y':[-1.25,1.25]} if 'random' in scene or scene.startswith('spin_move') else None)
