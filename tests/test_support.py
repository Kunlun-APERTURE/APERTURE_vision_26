import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from report import load,write
from benchmark.core import identity
import manage,background_jobs as jobs


class QualificationTests(unittest.TestCase):
 def test_qualification_and_migration(self):
  from benchmark.qualification import qualify,signature,limit
  from benchmark.migration import run,SCENES
  from test_execution import payload
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);bundle=payload(root/'payload');out=root/'study';out.mkdir();runroot=root/'experiments/run';runroot.mkdir(parents=True);(runroot/'shots.jsonl').write_text('');write(runroot/'timing.json',dict(wall_seconds=5));write(runroot/'summary.json',dict(metrics=dict(shots_fired=0,target_armor_hits=0,weighted_hit_score_sum=0)))
   contract=dict(bundle=str(bundle));write(out/'contract.json',contract);write(out/'state.json',dict(contract_sha256=identity(contract),job_id='ref'))
   with patch.object(jobs,'STATE',root/'queue'),patch.object(jobs,'DB',root/'queue/db'),patch.object(manage,'OPS',root):
    with jobs.connect() as db:
     db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',('ref','completed',1,1,'{}',str(bundle),1,0,None))
     db.execute('INSERT INTO items(job_id,ordinal,state,settings) VALUES(?,?,?,?)',('ref',0,'completed',json.dumps(dict(scene='hf_static_5'))))
     db.execute('INSERT INTO attempts(id,item_id,slot,state,started,ended,run_id,verdict) VALUES(?,?,?,?,?,?,?,?)',('a',1,1,'finished',1,3,'run','passed'))
    write(out/'qualification-state.json',{'hf_static_5:1':'one','hf_static_5:2':'two'})
    def status(db,id):return dict(state='completed',attempts=[dict(slot=1,started=1,ended=3,run_id='run')] if id!='two' else [dict(slot=1,started=1,ended=3,run_id='run'),dict(slot=2,started=1,ended=3,run_id='run')])
    with patch.object(jobs,'status',side_effect=status),patch('benchmark.qualification.behavior',return_value='x'),patch('benchmark.diagnostics.first_difference',return_value=None),patch('benchmark.diagnostics.relative_events',return_value=[]),patch('benchmark.diagnostics.relative_shots',return_value=[]):
     self.assertTrue(qualify(out)['passed'])
     self.assertEqual(limit(dict(settings=json.dumps({'scene':'hf_static_5'}),bundle=str(bundle),parallelism=2)),2)
    migration=root/'migration';migration.mkdir();write(migration/'state.json',dict(jobs={'reference':'old','candidate':'new'}))
    attempts=[dict(run_id='run') for _ in SCENES]
    with patch.object(jobs,'status',return_value={'state':'completed','attempts':attempts}),patch('benchmark.migration.behavior',return_value='x'),patch('benchmark.diagnostics.first_difference',return_value=None),patch('benchmark.diagnostics.relative_events',return_value=[]),patch('benchmark.diagnostics.relative_shots',return_value=[]):self.assertTrue(run(bundle,bundle,migration)['passed'])


class ProtocolExampleTests(unittest.TestCase):
 def test_socket(self):
  from benchmark.examples.idle_algorithm import serve,read,send
  import socket,threading,struct
  a,b=socket.socketpair();results=[]
  def server():
   try:
    n,p=struct.unpack('!II',read(b,8));read(b,n+p)
    body=json.dumps(dict(type='frame',session='s',seq=1,stamp_ns=1)).encode();b.sendall(struct.pack('!II',len(body),0)+body)
    n,p=struct.unpack('!II',read(b,8));results.append(json.loads(read(b,n)));b.close()
   except Exception as e:results.append(e)
  thread=threading.Thread(target=server);thread.start()
  with self.assertRaises((EOFError,OSError)):serve(a)
  a.close();thread.join();self.assertTrue(results[0]['detector_reset']);self.assertFalse(results[0]['fire_advice'])


class VideoTests(unittest.TestCase):
 def test_prepare(self):
  from benchmark.video import prepare
  import review_video
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);ops=root/'ops';ops.mkdir()
   names=('review_video.py','review_video_render.py','review_video_checks.py','report.py','shots.py','impact_quality.py','scenario_catalog.py','highspeed_scenarios.py','focus_scenarios.py','movement_scenarios.py')
   for n in names:(ops/n).write_text('source')
   font=root/'font';font.write_text('font')
   sources=[]
   for label in ('a','b'):
    folder=root/label;folder.mkdir();sources.append(folder);(folder/'configuration').mkdir()
    write(folder/'manifest.json',dict(scene='hf_static_5',sensor_seed=1,shooting={}))
    write(folder/'run-state.json',dict(measurement_start_stamp_ns=0,measurement_end_stamp_ns=1000000000))
    write(folder/'configuration/bundle.json',dict(images={'evaluator':'image'}))
    for n in review_video.SOURCE_FILES:
     if not (folder/n).exists():(folder/n).parent.mkdir(exist_ok=True);(folder/n).write_text('{}')
   def evaluate(folder,output):
    output.mkdir(parents=True);(output/'shots.jsonl').write_text('')
    return dict(evidence_valid=True,metrics=dict(target_hit_rate=1))
   with patch.object(manage,'OPS',ops),patch.object(review_video,'FONT',font),patch.object(review_video,'validate_pair'),patch.object(review_video,'lines',return_value=iter([{'stamp_ns':2000000000}])),patch('benchmark.video.Evaluator.evaluate',side_effect=evaluate):
    # lines returns a fresh iterator for each side.
    with patch.object(review_video,'lines',side_effect=lambda p:iter([{'stamp_ns':2000000000}])):
     output=prepare(*sources,root/'video');self.assertTrue((output/'export.json').exists())
   with self.assertRaises(ValueError):prepare(*sources,root/'video')

if __name__=='__main__':unittest.main()
