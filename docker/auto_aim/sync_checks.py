#!/usr/bin/env python3
"""Regression checks for simulation-time scheduling and clock isolation."""
import copy
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import queue

from sync_timing import Schedule,validate,SCENES,complete_timing
from sync_bridge import Coordinator,EPOCH_NS,StepClient
import manage
import background_jobs as jobs


def profile():
    return dict(version=1,kind='fixed',name='test',control_hz=600,
        input_delay_ns=30_000_000,processing_delay_ns=7_000_000,warmup_frames=30)


class TimingChecks(unittest.TestCase):
    def test_extra_realtime_recording_has_no_inherited_capacity(self):
        from unittest.mock import patch
        from background_capacity import limit
        job=dict(spec=json.dumps({'_protocol':2}),parallelism=0,exploratory=0,bundle='unused')
        capacity={'ordinary':dict(healthy_parallelism=2,runtime_ceiling=8)}
        with patch('background_capacity.signature',return_value='ordinary'):
            job['settings']=json.dumps(dict(session_evidence='auto'))
            self.assertEqual(limit(job,capacity,{}),2)
            job['settings']=json.dumps(dict(session_evidence='on'))
            self.assertEqual(limit(job,capacity,{}),1)

    def test_rational_time_does_not_drift(self):
        s=Schedule(profile(),SCENES[0])
        self.assertEqual(sum(s.control_delta() for _ in range(6000)),10_000_000_000)
        self.assertEqual(sum(s.image_interval() for _ in range(300)),10_000_000_000)

    def test_trace_uses_only_frozen_schedule(self):
        p=profile();p.update(kind='trace',scenes={scene:dict(control_intervals_ns=[1_000_000,2_000_000],
            capture_intervals_ns=[33_000_000,35_000_000],input_delays_ns=[31_000_000,32_000_000],
            processing_delays_ns=[6_000_000,7_000_000],source_run='calibration') for scene in SCENES})
        s=Schedule(p,SCENES[0])
        self.assertEqual([s.control_delta() for _ in range(4)],[1_000_000,2_000_000]*2)
        self.assertEqual(s.delays(3),(32_000_000,7_000_000))

    def test_reject_invalid_profiles_and_truth_injection(self):
        for patch in (dict(control_hz=0),dict(control_hz=True),dict(control_hz=float('nan')),
                dict(input_delay_ns=-1),dict(processing_delay_ns=0.2),dict(warmup_frames=0),dict(commands=[]),dict(truth=[])):
            with self.subTest(patch=patch),self.assertRaises(ValueError):validate(dict(profile(),**patch))

    def test_schedule_independent_of_computing_time(self):
        a=Schedule(profile(),SCENES[0]);b=Schedule(profile(),SCENES[0])
        self.assertEqual([a.control_delta() for _ in range(100)],[b.control_delta() for _ in range(100)])

    def test_complete_timing_includes_decode_and_return_path(self):
        observation=dict(frame=dict(stamp_ns=1_000_000_000,sent_ns=1_025_000_000,
            latest_muzzle_pose=dict(stamp_ns=1_023_000_000)),
            result=dict(pipeline_ms=8.,fire_check=dict(pose_age_s=.014)),receipt_ns=1_038_000_000)
        timing=complete_timing(observation)
        self.assertEqual(timing,dict(input=25_000_000,processing=12_000_000,
            return_delay=1_000_000,pose_delay=2_000_000,excluded_from_pipeline=4_000_000))
        self.assertEqual(timing['input']+timing['processing']+timing['return_delay'],
            observation['receipt_ns']-observation['frame']['stamp_ns'])
        for age in (-1,float('nan'),0,.1):
            bad=copy.deepcopy(observation);bad['result']['fire_check']['pose_age_s']=age
            with self.subTest(age=age),self.assertRaises(ValueError):complete_timing(bad)

    def test_optional_return_and_pose_delays_preserve_legacy_profiles(self):
        old=Schedule(profile(),SCENES[0])
        self.assertEqual(old.return_delay(0),0);self.assertEqual(old.pose_delay(0),0)
        updated=dict(profile(),return_delay_ns=400_000,pose_delay_ns=1_200_000)
        new=Schedule(updated,SCENES[0])
        self.assertEqual(new.return_delay(100),400_000);self.assertEqual(new.pose_delay(100),1_200_000)
        for bad in (-1,True,float('nan'),[] ):
            with self.subTest(bad=bad),self.assertRaises(ValueError):validate(dict(updated,return_delay_ns=bad))
        with self.assertRaises(ValueError):validate(dict(updated,preparation_reset=1))
        with self.assertRaises(ValueError):validate(dict(updated,continuous_measurement=1))

    def test_cli_keeps_real_time_default(self):
        args=manage.parser().parse_args(['experiment','submit','--scene','static'])
        rows,_,_=jobs.expand(args)
        self.assertEqual(rows[0]['execution_mode'],'realtime')
        self.assertEqual(rows[0]['session_evidence'],'auto')

    def test_session_recording_is_explicit_for_realtime_and_required_for_lockstep(self):
        parsed=manage.parser().parse_args(['experiment','submit','--scene','static','--session-evidence','on'])
        rows,_,_=jobs.expand(parsed)
        self.assertEqual(rows[0]['session_evidence'],'on')
        with self.assertRaises(ValueError):
            jobs.validate(dict(scene='hf_uniform_3',terrain='flat_range',algorithm_profile='tracking_fire_v3',
                execution_mode='lockstep',timing_profile='ui-fixed-v6',session_evidence='off'))

    def test_lockstep_scope_and_profile_validation(self):
        common=['experiment','submit','--execution-mode','lockstep','--timing-profile','ui-fixed-v1',
            '--scene','hf_static_8','--terrain','flat_range','--algorithm-profile','tracking_fire_v3']
        rows,_,_=jobs.expand(manage.parser().parse_args(common))
        self.assertEqual(rows[0]['algorithm_lifecycle'],'fresh')
        for argv in (['--physics','drag'],['--scene','hf_static_5'],['--timing-profile','../../bad'],['--algorithm-profile','legacy']):
            with self.subTest(argv=argv),self.assertRaises(ValueError):jobs.expand(manage.parser().parse_args(common+argv))


