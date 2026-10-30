#!/usr/bin/env python3
"""Deterministic retention, settlement, comparison and compatibility checks."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import background_jobs as jobs
from background_pipeline import compact,archived_frames,jpeg_size,enqueue
from background_compare import paired_result
from background_capacity import control_hz,limit,signature
import manage
from report import write,load

JPEG=bytes.fromhex('ffd8ffc00011080002000303011100021100031100ffd9')


class PipelineChecks(unittest.TestCase):
    def folder(self,root,stage='screen'):
        path=Path(root)/'ordinary'
        while int(hashlib.sha256(path.name.encode()).hexdigest()[:8],16)%20==0:path=path.with_name(path.name+'x')
        path.mkdir()
        write(path/'manifest.json',dict(pipeline_version=2,stage=stage,evidence='auto',mode='shoot'))
        write(path/'summary.json',dict(verdict='passed',rules=[]))
        write(path/'run-state.json',dict(complete=True))
        rows=[dict(video_index=i,stamp_ns=i*100_000_000,jpeg_offset=i*len(JPEG),jpeg_bytes=len(JPEG)) for i in range(30)]
        (path/'frames.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
        (path/'input.mjpg').write_bytes(JPEG*len(rows))
        (path/'telemetry.jsonl').write_text('')
        (path/'shots.jsonl').write_text(json.dumps(dict(outcome='target_armor',fire_stamp_ns=1_500_000_000,terminal_stamp_ns=1_700_000_000))+'\n')
        return path

    def test_jpeg_header(self):
        self.assertEqual(jpeg_size(JPEG),(3,2))
        for invalid in (b'',JPEG[:-1],b'garbage'):
            with self.assertRaises(ValueError):jpeg_size(invalid)

    def test_sampling_and_idempotence(self):
        with tempfile.TemporaryDirectory() as root:
            folder=self.folder(root);original=(folder/'frames.jsonl').read_bytes()
            policy=compact(folder)
            self.assertEqual(policy['level'],'sampled')
            self.assertFalse((folder/'input.mjpg').exists())
            entries=list(archived_frames(folder));ids=[row['video_index'] for row,data in entries]
            self.assertTrue({0,1,2,3,4,5,10,14,15,16,17,18,20,24,25,26,27,28,29}<=set(ids))
            self.assertLess(len(ids),30)
            self.assertTrue(all(data==JPEG for _,data in entries))
            self.assertEqual((folder/'frames.jsonl').read_bytes(),original)
            self.assertEqual(compact(folder),policy)
            (folder/'input.mjpg').write_bytes(JPEG*30)
            compact(folder)
            self.assertFalse((folder/'input.mjpg').exists())

    def test_final_and_anomaly_keep_full(self):
        for stage,invalid in [('final',False),('screen',True)]:
            with self.subTest(stage=stage),tempfile.TemporaryDirectory() as root:
                folder=self.folder(root,stage)
                if invalid:write(folder/'summary.json',dict(verdict='invalid',rules=[]))
                self.assertEqual(compact(folder)['level'],'full')
                self.assertTrue((folder/'input.mjpg').exists())
                self.assertEqual(len(list(archived_frames(folder))),30)

    def test_atomic_failure_retains_full(self):
        with tempfile.TemporaryDirectory() as root:
            folder=self.folder(root)
            with patch('background_pipeline.write',side_effect=OSError('disk full')):
                with self.assertRaises(OSError):compact(folder)
            self.assertTrue((folder/'input.mjpg').exists())
            self.assertFalse((folder/'evidence-policy.json').exists())
            self.assertEqual(compact(folder)['level'],'sampled')

    def test_media_retry_does_not_change_items(self):
        with tempfile.TemporaryDirectory() as root,patch.object(jobs,'OPS',Path(root)),patch.object(jobs,'STATE',Path(root)),patch.object(jobs,'DB',Path(root)/'queue.sqlite3'):
            folder=Path(root)/'experiments'/'run';folder.mkdir(parents=True)
            write(folder/'manifest.json',dict(pipeline_version=2))
            with jobs.connect() as db:
                enqueue(db,'job','/frozen',['run'])
                db.execute("UPDATE artifacts SET state='failed'")
                enqueue(db,'job','/frozen',['run'])
                self.assertEqual(db.execute('SELECT state FROM artifacts').fetchone()[0],'queued')
                self.assertEqual(db.execute('SELECT count(*) FROM items').fetchone()[0],0)


class CapacityChecks(unittest.TestCase):
    def test_parser_levels_and_defaults(self):
        for level,expected in [('auto',0),('1',1),('2',2),('4',4),('8',8)]:
            args=manage.parser().parse_args(['experiment','submit','--scene','static','--parallelism',level])
            rows,n,_=jobs.expand(args)
            self.assertEqual(n,expected);self.assertEqual(rows[0]['max_fps'],240)

    def test_explicit_parallelism_overrides_spec(self):
        with tempfile.TemporaryDirectory(dir=manage.RUNTIME) as root:
            path=Path(root)/'spec.json'
            write(path,dict(version=1,parallelism=2,defaults=dict(scene='static')))
            args=manage.parser().parse_args(['experiment','submit','--spec',str(path),'--parallelism','auto'])
            self.assertEqual(jobs.expand(args)[1],0)
            args=manage.parser().parse_args(['experiment','submit','--spec',str(path)])
            self.assertEqual(jobs.expand(args)[1],2)

    def test_capacity_separate_from_effect_gate(self):
        job=dict(spec=json.dumps(dict(_protocol=2)),parallelism=8,exploratory=0,bundle='/frozen',settings='{}')
        with patch('background_capacity.signature',return_value='runtime'):
            self.assertEqual(limit(job,{},dict(parallel_qualified=True)),1)
            self.assertEqual(limit(job,{'runtime':dict(healthy_parallelism=4,runtime_ceiling=2)},{}),2)
            job['exploratory']=1
            self.assertEqual(limit(job,{},{}),8)

    def test_additive_database_migration(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as root,patch.object(jobs,'STATE',Path(root)),patch.object(jobs,'DB',Path(root)/'queue.sqlite3'):
            db=sqlite3.connect(jobs.DB)
            db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,state TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,spec TEXT NOT NULL,bundle TEXT NOT NULL,parallelism INTEGER NOT NULL,exploratory INTEGER NOT NULL,error TEXT)')
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',('legacy','completed',1,1,'{}','/old',2,0,None));db.commit();db.close()
            with jobs.connect() as db:
                self.assertEqual(jobs.get_job(db,'legacy')['bundle'],'/old')
                self.assertEqual(db.execute('SELECT count(*) FROM artifacts').fetchone()[0],0)

    def test_recovery_pauses_without_losing_attempt(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as root,patch.object(jobs,'STATE',Path(root)),patch.object(jobs,'DB',Path(root)/'queue.sqlite3'),patch.object(jobs,'cleanup_slot'),patch.object(jobs,'interrupt_evidence') as interrupted,patch('background_jobs.subprocess.run',return_value=SimpleNamespace(stdout='')):
            with jobs.connect() as db:
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',('job','running',1,1,'{}','/old',4,0,None))
                db.execute('INSERT INTO items(id,job_id,ordinal,state,settings) VALUES(?,?,?,?,?)',(1,'job',0,'running','{}'))
                db.execute('INSERT INTO attempts(id,item_id,slot,state,started,run_id) VALUES(?,?,?,?,?,?)',('attempt',1,4,'running',1,'evidence'))
                db.execute('INSERT INTO artifacts(run_id,job_id,bundle,state,requested) VALUES(?,?,?,?,?)',('media','job','/old','running',1))
                jobs.reconcile(db)
                self.assertEqual(jobs.get_job(db,'job')['state'],'paused')
                self.assertEqual(db.execute('SELECT state FROM attempts').fetchone()[0],'interrupted')
                self.assertEqual(db.execute('SELECT state FROM items').fetchone()[0],'pending')
                self.assertEqual(db.execute('SELECT state FROM artifacts').fetchone()[0],'queued')
                interrupted.assert_called_once()

    def test_cleanup_scopes_workspace_and_legacy_mounts(self):
        from types import SimpleNamespace
        records=[dict(Id='own',Config=dict(Labels={'rm26.workspace':str(manage.ROOT)})),
            dict(Id='foreign',Config=dict(Labels={'rm26.workspace':'/other'})),
            dict(Id='legacy',Config=dict(Labels={}),Mounts=[dict(Source=str(jobs.STATE/'workers/7'))])]
        with patch('background_jobs.subprocess.run',return_value=SimpleNamespace(stdout='own foreign legacy')) as called,patch('background_jobs.subprocess.check_output',return_value=json.dumps(records).encode()):
            jobs.cleanup_slot(None)
            self.assertEqual(called.call_args.args[0],['docker','rm','-f','own','legacy'])

    def test_control_counter_resets(self):
        with tempfile.TemporaryDirectory() as root:
            folder=Path(root)
            write(folder/'run-state.json',dict(measurement_start_stamp_ns=0,measurement_end_stamp_ns=1_000_000_000))
            rows=[dict(kind='gimbal_trace',data=dict(stamp_ns=t,control_steps=n)) for t,n in [(0,0),(50_000_000,12),(100_000_000,2),(150_000_000,14)]]
            (folder/'telemetry.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
            self.assertEqual(control_hz(folder),240)


class ComparisonChecks(unittest.TestCase):
    def data(self,hits=8,weight=7,shots=10):
        return {(1,i):dict(run_id=str(i),shots=shots,hits=hits,weight=weight,duration=6) for i in range(5)}

    def test_pass_regression_and_insufficient(self):
        self.assertEqual(paired_result(self.data(),self.data())['conclusion'],'passed')
        self.assertEqual(paired_result(self.data(),self.data(1,1))['conclusion'],'regressed')
        self.assertEqual(paired_result(self.data(),self.data(0,0,0))['conclusion'],'regressed_no_fire')
        self.assertEqual(paired_result({(1,0):next(iter(self.data().values()))},self.data())['conclusion'],'insufficient_samples')

    def test_uniform_armor_scoring_keeps_front_supplement(self):
        from shots import score
        with tempfile.TemporaryDirectory() as root:
            folder=Path(root)
            truth=dict(kind='armors',data=dict(armors=[dict(id=10,robot_id=1,slot=0)]))
            (folder/'telemetry.jsonl').write_text(json.dumps(truth)+'\n')
            fire=dict(kind=0,shot_seq=1,stamp_ns=1_000_000_000,shooter_id=0,world_point=[0,0,1],velocity=[25,0,0])
            hit=dict(kind=1,shot_seq=1,stamp_ns=1_100_000_000,target_id=1,armor_id=11,
                contact_point=[2,0,1],flight_time_s=.1,impact_geometry_valid=True,
                plate_center=[2,0,1],plate_right=[0,1,0],plate_up=[0,0,1],plate_size=[.2,.1])
            manifest=dict(mode='shoot',duration_s=2,impact_quality_version=1,scoring_version=2,
                scenario_semantics=dict(hit_evaluation='front_armor'),shooting={})
            state=dict(shot_events=[fire,hit],measurement_start_stamp_ns=0,measurement_end_stamp_ns=2_000_000_000)
            metrics,errors=score(folder,manifest,state)
            self.assertFalse(errors)
            self.assertEqual(metrics['target_hit_rate'],1)
            self.assertEqual(metrics['weighted_hit_rate'],1)
            self.assertEqual(metrics['front_armor_hit_rate'],0)
            manifest['scoring_version']=1
            old,_=score(folder,manifest,state)
            self.assertEqual(old['weighted_hit_rate'],0)

    def test_supplements_reuse_payload_and_keep_previous_scores(self):
        from types import SimpleNamespace
        from background_compare import extend
        with tempfile.TemporaryDirectory() as root,patch.object(jobs,'STATE',Path(root)),patch.object(jobs,'DB',Path(root)/'queue.sqlite3'):
            with jobs.connect() as db:
                for parent in ('base','candidate'):
                    db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(parent,'completed',1,1,json.dumps(dict(_protocol=2)),'/frozen/'+parent,0,0,None))
                    item=db.execute('INSERT INTO items(job_id,ordinal,state,settings) VALUES(?,?,?,?)',(parent,0,'completed',json.dumps(dict(seed=1,repeat_id=0)))).lastrowid
                    db.execute('INSERT INTO attempts(id,item_id,slot,state,started,run_id) VALUES(?,?,?,?,?,?)',(parent,item,1,'finished',1,'0'))
                a=self.data();b=self.data()
                # Each side's first run resolves to the same immutable fixture
                # settings; the payload itself is chosen from its parent job.
                result=dict(condition={},conclusion='insufficient_samples',additional_pairs=1)
                args=SimpleNamespace(baseline='base',candidate='candidate')
                queued=extend(db,args,[result],{'{}':a},{'{}':b})['queued_jobs']
                self.assertEqual(len(queued),2)
                for name,parent in zip(queued,('base','candidate')):
                    self.assertEqual(jobs.get_job(db,name)['bundle'],'/frozen/'+parent)
                    row=json.loads(db.execute('SELECT settings FROM items WHERE job_id=?',(name,)).fetchone()[0])
                    self.assertEqual(row['repeat_id'],5)
                self.assertEqual(extend(db,args,[result],{'{}':a},{'{}':b})['queued_jobs'],[])
                self.assertEqual(db.execute("SELECT count(*) FROM attempts WHERE state='finished'").fetchone()[0],2)


if __name__=='__main__':unittest.main()
