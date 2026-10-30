#!/usr/bin/env python3
"""Queue/parser checks; runtime acceptance is in background_acceptance.py."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import background_jobs as jobs
import manage
from report import load, write
from background_runtime import Worker


class QueueChecks(unittest.TestCase):
    def arguments(self,*argv):
        return manage.parser().parse_args(['experiment','submit',*argv])

    def test_existing_parameters(self):
        rows,n,_=jobs.expand(self.arguments('--scene','hf_turn90_3','--terrain','flat_range','--matrix','single',
            '--mode','shoot','--algorithm-profile','tracking_fire_v3','--parallelism','2'))
        self.assertEqual((len(rows),n),(1,2))
        self.assertEqual(rows[0]['fire'],'auto')
        self.assertEqual(rows[0]['fire_rate_hz'],5)

    def test_matrix_counts(self):
        rows,_,_=jobs.expand(self.arguments('--scene','static','--matrix','hit-v3','--mode','shoot','--repeat','2'))
        self.assertEqual(len(rows),8)
        self.assertEqual({s['physics'] for s in rows},{'drag','no-drag'})
        self.assertEqual({s['aim_origin'] for s in rows},{'muzzle','gimbal'})

    def test_highspeed_expansion(self):
        rows,_,_=jobs.expand(self.arguments('--matrix','highspeed-v1'))
        self.assertGreater(len(rows),10)
        self.assertTrue(all(s['terrain']=='flat_range' and s['mode']=='shoot' for s in rows))

    def test_invalid_numbers(self):
        for argv in (('--fire-rate-hz','6'),('--duration','19'),('--bullet-speed','nan'),('--bullet-speed','10'),('--seed','-1'),('--fire-policy','continuous'),('--max-fps','nan'),('--max-fps','0.5'),('--max-fps','1001')):
            with self.subTest(argv=argv),self.assertRaises(ValueError):jobs.expand(self.arguments(*argv))

    def test_shared_timing_and_lifecycle(self):
        import tomllib
        for cap in (None,0,240,1000):
            argv=[] if cap is None else ['--max-fps',str(cap)]
            rows,_,_=jobs.expand(self.arguments('--scene','static',*argv))
            self.assertEqual(rows[0]['algorithm_lifecycle'],'session')
            config=tomllib.loads(manage.render_config(rows[0]))
            if cap is not None:self.assertEqual(config['performance']['max_fps'],cap)
        rows,_,_=jobs.expand(self.arguments('--algorithm-lifecycle','fresh','--scene','static'))
        self.assertEqual(rows[0]['algorithm_lifecycle'],'fresh')

    def test_ordered_cases(self):
        with tempfile.TemporaryDirectory(dir=manage.RUNTIME) as temporary:
            spec=Path(temporary)/'cases.json'
            cases=[dict(scene='hf_turn90_3',seed=2),dict(scene='hf_static_5',seed=1),dict(scene='hf_turn90_3',seed=2)]
            data=dict(version=1,defaults=dict(terrain='flat_range',mode='shoot',matrix='single'),cases=cases,parallelism=1)
            spec.write_text(json.dumps(data))
            rows,_,_=jobs.expand(self.arguments('--spec',str(spec)))
            self.assertEqual([(r['scene'],r['seed']) for r in rows],[(r['scene'],r['seed']) for r in cases])
            for override in ({'repeat':2},{'matrix':{'seed':[1]}},{'cases':[{'command':'shell'}]}):
                spec.write_text(json.dumps(data|override))
                with self.assertRaises(ValueError):jobs.expand(self.arguments('--spec',str(spec)))

    def test_matrix_spec(self):
        with tempfile.TemporaryDirectory(dir=manage.RUNTIME) as temporary:
            spec=Path(temporary)/'matrix.json'
            spec.write_text(json.dumps(dict(version=1,defaults=dict(terrain='flat_range',mode='shoot',matrix='single'),
                matrix=dict(scene=['hf_static_5','hf_turn90_3'],seed=[1,2]),parallelism=2)))
            rows,n,_=jobs.expand(self.arguments('--spec',str(spec)))
            self.assertEqual((len(rows),n),(4,2))
            self.assertEqual({s['seed'] for s in rows},{1,2})

    def test_spec_cannot_run_shell(self):
        with tempfile.TemporaryDirectory(dir=manage.RUNTIME) as temporary:
            spec=Path(temporary)/'matrix.json'
            spec.write_text(json.dumps(dict(version=1,matrix={'command':['sh','-c','true']})))
            with self.assertRaises(ValueError):jobs.expand(self.arguments('--spec',str(spec)))

    def test_transaction_and_controls(self):
        with tempfile.TemporaryDirectory() as temporary,patch.object(jobs,'STATE',Path(temporary)),patch.object(jobs,'DB',Path(temporary)/'queue.sqlite3'):
            db=jobs.connect()
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',('job_test','queued',1,1,'{}','/tmp/test',2,0,None))
            db.execute('INSERT INTO items(job_id,ordinal,state,settings) VALUES(?,?,?,?)',('job_test',0,'pending','{}'))
            with self.assertRaises(RuntimeError),jobs.transaction(db):
                db.execute("UPDATE jobs SET state='cancelled'");raise RuntimeError('rollback')
            self.assertEqual(jobs.get_job(db,'job_test')['state'],'queued')
            with contextlib.redirect_stdout(io.StringIO()):
                for action,expected in [('pause','pausing'),('resume','queued'),('cancel','cancelled')]:
                    jobs.execute(manage.parser().parse_args(['jobs',action,'job_test']))
                    self.assertEqual(jobs.get_job(db,'job_test')['state'],expected)
            self.assertEqual(db.execute('SELECT state FROM items').fetchone()[0],'cancelled')
            db.close()


class RuntimeChecks(unittest.TestCase):
    def test_algorithm_lifecycle_and_environment_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);bundle=root/'bundle';(bundle/'vision_simulator_26').mkdir(parents=True)
            (bundle/'vision_simulator_26/config.toml').write_text('')
            write(bundle/'bundle.json',{'images':{'simulator':'image'}})
            worker=Worker(dict(bundle=str(bundle),ops=str(root),desktop=str(root),runtime=str(root/'runtime'),
                slot=1,settings={'algorithm_lifecycle':'session'}))
            generation=0;running={};calls=[];config={'version':1}
            worker.definition=lambda:dict(config)
            worker.inspect=lambda name:running.get(name)
            worker.rpc=lambda *_:dict(connected=True,frame_age_ms=10)
            def compose(*args):
                nonlocal generation
                calls.append(args)
                if args[0]=='down':running.clear()
                if args[0]=='up':
                    for service in ('daedalus','bridge','algorithm'):
                        if service not in args:continue
                        generation+=1
                        name={'daedalus':worker.SIMULATOR,'bridge':worker.BRIDGE,'algorithm':worker.DESKTOP_CONTAINER}[service]
                        running[name]=dict(Id=str(generation),State=dict(Running=True,StartedAt=str(generation)))
            worker.compose=compose
            with patch('manage.render_config',return_value=''):
                worker.start();first=running[worker.DESKTOP_CONTAINER]['Id']
                self.assertFalse(load(worker.RUNTIME/'provisioning.json')['algorithm_reused'])
                calls.clear();worker.start()
                self.assertEqual(calls,[])
                self.assertEqual(running[worker.DESKTOP_CONTAINER]['Id'],first)
                self.assertTrue(load(worker.RUNTIME/'provisioning.json')['algorithm_reused'])
                running[worker.DESKTOP_CONTAINER]['State']['Running']=False
                worker.start()
                self.assertFalse(load(worker.RUNTIME/'provisioning.json')['algorithm_reused'])
                worker.settings['algorithm_lifecycle']='fresh';worker.start()
                self.assertNotEqual(running[worker.DESKTOP_CONTAINER]['Id'],first)
                self.assertFalse(load(worker.RUNTIME/'provisioning.json')['algorithm_reused'])
                worker.settings['algorithm_lifecycle']='session';config['version']=2;calls.clear();worker.start()
                self.assertTrue(any(c[0]=='down' for c in calls))
                self.assertFalse(load(worker.RUNTIME/'provisioning.json')['algorithm_reused'])


if __name__=='__main__':unittest.main()