class TransportChecks(unittest.TestCase):
    def test_acknowledged_steps(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'step.sock';server=socket.socket(socket.AF_UNIX);server.bind(str(path));server.listen()
            received=[]
            def run():
                with server.accept()[0] as connection:
                    stream=connection.makefile('rb')
                    for _ in range(3):
                        msg=json.loads(stream.readline());received.append(msg)
                        reply=dict(ok=True,id=msg['id'],stamp_ns=msg['id']*10,generation=1)
                        if msg.get('reset_scene'):reply['reset_scene']=True
                        connection.sendall((json.dumps(reply)+'\n').encode())
                    stream.close()
            thread=threading.Thread(target=run);thread.start();client=StepClient(path)
            self.assertEqual(client.step(10)['id'],1);self.assertEqual(client.step(20,capture=True)['id'],2)
            self.assertTrue(client.step(10,reset_scene=True,align_epoch=True)['reset_scene'])
            client.reader.close();client.socket.close();thread.join();server.close()
            self.assertEqual([r['delta_ns'] for r in received],[10,20,10]);self.assertTrue(received[1]['capture'])
            self.assertEqual(received[2]['generation'],1);self.assertTrue(received[2]['align_epoch'])

    def test_bad_ack_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'step.sock';server=socket.socket(socket.AF_UNIX);server.bind(str(path));server.listen()
            def run():
                with server.accept()[0] as connection:
                    stream=connection.makefile('rb');stream.readline()
                    connection.sendall(b'{"ok":true,"id":9}\n');stream.close()
            thread=threading.Thread(target=run);thread.start();client=StepClient(path)
            with self.assertRaises(RuntimeError):client.step(10)
            client.reader.close();client.socket.close();thread.join();server.close()


class CoordinatorTransitionChecks(unittest.TestCase):
    def test_receipt_boundary_drains_final_command_without_losing_captures(self):
        from unittest.mock import patch
        from types import SimpleNamespace as N
        class Driver(Coordinator):
            def tick(self,capture=False,command=None,eligibility=None):
                self.now_ns+=self.schedule.control_delta();self.steps+=1
                if capture:
                    self.image_index+=1
                    header=dict(session='s',seq=self.image_index,stamp_ns=self.now_ns)
                    delay,processing=self.schedule.delays(self.image_index-1)
                    self.captured.append((self.now_ns+delay,processing,0,0,header,b'jpeg'))
                return {}
            def own_pose(self,delay_ns=0):return dict(stamp_ns=self.now_ns,yaw=0,elevation=0)
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'step.sock';path.touch();config=Path(root)/'timing.json'
            config.write_text(json.dumps(dict(profile(),continuous_measurement=True,warmup_receipt_boundary=True)))
            events=[];receipts=[];finish=threading.Event()
            bridge=N(scenario={'spawn_applied':True},last_error='',archive=lambda *a:events.append(a),
                publisher=N(publish=lambda *a:None))
            with patch.dict('sys.modules',{'rm_interfaces.msg':N(GimbalCmd=lambda:N())}):
                driver=Driver(bridge,path,config,SCENES[0])
                def consume():
                    while not finish.is_set():
                        try:frame,_=driver.ready.get(timeout=.1)
                        except queue.Empty:continue
                        receipts.append(frame['logical_receipt_ns'])
                        driver.submit(frame,{},dict(enabled=False))
                consumer=threading.Thread(target=consume);consumer.start()
                try:
                    self.assertTrue(driver.phase_done.wait(2));driver.warm(SCENES[0])
                    self.assertEqual(len(receipts),30)
                    self.assertGreaterEqual(driver.now_ns,max(receipts))
                    self.assertIsNone(driver.pending_command)
                    self.assertFalse(driver.warm_drain)
                    self.assertGreater(driver.image_index,30)
                    self.assertTrue(driver.captured)
                    before=driver.pipeline_state();driver.measure()
                    boundary=next(data for kind,data in events if kind=='measurement_pipeline')
                    self.assertEqual(boundary['before'],before)
                    self.assertEqual(boundary['before'],boundary['after'])
                finally:
                    finish.set();driver.stop.set()
                    with driver.condition:driver.condition.notify_all()
                    consumer.join(2);driver.thread.join(2)

    def test_continuous_measurement_preserves_whole_pipeline(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        for continuous in (False,True):
            with self.subTest(continuous=continuous),tempfile.TemporaryDirectory() as root:
                config=Path(root)/'timing.json'
                config.write_text(json.dumps(dict(profile(),continuous_measurement=continuous)))
                events=[]
                with patch('threading.Thread.start'):
                    driver=Coordinator(SimpleNamespace(archive=lambda *a:events.append(a)),
                        Path(root)/'step.sock',config,SCENES[0])
                driver.schedule.control_delta();driver.schedule.image_interval()
                driver.image_index=31;driver.image_due=EPOCH_NS+17
                driver.captured.append((EPOCH_NS+9,2,3,4,dict(stamp_ns=EPOCH_NS-8,seq=30),b'jpeg'))
                driver.pending_command=(EPOCH_NS+11,dict(yaw=1),dict(enabled=False))
                before=driver.pipeline_state();old_schedule=driver.schedule
                request=dict(mode='measure',seconds=None,scene=None,entered=threading.Event(),done=threading.Event())
                driver.transitions.put(request);driver.enter_transitions()
                self.assertTrue(driver.begin_requested)
                self.assertTrue(request['entered'].is_set())
                self.assertFalse(request['done'].is_set())
                if continuous:
                    self.assertIs(driver.schedule,old_schedule)
                    self.assertEqual(driver.pipeline_state(),before)
                    self.assertEqual(driver.captured[0][5],b'jpeg')
                else:
                    self.assertIsNot(driver.schedule,old_schedule)
                    self.assertFalse(driver.captured)
                    self.assertEqual(driver.image_index,0)
                self.assertEqual(events[0][1]['before'],before)
                self.assertEqual(events[0][1]['after'],driver.pipeline_state())

    def test_command_applies_at_receipt_after_decision(self):
        from types import SimpleNamespace
        f=dict(session='s',seq=1,stamp_ns=100,logical_now_ns=130,logical_receipt_ns=134)
        driver=SimpleNamespace(outstanding=f,response=queue.Queue(maxsize=1))
        Coordinator.submit(driver,f,{'yaw':1},{'enabled':True})
        self.assertEqual(driver.response.get_nowait()[0],134)
        old=dict(f);old.pop('logical_receipt_ns')
        driver.outstanding=old;Coordinator.submit(driver,old,{},None)
        self.assertEqual(driver.response.get_nowait()[0],130)

    def test_phase_deadline_uses_acknowledged_step_boundary(self):
        from types import SimpleNamespace
        entered=threading.Event();release=threading.Event();blocked=[False]
        class Driver(Coordinator):
            def tick(self,*args,**kwargs):
                delta=self.schedule.control_delta()
                if self.mode=='preparing' and not blocked[0]:
                    blocked[0]=True;entered.set()
                    if not release.wait(3):raise RuntimeError('Fixture step was not released')
                self.now_ns+=delta;self.steps+=1
                time.sleep(.001)
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'step.sock';path.touch()
            config=Path(root)/'timing.json';config.write_text(json.dumps(profile()))
            bridge=SimpleNamespace(scenario={'spawn_applied':True},last_error='')
            driver=Driver(bridge,path,config,SCENES[0])
            try:
                self.assertTrue(driver.phase_done.wait(2))
                driver.phase('preparing',wait=False)
                self.assertTrue(entered.wait(2))
                before=driver.now_ns;finished=threading.Event();errors=[]
                def phase():
                    try:driver.phase('home',.01)
                    except BaseException as error:errors.append(error)
                    finally:finished.set()
                thread=threading.Thread(target=phase);thread.start()
                deadline=time.monotonic()+2
                while driver.transitions.empty() and time.monotonic()<deadline:time.sleep(.001)
                self.assertFalse(driver.transitions.empty())
                self.assertEqual(driver.mode,'preparing')
                self.assertIsNone(driver.phase_end)
                self.assertEqual(driver.now_ns,before)
                release.set();self.assertTrue(finished.wait(3));thread.join()
                self.assertEqual(errors,[])
                home=next(h for h in driver.phase_history if h['mode']=='home')
                self.assertGreater(home['stamp_ns'],before)
                self.assertEqual(home['phase_end_ns']-home['stamp_ns'],10_000_000)
                self.assertGreaterEqual(driver.now_ns,home['phase_end_ns'])
            finally:
                release.set();driver.stop.set()
                with driver.condition:driver.condition.notify_all()
                driver.thread.join(3)

    def test_warm_and_measure_enter_with_all_flags_at_once(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as root:
            config=Path(root)/'timing.json';config.write_text(json.dumps(profile()))
            with patch('threading.Thread.start'):
                driver=Coordinator(SimpleNamespace(),Path(root)/'step.sock',config,SCENES[0])
            driver.now_ns=EPOCH_NS+123;driver.schedule.control_delta()
            def apply(mode,**fields):
                request=dict(mode=mode,seconds=None,scene=SCENES[0],entered=threading.Event(),done=threading.Event())
                request.update(fields)
                driver.transitions.put(request);driver.enter_transitions()
                self.assertTrue(request['entered'].is_set())
                return request
            request=apply('warm')
            self.assertTrue(driver.align_requested)
            self.assertEqual(driver.image_due,driver.now_ns)
            self.assertEqual(driver.schedule.step_index,0)
            self.assertEqual(driver.warm_remaining,30)
            self.assertFalse(request['done'].is_set())
            request=apply('measure')
            self.assertTrue(driver.begin_requested)
            self.assertEqual(driver.schedule.step_index,0)
            self.assertEqual(driver.image_due,driver.now_ns)

    def test_preparation_reset_restarts_schedule_before_settling(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as root:
            config=Path(root)/'timing.json';config.write_text(json.dumps(dict(profile(),preparation_reset=True)))
            with patch('threading.Thread.start'):
                driver=Coordinator(SimpleNamespace(),Path(root)/'step.sock',config,SCENES[0])
            for _ in range(11):driver.schedule.control_delta()
            request=dict(mode='settle_setup',seconds=2,scene=SCENES[0],entered=threading.Event(),done=threading.Event())
            driver.transitions.put(request);driver.enter_transitions()
            self.assertEqual(driver.schedule.step_index,0)
            self.assertTrue(driver.reset_requested);self.assertTrue(driver.align_requested)
            self.assertEqual(driver.phase_end,driver.now_ns+2_000_000_000)


class CadenceChecks(unittest.TestCase):
    def bridge(self,now,last,synchronous=True):
        # Exercise the production method without importing ROS on the host.
        import ast
        import collections
        import math
        from types import SimpleNamespace as N
        source=ast.parse((Path(__file__).with_name('bridge.py')).read_text())
        cls=next(n for n in source.body if isinstance(n,ast.ClassDef) and n.name=='Bridge')
        method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='result')
        tree=ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[]))
        def command():return N(header=N(stamp=N(sec=0,nanosec=0)))
        scope=dict(math=math,time=time,GimbalCmd=command)
        exec(compile(tree,'bridge.py','exec'),scope)
        packets=[]
        fake=N(lock=threading.RLock(),session='test',counts=collections.Counter(),ages=[],pipeline_times=[],
            clock_ns=lambda:now,command_script=None,control=True,fire_mode='auto',fire_armed=True,
            connected=True,scenario={'phase':1},fire_policy='advice',fire_rate_hz=5.,last_fire=last,
            publish_eligibility=lambda *args:None,observer=N(publish=lambda metadata,jpeg:packets.append(metadata)),
            archive=lambda *args:None,
            physical_phase=lambda:1,scenario_boundary=None,count_warmup=lambda *args:None,
            sync=N(command=lambda *args:args) if synchronous else None,publisher=N(publish=lambda *args:None))
        frame=dict(session='test',seq=1,stamp_ns=now-50_000_000)
        result=dict(frame,valid=True,yaw=0.,pitch=0.,distance=5.,fire_advice=True)
        scope['result'](fake,result,frame,b'jpeg')
        return packets[0]['fire_forwarded'],fake.last_fire

    def test_exact_200ms_cadence_is_independent_of_epoch_and_phase(self):
        for origin in (EPOCH_NS+4_700_000_000,EPOCH_NS+4_702_083_333,EPOCH_NS+4_704_166_667):
            with self.subTest(origin=origin):
                fired,last=self.bridge(origin+200_000_000,origin)
                self.assertTrue(fired);self.assertEqual(last,origin+200_000_000);self.assertIsInstance(last,int)
                self.assertFalse(self.bridge(origin+199_999_999,origin)[0])
                self.assertTrue(self.bridge(origin+200_000_001,origin)[0])

    def test_real_time_path_keeps_monotonic_seconds(self):
        from unittest.mock import patch
        now=EPOCH_NS+1_000_000_000
        with patch('time.monotonic',return_value=7.25):
            fired,last=self.bridge(now,7.,synchronous=False)
            self.assertTrue(fired);self.assertEqual(last,7.25)


class EquivalenceChecks(unittest.TestCase):
    def data(self,weight=.8,hits=8,fires=10):
        return {n:dict(invalid=False,duration=6,metrics=dict(shots_fired=fires,target_armor_hits=hits,
            weighted_hit_score_sum=weight*fires,correct_tracking_fraction=.9,longest_loss_s=.1)) for n in range(5)}

    def test_two_sided_acceptance(self):
        from sync_study import bootstrap
        self.assertEqual(bootstrap(self.data(),self.data(),.02)['conclusion'],'passed')
        self.assertEqual(bootstrap(self.data(),self.data(.9),.05)['conclusion'],'different')
        self.assertEqual(bootstrap(self.data(),self.data(.7),.05)['conclusion'],'different')

    def test_auxiliary_firing_gate_and_insufficient(self):
        from sync_study import bootstrap
        from sync_acceptance import gate_notes
        result=bootstrap(self.data(),self.data(weight=8/12,hits=8,fires=12),.05,True)
        self.assertEqual(result['conclusion'],'different')
        self.assertIn('发射频率超界',gate_notes(result))
        self.assertEqual(bootstrap({},self.data(),.02)['conclusion'],'insufficient_samples')
        bad=self.data();bad[0]['invalid']=True
        self.assertEqual(bootstrap(bad,self.data(),.02)['conclusion'],'invalid')
        empty=self.data();empty[0]['metrics']['shots_fired']=0
        self.assertEqual(bootstrap(empty,self.data(),.02)['conclusion'],'invalid')

    def test_readonly_rescore(self):
        from sync_study import row
        from report import write
        import hashlib
        with tempfile.TemporaryDirectory() as root:
            source=Path(root)/'source';source.mkdir()
            write(source/'manifest.json',dict(scene='hf_static_8',duration_s=6,mode='shoot',shooting={},scoring_version=1))
            write(source/'summary.json',dict(verdict='no_shooting_samples',metrics={}))
            write(source/'run-state.json',dict(shot_events=[]))
            (source/'shots.jsonl').write_text('original historical evidence\n')
            before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}
            output=Path(root)/'rescore';row(source,output,0)
            self.assertEqual(before,{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()})
            self.assertTrue((output/'rescore.json').exists())

    def test_acceptance_summary_preserves_failed_and_insufficient(self):
        from sync_acceptance import summarize
        from report import write
        with tempfile.TemporaryDirectory() as root:
            folder=Path(root)
            write(folder/'study.json',dict(name='test',state='completed',calibration=[],reference=[],
                certified=False,recommended_profile=None,qualification={'fixed':dict(
                    ui_equivalence={'static':dict(pairs=5,conclusion='different')},
                    repeatability={'1':{'static':dict(pairs=30,conclusion='insufficient_samples')}})},
                capacity={'fixed':dict(accepted_levels=[],recommended_parallelism=None)}))
            result=summarize(folder)
            self.assertFalse(result['certified']);self.assertEqual(result['default_execution_mode'],'realtime')
            text=(folder/'acceptance.md').read_text()
            self.assertIn('差异明确',text);self.assertIn('样本不足',text)

    def test_capacity_signature_covers_transport_and_timing(self):
        from background_capacity import synchronous_signature
        from report import write
        with tempfile.TemporaryDirectory() as root:
            bundle=Path(root);timing=bundle/'docker/auto_aim/timing_profiles'
            timing.mkdir(parents=True);(timing/'fixed.json').write_text('{}')
            metadata=dict(gpu_uuid='gpu',images={},files={})
            write(bundle/'bundle.json',metadata)
            initial=synchronous_signature(bundle,'fixed')
            metadata['files']['docker/auto_aim/sync_bridge.py']='changed'
            write(bundle/'bundle.json',metadata)
            updated=synchronous_signature(bundle,'fixed')
            self.assertNotEqual(initial,updated)
            (timing/'fixed.json').write_text('{"changed":true}')
            self.assertNotEqual(updated,synchronous_signature(bundle,'fixed'))
            updated=synchronous_signature(bundle,'fixed')
            metadata['files']['sp_vision_25/build/simulator/sim_auto_aim']='new-binary'
            write(bundle/'bundle.json',metadata)
            self.assertNotEqual(updated,synchronous_signature(bundle,'fixed'))
            updated=synchronous_signature(bundle,'fixed')
            metadata['files']['docker/auto_aim/experiments.py']='new-preparation'
            write(bundle/'bundle.json',metadata)
            self.assertNotEqual(updated,synchronous_signature(bundle,'fixed'))

    def test_lockstep_capacity_does_not_extend_qualified_settings(self):
        from background_capacity import limit
        from unittest.mock import patch
        job=dict(spec=json.dumps(dict(_protocol=2)),parallelism=8,exploratory=0,bundle='/frozen',
            settings=json.dumps(dict(execution_mode='lockstep',timing_profile='fixed',bullet_speed=25)))
        capacity={'sync':dict(healthy_parallelism=4,runtime_ceiling=4,qualified_settings=dict(bullet_speed=25))}
        with patch('background_capacity.synchronous_signature',return_value='sync'):
            self.assertEqual(limit(job,capacity,{}),4)
            job['settings']=json.dumps(dict(execution_mode='lockstep',timing_profile='fixed',bullet_speed=30))
            self.assertEqual(limit(job,capacity,{}),1)

    def test_supplement_requests_only_unresolved_scenes(self):
        from sync_study import background,ui_runs
        from unittest.mock import patch
        scene=SCENES[0]
        entry=dict(profile='fixed',level=1,jobs=[],runs=[dict(scene=scene,repeat_id=0,run_id='already')])
        state=dict(modes={'fixed_p1':entry},reference=[dict(scene=scene,repeat_id=i) for i in range(3)])
        with patch('background_jobs.connect',side_effect=AssertionError('Unexpected scheduling')):
            self.assertIs(background(Path('/unused'),state,'fixed',1,1,scenes=(scene,)),entry)
        with patch('sync_study.original_ui',side_effect=AssertionError('Unexpected UI recollection')):
            ui_runs(Path('/unused'),state,'reference',3,scenes=(scene,))

    def test_failed_eight_lane_exploration_keeps_accepted_four(self):
        from sync_study import recommend,all_scenes_pass
        passed={s:dict(conclusion='passed') for s in SCENES}
        failed={s:dict(conclusion='different') for s in SCENES}
        qualification={'ui-fixed-v1':dict(ui_equivalence=passed,
            repeatability={'1':passed,'2':passed,'4':passed,'8':failed})}
        self.assertEqual(recommend(qualification),'ui-fixed-v1')
        qualification['ui-fixed-v1']['repeatability']['4']=failed
        self.assertIsNone(recommend(qualification));self.assertFalse(all_scenes_pass({}))

    def test_completed_batch_recovery_never_replaces_valid_attempt(self):
        from sync_study import ingest
        import sqlite3
        with sqlite3.connect(':memory:') as db:
            db.row_factory=sqlite3.Row
            db.execute('CREATE TABLE items(id INTEGER,job_id TEXT,ordinal INTEGER,settings TEXT)')
            db.execute('CREATE TABLE attempts(item_id INTEGER,verdict TEXT,started REAL,run_id TEXT)')
            db.execute('INSERT INTO items VALUES(1,?,?,?)',('job',0,json.dumps(dict(scene=SCENES[0],repeat_id=0,seed=1))))
            db.execute('INSERT INTO attempts VALUES(1,?,?,?)',('passed',1,'immutable'))
            entry=dict(runs=[])
            self.assertTrue(ingest(db,'job',entry));self.assertFalse(ingest(db,'job',entry))
            self.assertEqual(entry['runs'][0]['run_id'],'immutable')
            db.execute('INSERT INTO attempts VALUES(1,?,?,?)',('passed',2,'favourable-rerun'))
            with self.assertRaises(RuntimeError):ingest(db,'job',entry)

    def test_capacity_uses_matching_balanced_batch(self):
        from sync_study import balanced_performance
        from unittest.mock import patch
        balanced=[dict(settings=json.dumps(dict(scene=s,repeat_id=n))) for n in range(5) for s in SCENES]
        state=dict(repeats=5,modes={'fixed_p2':dict(jobs=['supplement','balanced'])})
        attempts=[dict(ended=110,verdict='passed',state='finished') for _ in balanced]
        with patch('background_jobs.connect') as connection,patch('background_jobs.status',return_value=dict(
            state='completed',created=100,attempts=attempts)) as status:
            connection.return_value.__enter__.return_value.execute.side_effect=[balanced[:1],balanced]
            result=balanced_performance(state,'fixed',2)
            self.assertEqual(result['job_id'],'balanced');self.assertEqual(result['valid_runs_per_hour'],7200)
            self.assertEqual(status.call_count,1)


if __name__=='__main__':unittest.main()
