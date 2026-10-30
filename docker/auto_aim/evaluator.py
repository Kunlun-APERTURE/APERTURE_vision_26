#!/usr/bin/env python3
"""Independent ROS observer, truth scorer and experiment evidence recorder.

Reads the bridge's one-way Unix stream and simulator GT. It has no publisher
and cannot provide target truth or commands to the algorithm.
"""
import argparse
import bisect
import collections
import json
import math
from pathlib import Path
import queue
import signal
import socket
import threading
import time
import hashlib

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from std_msgs.msg import String as RosString
from rm_interfaces.msg import SimArmorStates, SimRobotStates, SimScenarioStatus, SimEvent, GimbalCmd
from observer import receive, send
from report import finalize, load
from scenario_catalog import semantics, EXTRA, SCENES
from input_contract import accept_boundary


def ns(stamp):
    return stamp.sec*1000000000+stamp.nanosec


def clean(value):
    if isinstance(value,np.generic):
        return clean(value.item())
    if isinstance(value,np.ndarray):
        return clean(value.tolist())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k:clean(v) for k,v in value.items()}
    if isinstance(value, (tuple,list)):
        return [clean(v) for v in value]
    return value


def rotation(q):
    x,y,z,w = np.asarray(q,dtype=float)/np.linalg.norm(q)
    return np.array([[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)],
        [2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)],
        [2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]])


class History:
    def __init__(self, maxlen=1200):
        self.rows = collections.deque(maxlen=maxlen)
        self.last_sample = 0

    def append(self, stamp, value):
        if stamp <= self.last_sample:
            return
        self.last_sample = stamp
        self.rows.append((stamp,value))

    def pair(self, stamp, tolerance=30000000):
        rows = list(self.rows)
        if not rows:
            return None
        stamps = [r[0] for r in rows]
        i = bisect.bisect_left(stamps,stamp)
        if i < len(rows) and stamps[i] == stamp:
            return rows[i],rows[i],0.0
        if i == 0 or i == len(rows):
            return None
        before,after = rows[i-1],rows[i]
        if max(stamp-before[0],after[0]-stamp) > tolerance:
            return None
        return before,after,(stamp-before[0])/(after[0]-before[0])


