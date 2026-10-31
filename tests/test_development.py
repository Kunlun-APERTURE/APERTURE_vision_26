import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from benchmark.context import RunContext
from benchmark.core import Scenario,ScenarioPlan,EnvironmentSpec
from benchmark.scenarios import register,get,selection,REGISTRY
from benchmark.jobs import JobService
from pr_service.request import Request,selection as choices,authorize
from pr_service.lifecycle import Lifecycle
from pr_service.resources import DockerResources
from pr_service.github import score_table,GitHub
from pr_service.service import Service

HEAD='a'*40
BASE='b'*40

def request():return Request('team/repo',1,HEAD,BASE,3,'reviewer',('basic',))

def pr():return dict(number=1,state='open',draft=False,user={'login':'author'},head={'sha':HEAD},base={'sha':BASE},body='- [x] experiment:basic')

def review():return dict(id=3,state='APPROVED',user={'login':'reviewer'},commit_id=HEAD,submitted_at='2026-01-02')


class RequestTests(unittest.TestCase):
 def test_choices(self):
  for body,out in [('',()),('- [x] experiment:none',()),('- [X] experiment:basic',('basic',))]:self.assertEqual(choices(body),out)
  for body in ('- [x] experiment:unknown','- [x] experiment:pressure','- [x] experiment:none\n- [x] experiment:basic'):
   with self.assertRaises(ValueError):choices(body)
 def test_approval(self):
  a=authorize('team/repo',pr(),review(),'write');self.assertEqual(a.key(),request().key())
  for change,rev,permission,time in [(dict(state='closed'),{},'write',None),(dict(draft=True),{},'write',None),({},dict(state='DISMISSED'),'write',None),({},dict(user={'login':'author'}),'write',None),({},dict(commit_id=BASE),'write',None),({}, {},'read',None),({}, {},'write','2026-01-03')]:
   with self.assertRaises(ValueError):authorize('team/repo',dict(pr(),**change),dict(review(),**rev),permission,time)
 def test_bad_identity(self):
  for repo,number,head,options in [('../bad',1,HEAD,('basic',)),('team/repo',0,HEAD,('basic',)),('team/repo',1,'bad',('basic',)),('team/repo',1,HEAD,())]:
   with self.assertRaises(ValueError):Request(repo,number,head,BASE,1,'r',options)
  self.assertEqual(request().key(),Request('team/repo',1,HEAD,BASE,9,'another',('basic',)).key())


class LifecycleTests(unittest.TestCase):
 def test_success_failure_and_cleanup(self):
  for failure in ('none','execute','upload','invalid'):
   with self.subTest(failure=failure),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);resources=Mock();life=Lifecycle(root/'scratch',root/'logs',resources)
    local=root/'local';local.mkdir();(local/'sentinel').write_text('keep')
    def execute(r,w,v):
     (w/'image.mjpg').write_bytes(b'raw');(w/'score.json').write_text('{}')
     if failure=='execute':raise RuntimeError('build failed')
     return dict(valid=True)
    publish=Mock(side_effect=RuntimeError('offline') if failure=='upload' else None)
    result=life.run(request(),execute,publish,lambda:failure!='invalid')
    self.assertEqual(result,'completed' if failure=='none' else 'failed')
    self.assertFalse(life.workspace(request().key()).exists());resources.remove.assert_called_once()
    self.assertTrue((local/'sentinel').exists());self.assertEqual(life.run(request(),execute,publish),'already-attempted')
 def test_recover_and_log_bound(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);life=Lifecycle(root/'scratch',root/'logs',Mock(),100)
   path=life.workspace(request().key());path.mkdir(parents=True);(path/'raw').write_text('x');life.recover()
   self.assertFalse(path.exists());life.log(request().key(),'x'*1000);self.assertLessEqual((root/'logs'/(request().key()+'.log')).stat().st_size,100)
   with self.assertRaises(ValueError):life.workspace('../bad')
 def test_cleanup_on_base_exception(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);life=Lifecycle(root/'scratch',root/'logs',Mock())
   with self.assertRaises(KeyboardInterrupt):life.run(request(),Mock(side_effect=KeyboardInterrupt),Mock())
   self.assertFalse(life.workspace(request().key()).exists())


class SDKTests(unittest.TestCase):
 def test_context(self):
  for source,keep in [('local',True),('pr',False)]:
   c=RunContext(Path('/tmp'),source,'test');self.assertEqual(c.retain_results,keep);self.assertEqual(c.publish_desktop,keep);self.assertEqual(c.workspace(),Path('/tmp/test'))
  for source,key in [('bad','x'),('local','../x')]:
   with self.assertRaises(ValueError):RunContext(Path('/tmp'),source,key)
 def test_scene_extension(self):
  class New(Scenario):
   def generate(self,seed):return ScenarioPlan('new_example',1,'duration = 1',dict(duration_s=1,seed=seed))
  register('new_example',New())
  try:
   self.assertEqual(selection('basic','new_example'),['new_example']);self.assertEqual(get('new_example').generate(2).metadata['seed'],2)
   self.assertEqual(get('new_example').difficulty(None,None,None),dict(applicable=False))
  finally:REGISTRY.pop('new_example')
  self.assertNotIn('algorithm_profile',EnvironmentSpec().settings('s',1,1))
 def test_job_service(self):
  import background_jobs as jobs
  with tempfile.TemporaryDirectory() as tmp,patch.object(jobs,'STATE',Path(tmp)),patch.object(jobs,'DB',Path(tmp)/'queue.db'):
   service=JobService(jobs);self.assertEqual(service.submit('id','bundle',[{'scene':'s'}]),'id');self.assertEqual(service.status('id')['items'],{'pending':1})
   service.submit('probe','bundle',[{}],2,True)
   for rows,level in [([],1),([{}],8)]:
    with self.assertRaises(ValueError):service.submit('bad','bundle',rows,level)


