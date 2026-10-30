"""Per-instance acknowledged stepping. Only image/own-pose data reach vision."""
import collections
import json
import math
from pathlib import Path
import queue
import socket
import threading
import time

from sync_timing import Schedule,load_profile

EPOCH_NS=1_700_000_000_000_000_000


class StepClient:
    def __init__(self,path):
        self.path=str(path);self.socket=None;self.reader=None;self.id=0;self.generation=None

    def step(self,delta,capture=False,command=None,eligibility=None,begin_scene=False,align_epoch=False,observe_only=False,reset_scene=False,sensor_reset=None):
        if self.socket is None:
            self.socket=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.socket.settimeout(15)
            deadline=time.monotonic()+60
            while True:
                try:self.socket.connect(self.path);break
                except (ConnectionRefusedError,FileNotFoundError):
                    if time.monotonic()>deadline:raise
                    self.socket.close();time.sleep(.05)
                    self.socket=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.socket.settimeout(15)
            self.reader=self.socket.makefile('rb')
        request=dict(id=self.id+1,delta_ns=int(delta),capture=bool(capture))
        if begin_scene:request['begin_scene']=True
        if align_epoch:request['align_epoch']=True
        if observe_only:request['observe_only']=True
        if reset_scene:request['reset_scene']=True
        if sensor_reset is not None:request['sensor_reset']=sensor_reset
        if command is not None:
            request.update(command=command,eligibility=eligibility)
        if self.generation is not None and (command is not None or begin_scene or reset_scene):request['generation']=self.generation
        self.socket.sendall((json.dumps(request,allow_nan=False)+'\n').encode())
        data=self.reader.readline(65537)
        if not data or len(data)>65536:raise RuntimeError('Invalid step acknowledgement')
        result=json.loads(data)
        if not result.get('ok') or result.get('id')!=request['id']:raise RuntimeError('Step rejected: '+str(result))
        if reset_scene and result.get('reset_scene') is not True:raise RuntimeError('Simulator did not acknowledge preparation reset')
        if sensor_reset is not None and result.get('sensor_reset_id')!=sensor_reset:raise RuntimeError('Simulator did not acknowledge sensor reset')
        self.id=request['id'];self.generation=result['generation'];return result


