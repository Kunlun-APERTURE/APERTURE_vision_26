import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock,patch
from report import write,load
from benchmark.core import ExperimentSpec,identity,sha
from benchmark.artifacts import seal
import manage
import background_jobs as jobs


def payload(path):
 path.mkdir();(path/'program').write_text('binary')
 meta=dict(images={'simulator':'sim','evaluator':'eval','algorithm':'algo'},files={'program':sha(path/'program')},gpu_uuid='gpu',algorithm=dict(id='B1',binary_sha256=sha(path/'program'),algorithm_profile='image_v1'),environment={'id':'E1','gimbal_dynamics':'rm2017-yaw-corrected-raw','timing_profile':'ui-aligned-v7-clock'})
 seal(path,meta);return path


class RunnerTests(unittest.TestCase):
 def test_submission_resume_wait(self):
  from benchmark.runner import Runner
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);bundle=payload(root/'payload');out=root/'output';spec=ExperimentSpec('B1','E1',('hf_static_5',),(1,))
   with patch.object(jobs,'STATE',root/'queue'),patch.object(jobs,'DB',root/'queue/db'),patch('benchmark.runner.algorithm') as adapter,patch('benchmark.runner.freeze',return_value=bundle):
    adapter.return_value.describe.return_value={'algorithm_profile':'image_v1'}
    runner=Runner();state=runner.submit(spec,out,None)
    self.assertEqual(state['items'],1)
    self.assertEqual(runner.submit(spec,out,None,resume=True)['job_id'],state['job_id'])
    with self.assertRaises(ValueError):runner.submit(spec,out,None)
    with self.assertRaises(ValueError):runner.submit(spec,root/'missing',None,resume=True)
    with self.assertRaises(ValueError):runner.submit(spec,root/'parallel',None,parallelism=2,bundle_path=bundle)
    with patch.object(jobs,'status',return_value={'state':'completed'}),patch('benchmark.evaluation.report',return_value={'done':True}):self.assertEqual(runner.wait(out),{'done':True})
    with patch.object(jobs,'status',return_value={'state':'paused'}):
     with self.assertRaises(RuntimeError):runner.wait(out)
    with patch.object(jobs,'status',return_value={'state':'running'}):
     with self.assertRaises(RuntimeError):runner.wait(out)
    # Recover the interrupted enqueue window from the saved plan.
    with jobs.connect() as db:db.execute('DELETE FROM jobs WHERE id=?',(state['job_id'],))
    with patch.object(runner,'enqueue') as enqueue:runner.submit(spec,out,None,resume=True);enqueue.assert_called_once()
    with self.assertRaises(ValueError):runner.submit(ExperimentSpec('B1','bad',('hf_static_5',),(1,)),root/'bad',None)
 def test_imported_and_execute(self):
  from benchmark.runner import Runner,execute
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);bundle=payload(root/'payload')
   with patch.object(jobs,'STATE',root/'queue'),patch.object(jobs,'DB',root/'queue/db'):
    runner=Runner();spec=ExperimentSpec('B1','E1',('hf_static_5',),(1,));runner.submit(spec,root/'out',None,bundle_path=bundle)
    with self.assertRaises(ValueError):runner.submit(ExperimentSpec('other','E1',('hf_static_5',),(1,)),root/'other',None,bundle_path=bundle)
   args=argparse.Namespace(list=True,tier='basic',cases=None,pressure=False)
   with contextlib.redirect_stdout(io.StringIO()):execute(args)
   args.list=False;args.output=None
   with self.assertRaises(ValueError):execute(args)
   args.output=root/'x';args.sensor_seeds='1';args.algorithm='B1';args.environment='E1';args.trajectory_seed=1;args.repeats=1;args.artifact=None;args.bundle=bundle;args.parallelism='1';args.resume=False;args.wait=True
   with patch.object(Runner,'submit',return_value={'ok':True}),patch.object(Runner,'wait'),contextlib.redirect_stdout(io.StringIO()):execute(args)