class ServiceTests(unittest.TestCase):
 def api(self):
  api=Mock(repository='team/repo')
  def repo(method,path,data=None):
   if path.startswith('/pulls?'):return [pr()]
   if '/reviews?' in path:return [review()]
   if path.startswith('/collaborators'):return {'permission':'write'}
   return pr()
  api.repo.side_effect=repo;api.checks.return_value=[dict(name='development-tests',conclusion='success',app={'slug':'github-actions'})]
  api.request_snapshot.return_value=dict(head=HEAD,base=BASE,body=pr()['body']);return api
 def test_poll_and_revalidate(self):
  api=self.api();life=Mock();life.logs=Path('/nonexistent');s=Service(api,life,Mock());s.poll();self.assertTrue(life.run.called)
  self.assertTrue(life.run.call_args.args[3]())
  self.assertEqual(life.run.call_args.args[1](request(),Path('/tmp'),lambda:True),s.execute.return_value)
 def test_reject_no_ci_or_changed_options(self):
  api=self.api();s=Service(api,Mock(),Mock());api.ci_passed.return_value=False;self.assertIsNone(s.approved(pr()))
  api=self.api();api.request_snapshot.return_value=dict(head=HEAD,base=BASE,body='- [x] experiment:pursuit');self.assertIsNone(Service(api,Mock(),Mock()).approved(pr()))
 def test_resources_only_task_label(self):
  run=Mock(return_value=Mock(stdout='id\nid\n'));DockerResources(run).remove(request().key());self.assertEqual(run.call_count,9)
  for call in run.call_args_list:
   argv=call.args[0];self.assertNotIn('prune',argv)
 def test_score_and_publish(self):
  text=score_table(dict(valid=True,scores=[dict(scene='s',base={'weighted':None,'exact':None},candidate={'weighted':.2,'exact':1})]));self.assertIn('N/A',text)
  api=GitHub('team/repo',1,2,'/unused');api.repo=Mock(return_value={'html_url':'url','id':1})
  api.publish(request(),dict(valid=True,scores=[],log='hello'));self.assertEqual(api.repo.call_count,3)
  self.assertIn('+',score_table(dict(valid=True,scores=[dict(scene='s',base={'weighted':.1},candidate={'weighted':.2})])))

if __name__=='__main__':unittest.main()

class AcceptanceRegressionTests(unittest.TestCase):
 def test_one_check_is_updated(self):
  api=GitHub('team/repo',1,2,'/unused');api.repo=Mock(return_value={'html_url':'url','id':17})
  api.status(request(),'queued');api.status(request(),'running');api.publish(request(),{'valid':True,'scores':[]})
  calls=[c for c in api.repo.call_args_list if '/check-runs' in c.args[1]]
  self.assertEqual([c.args[0] for c in calls],['POST','PATCH','PATCH'])
  self.assertEqual(calls[-1].args[1],'/check-runs/17')
 def test_logging_failure_does_not_skip_cleanup(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);resources=Mock();life=Lifecycle(root/'scratch',root/'logs',resources)
   life.log=Mock(side_effect=OSError('disk full'))
   with self.assertRaises(OSError):life.run(request(),Mock(),Mock())
   resources.remove.assert_called_once_with(request().key())
   self.assertFalse(life.workspace(request().key()).exists())

class RetryAcceptanceTests(unittest.TestCase):
 def test_new_review_retries_failure_only(self):
  from dataclasses import replace
  for successful in (False,True):
   with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);life=Lifecycle(root/'scratch',root/'logs',Mock());old=request()
    life.log(old.key(),'request '+json.dumps(old.data()))
    life.log(old.key(),'finished; score delivered' if successful else 'failed RuntimeError: unavailable')
    api=Mock(repository='team/repo');api.repo.return_value=[pr()]
    api.repo.side_effect=lambda method,path,data=None:[pr()] if path.startswith('/pulls?') else pr()
    service=Service(api,life,Mock(return_value={'valid':True}))
    service.approved=Mock(return_value=replace(old,review_id=99))
    service.poll()
    self.assertEqual(service.execute.call_count,0 if successful else 1)
    if not successful:
     called=service.execute.call_args.args[0];self.assertEqual(called.attempt,99)
     self.assertTrue((life.logs/(called.key()+'.log')).exists())
