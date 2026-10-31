#!/usr/bin/env python3
"""ROS lives here. The algorithm speaks only framed JSON/JPEG over loopback."""
from scenario_catalog import SCENES,semantics
from command_script import CommandScript
import argparse
import collections
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import socket
import socketserver
import struct
import threading
import time
import uuid
from observer import ObserverHub
from input_contract import accept_boundary

import rclpy
from rclpy.duration import Duration
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rclpy.time import Time
from sensor_msgs.msg import CameraInfo, CompressedImage
from std_msgs.msg import String as RosString
from tf2_msgs.msg import TFMessage
from tf2_ros import Buffer
from rm_interfaces.msg import GimbalCmd, SimScenarioStatus, SimEvent
from rm_interfaces.srv import SimScenarioLoad, SimScenarioStart, SimScenarioReset

VERSION = 1
MAX_HEADER, MAX_JPEG = 65536, 16 * 1024 * 1024


def receive(sock):
    def exact(size):
        chunks = bytearray()
        while len(chunks) < size:
            part = sock.recv(size - len(chunks))
            if not part:
                raise EOFError()
            chunks.extend(part)
        return bytes(chunks)
    h, b = struct.unpack('!II', exact(8))
    if not 0 < h <= MAX_HEADER or b > MAX_JPEG:
        raise ValueError('Invalid envelope length')
    header = json.loads(exact(h))
    if not isinstance(header, dict) or header.get('version') != VERSION:
        raise ValueError('Unsupported protocol version')
    return header, exact(b)


def send(sock, header, payload=b''):
    body = json.dumps(dict(header, version=VERSION), allow_nan=False, separators=(',', ':')).encode()
    if len(body) > MAX_HEADER or len(payload) > MAX_JPEG:
        raise ValueError('Envelope too large')
    sock.sendall(struct.pack('!II', len(body), len(payload)) + body + payload)


def stamp_ns(stamp):
    return stamp.sec * 1000000000 + stamp.nanosec


def quaternion_matrix(rotation):
    x, y, z, w = rotation.x, rotation.y, rotation.z, rotation.w
    norm = math.sqrt(x*x + y*y + z*z + w*w)
    if not math.isfinite(norm) or norm < 0.5:
        raise ValueError('Invalid TF quaternion')
    x, y, z, w = (v / norm for v in (x, y, z, w))
    return [1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w),
            2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w),
            2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]


def percentile(values, fraction):
    values = sorted(values)
    return values[min(len(values)-1, int((len(values)-1)*fraction))] if values else None