class EvaluationTests(unittest.TestCase):
 def test_scoring_and_compare(self):
  from benchmark.evaluation import Evaluator,compare,report
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);folder=root/'run';folder.mkdir();bundle=payload(root/'payload');(folder/'configuration').mkdir()
   write(folder/'configuration/bundle.json',load(bundle/'bundle.json'));write(folder/'manifest.json',dict(scene='hf_static_5',sensor_seed=1,duration_s=3))
   write(folder/'run-state.json',{});write(folder/'summary.json',dict(verdict='passed'));write(folder/'session-evidence-audit.json',{'passed':True});write(folder/'timing.json',{'wall_seconds':4})
   output=root/'score'
   def score(f,m,s,d):
    (d/'shots.jsonl').write_text('');return dict(target_armor_hits=0,shots_fired=0,weighted_hit_score_sum=0,weighted_hit_rate=None),[]
   with patch('shots.score',side_effect=score),patch('benchmark.evaluation.behavior',return_value='same'):
    self.assertTrue(Evaluator().evaluate(folder,output)['evidence_valid'])
    with self.assertRaises(ValueError):Evaluator().evaluate(folder,folder/'nested')
   contract=dict(environment={},plans={},scoring_version=2,impact_quality_version=1,bundle=str(bundle),spec=dict(environment='E1',scenes=['hf_static_5'],sensor_seeds=[1],trajectory_seed=1,repeats=1))
   for name in ('left','right'):
    p=root/name;p.mkdir();write(p/'contract.json',contract);write(p/'results.json',dict(complete=True,all_evidence_valid=True))
   self.assertTrue(compare(root/'left',root/'right')['eligible'])
   write(root/'right/results.json',dict(complete=False,all_evidence_valid=True))
   with self.assertRaises(ValueError):compare(root/'left',root/'right')
   state=dict(job_id='test',items=1,contract_sha256=identity(contract));write(root/'left/state.json',state)
   with patch.object(jobs,'STATE',root/'queue'),patch.object(jobs,'DB',root/'queue/db'),patch('benchmark.evaluation.Evaluator.evaluate',return_value=dict(run_id='run',scene='hf_static_5',sensor_seed=1,evidence_valid=True,within_cost_budget=True,metrics=dict(shots_fired=1,target_armor_hits=1,weighted_hit_score_sum=.5,weighted_hit_rate=.5,hits_per_s=.3))):
    with jobs.connect() as db:
     db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',('test','completed',1,1,'{}',str(bundle),1,0,None))
     db.execute('INSERT INTO items(job_id,ordinal,state,settings) VALUES(?,?,?,?)',('test',0,'completed','{}'))
     db.execute('INSERT INTO attempts(id,item_id,slot,state,started,run_id,verdict) VALUES(?,?,?,?,?,?,?)',('a',1,1,'finished',1,'run','passed'))
    (root/'experiments/run').mkdir(parents=True);write(root/'experiments/run/manifest.json',dict(duration_s=3))
    with patch.object(manage,'OPS',root):self.assertTrue(report(root/'left')['complete'])


class AdapterTests(unittest.TestCase):
 def test_build_and_manifest(self):
  from benchmark.algorithms import R4Adapter,ProcessAdapter,get
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=root/'sp_vision_25';source.mkdir();(source/'file.cpp').write_text('source');out=root/'out';(out/'build').mkdir(parents=True);binary=out/'build/sim_auto_aim';binary.write_text('binary')
   with patch('benchmark.algorithms.subprocess.run'),patch('benchmark.algorithms.subprocess.check_output',side_effect=lambda argv,**k: 'a'*40 if 'rev-parse' in argv else b'file.cpp\0' if 'ls-files' in argv else '[{"Id":"image"}]'):
    adapter=R4Adapter();self.assertEqual(adapter.build(root,out),binary);self.assertTrue((out/'build-provenance.json').exists());d=adapter.describe(root,binary)
   with self.assertRaises(ValueError):adapter.describe(root,root/'missing')
   d.update(id='team',protocol='rm26-image-v1',capabilities=['simulation_clock','session_reset','fire_advice'],launch=['program']);manifest=root/'adapter.json';write(manifest,d)
   self.assertEqual(get(str(manifest)).describe(root,binary)['id'],'team')
   with self.assertRaises(ValueError):ProcessAdapter(manifest).build(root,out)
   with self.assertRaises(ValueError):ProcessAdapter(manifest).describe(root,source/'file.cpp')
   for key,value in [('id',''),('protocol','bad'),('launch',[]),('capabilities',[])]:
    write(manifest,dict(d,**{key:value}))
    with self.assertRaises(ValueError):ProcessAdapter(manifest)
   self.assertIsInstance(get('B1'),R4Adapter)


class CLITests(unittest.TestCase):
 def test_dispatch(self):
  from benchmark.cli import execute,add_parser
  parser=argparse.ArgumentParser();add_parser(parser.add_subparsers())
  targets={'verify':'benchmark.cli.verify','export':'benchmark.cli.export_bundle','import':'benchmark.cli.import_bundle','report':'benchmark.evaluation.report','qualify':'benchmark.qualification.qualify','ui-check':'benchmark.ui.check','compare':'benchmark.evaluation.compare'}
  with tempfile.TemporaryDirectory() as tmp:
   for operation,target in targets.items():
    a=argparse.Namespace(operation=operation,algorithm='B1',output=Path(tmp),bundle=Path(tmp),candidate=Path(tmp),reference=Path(tmp),images=False)
    with patch(target,return_value={}),contextlib.redirect_stdout(io.StringIO()):execute(a)
   a.operation='build'
   with patch('benchmark.cli.get') as adapter,contextlib.redirect_stdout(io.StringIO()):
    adapter.return_value.describe.return_value={};execute(a)
   a.operation='video'
   with patch('benchmark.video.prepare',return_value=tmp),patch('review_video.enqueue'),contextlib.redirect_stdout(io.StringIO()):execute(a)
   a.operation='verify';a.bundle=None
   with self.assertRaises(ValueError):execute(a)

if __name__=='__main__':unittest.main()