class Coordinator:
    def __init__(self,bridge,path,profile,scene):
        self.bridge=bridge;self.client=StepClient(path);self.profile=load_profile(profile)
        self.schedule=Schedule(self.profile,scene);self.scene=scene
        self.now_ns=EPOCH_NS;self.mode='boot';self.error=None;self.stop=threading.Event()
        self.lock=threading.RLock();self.condition=threading.Condition(self.lock)
        self.transitions=queue.Queue();self.phase_history=collections.deque(maxlen=32)
        self.ready=queue.Queue(maxsize=1);self.response=queue.Queue(maxsize=1)
        self.pending_command=None;self.outstanding=None;self.phase_end=None;self.phase_done=threading.Event()
        self.image_due=EPOCH_NS;self.image_index=0;self.captured=collections.deque()
        self.delivered=0;self.generation=None;self.wait_wall_s=0.;self.steps=0
        self.warm_remaining=0;self.current_result_time=None;self.frame_sequence=[]
        self.warm_drain=False
        self.begin_requested=False
        self.align_requested=False
        self.reset_requested=False
        self.sensor_reset_requested=None
        self.release_home_pending=False
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()

    def state(self):
        return dict(mode=self.mode,stamp_ns=self.now_ns,step=self.steps,generation=self.generation,
            delivered_frames=self.delivered,profile=self.profile['name'],error=self.error,wait_wall_s=self.wait_wall_s,
            phase_history=list(self.phase_history))

    def pipeline_state(self):
        return dict(image_due_ns=self.image_due,image_index=self.image_index,
            control_index=self.schedule.step_index,capture_interval_index=self.schedule.image_index,
            queued_images=[dict(stamp_ns=r[4]['stamp_ns'],delivery_ns=r[0],seq=r[4]['seq']) for r in self.captured],
            pending_command_receipt_ns=self.pending_command[0] if self.pending_command else None)

    def transition(self,mode,seconds=None,scene=None):
        # Only the stepping thread changes the schedule and phase. A ROS RPC
        # can arrive while tick() is waiting for its ACK: using now_ns there
        # would start the next phase one control step too early. The request is
        # acknowledged at the boundary AFTER that step has finished.
        self.check()
        request=dict(mode=mode,seconds=seconds,scene=scene,entered=threading.Event(),done=threading.Event())
        self.transitions.put(request)
        with self.condition:self.condition.notify_all()
        deadline=time.monotonic()+40
        while not request['entered'].wait(.1):
            self.check()
            if time.monotonic()>deadline:raise RuntimeError('Phase transition was not acknowledged: '+mode)
        self.check()
        return request['done']

    def enter_transitions(self):
        while True:
            try:request=self.transitions.get_nowait()
            except queue.Empty:return
            mode=request['mode'];self.mode=mode;self.phase_done=request['done']
            self.phase_end=self.now_ns+round(request['seconds']*1e9) if request['seconds'] is not None else None
            if mode=='warm':
                self.scene=request['scene'];self.schedule=Schedule(self.profile,self.scene)
                self.captured.clear();self.image_due=self.now_ns;self.image_index=0
                self.warm_remaining=self.profile['warmup_frames'];self.align_requested=True
                if getattr(self.bridge,'input_contract','legacy')=='aligned-v1':
                    self.warm_remaining=self.bridge.warmup_frames
                    self.sensor_reset_requested=self.bridge.session
                self.warm_drain=False
            elif mode=='measure':
                before=self.pipeline_state()
                if not self.profile.get('continuous_measurement',False):
                    self.schedule=Schedule(self.profile,self.scene);self.captured.clear()
                    self.image_due=self.now_ns;self.image_index=0
                self.begin_requested=True
                if hasattr(self.bridge,'archive'):
                    self.bridge.archive('measurement_pipeline',dict(stamp_ns=self.now_ns,
                        continuous=self.profile.get('continuous_measurement',False),before=before,after=self.pipeline_state()))
            else:
                self.captured.clear();self.pending_command=None
                self.warm_drain=False
                if mode=='release_home':self.release_home_pending=True
                if mode=='settle_setup' and self.profile.get('preparation_reset',False):
                    self.schedule=Schedule(self.profile,self.scene)
                    self.align_requested=True;self.reset_requested=True
            self.phase_history.append(dict(mode=mode,stamp_ns=self.now_ns,step=self.steps,
                phase_end_ns=self.phase_end,control_index=self.schedule.step_index))
            if mode=='hold':self.phase_done.set()
            request['entered'].set()

    def phase(self,mode,seconds=None,wait=True):
        done=self.transition(mode,seconds)
        if wait and not done.wait(120):raise RuntimeError('Logical preparation timed out: '+mode)
        self.check()

    def check(self):
        if self.error:raise RuntimeError(self.error)

    def warm(self,scene):
        done=self.transition('warm',scene=scene)
        if not done.wait(120):raise RuntimeError('Fixed warmup did not complete')
        self.check()

    def measure(self):
        self.transition('measure')

    def park_warm(self):
        self.bridge.archive('warmup_park',dict(stamp_ns=self.now_ns,pipeline=self.pipeline_state(),
            receipt_boundary=self.profile.get('warmup_receipt_boundary',False)))
        self.mode='hold';self.phase_done.set()

    def next(self):
        self.check()
        try:return self.ready.get(timeout=.2)
        except queue.Empty:return None

    def submit(self,frame,command,eligibility):
        if self.outstanding is None or any(frame[k]!=self.outstanding[k] for k in ('session','seq','stamp_ns')):
            raise ValueError('Response does not match coordinated input')
        self.response.put_nowait((frame.get('logical_receipt_ns',frame['logical_now_ns']),command,eligibility))

    def command(self,cmd,eligible):
        # ROS-generated Python messages provide get_fields_and_field_types;
        # serialize to a plain schema, never pickle executable objects.
        def plain(value):
            if hasattr(value,'get_fields_and_field_types'):
                return {k:plain(getattr(value,k)) for k in value.get_fields_and_field_types()}
            return value
        value=plain(cmd)
        return value,dict(issued_ns=self.current_result_time or self.now_ns,
            command_stamp_ns=value['header']['stamp']['sec']*1_000_000_000+value['header']['stamp']['nanosec'],enabled=eligible)

    def own_pose(self,delay_ns=0):
        from rclpy.time import Time
        from bridge import quaternion_matrix,stamp_ns
        deadline=time.monotonic()+3
        while True:
            tf=self.bridge.buffer.lookup_transform('map','gimbal_link',Time())
            if stamp_ns(tf.header.stamp)==self.now_ns:break
            if time.monotonic()>deadline:raise RuntimeError('Own pose has not acknowledged the logical delivery time')
            time.sleep(.001)
        if delay_ns:
            tf=self.bridge.buffer.lookup_transform('map','gimbal_link',Time(nanoseconds=self.now_ns-delay_ns))
        r=quaternion_matrix(tf.transform.rotation)
        return dict(stamp_ns=stamp_ns(tf.header.stamp),yaw=math.atan2(r[3],r[0]),elevation=math.atan2(r[6],math.hypot(r[0],r[3])))

    def tick(self,capture=False,command=None,eligibility=None):
        b=self.bridge;delta=self.schedule.control_delta()
        if self.release_home_pending:
            # UI reset_stream publishes distance=-1 immediately. The stepping
            # path used to store that hold without ever applying it, leaving
            # the homing PID alive during the first algorithm exposure.
            from rm_interfaces.msg import GimbalCmd
            cmd=GimbalCmd();cmd.header.frame_id='map';cmd.header.stamp.sec,cmd.header.stamp.nanosec=divmod(self.now_ns+delta,1_000_000_000)
            cmd.distance=-1.;cmd.fire_advice=False
            command,eligibility=self.command(cmd,False);self.release_home_pending=False
        if self.mode=='home':
            from rm_interfaces.msg import GimbalCmd
            cmd=GimbalCmd();cmd.header.frame_id='map';cmd.header.stamp.sec,cmd.header.stamp.nanosec=divmod(self.now_ns+delta,1_000_000_000)
            cmd.yaw=b.homing_yaw_deg;cmd.pitch=0.;cmd.distance=4.;cmd.fire_advice=False
            command,eligibility=self.command(cmd,False)
        beginning=self.begin_requested
        result=self.client.step(delta,capture,command,eligibility,begin_scene=beginning,
            align_epoch=self.align_requested,reset_scene=self.reset_requested,sensor_reset=self.sensor_reset_requested)
        self.sensor_reset_requested=None
        self.begin_requested=False;self.align_requested=False;self.reset_requested=False
        self.now_ns=result['stamp_ns'];self.generation=result['generation'];self.steps+=1
        if result.get('scenario_boundary'):b.on_boundary(result['scenario_boundary'])
        if beginning:
            b.archive('measurement_ack',dict(stamp_ns=self.now_ns,step=self.steps,generation=self.generation))
        if capture:
            start=time.monotonic();deadline=start+10
            while True:
                self.check()
                with b.lock:
                    matching=b.latest is not None and b.latest[0]==self.now_ns
                    frame=b.frame(-1) if matching else None
                if frame:break
                if time.monotonic()>deadline:raise RuntimeError('Image/TF did not acknowledge capture step '+str(dict(result,latest_stamp=b.latest[0] if b.latest else None,info=bool(b.info),bridge_error=b.last_error)))
                time.sleep(.001)
            self.wait_wall_s+=time.monotonic()-start
            header,jpeg=frame;index=self.image_index;self.image_index+=1
            input_delay,processing=self.schedule.delays(index)
            self.captured.append((header['stamp_ns']+input_delay,processing,
                self.schedule.return_delay(index),self.schedule.pose_delay(index),header,jpeg))
        return result

    def run(self):
        deadline=time.monotonic()+60
        while not Path(self.client.path).exists() and not self.stop.is_set():
            if time.monotonic()>deadline:self.error='Step socket was not ready';return
            self.stop.wait(.05)
        try:
            while not self.stop.is_set():
                self.enter_transitions()
                if self.mode=='hold':
                    with self.condition:
                        if self.transitions.empty():self.condition.wait(timeout=.05)
                    continue
                b=self.bridge
                if self.mode=='boot' and b.scenario.get('spawn_applied'):
                    self.mode='hold';self.phase_done.set();continue
                if self.phase_end is not None and self.now_ns>=self.phase_end:
                    self.mode='hold';self.phase_end=None;self.phase_done.set();continue
                active=self.mode in ('warm','measure')
                capture=active and self.now_ns>=self.image_due
                pending=self.pending_command
                command=gate=None
                if pending and self.now_ns>=pending[0]:
                    _,command,gate=pending;self.pending_command=None
                    # The vision command topic remains observable, but Bevy's
                    # lockstep consumer exclusively reads the acknowledged queue.
                    from rm_interfaces.msg import GimbalCmd
                    def fill(obj,data):
                        for k,v in data.items():
                            if isinstance(v,dict):fill(getattr(obj,k),v)
                            else:setattr(obj,k,v)
                    msg=GimbalCmd();fill(msg,command);b.publisher.publish(msg)
                self.tick(capture,command,gate)
                if capture:self.image_due+=self.schedule.image_interval()
                if self.warm_drain and command is not None:
                    # The last warmup result has a logical future receipt. Do
                    # not begin measurement before it; continue camera sampling
                    # while advancing through that receipt and command ACK.
                    self.warm_drain=False;self.park_warm();continue
                if active and self.pending_command is None and self.captured and self.captured[0][0]<=self.now_ns:
                    _,processing,return_delay,pose_delay,header,jpeg=self.captured.popleft()
                    header=dict(header,clock_mode='simulation',logical_now_ns=self.now_ns+processing,
                        logical_receipt_ns=self.now_ns+processing+return_delay,
                        step_id=self.steps,scenario_generation=self.generation)
                    header['latest_muzzle_pose']=self.own_pose(pose_delay)
                    self.outstanding=header;self.current_result_time=header['logical_receipt_ns']
                    self.ready.put((header,jpeg));start=time.monotonic()
                    try:pending=self.response.get(timeout=30)
                    except queue.Empty:raise RuntimeError('Algorithm failed to acknowledge logical input')
                    self.wait_wall_s+=time.monotonic()-start
                    if self.pending_command is not None:raise RuntimeError('Overlapping command application deadlines')
                    self.pending_command=pending;self.outstanding=None;self.current_result_time=None;self.delivered+=1
                    if self.mode=='warm':
                        self.warm_remaining-=1
                        if self.warm_remaining==0:
                            if self.profile.get('warmup_receipt_boundary',False):
                                self.warm_drain=True;continue
                            if not self.profile.get('continuous_measurement',False):
                                # Legacy boundary retained for frozen controls.
                                while self.now_ns<pending[0]:self.tick(False)
                                self.tick(False,pending[1],pending[2]);self.pending_command=None
                                self.captured.clear()
                            # Park wall time only. The continuous variant keeps
                            # queued images, response deadline and sampling phase.
                            self.park_warm()
        except BaseException as error:
            self.error=type(error).__name__+': '+str(error);self.bridge.last_error=self.error
            self.mode='failed';self.phase_done.set()