class Bridge(Node):
    def __init__(self, args):
        super().__init__('sp_vision_sim_bridge')
        self.sync=None
        self._sync_command=None
        self._sync_eligible=False
        self.timing_log=None
        self.session_archive=None
        self.archive_requested=False
        self.lock = threading.RLock()
        self.client_lock = threading.Lock()
        self.buffer = Buffer(cache_time=Duration(seconds=10))
        self.session = uuid.uuid4().hex
        self.seq = 0
        self.latest = None
        self.info = None
        self.connected = False
        self.control = False
        self.homing_until = 0.0
        self.homing_yaw_deg = 0.0
        self.previous_fire_pose = None
        self.last_valid = 0.0
        self.last_command = None
        self.last_status_write = 0.0
        self.last_debug_write = 0.0
        self.last_wall = time.time_ns()
        self.last_mono = time.monotonic_ns()
        self.command_script=CommandScript(os.environ["RM26_COMMAND_SCRIPT"]) if os.environ.get("RM26_COMMAND_SCRIPT") else None
        self.scenario = {}
        self.scenario_boundary = None
        self.input_contract='legacy'
        self.warmup_frames=30
        self.warmup_state='legacy'
        self.warmup_processed=[]
        self.warmup_done=threading.Event()
        self.warmup_images=collections.deque()
        self.warmup_error=None
        self.sensor_frames=collections.OrderedDict()
        self.sensor_reset_id=None
        self.sensor_reset_ack=None
        self.last_error = ''
        self.counts = collections.Counter()
        self.ages = collections.deque(maxlen=2048)
        self.pipeline_times = collections.deque(maxlen=2048)
        self.yaw_errors = collections.deque(maxlen=2048)
        self.pitch_errors = collections.deque(maxlen=2048)
        self.started = time.monotonic()
        self.bullet_speed = args.bullet_speed
        self.actual_bullet_speed = args.actual_bullet_speed
        self.algorithm_profile = args.algorithm_profile
        self.fire_mode = args.fire
        self.fire_policy = args.fire_policy
        self.fire_rate_hz = args.fire_rate_hz
        self.last_shot = 0.0
        self.physics = args.physics
        self.fire_armed = False
        self.last_fire = 0
        self.fps = args.fps
        self.state_path = Path(args.state)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.observer = ObserverHub(self.state_path.with_name('observer.sock'))
        self.publisher = self.create_publisher(GimbalCmd, '/rm_gimbal/cmd', qos_profile_sensor_data)
        self.eligibility_publisher = self.create_publisher(RosString, '/sim/aim/fire_eligibility', qos_profile_sensor_data)
        self.continuous_publisher = self.create_publisher(RosString, '/sim/bench/continuous_fire', qos_profile_sensor_data)
        self.create_subscription(CompressedImage, '/image_raw/compressed', self.on_image, qos_profile_sensor_data)
        self.create_subscription(CameraInfo, '/camera_info', self.on_info, qos_profile_sensor_data)
        self.create_subscription(TFMessage, '/tf', self.on_tf, 100)
        self.create_subscription(TFMessage, '/tf_static', self.on_tf_static, 10)
        self.create_subscription(SimScenarioStatus, '/sim/scenario/status', self.on_scenario, qos_profile_sensor_data)
        self.create_subscription(RosString,'/sim/scenario/boundary',lambda msg:self.on_boundary(json.loads(msg.data)),100)
        self.create_subscription(RosString,'/sim/camera/sensor',lambda msg:self.on_sensor(json.loads(msg.data)),100)
        self.sensor_control=self.create_publisher(RosString,'/sim/camera/sensor_control',100)
        self.create_subscription(SimEvent,'/sim/events/fire',self.on_shot,100)
        self.create_subscription(SimEvent,'/sim/events/hit',self.on_shot,100)
        self.scenario_clients = {
            'load': self.create_client(SimScenarioLoad, '/sim/scenario/load'),
            'start': self.create_client(SimScenarioStart, '/sim/scenario/start'),
            'begin': self.create_client(SimScenarioStart, '/sim/scenario/begin'),
            'reset': self.create_client(SimScenarioReset, '/sim/scenario/reset'),
        }
        self.create_timer(0.05, self.watchdog)
        if self.command_script:self.create_timer(1/120,self.play_script)
        if os.environ.get('RM26_RECORD_TIMING')=='1':
            self.timing_log=self.state_path.with_name('control-timing.jsonl').open('w',buffering=65536)
        if os.environ.get('DAEDALUS_EXECUTION_MODE')=='lockstep':
            from sync_bridge import Coordinator
            self.sync=Coordinator(self,self.state_path.with_name('lockstep.sock'),
                os.environ['RM26_TIMING_PROFILE'],os.environ['RM26_SCENE'])

    def clock_ns(self):
        return (self.sync.current_result_time or self.sync.now_ns) if self.sync else time.time_ns()

    def on_shot(self, msg):
        with self.lock:
            if msg.kind == 0:
                self.counts['shots_fired'] += 1
                self.last_shot = time.monotonic()
            elif msg.kind == 1 and msg.target_id == 1:
                self.counts['target_armor_hits'] += 1

    def on_image(self, msg):
        with self.lock:
            stamp = stamp_ns(msg.header.stamp)
            self.counts['received'] += 1
            if self.latest and stamp <= self.latest[0]:
                self.counts['out_of_order'] += 1
                return
            if len(msg.data) > MAX_JPEG:
                self.counts['oversized'] += 1
                return
            self.seq += 1
            self.latest = (stamp, self.seq, bytes(msg.data))
            if self.input_contract=='aligned-v1' and not self.sync and self.warmup_state=='warming':
                if len(self.warmup_images)>=64:
                    self.warmup_error='Counted warmup image queue overflow';self.warmup_state='failed';self.warmup_done.set()
                else:self.warmup_images.append(self.latest)
            if self.sync and os.environ.get('RM26_SYNC_PREVIEW'):
                preview=Path(os.environ['RM26_SYNC_PREVIEW'])
                temporary=preview/'latest.jpg.tmp';temporary.write_bytes(bytes(msg.data));temporary.replace(preview/'latest.jpg')

    def on_info(self, msg):
        with self.lock:
            self.info = msg

    def on_tf(self, msg):
        with self.lock:
            if self.timing_log and msg.transforms:
                self.timing_log.write(json.dumps(dict(stamp_ns=stamp_ns(msg.transforms[0].header.stamp)))+'\n')
            for tf in msg.transforms:
                self.buffer.set_transform(tf, 'simulator')

    def on_tf_static(self, msg):
        with self.lock:
            for tf in msg.transforms:
                self.buffer.set_transform_static(tf, 'simulator')

    def on_scenario(self, msg):
        with self.lock:
            previous = self.scenario
            if previous and (previous['id'] != msg.scenario_id
                    or msg.t_scenario < previous['time']-0.5):
                # A simulator restarted/reset outside the manager must not
                # inherit an enabled controller and the previous EKF history.
                # Managed scene changes explicitly restore their saved switch
                # only after spawning and homing have completed.
                self.control = False
                self.reset_stream()
                self.last_error = 'Scenario reset; waiting for a fresh stream'
            if msg.phase == 2 or (previous.get('phase') == 1 and msg.phase == 0):
                if not self.scenario_boundary:self.fire_armed = False
            self.scenario = {'id': msg.scenario_id, 'phase': msg.phase,
                'stamp_ns': stamp_ns(msg.header.stamp),
                'time': msg.t_scenario, 'spawn_applied': msg.spawn_applied,
                'spawn_error': msg.spawn_error}
            if msg.phase == 2 and previous.get('phase') == 1:
                # Freeze metrics at the end of motion; later stationary frames
                # must not dilute a motion scenario's result.
                name = msg.scenario_id if msg.scenario_id in ('rm26_static','rm26_line','rm26_spin') else 'scenario'
                self.state_path.with_name(name+'-completed.json').write_text(
                    json.dumps(self.status(),allow_nan=False,indent=2))

    def on_boundary(self,value):
        with self.lock:
            if not accept_boundary(self.scenario_boundary,value):return
            previous=self.scenario_boundary
            self.scenario_boundary=dict(value)
            if value['phase']==2:self.fire_armed=False
            if not previous or any(previous[k]!=value[k] for k in ('id','generation','phase')):
                self.archive('scenario_boundary',value)

    def physical_phase(self):
        value=self.scenario_boundary
        if value and value['id']==self.scenario.get('id'):return value['phase']
        return self.scenario.get('phase')

    def on_sensor(self,value):
        with self.lock:
            if value.get('kind')=='reset':self.sensor_reset_ack=value['reset_id']
            elif value.get('kind')=='frame':
                self.sensor_frames[value['stamp_ns']]=value
                while len(self.sensor_frames)>512:self.sensor_frames.popitem(last=False)

    def start_warmup(self):
        with self.lock:
            self.reset_stream();self.control=True
            self.warmup_state='warming';self.warmup_processed=[];self.warmup_images.clear();self.warmup_done.clear();self.warmup_error=None
            self.sensor_reset_id=self.session;self.sensor_reset_ack=None
        if self.sync:
            self.sync.warm(self.sync.scene)
        else:
            deadline=time.monotonic()+4
            while self.sensor_reset_ack!=self.sensor_reset_id:
                self.sensor_control.publish(RosString(data=json.dumps(dict(reset_id=self.sensor_reset_id))))
                if time.monotonic()>deadline:raise RuntimeError('Native sensor reset was not acknowledged')
                time.sleep(.02)
            if not self.warmup_done.wait(8):raise RuntimeError('Counted warmup did not finish')
        with self.lock:
            if self.warmup_error:raise RuntimeError(self.warmup_error)
            if len(self.warmup_processed)!=self.warmup_frames:raise RuntimeError('Warmup input count mismatch')
            self.warmup_state='parked'
            self.archive('warmup_contract',dict(contract=self.input_contract,completed_frames=self.warmup_frames,
                inputs=self.warmup_processed,sensor_reset_id=self.sensor_reset_id,stamp_ns=self.clock_ns()))
        return self.status()

    def count_warmup(self,expected):
        if self.input_contract=='aligned-v1' and self.warmup_state=='warming':
            self.warmup_processed.append(dict(seq=expected['seq'],stamp_ns=expected['stamp_ns'],sensor=expected.get('sensor')))
            if len(self.warmup_processed)==self.warmup_frames:
                if not self.sync:self.warmup_state='parked'
                self.warmup_done.set()

    def reset_stream(self):
        self.previous_fire_pose = None
        with self.lock:
            self.session = uuid.uuid4().hex
            self.latest = None
            self.last_valid = 0
            self.last_command = None
            self.homing_until = 0.0
            self.fire_armed = False
            self.last_fire = 0
            self.last_shot = 0.0
            if self.command_script:self.command_script.reset()
            self.buffer.clear()
            self.counts.clear()
            for samples in (self.ages,self.pipeline_times,self.yaw_errors,self.pitch_errors):
                samples.clear()
            self.started = time.monotonic()
            if self.session_archive:self.session_archive.close();self.session_archive=None
            if self.archive_requested:
                from session_evidence import SessionArchive
                self.session_archive=SessionArchive(self.state_path.parent/'session-evidence',self.session)
            self.hold()

    def archive(self,kind,data,jpeg=b''):
        archive=self.session_archive
        if archive:archive.record(kind,data,jpeg)

    def publish_eligibility(self, enabled, command_stamp_ns):
        if self.sync:
            self._sync_eligible=bool(enabled);return
        if self.algorithm_profile in ('tracking_fire_v2','tracking_fire_v3','tracking_fire_v4m','tracking_fire_v4mt','tracking_fire_v4mtr','image_v1'):
            self.eligibility_publisher.publish(RosString(data=json.dumps({
                'issued_ns':time.time_ns(),'command_stamp_ns':command_stamp_ns,'enabled':bool(enabled)})))

    def hold(self):
        if self.sync:
            cmd=GimbalCmd();cmd.header.frame_id='map';cmd.header.stamp.sec,cmd.header.stamp.nanosec=divmod(self.clock_ns(),1_000_000_000)
            cmd.distance=-1.;cmd.fire_advice=False
            self._sync_command=self.sync.command(cmd,False)
            return
        self.publish_eligibility(False,self.latest[0] if self.latest else time.time_ns())
        if time.monotonic() < self.homing_until:
            return
        cmd = GimbalCmd()
        cmd.header.stamp = self.get_clock().now().to_msg()
        cmd.header.frame_id = 'map'
        cmd.distance = -1.0
        cmd.fire_advice = False
        self.publisher.publish(cmd)

    def play_script(self):
        # Rendering diagnostics use an independent scheduler. Image inference
        # latency cannot move the commanded playback timeline.
        with self.lock:
            if not (self.command_script and self.control and self.connected and self.scenario.get('phase')==1):return
            now_ns=time.time_ns();elapsed=(now_ns-self.scenario['stamp_ns'])*1e-9+self.scenario['time']
            row=self.command_script.at(elapsed)
            if not row['valid']:self.hold();return
            fire=(row['fire_advice'] and self.fire_armed and self.fire_mode=='auto' and time.monotonic()-self.last_fire>=1/self.fire_rate_hz)
            if fire:
                self.command_script.fired();self.last_fire=time.monotonic();self.counts['fire_pulses']+=1
            cmd=GimbalCmd();cmd.header.stamp.sec,cmd.header.stamp.nanosec=divmod(now_ns,1000000000)
            cmd.header.frame_id='map';cmd.yaw=math.degrees(row['yaw']);cmd.pitch=-math.degrees(row['pitch']);cmd.distance=row['distance'];cmd.fire_advice=fire
            self.publisher.publish(cmd);self.last_valid=time.monotonic()

    def watchdog(self):
        with self.lock:
            if self.sync:return
            wall, mono = time.time_ns(), time.monotonic_ns()
            if self.fire_policy == 'continuous':
                enabled = (self.fire_mode == 'auto' and self.fire_armed and self.control
                    and self.connected and self.scenario.get('phase') == 1)
                self.continuous_publisher.publish(RosString(data=json.dumps({
                    'stamp_ns':wall, 'enabled':enabled})))
            if abs((wall-self.last_wall) - (mono-self.last_mono)) > 50000000:
                self.reset_stream()
                self.control = False
                self.last_error = 'Wall clock changed; control disabled'
            self.last_wall, self.last_mono = wall, mono
            if time.monotonic() < self.homing_until:
                # Asset rest pose points the bore ~25 degrees upwards. Scene
                # preparation homes to the known map heading/horizon without
                # exposing any target truth to the vision algorithm.
                cmd = GimbalCmd()
                cmd.header.stamp = self.get_clock().now().to_msg()
                cmd.header.frame_id = 'map'
                cmd.yaw = self.homing_yaw_deg
                cmd.pitch = 0.0
                cmd.distance = 4.0
                cmd.fire_advice = False
                self.publisher.publish(cmd)
            elif not self.control or not self.connected or time.monotonic()-self.last_valid > 0.25:
                self.hold()
            elif self.last_command:
                # Measure command-to-actuator error, not image-centre error:
                # the original Aimer intentionally adds ballistic lead.
                try:
                    tf = self.buffer.lookup_transform('map', 'gimbal_link', Time())
                    r = quaternion_matrix(tf.transform.rotation)
                    yaw = math.atan2(r[3], r[0])
                    pitch = math.atan2(r[6], math.hypot(r[0], r[3]))
                    dy = math.remainder(self.last_command['yaw']-yaw, 2*math.pi)
                    dp = -self.last_command['pitch']-pitch
                    self.yaw_errors.append(abs(math.degrees(dy)))
                    self.pitch_errors.append(abs(math.degrees(dp)))
                except Exception:
                    pass
            if time.monotonic()-self.last_status_write >= 1:
                self.last_status_write = time.monotonic()
                state = self.status()
                temporary = self.state_path.with_suffix('.tmp')
                temporary.write_text(json.dumps(state, allow_nan=False, indent=2))
                temporary.replace(self.state_path)
                print(json.dumps(state, allow_nan=False), flush=True)

    def status(self):
        with self.lock:
            now = self.clock_ns()
            age_ms = (now-self.latest[0])/1e6 if self.latest else None
            issue = self.last_error
            if age_ms is None or age_ms > 250:
                issue = 'Waiting for a fresh image stream'
            elif not self.connected:
                issue = 'Algorithm disconnected'
            return {'session': self.session, 'connected': self.connected,
                'control': self.control,
                'initializing': time.monotonic() < self.homing_until,
                'active': self.control and self.connected and time.monotonic()-self.last_valid <= 0.25,
                'frame_age_ms': age_ms,
                'scenario': self.scenario, 'counts': dict(self.counts),
                'processed_hz': self.counts['processed']/max(time.monotonic()-self.started, 0.001),
                'age_p95_ms': percentile(self.ages, .95),
                'pipeline_p95_ms': percentile(self.pipeline_times, .95),
                'yaw_error_p95_deg': percentile(self.yaw_errors, .95),
                'pitch_error_p95_deg': percentile(self.pitch_errors, .95),
                'last_result': self.last_command, 'last_error': issue,
                'observer_dropped': self.observer.dropped,
                'services_ready': all(client.service_is_ready() for client in self.scenario_clients.values()),
                'fire_enabled': (self.fire_mode == 'auto' and self.fire_armed and self.control
                    and self.connected and (self.fire_policy == 'continuous' or time.monotonic()-self.last_valid <= .25)
                    and self.scenario.get('phase') == 1),
                'fire_armed':self.fire_armed,
                'algorithm_profile':self.algorithm_profile,'fire_rate_limit_hz':self.fire_rate_hz,'fire_mode':self.fire_mode,'fire_policy':self.fire_policy,'actual_bullet_speed':self.actual_bullet_speed,
                'reported_bullet_speed':self.bullet_speed,'physics':self.physics,
                'execution_mode':'lockstep' if self.sync else 'realtime',
                'input_contract':self.input_contract,'physical_boundary':self.scenario_boundary,
                'warmup':dict(state=self.warmup_state,frames=len(self.warmup_processed),requested=self.warmup_frames),
                'synchronization':self.sync.state() if self.sync else None}

    def frame(self, after):
        with self.lock:
            if self.input_contract=='aligned-v1' and self.warmup_state in ('await_recorder','parked','failed'):return None
            if not self.latest or not self.info:
                return None
            stamp, seq, jpeg = self.latest
            queued=False
            while self.input_contract=='aligned-v1' and self.warmup_images:
                candidate=self.warmup_images[0];metadata=self.sensor_frames.get(candidate[0])
                if candidate[1]<=after or metadata and metadata['reset_id']!=self.sensor_reset_id:
                    self.warmup_images.popleft();continue
                stamp,seq,jpeg=candidate;queued=True;break
            sensor=self.sensor_frames.get(stamp)
            if self.input_contract=='aligned-v1' and self.warmup_state in ('warming','measuring'):
                if not sensor or sensor['reset_id']!=self.sensor_reset_id:return None
            if seq <= after or not -10000000 <= self.clock_ns()-stamp <= 250000000:
                return None
            if self.info.header.frame_id != 'camera_optical_frame' or len(self.info.k) != 9:
                self.last_error = 'Unsupported camera calibration'
                return None
            try:
                at = Time(nanoseconds=stamp)
                attitude = self.buffer.lookup_transform('map', 'gimbal_link', at)
                camera_map = self.buffer.lookup_transform('map', 'camera_optical_frame', at)
                extrinsic = self.buffer.lookup_transform('gimbal_link', 'camera_optical_frame', at)
                muzzle = self.buffer.lookup_transform('map','muzzle_link',at)
                r = quaternion_matrix(extrinsic.transform.rotation)
            except Exception as error:
                self.last_error = 'Waiting for image-time TF: ' + str(error)[:160]
                return None
            t = extrinsic.transform.translation
            # Quantisation removes float32 TF roundoff on rigid extrinsics.
            calibration = {'width': self.info.width, 'height': self.info.height,
                'k': list(self.info.k), 'd': list(self.info.d),
                'r_camera2gimbal': [round(x, 5) for x in r],
                't_camera2gimbal': [round(x, 5) for x in (t.x,t.y,t.z)]}
            if len(calibration['d']) != 5 or not all(math.isfinite(x) for x in calibration['k'] + calibration['d'] + r):
                self.last_error = 'Invalid or unsupported camera calibration'
                return None
            # The source camera's position can vary relative to the model's
            # gimbal pivot. Apply extrinsics each frame; reset tracking only for
            # an intrinsic calibration change, not for camera/gimbal motion.
            intrinsic = {key:calibration[key] for key in ('width','height','k','d')}
            calibration['id'] = hashlib.sha256(json.dumps(intrinsic, sort_keys=True).encode()).hexdigest()[:16]
            q = attitude.transform.rotation
            self.last_error = ''
            header = {'type': 'frame', 'session': self.session, 'seq': seq,
                'stamp_ns': stamp, 'calibration': calibration,
                'q_gimbal2world': [q.x,q.y,q.z,q.w], 'bullet_speed': self.bullet_speed}
            origin = attitude.transform.translation
            position, rotation = camera_map.transform.translation, camera_map.transform.rotation
            header['gimbal_position_map'] = [origin.x,origin.y,origin.z]
            m = muzzle.transform.translation
            header['muzzle_position_map'] = [m.x,m.y,m.z]
            header['camera_in_map'] = {'position':[position.x,position.y,position.z],
                'quaternion':[rotation.x,rotation.y,rotation.z,rotation.w]}
            if sensor:header['sensor']=dict(sensor)
            # Fresh own pose is independent of image-time localization. This
            # carries no target state and is used only for firing eligibility.
            try:
                latest_pose=self.buffer.lookup_transform('map','gimbal_link',Time())
                matrix=quaternion_matrix(latest_pose.transform.rotation)
                header['latest_muzzle_pose']={'stamp_ns':stamp_ns(latest_pose.header.stamp),
                    'yaw':math.atan2(matrix[3],matrix[0]),
                    'elevation':math.atan2(matrix[6],math.hypot(matrix[0],matrix[3]))}
                if self.algorithm_profile in ('tracking_fire_v4mt','tracking_fire_v4mtr','image_v1'):
                    pose=header['latest_muzzle_pose'];old=self.previous_fire_pose
                    pose['rates_valid']=False
                    if old:
                        dt=(pose['stamp_ns']-old['stamp_ns'])*1e-9
                        if 0<dt<=.1:
                            yaw_rate=math.remainder(pose['yaw']-old['yaw'],2*math.pi)/dt
                            pitch_rate=(pose['elevation']-old['elevation'])/dt
                            if abs(yaw_rate)<=8 and abs(pitch_rate)<=8:
                                pose.update(rates_valid=True,yaw_rate_radps=yaw_rate,elevation_rate_radps=pitch_rate)
                    self.previous_fire_pose=pose.copy()
            except Exception:
                pass # Missing fresh pose explicitly fails the new firing gate.
            if time.monotonic()-self.last_debug_write >= 5:
                self.last_debug_write = time.monotonic()
                # One bounded diagnostic snapshot, paired with its exact input
                # metadata; no unbounded recording or ground-truth input.
                self.state_path.with_name('latest.jpg').write_bytes(jpeg)
                self.state_path.with_name('latest-frame.json').write_text(json.dumps(header,indent=2))
            if queued:self.warmup_images.popleft()
            return header, jpeg

    def result(self, result, expected, jpeg):
        if os.environ.get('RM26_ALGORITHM_PROTOCOL')=='rm26-image-v1':
            from benchmark.protocol import validate_result
            validate_result(expected or {},result)
        with self.lock:
            if not expected or any(result.get(k) != expected[k] for k in ('seq','stamp_ns','session')):
                raise ValueError('Command does not match the outstanding frame')
            if result['session'] != self.session:
                self.hold()
                return
            self.counts['processed'] += 1
            self.counts['detection_frames'] += bool(result.get('detections', 0))
            self.counts['tracking_frames'] += bool(result.get('tracking', False))
            age = (self.clock_ns()-result['stamp_ns'])/1e6
            self.ages.append(age)
            pipeline_ms = float(result.get('pipeline_ms', 0))
            if math.isfinite(pipeline_ms):
                self.pipeline_times.append(pipeline_ms)
            if self.command_script and self.scenario.get('phase')==1:
                elapsed=(result['stamp_ns']-self.scenario['stamp_ns'])*1e-9+self.scenario['time']
                algorithm_command={k:result.get(k) for k in ('yaw','pitch','distance','valid','fire_advice')}
                result=dict(result,**self.command_script.at(elapsed))
                result['algorithm_command']=algorithm_command;result['control_source']='fixed_script';result['script_time_s']=elapsed
                result['script_expired_fires']=self.command_script.expired_fires
            values = [float(result.get(k, math.nan)) for k in ('yaw','pitch','distance')]
            valid = (result.get('valid') is True and all(math.isfinite(v) for v in values)
                and values[2] > 0 and -10 <= age <= 250)
            self.counts['solutions'] += valid
            self.last_command = result
            applied = valid and self.control
            now = self.clock_ns() if self.sync else time.monotonic()
            fire_interval = math.ceil(1_000_000_000/self.fire_rate_hz) if self.sync else 1/self.fire_rate_hz
            suggested = result.get('fire_advice') is True
            eligible = (applied and suggested and self.fire_mode == 'auto' and self.fire_armed
                and self.connected and self.physical_phase() == 1)
            self.publish_eligibility(eligible,result['stamp_ns'])
            fire = (not self.command_script and applied and suggested and self.fire_policy == 'advice' and self.fire_mode == 'auto' and self.fire_armed
                and self.physical_phase() == 1 and now-self.last_fire >= fire_interval)
            if self.physical_phase() == 1:
                self.counts['fire_suggestions'] += suggested
            if fire:
                self.last_fire = now
                self.counts['fire_pulses'] += 1
            observation={'frame':expected,'result':result,
                'receipt_ns':self.clock_ns(),'wall_receipt_ns':time.time_ns(),'command_emitted':applied,
                'control_requested':self.control,'fire_forwarded':fire}
            if self.scenario_boundary:observation['scenario_boundary']=dict(self.scenario_boundary)
            self.archive('result',observation)
            self.observer.publish(observation,jpeg)
            if not valid or not self.control:
                self.counts['invalid_or_preview'] += 1
                self.last_valid = 0
                self.hold()
                self.count_warmup(expected)
                return
            if self.command_script:return
            cmd = GimbalCmd()
            cmd.header.stamp.sec, cmd.header.stamp.nanosec = divmod(result['stamp_ns'], 1000000000)
            cmd.header.frame_id = 'map'
            cmd.yaw = math.degrees(values[0])
            cmd.pitch = -math.degrees(values[1])
            cmd.distance = values[2]
            cmd.yaw_diff = cmd.pitch_diff = 0.0
            if result.get('temporal_command') and self.algorithm_profile in ('tracking_fire_v4mt','tracking_fire_v4mtr','image_v1'):
                cmd.temporal_valid=True
                cmd.aim_stamp_ns=int(result['aim_stamp_ns'])
                cmd.yaw_rate_radps=float(result['yaw_rate_radps'])
                cmd.pitch_rate_radps=-float(result['pitch_rate_radps'])
            cmd.fire_advice = fire
            if self.sync:self._sync_command=self.sync.command(cmd,eligible)
            else:self.publisher.publish(cmd)
            self.counts['controlled'] += 1
            self.last_valid = time.monotonic()
            self.count_warmup(expected)

    def service(self, kind, request):
        client = self.scenario_clients[kind]
        if not client.wait_for_service(timeout_sec=3):
            raise RuntimeError('Scenario service unavailable: ' + kind)
        complete = threading.Event()
        future = client.call_async(request)
        future.add_done_callback(lambda _: complete.set())
        if not complete.wait(5):
            raise RuntimeError('Scenario service timed out: ' + kind)
        response = future.result()
        if not response.success:
            raise RuntimeError(response.message)
        return response

    def admin(self, request):
        op = request.get('op')
        if op == 'status':
            return self.status()
        if op in ('scene','prepare'):
            self.archive_requested=request.get('record_session') is True
            self.input_contract=request.get('input_contract','legacy')
            self.warmup_frames=request.get('warmup_frames',30)
            if self.input_contract=='aligned-v1' and (not self.archive_requested or not 3<=self.warmup_frames<=120):
                raise ValueError('Aligned input requires full evidence and a valid warmup count')
        if op=='warm':
            if self.input_contract!='aligned-v1':raise ValueError('Explicit warmup requires aligned-v1')
            return self.start_warmup()
        if op == 'seal_session':
            if self.control:raise RuntimeError('Disable control before sealing session evidence')
            with self.lock:
                self.archive('session_stop',dict(stamp_ns=self.clock_ns(),scenario=self.scenario))
                if self.session_archive:self.session_archive.close();self.session_archive=None
            return dict(sealed=(self.state_path.parent/'session-evidence/manifest.json').exists())
        if op == 'begin':
            self.archive('measurement_request',dict(stamp_ns=self.clock_ns(),scenario=self.scenario))
        if self.sync and op in ('scene','prepare','begin','control','prime'):
            from sync_bridge import prepare
            return prepare(self,request)
        if op == 'diagnosis_reset':
            with self.lock:
                if self.scenario.get('phase')!=0:raise RuntimeError('Diagnostic reset requires an Idle scene')
                self.reset_stream()
            return self.status()
        if op == 'control':
            if request.get('value') not in ('on', 'off'):
                raise ValueError('Control must be on or off')
            with self.lock:
                self.control = request['value'] == 'on'
                if not self.control:
                    self.fire_armed = False
                self.homing_until = 0.0
                self.last_valid = 0
                self.hold()
            return self.status()
        if op in ('scene','prepare'):
            scene = request.get('name')
            if scene not in SCENES:
                raise ValueError('Unknown scene')
            with self.lock:
                resume = self.control if op == 'scene' else request.get('control', 'on') == 'on'
                self.control = False
                self.reset_stream()
            req = SimScenarioLoad.Request()
            duration = request.get('duration')
            if duration is not None and (not isinstance(duration,(int,float)) or not 1 <= duration <= 18):
                raise ValueError('Short experiment duration must be within 1..18 seconds')
            req.scenario_path = ('/run/rm26-scenarios/' if duration is not None else '/scenarios/') + scene + '.toml'
            seed=request.get('seed',20261019)
            if isinstance(seed,bool) or not isinstance(seed,int) or not 0<=seed<=4294967295:raise ValueError('Invalid unsigned scenario seed')
            req.seed = seed
            self.service('load', req)
            deadline = time.monotonic()+12
            while time.monotonic() < deadline:
                with self.lock:
                    state = self.scenario.copy()
                if state.get('id') == 'rm26_'+scene and state.get('spawn_error'):
                    raise RuntimeError(state['spawn_error'])
                if (state.get('id') == 'rm26_'+scene and state.get('phase') == 0
                        and state.get('spawn_applied')):
                    break
                time.sleep(.05)
            else:
                raise RuntimeError('Scenario spawn did not become ready')
            # Spawns are dropped above the ground; let contact physics settle.
            time.sleep(2)
            with self.lock:
                self.homing_yaw_deg = semantics(scene)['homing_yaw_deg']
                self.homing_until = time.monotonic()+1.5
            time.sleep(1.6)
            with self.lock:
                self.reset_stream()
                self.control = resume
                if self.input_contract=='aligned-v1':self.warmup_state='await_recorder'
            if op == 'scene':
                self.service('begin', SimScenarioStart.Request())
                with self.lock:
                    self.fire_armed = self.fire_mode == 'auto'
            return self.status()
        if op == 'begin':
            with self.lock:
                if self.scenario.get('phase') != 0:
                    raise RuntimeError('Prepare an Idle scene before beginning')
                self.counts.clear()
                for values in (self.ages,self.pipeline_times,self.yaw_errors,self.pitch_errors):
                    values.clear()
                self.started = time.monotonic()
            if self.input_contract=='aligned-v1' and self.warmup_state!='parked':raise RuntimeError('Counted warmup must finish before begin')
            with self.lock:self.fire_armed=request.get('fire') is True and self.fire_mode=='auto'
            self.service('begin', SimScenarioStart.Request())
            if self.input_contract=='aligned-v1':
                deadline=time.monotonic()+3
                while self.physical_phase()!=1:
                    if time.monotonic()>deadline:raise RuntimeError('Physical Running boundary not received')
                    time.sleep(.001)
                self.warmup_state='measuring'
            with self.lock:
                self.fire_armed = request.get('fire') is True and self.fire_mode == 'auto'
            return self.status()
        raise ValueError('Unknown administrative operation')


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        bridge = self.server.bridge
        acquired = False
        self.request.settimeout(180 if bridge.sync else 3)
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        expected, expected_image, after, last_send = None, b'', 0, 0.0
        try:
            header, data = receive(self.request)
            if data:
                raise ValueError('Client messages must not contain image data')
            if header.get('type') == 'admin':
                try:
                    send(self.request, {'type':'response','ok':True,'result':bridge.admin(header)})
                except Exception as error:
                    send(self.request, {'type':'response','ok':False,'error':str(error)})
                return
            acquired = bridge.client_lock.acquire(blocking=False)
            if not acquired:
                raise RuntimeError('An algorithm client is already connected')
            with bridge.lock:
                bridge.connected = True
                bridge.last_valid = 0
            while True:
                kind = header.get('type')
                if kind == 'next':
                    if expected is not None:
                        raise ValueError('Outstanding frame requires a command response')
                    deadline = time.monotonic()+.4
                    frame = None
                    while time.monotonic() < deadline:
                        if bridge.sync:
                            frame=bridge.sync.next()
                            break
                        # The native camera already publishes at the declared
                        # capture rate. A second arrival-clock limiter slowly
                        # skips source frames in the aligned experiment path.
                        if bridge.input_contract=='aligned-v1' or time.monotonic()-last_send >= 1/bridge.fps:
                            frame = bridge.frame(after)
                            if frame:
                                break
                        time.sleep(.003)
                    if frame:
                        expected, jpeg = frame
                        expected['sent_ns']=bridge.sync.now_ns if bridge.sync else time.time_ns()
                        if expected['session']==bridge.session:bridge.archive('input',expected,jpeg)
                        expected_image = jpeg
                        after = expected['seq']
                        last_send = time.monotonic()
                        send(self.request, expected, jpeg)
                    else:
                        send(self.request, {'type':'wait'})
                elif kind == 'command':
                    bridge.result(header, expected, expected_image)
                    if bridge.sync:
                        if bridge._sync_command is None:raise RuntimeError('Missing coordinated command response')
                        bridge.sync.submit(expected,*bridge._sync_command);bridge._sync_command=None
                    expected = None
                    expected_image = b''
                else:
                    raise ValueError('Unknown algorithm operation')
                header, data = receive(self.request)
                if data:
                    raise ValueError('Unexpected binary payload')
        except (EOFError, ConnectionError, socket.timeout):
            pass
        except Exception as error:
            bridge.get_logger().warning(str(error))
        finally:
            if acquired:
                with bridge.lock:
                    bridge.connected = False
                    bridge.last_valid = 0
                    bridge.hold()
                bridge.client_lock.release()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18726)
    parser.add_argument('--fps', type=float, default=30)
    parser.add_argument('--bullet-speed', type=float, default=25)
    parser.add_argument('--actual-bullet-speed',type=float,default=25)
    parser.add_argument('--fire',choices=['off','auto'],default='off')
    parser.add_argument('--algorithm-profile',choices=['legacy','tracking_v2','tracking_fire_v2','tracking_v3','tracking_fire_v3','tracking_fire_v4m','tracking_fire_v4mt','tracking_fire_v4mtr','image_v1'],default='legacy')
    parser.add_argument('--fire-policy',choices=['advice','continuous'],default='advice')
    parser.add_argument('--fire-rate-hz',type=float,default=5.0)
    parser.add_argument('--physics',choices=['no-drag','drag'],default='no-drag')
    parser.add_argument('--state', default='/run/rm26-sim/bridge-status.json')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not math.isfinite(args.fps) or not 1 <= args.fps <= 120:
        parser.error('Invalid port or frame rate')
    if not math.isfinite(args.fire_rate_hz) or not 0 < args.fire_rate_hz <= 5:
        parser.error('Fire rate must be within (0,5] Hz')
    rclpy.init()
    bridge = Bridge(args)
    server = Server(('127.0.0.1', args.port), Handler)
    server.bridge = bridge
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        rclpy.spin(bridge)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        if bridge.sync:bridge.sync.stop.set()
        if bridge.timing_log:bridge.timing_log.close()
        if bridge.session_archive:bridge.session_archive.close()
        with bridge.lock:
            bridge.control = False
            if rclpy.ok():
                bridge.hold()
        server.shutdown()
        server.server_close()
        bridge.observer.close()
        bridge.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