class Evaluator(Node):
    def __init__(self, args):
        super().__init__('rm26_experiment_evaluator')
        self.args = args
        self.output = Path(args.output)
        self.output.mkdir(parents=True,exist_ok=True)
        self.manifest=load(self.output/'manifest.json',{})
        self.lockstep=self.manifest.get('execution',{}).get('mode')=='lockstep'
        self.last_settlement_stamp=None
        self.deferred=getattr(args,'defer_media',False)
        self.settlement_snapshot=None
        self.settlement_generation=None
        self.last_shot_activity=time.monotonic()
        self.semantics=self.manifest.get('scenario_semantics',semantics(args.scene))
        self.fire_rate_hz = (load(self.output/'manifest.json',{}).get('shooting') or {}).get('fire_rate_limit_hz',2)
        self.fire_policy = (load(self.output/'manifest.json',{}).get('shooting') or {}).get('fire_policy','advice')
        (self.output/'recorder-code.json').write_text(json.dumps({
            name:hashlib.sha256(Path('/opt/rm26',name).read_bytes()).hexdigest()
            for name in ('evaluator.py','observer.py','report.py','shots.py')},indent=2))
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.status_lock = threading.Lock()
        self.armors = History()
        self.robots = History()
        self.scenario = {}
        self.scenario_history = collections.deque(maxlen=1500)
        self.observations = queue.Queue(maxsize=64)
        self.stopped = threading.Event()
        self.abort = threading.Event()
        self.measurement_done = threading.Event()
        self.stop_reason = ''
        self.errors = []
        self.records = []
        self.frame_rows = []
        self.first_observer_seq = None
        self.last_observer_seq = None
        self.observer_gaps = 0
        self.queue_drops = 0
        self.fire_events = 0
        self.shot_events = []
        self.projectile_traces = []
        self.algorithm_fire_suggestions = 0
        self.fire_advice = 0
        self.control_messages = 0
        self.last_observation_wall = time.monotonic()
        self.stream_ready = False
        self.session = None
        self.end_stamp = None
        self.end_received = None
        self.scenario_started_stamp = None
        self.require_boundary=getattr(args,'input_contract','legacy')=='aligned-v1'
        self.scenario_boundary=None
        self.boundary_pending=collections.deque()
        self.started_received = None
        self.created_mono = time.monotonic()
        self.raw_log = (self.output/'telemetry.jsonl').open('w')
        self.samples_log = (self.output/'frames.jsonl').open('w')
        self.original_jpegs = (self.output/'input.mjpg').open('wb')
        self.record_status('starting')
        self.create_subscription(SimArmorStates,'/sim/gt/armors',self.on_armors,qos_profile_sensor_data)
        self.create_subscription(SimRobotStates,'/sim/gt/robots',self.on_robots,qos_profile_sensor_data)
        self.create_subscription(SimScenarioStatus,'/sim/scenario/status',self.on_scenario,qos_profile_sensor_data)
        self.create_subscription(RosString,'/sim/scenario/boundary',lambda msg:self.on_boundary(json.loads(msg.data)),100)
        self.create_subscription(SimEvent,'/sim/events/fire',self.on_fire,100)
        self.create_subscription(SimEvent,'/sim/events/hit',self.on_fire,100)
        self.create_subscription(RosString,'/sim/projectiles/trace',self.on_trace,100)
        self.create_subscription(RosString,'/sim/projectiles/settlement',self.on_settlement,100)
        self.create_subscription(RosString,'/sim/gimbal/trace',self.on_gimbal_trace,qos_profile_sensor_data)
        self.create_subscription(RosString,'/sim/aim/fire_eligibility',lambda msg:self.log('fire_eligibility',json.loads(msg.data)),qos_profile_sensor_data)
        self.create_subscription(RosString,'/sim/bench/continuous_fire',lambda msg:self.log('continuous_fire_heartbeat',json.loads(msg.data)),qos_profile_sensor_data)
        self.create_subscription(GimbalCmd,'/rm_gimbal/cmd',self.on_command,qos_profile_sensor_data)
        self.raw_video = self.annotated_video = None
        threading.Thread(target=self.observe,daemon=True).start()

    def log(self, kind, value):
        with self.write_lock:
            self.raw_log.write(json.dumps(clean({'kind':kind,'received_ns':time.time_ns(),'data':value}),allow_nan=False)+'\n')

    def record_status(self, state, **extra):
        with self.status_lock:
            temporary = self.output/'recorder-status.tmp'
            temporary.write_text(json.dumps({'state':state,'updated_ns':time.time_ns(),
                'stream_ready':getattr(self,'stream_ready',False),
                'truth_ready':bool(self.armors.rows and self.robots.rows and self.scenario),
                'frames':len(getattr(self,'records',[])),**extra},indent=2))
            temporary.replace(self.output/'recorder-status.json')

    def on_armors(self, msg):
        stamp = ns(msg.header.stamp)
        # GT is every render frame (~600 Hz). 100 Hz retained truth brackets
        # still resolve every 30 Hz input; record each retained sample.
        with self.lock:
            if stamp-self.armors.last_sample < 10000000:
                return
            values = [{'id':a.armor_id,'robot_id':a.robot_id,'slot':a.slot,
                'position':list(a.pos),'normal':list(a.normal),'width':a.plate_w,'height':a.plate_h,
                'line_of_sight_clear':a.line_of_sight_clear,'visibility_valid':a.visibility_valid}
                for a in msg.armors]
            sim = msg.armors[0].sim_time_ns/1e9 if msg.armors else None
            self.armors.append(stamp,{'time':sim,'armors':values})
            self.log('armors',{'stamp_ns':stamp,'time':sim,'armors':values})

    def on_robots(self, msg):
        stamp = ns(msg.header.stamp)
        with self.lock:
            if stamp<=self.robots.last_sample:return
            values = [{'id':r.robot_id,'team':r.team,'position':list(r.pos),'velocity':list(r.vel),
                'yaw':r.yaw,'yaw_rate':r.yaw_rate} for r in msg.robots]
            sim = msg.robots[0].sim_time_ns/1e9 if msg.robots else None
            self.robots.append(stamp,{'time':sim,'robots':values})
            self.log('robots',{'stamp_ns':stamp,'time':sim,'robots':values})

    def on_scenario(self, msg):
        if msg.scenario_id != 'rm26_'+self.args.scene:
            self.fail('Unexpected scenario: '+msg.scenario_id)
            return
        sample = {'stamp_ns':ns(msg.header.stamp),'id':msg.scenario_id,'phase':msg.phase,
            'time':msg.t_scenario,'spawn_applied':msg.spawn_applied,'spawn_error':msg.spawn_error,
            'max_track_deviation_m':msg.max_track_deviation_m}
        with self.lock:
            if self.scenario and msg.t_scenario < self.scenario['time']-.5:
                self.fail('Scenario reset during measurement')
            self.scenario = sample
            self.scenario_history.append(sample)
            if not self.require_boundary and msg.phase == 1 and self.started_received is None:
                self.started_received = time.monotonic()
                self.scenario_started_stamp = sample['stamp_ns']-int(msg.t_scenario*1e9)
            if not self.require_boundary and msg.phase == 2 and self.end_stamp is None:
                self.end_stamp = sample['stamp_ns']
                self.end_received = time.monotonic()
            self.log('scenario',sample)

    def on_boundary(self,value):
        if value.get('id')!='rm26_'+self.args.scene:
            self.fail('Unexpected physical scenario identity');return
        with self.lock:
            if not accept_boundary(self.scenario_boundary,value):return
            if self.scenario_started_stamp is not None and self.scenario_boundary and value['generation']!=self.scenario_boundary['generation']:
                self.fail('Physical scenario generation changed');return
            self.scenario_boundary=dict(value)
            if self.require_boundary:
                start=value.get('measurement_start_stamp_ns');end=value.get('measurement_end_stamp_ns')
                if start is not None:
                    self.scenario_started_stamp=start
                    if self.started_received is None:self.started_received=time.monotonic()
                if end is not None:
                    self.end_stamp=end
                    if self.end_received is None:self.end_received=time.monotonic()
            self.log('scenario_boundary',value)

    def on_trace(self, msg):
        data = json.loads(msg.data)
        with self.lock:
            self.projectile_traces.append(data)
            self.log('projectile_trace',data)

    def on_settlement(self,msg):
        data=json.loads(msg.data)
        if data.get('version')!=1 or data.get('method')!='swept_actual_segment_v3':return
        with self.lock:
            if self.settlement_generation is None:self.settlement_generation=data['generation']
            elif self.settlement_generation!=data['generation']:
                self.fail('Settlement generation changed during capture')
            previous=self.settlement_snapshot
            if previous is None or (previous['allocated'],previous['terminals'])!=(data['allocated'],data['terminals']):
                self.last_shot_activity=time.monotonic()
                self.last_settlement_stamp=data['stamp_ns']
            self.settlement_snapshot=data
            self.log('settlement',data)

    def on_gimbal_trace(self, msg):
        self.log('gimbal_trace',json.loads(msg.data))

    def on_fire(self, msg):
        value = clean({'stamp_ns':ns(msg.header.stamp),'sim_time_ns':msg.sim_time_ns,
            'shot_seq':msg.shot_seq,'kind':msg.kind,'shooter_id':msg.shooter_id,
            'target_id':msg.target_id,'armor_id':msg.armor_id,
            'world_point':list(msg.world_point),'contact_point':list(msg.contact_point),
            'velocity':list(msg.velocity),'flight_time_s':msg.flight_time_s,
            'impact_geometry_valid':msg.impact_geometry_valid,'impact_sim_time_ns':msg.impact_sim_time_ns,
            'plate_center':list(msg.plate_center),'plate_right':list(msg.plate_right),'plate_up':list(msg.plate_up),'plate_size':list(msg.plate_size)})
        with self.lock:
            self.fire_events += msg.kind == 0
            self.last_shot_activity=time.monotonic()
            self.shot_events.append(value)
            self.log('shot_event',value)

    def on_command(self, msg):
        self.control_messages += 1
        self.fire_advice += bool(msg.fire_advice)
        self.log('command',{'stamp_ns':ns(msg.header.stamp),'yaw_deg':msg.yaw,'pitch_deg':msg.pitch,
            'distance':msg.distance,'fire_advice':msg.fire_advice,'temporal_valid':msg.temporal_valid,
            'aim_stamp_ns':msg.aim_stamp_ns,'yaw_rate_radps':msg.yaw_rate_radps,'pitch_rate_radps':msg.pitch_rate_radps})

    def fail(self, reason):
        with self.lock:
            if reason not in self.errors:
                self.errors.append(reason)

    def observe(self):
        try:
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
                connection.settimeout(3)
                connection.connect('/run/rm26-sim/observer.sock')
                send(connection,{'type':'observe'})
                while not self.stopped.is_set():
                    header,jpeg = receive(connection)
                    if header.get('type') == 'ready':
                        self.stream_ready = True
                        self.record_status('ready')
                    if header.get('type') != 'observation':
                        continue
                    self.last_observation_wall = time.monotonic()
                    seq = header['observer_seq']
                    if self.last_observer_seq is not None:
                        self.observer_gaps += max(0,seq-self.last_observer_seq-1)
                    self.last_observer_seq = seq
                    if self.first_observer_seq is None:
                        self.first_observer_seq = seq
                    try:
                        self.observations.put_nowait((header,jpeg))
                    except queue.Full:
                        self.queue_drops += 1
        except (OSError,EOFError,ValueError) as error:
            if not self.stopped.is_set():
                self.fail('Observer connection lost: '+str(error))
                self.abort.set()

    def truth(self, stamp):
        with self.lock:
            plate_pair = self.armors.pair(stamp)
            robot_pair = self.robots.pair(stamp,50000000)
        if plate_pair is None or robot_pair is None:
            return None
        (ta,a),(tb,b),alpha = plate_pair
        (ra,c),(rb,d),beta = robot_pair
        if a['time'] is None or b['time'] is None or b['time'] < a['time']:
            return None
        if c['time'] is None or d['time'] is None or d['time'] < c['time']:
            return None
        def interpolate(left,right,key,fraction):
            result = []
            by_id = {entry['id']:entry for entry in right[key]}
            for entry in left[key]:
                other = by_id.get(entry['id'])
                if other is None:
                    continue
                row = dict(entry)
                for name in ('position','velocity','normal'):
                    if name in entry:
                        row[name] = ((1-fraction)*np.array(entry[name])+fraction*np.array(other[name])).tolist()
                if key == 'armors':
                    row['line_of_sight_clear'] = entry['line_of_sight_clear'] and other['line_of_sight_clear']
                    row['visibility_valid'] = entry['visibility_valid'] and other['visibility_valid']
                if key == 'robots':
                    row['yaw_rate'] = (1-fraction)*entry['yaw_rate']+fraction*other['yaw_rate']
                result.append(row)
            return result
        return {'time':(1-alpha)*a['time']+alpha*b['time'],
            'alignment_ms':max(stamp-ta,tb-stamp,stamp-ra,rb-stamp)/1e6,
            'armors':interpolate(a,b,'armors',alpha),
            'robots':interpolate(c,d,'robots',beta)}

    def process(self, packet, jpeg):
        if self.require_boundary and packet.get('scenario_boundary'):self.on_boundary(packet['scenario_boundary'])
        frame,result = packet['frame'],packet['result']
        stamp = frame['stamp_ns']
        truth = self.truth(stamp)
        self.log('observation',packet)
        # A queued preparation frame can arrive after the Running status. Its
        # missing GT bracket belongs to warmup, not to the short measurement.
        with self.lock:
            if self.scenario_started_stamp is None or stamp < self.scenario_started_stamp:
                return
        if truth is None:
            # Count missing truth in the measured interval, not the idle warmup.
            with self.lock:
                running = self.started_received is not None and (self.end_stamp is None or stamp <= self.end_stamp)
            if not running:
                return
            t = None
        else:
            t = truth['time']
            if (t < 0 if self.require_boundary else t <= 0) or t > self.args.duration:
                return
            if self.end_stamp is not None and stamp > self.end_stamp and self.args.mode != 'shoot':
                return
        if self.session is None:
            self.session = frame['session']
        elif self.session != frame['session']:
            self.fail('Algorithm session changed during measurement')
        image = cv2.imdecode(np.frombuffer(jpeg,np.uint8),cv2.IMREAD_COLOR) if not self.deferred or not self.records else None
        if image is None and (not self.deferred or not self.records):
            self.fail('JPEG decode failure')
            return
        from background_pipeline import jpeg_size
        w,h=jpeg_size(jpeg)
        if (w,h)!=(frame['calibration']['width'],frame['calibration']['height']):
            raise ValueError('JPEG dimensions differ from calibration')
        if self.raw_video is None and not self.deferred:
            fourcc = cv2.VideoWriter_fourcc(*'MJPG')
            self.raw_video = cv2.VideoWriter(str(self.output/'input.avi'),fourcc,30,(w,h))
            self.annotated_video = cv2.VideoWriter(str(self.output/'annotated.avi'),fourcc,30,(w,h))
            if not self.raw_video.isOpened() or not self.annotated_video.isOpened():
                raise RuntimeError('Cannot open experiment video writers')
        if not self.deferred:self.raw_video.write(image)
        jpeg_offset = self.original_jpegs.tell()
        self.original_jpegs.write(jpeg)
        marked = image.copy() if not self.deferred else None
        drawings=[]
        def draw(name,*arguments):
            if self.deferred:drawings.append([name,clean(arguments)])
            else:getattr(cv2,name)(marked,*arguments)
        camera = frame['camera_in_map']
        r = rotation(camera['quaternion'])
        position = np.asarray(camera['position'])
        origin = np.asarray(frame['gimbal_position_map'])
        k = np.asarray(frame['calibration']['k']).reshape(3,3)
        measurement = self.end_stamp is None or stamp <= self.end_stamp
        if measurement:
            self.algorithm_fire_suggestions += result.get('fire_advice') is True
        row = {'measurement':measurement,'seq':frame['seq'],'stamp_ns':stamp,'time':t,'video_index':len(self.records),
            'jpeg_offset':jpeg_offset,'jpeg_bytes':len(jpeg),
            'truth_available':truth is not None,'age_ms':(packet['receipt_ns']-stamp)/1e6,
            'pipeline_ms':result.get('pipeline_ms'),'tracker_state':result.get('tracker'),
            'command_emitted':packet['command_emitted'],'visible_plates':0,
            'detection_matches':0,'false_detections':0,'correct_tracking':False,
            'plate_position_error_m':None,'center_xy_error_m':None,'velocity_error_mps':None,
            'yaw_execution_error_deg':None,'pitch_execution_error_deg':None,
            'alignment_ms':truth['alignment_ms'] if truth else None}
        def project(points):
            optical = (np.asarray(points)-position)@r
            if np.any(optical[:,2] <= .05):
                return None
            uvw = optical@k.T
            return uvw[:,:2]/uvw[:,2,None]
        candidates = []
        plates = truth['armors'] if truth else []
        for plate in plates:
            centre = np.asarray(plate['position'])
            normal = np.asarray(plate['normal'])
            normal /= max(np.linalg.norm(normal),1e-9)
            right = np.cross([0,0,1],normal)
            right /= max(np.linalg.norm(right),1e-9)
            up = np.cross(normal,right)
            points = [centre+sx*right*plate['width']/2+sy*up*plate['height']/2
                for sx,sy in ((-1,1),(1,1),(1,-1),(-1,-1))]
            polygon = project(points)
            facing = float(np.dot(normal,position-centre)) > 0
            inside = (polygon is not None and np.all(polygon[:,0]>=0) and np.all(polygon[:,0]<w)
                and np.all(polygon[:,1]>=0) and np.all(polygon[:,1]<h))
            eligible = bool(plate['visibility_valid'] and plate['line_of_sight_clear'] and facing and inside)
            if polygon is not None and inside:
                color = (255,255,0) if eligible else (140,140,140)
                draw('polylines',[np.rint(polygon).astype(np.int32)],True,color,2)
                p = tuple(np.rint(polygon.mean(axis=0)).astype(int))
                draw('putText',f"GT {plate['robot_id']}:{plate['slot']}",p,cv2.FONT_HERSHEY_SIMPLEX,.45,color,1)
            if eligible and plate['robot_id'] == 1:
                candidates.append((plate,polygon))
        row['visible_plates'] = len(candidates)
        observations = result.get('observations',[])
        pairs = []
        for oi,observation in enumerate(observations):
            points = np.asarray(observation.get('corners',[]),dtype=float)
            if points.shape != (4,2) or not np.isfinite(points).all():
                continue
            draw('polylines',[np.rint(points).astype(np.int32)],True,(0,255,0),2)
            draw('putText','DET '+observation.get('name','?'),tuple(np.rint(points[0]).astype(int)),
                cv2.FONT_HERSHEY_SIMPLEX,.45,(0,255,0),1)
            for pi,(plate,polygon) in enumerate(candidates):
                diagonal = np.linalg.norm(polygon.max(axis=0)-polygon.min(axis=0))
                distance = np.linalg.norm(points.mean(axis=0)-polygon.mean(axis=0))
                if distance <= max(12,diagonal*.6):
                    pairs.append((distance,oi,pi))
        used_observations,used_plates = set(),set()
        matched = []
        for _,oi,pi in sorted(pairs):
            if oi in used_observations or pi in used_plates:
                continue
            used_observations.add(oi)
            used_plates.add(pi)
            observation = observations[oi]
            correct_label = observation.get('color') == 'blue' and observation.get('name') == 'three'
            row['detection_matches'] += correct_label
            if 'position' in observation and np.isfinite(observation['position']).all():
                error = float(np.linalg.norm(np.asarray(observation['position'])+origin-candidates[pi][0]['position']))
                matched.append(error)
        row['false_detections'] = max(0,len(observations)-row['detection_matches'])
        if matched:
            row['plate_position_error_m'] = min(matched)
        track = result.get('track_estimate')
        if truth and track:
            target_plates = [p for p in plates if p['robot_id'] == 1]
            robots = {entry['id']:entry for entry in truth['robots']}
            estimate = np.asarray(track['center'],dtype=float)+origin
            if target_plates and np.isfinite(estimate).all():
                centre = np.mean([p['position'] for p in target_plates],axis=0)
                row['center_xy_error_m'] = float(np.linalg.norm((estimate-centre)[:2]))
                nearest_robot = min({p['robot_id'] for p in plates},key=lambda rid:
                    np.linalg.norm((estimate-np.mean([p['position'] for p in plates if p['robot_id']==rid],axis=0))[:2]))
                row['correct_tracking'] = (result.get('tracker') == 'tracking' and nearest_robot == 1
                    and row['center_xy_error_m'] <= .5 and track.get('name') == 'three')
                if 1 in robots:
                    reference = np.asarray(robots[1]['velocity'])
                    if track.get('frame')=='gimbal_origin_world_aligned' and 0 in robots:
                        reference = reference-np.asarray(robots[0]['velocity'])
                    row['center_velocity_approximation']=result.get('prediction_model')=='plate_cv'
                    row['velocity_error_mps'] = float(np.linalg.norm(np.asarray(track['velocity'])[:2]-reference[:2]))
            selected=result.get('selected_plate_id',-1)
            model_plates=track.get('plates',[]);velocities=result.get('plate_velocities',[])
            if 0<=selected<len(model_plates) and selected<len(velocities) and target_plates:
                world=np.asarray(model_plates[selected])+origin
                nearest=sorted((float(np.linalg.norm(world-p['position'])),p['id'],p) for p in target_plates)
                if nearest[0][0]<.25 and (len(nearest)==1 or nearest[1][0]-nearest[0][0]>.03):
                    row['selected_gt_armor_id']=nearest[0][1]
                    row['selected_plate_position_error_m']=nearest[0][0]
                    before=self.truth(stamp-20_000_000);after=self.truth(stamp+20_000_000)
                    if before and after:
                        left=next((p for p in before['armors'] if p['id']==nearest[0][1]),None)
                        right=next((p for p in after['armors'] if p['id']==nearest[0][1]),None)
                        if left and right:
                            velocity=(np.asarray(right['position'])-left['position'])/.04
                            velocity-=np.asarray(robots.get(0,{}).get('velocity',[0,0,0]))
                            row['selected_plate_velocity_error_mps']=float(np.linalg.norm((np.asarray(velocities[selected])-velocity)[:2]))
            predicted_plates = np.asarray(track.get('plates',[]),dtype=float)
            if predicted_plates.ndim == 2 and predicted_plates.shape[1:] == (3,):
                projected = project(predicted_plates+origin)
                if projected is not None:
                    for point in projected:
                        if np.isfinite(point).all() and -w<int(point[0])<2*w and -h<int(point[1])<2*h:
                            draw('drawMarker',tuple(np.rint(point).astype(int)),(255,0,255),cv2.MARKER_CROSS,12,2)
        aim = result.get('aim_point')
        if aim is not None:
            point = project([np.asarray(aim)+origin])
            if point is not None and np.isfinite(point).all():
                draw('circle',tuple(np.rint(point[0]).astype(int)),7,(0,255,255),2)
        rg = rotation(frame['q_gimbal2world'])
        if packet['command_emitted']:
            yaw = math.atan2(rg[1,0],rg[0,0])
            elevation = math.atan2(rg[2,0],math.hypot(rg[0,0],rg[1,0]))
            row['yaw_execution_error_deg'] = abs(math.degrees(math.remainder(result['yaw']-yaw,2*math.pi)))
            row['pitch_execution_error_deg'] = abs(math.degrees(-result['pitch']-elevation))
        if truth:
            robots = {entry['id']:entry for entry in truth['robots']}
            row['ego_position'] = robots.get(0,{}).get('position')
            row['target_position'] = robots.get(1,{}).get('position')
            row['target_speed_mps'] = float(np.linalg.norm(robots[1]['velocity'][:2])) if 1 in robots else None
            row['target_yaw_rate'] = robots.get(1,{}).get('yaw_rate')
            row['ego_velocity'] = robots.get(0,{}).get('velocity')
            row['ego_yaw'] = robots.get(0,{}).get('yaw')
            row['ego_speed_mps'] = float(np.linalg.norm(robots[0]['velocity'][:2])) if 0 in robots else None
            row['velocity_reference'] = 'relative_to_translating_ego_origin'
        overlay = [f"{self.args.scene}  t={t if t is not None else -1:.3f}s  frame={frame['seq']}  age={row['age_ms']:.1f}ms",
            f"state={row['tracker_state']} correct_track={row['correct_tracking']} visible={len(candidates)}",
            'GT cyan / detection green / estimated plates magenta / ballistic aim yellow',
            ('NO FIRE | video timeline: frames.jsonl' if self.args.mode == 'track' else
                self.shot_overlay(stamp,measurement))]
        draw('rectangle',(0,0),(w,112),(0,0,0),-1)
        for i,line in enumerate(overlay):
            draw('putText',line,(12,23+i*26),cv2.FONT_HERSHEY_SIMPLEX,.53,(255,255,255),1,cv2.LINE_AA)
        if not self.deferred:self.annotated_video.write(marked)
        row.update(drawings=drawings,image_size=[w,h])
        if not self.deferred and (not self.records or len(self.records)%300 == 0 or (not row['correct_tracking'] and len(self.records)%30 == 0)):
            folder = self.output/'keyframes'
            folder.mkdir(exist_ok=True)
            cv2.imwrite(str(folder/f'{len(self.records):06d}.jpg'),marked)
        safe = clean(row)
        self.samples_log.write(json.dumps(safe,allow_nan=False)+'\n')
        self.samples_log.flush()
        self.records.append(safe)

    def shot_overlay(self, stamp, measurement):
        with self.lock:
            events = [e for e in self.shot_events if e['stamp_ns'] <= stamp]
        fires = [e for e in events if e['kind'] == 0]
        hits = [e for e in events if e['kind'] == 1 and e['target_id'] == 1]
        latest = events[-1] if events else None
        marker = ''
        if latest and stamp-latest['stamp_ns'] < 350000000:
            marker = ('FIRE' if latest['kind']==0 else 'HIT' if latest['kind']==1 else 'ENV')+f" #{latest['shot_seq']}"
        label = 'CONTINUOUS 2Hz' if self.fire_policy == 'continuous' else f'ADVICE FIRE <={self.fire_rate_hz:g}Hz'
        return f"{label} | fired={len(fires)} target_hits={len(hits)} {marker} " + ('' if measurement else 'IN-FLIGHT SETTLEMENT')

    def run(self):
        executor = rclpy.executors.SingleThreadedExecutor()
        executor.add_node(self)
        def spin_ros():
            try:
                executor.spin()
            except Exception as error:
                if not self.stopped.is_set():
                    self.fail('ROS observer callback failed: '+type(error).__name__+': '+str(error))
                    self.abort.set()
        spin = threading.Thread(target=spin_ros,daemon=True)
        spin.start()
        deadline = self.created_mono+self.args.wall_budget-2.5
        try:
            while not self.abort.is_set():
                if time.monotonic()>deadline:
                    self.fail('Experiment deadline exceeded')
                    break
                if not self.lockstep and self.started_received is None and time.monotonic()-self.last_observation_wall > (8 if self.require_boundary else 3):
                    self.fail('No live algorithm observations before measurement')
                    break
                if not self.scenario and time.monotonic()-self.created_mono > 15:
                    self.fail('Scenario/GT subscriptions did not become ready')
                    break
                if not self.lockstep and self.started_received is not None and time.monotonic()-self.last_observation_wall > 2:
                    self.fail('Image or algorithm stream missing for more than 2 seconds')
                    break
                if self.end_received is not None and self.observations.empty():
                    elapsed=time.monotonic()-self.end_received
                    with self.lock:
                        snapshot=self.settlement_snapshot
                        if self.lockstep and snapshot:
                            elapsed=(snapshot['stamp_ns']-self.end_stamp)/1e9
                        quiet=((snapshot['stamp_ns']-(self.last_settlement_stamp or snapshot['stamp_ns']))/1e9
                            if self.lockstep and snapshot else time.monotonic()-self.last_shot_activity)
                        fires={e['shot_seq'] for e in self.shot_events if e['kind']==0}
                        terminal={v['shot_seq'] for v in snapshot['terminals']} if snapshot else set()
                        hits={e['shot_seq'] for e in self.shot_events if e['kind'] in (1,2)}
                        collision={v['shot_seq'] for v in snapshot['terminals'] if v['reason']=='collision'} if snapshot else set()
                        settled=(snapshot and snapshot.get('allocated')==len(fires)
                            and fires==terminal and collision<=hits
                            and quiet>=.25)
                    if self.deferred and settled and elapsed>=.25:break
                    if elapsed>(self.args.settlement if self.args.mode=='shoot' else 1):
                        if self.deferred and snapshot and not settled:self.fail('Missing projectile settlement or collision events')
                        break
                try:
                    if self.boundary_pending and self.scenario_started_stamp is not None:
                        packet,jpeg=self.boundary_pending.popleft()
                    else:packet,jpeg = self.observations.get(timeout=.05)
                except queue.Empty:
                    if self.lockstep or self.require_boundary:self.record_status('ready' if self.started_received is None else 'recording')
                    continue
                if self.require_boundary and packet.get('scenario_boundary'):self.on_boundary(packet['scenario_boundary'])
                if self.require_boundary and self.scenario_started_stamp is None:
                    self.boundary_pending.append((packet,jpeg))
                    if len(self.boundary_pending)>256:
                        raise RuntimeError('Physical start boundary missing; early-observation buffer exhausted')
                    continue
                # Warmup precedes the recorder's oldest GT. Waiting cannot
                # recover that bracket and can build seconds of queue backlog.
                # process() still logs warmup observations and excludes them.
                with self.lock:
                    measured = (self.scenario_started_stamp is not None
                        and packet['frame']['stamp_ns'] >= self.scenario_started_stamp)
                if measured:
                    wait = time.monotonic()+.15
                    while self.truth(packet['frame']['stamp_ns']) is None and time.monotonic()<wait:
                        time.sleep(.003)
                self.process(packet,jpeg)
                if len(self.records)%30 == 0:
                    self.record_status('recording' if self.started_received else 'ready')
        except Exception as error:
            self.fail(type(error).__name__+': '+str(error))
        finally:
            self.stopped.set()
            self.raw_log.flush()
            self.samples_log.close()
            self.original_jpegs.close()
            for writer in (self.raw_video,self.annotated_video):
                if writer is not None:
                    writer.release()
            executor.shutdown(timeout_sec=3)
            spin.join(timeout=3)
            self.raw_log.close()
        run_state = {'errors':self.errors,'observer_gaps':self.observer_gaps,
            'recorder_queue_drops':self.queue_drops,'fire_events':self.fire_events,
            'fire_advice_messages':self.fire_advice,'control_messages':self.control_messages,
            'algorithm_fire_suggestions':self.algorithm_fire_suggestions,'shot_events':self.shot_events,
            'projectile_traces':self.projectile_traces,
            'settlement':self.settlement_snapshot,
            'settlement_wall_s':time.monotonic()-self.end_received if self.end_received else None,
            'measurement_start_stamp_ns':self.scenario_started_stamp,'measurement_end_stamp_ns':self.end_stamp,
            'physical_boundary':self.scenario_boundary,'pending_boundary_observations':len(self.boundary_pending),
            'scenario':self.scenario,'scenario_history':list(self.scenario_history),
            'complete':self.end_stamp is not None and not self.abort.is_set(),
            'ended_ns':time.time_ns()}
        (self.output/'run-state.json').write_text(json.dumps(clean(run_state),indent=2))
        finalize(self.output)
        self.record_status('finished',errors=self.errors)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',required=True)
    parser.add_argument('--scene',choices=SCENES,required=True)
    parser.add_argument('--duration',type=float,default=60)
    parser.add_argument('--mode',choices=['track','shoot'],default='track')
    parser.add_argument('--settlement',type=float,default=6)
    parser.add_argument('--wall-budget',type=float,default=35)
    parser.add_argument('--defer-media',action='store_true')
    parser.add_argument('--input-contract',choices=['legacy','aligned-v1'],default='legacy')
    args = parser.parse_args()
    cv2.setNumThreads(1)
    rclpy.init()
    evaluator = Evaluator(args)
    signal.signal(signal.SIGTERM,lambda *_:evaluator.abort.set())
    signal.signal(signal.SIGINT,lambda *_:evaluator.abort.set())
    try:
        evaluator.run()
    finally:
        evaluator.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