def prepare(bridge,request):
    from rm_interfaces.srv import SimScenarioLoad,SimScenarioStart
    from scenario_catalog import SCENES,semantics
    op=request['op'];sync=bridge.sync
    if op=='control':
        bridge.control=request.get('value')=='on';bridge.fire_armed=False
        if not bridge.control:sync.phase('hold',wait=False)
        return bridge.status()
    if op=='prime':
        if sync.mode!='hold':raise RuntimeError('Evidence snapshot requires a parked clock')
        sync.client.step(0,observe_only=True)
        return bridge.status()
    if op=='begin':
        if bridge.scenario.get('phase')!=0:raise ValueError('Prepare an idle scene')
        bridge.counts.clear();bridge.started=time.monotonic()
        bridge.fire_armed=request.get('fire') is True and bridge.fire_mode=='auto'
        if bridge.input_contract=='aligned-v1':bridge.warmup_state='measuring'
        sync.measure();return bridge.status()
    scene=request.get('name')
    if scene not in SCENES:raise ValueError('Unknown scene')
    bridge.control=False;bridge.fire_armed=False
    sync.phase('preparing',wait=False)
    req=SimScenarioLoad.Request();req.scenario_path='/run/rm26-scenarios/'+scene+'.toml';req.seed=request.get('seed',20261019)
    bridge.service('load',req)
    deadline=time.monotonic()+20
    while bridge.scenario.get('id')!='rm26_'+scene or not bridge.scenario.get('spawn_applied'):
        sync.check()
        if time.monotonic()>deadline:raise RuntimeError('Synchronous scenario spawn timed out')
        time.sleep(.005)
    sync.phase('settle_setup',2.)
    bridge.homing_yaw_deg=semantics(scene)['homing_yaw_deg'];sync.phase('home',1.6)
    if sync.profile.get('preparation_release_home',False):
        # One acknowledged no-fire release, before fresh-session warmup. It is
        # optional so all frozen v6 controls retain their historical semantics.
        sync.phase('release_home',1./sync.profile['control_hz'])
    with bridge.lock:bridge.reset_stream();bridge.control=request.get('control','on')=='on'
    if bridge.input_contract=='aligned-v1':
        sync.scene=scene;sync.phase('hold',wait=False);bridge.warmup_state='await_recorder'
    else:sync.warm(scene)
    return bridge.status()
