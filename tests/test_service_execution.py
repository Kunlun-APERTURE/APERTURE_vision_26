import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock,patch
from pr_service.executor import Executor
from pr_service.github import GitHub
from pr_service.request import Request
from pr_service.service import Service,main
from report import write


def req():return Request('team/repo',1,'a'*40,'b'*40,1,'reviewer',('basic',))


class ExecutorTests(unittest.TestCase):
 def test_command_success_failure_timeout_cancel(self):
  with tempfile.TemporaryDirectory() as tmp:
   log=Path(tmp)/'log';e=Executor({},Mock());e.command(['fake'],log);e.run.assert_called_once()
   e=Executor({});e.valid=lambda:True
   e.command(['python3','-c','print("ok")'],log)
   with self.assertRaises(subprocess.CalledProcessError):e.command(['python3','-c','raise SystemExit(2)'],log)
   e.valid=lambda:False
   with self.assertRaises(RuntimeError):e.command(['python3','-c','import time;time.sleep(10)'],log)
   e.valid=lambda:True
   with self.assertRaises(TimeoutError):e.command(['python3','-c','import time;time.sleep(10)'],log,timeout=0)
 def test_execute_happy_and_invalid(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);work=root/'task';work.mkdir();e=Executor({})
   def command(argv,log,cwd=None,timeout=1200):
    with log.open('a') as f:f.write('command\n')
    if argv[:2]==['git','clone']:
     dest=Path(argv[-1]);dest.mkdir();(dest/'sp_vision_25').mkdir();(dest/'vision_simulator_26').mkdir()
    if argv[:1]==['python3']:
     source=Path(argv[2]);write(source/'scores.json',dict(valid=True,groups=[dict(scene='s',exact=1,weighted=.5)]))
   e.command=command
   with patch('pr_service.executor.subprocess.check_output',side_effect=lambda a,**k:'' if 'diff' in a else 'https://github.com/team/source'):
    result=e.execute(req(),work,lambda:True);self.assertTrue(result['valid']);self.assertEqual(result['scores'][0]['scene'],'s')
  with tempfile.TemporaryDirectory() as tmp:
   with self.assertRaises(ValueError):Executor({},Mock()).execute(req(),Path(tmp),lambda:False)
 def test_resource_lease(self):
  with tempfile.TemporaryDirectory() as tmp,patch('manage.OPS',Path(tmp)):
   e=Executor({});e.execute=Mock(return_value='done');self.assertEqual(e(req(),Path(tmp),lambda:True),'done')
 def test_platform_change(self):
  with tempfile.TemporaryDirectory() as tmp:
   e=Executor({})
   def command(argv,log,cwd=None,timeout=1200):
    log.write_text('log')
    if argv[:2]==['git','clone']:Path(argv[-1]).mkdir()
   e.command=command
   with patch('pr_service.executor.subprocess.check_output',side_effect=lambda a,**k:'docker/auto_aim/shots.py' if 'diff' in a else 'https://github.com/team/source'),patch('pr_service.executor.shutil.copytree'),patch('pr_service.executor.Path.read_text',return_value='{"valid":true,"groups":[]}'):
    with self.assertRaises(ValueError):e.execute(req(),Path(tmp),lambda:True)


class GitHubTests(unittest.TestCase):
 def test_jwt_and_refresh(self):
  api=GitHub('team/repo',1,2,'/key')
  with patch('pr_service.github.subprocess.check_output',return_value=b'signed'):self.assertEqual(len(api.jwt().split('.')),3)
  responses=[{'token':'secret'}, {'ok':True}]
  def urlopen(request,timeout=30):
   response=Mock();response.__enter__=Mock(return_value=io.BytesIO(json.dumps(responses.pop(0)).encode()));response.__exit__=Mock(return_value=False);return response
  with patch.object(api,'jwt',return_value='jwt'),patch('pr_service.github.urllib.request.urlopen',side_effect=urlopen):self.assertEqual(api.repo('GET','/test'),{'ok':True})
 def test_snapshot(self):
  import zipfile
  buffer=io.BytesIO()
  with zipfile.ZipFile(buffer,'w') as archive:archive.writestr('request.json',json.dumps({'review_id':1}))
  api=GitHub('team/repo',1,2,'/key');api.token='token';api.repo=Mock(side_effect=[{'workflow_runs':[{'id':3}]},{'artifacts':[{'name':'experiment-request','expired':False,'archive_download_url':'https://example.org/artifact'}]}])
  response=Mock();response.__enter__=Mock(return_value=io.BytesIO(buffer.getvalue()));response.__exit__=Mock(return_value=False)
  with patch('pr_service.github.urllib.request.build_opener') as opener:
   opener.return_value.open.return_value=response
   self.assertEqual(api.request_snapshot(1),{'review_id':1})
  api.repo=Mock(return_value={'workflow_runs':[]});self.assertIsNone(api.request_snapshot(2))
 def test_service_check_disabled(self):
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'config.json';write(path,dict(repository='team/repo',app_id=1,installation_id=1,private_key='/key',scratch=str(Path(tmp)/'scratch'),logs=str(Path(tmp)/'logs')))
   with patch('sys.argv',['service','check','--config',str(path)]),patch('pr_service.service.GitHub') as api:
    api.return_value.repo.return_value={'full_name':'team/repo'};main()
   with patch('sys.argv',['service','once','--config',str(path)]):
    with self.assertRaises(ValueError):main()

if __name__=='__main__':unittest.main()

class CICommitAcceptanceTests(unittest.TestCase):
 def test_merge_commit_check_is_accepted(self):
  api=GitHub('team/repo',1,2,'/unused')
  api.checks=Mock(side_effect=lambda sha:[] if sha=='head' else [dict(name='development-tests',conclusion='success',app={'slug':'github-actions'})])
  self.assertTrue(api.ci_passed(dict(head={'sha':'head'},merge_commit_sha='merge')))
  api.checks=Mock(return_value=[])
  self.assertFalse(api.ci_passed(dict(head={'sha':'head'})))

class ArtifactRedirectTests(unittest.TestCase):
 def test_cross_host_removes_credentials(self):
  import urllib.request
  from pr_service.github import ArtifactRedirect
  req=urllib.request.Request('https://api.github.com/archive',headers={'Authorization':'Bearer test'})
  handler=ArtifactRedirect()
  redirected=handler.redirect_request(req,None,302,'Found',{},'https://storage.example/artifact?signature=abc')
  self.assertIsNone(redirected.get_header('Authorization'))
  same=handler.redirect_request(req,None,302,'Found',{},'https://api.github.com/other')
  self.assertEqual(same.get_header('Authorization'),'Bearer test')

class GitHubTransientReadTests(unittest.TestCase):
 def test_read_retries_tls_error(self):
  import urllib.error
  api=GitHub('team/repo',1,2,'unused');api.token='token';api.expires=10**20
  response=Mock();response.__enter__=Mock(return_value=io.BytesIO(json.dumps({'ok':True}).encode()));response.__exit__=Mock(return_value=False)
  with patch('pr_service.github.urllib.request.urlopen',side_effect=[urllib.error.URLError('TLS EOF'),response]) as call,patch('pr_service.github.time.sleep'):
   self.assertEqual(api.repo('GET','/test'),{'ok':True});self.assertEqual(call.call_count,2)
 def test_writes_and_permission_errors_are_not_retried(self):
  import urllib.error
  api=GitHub('team/repo',1,2,'unused');api.token='token';api.expires=10**20
  for method,error in [('POST',urllib.error.URLError('TLS EOF')),('GET',urllib.error.HTTPError('url',403,'Forbidden',{},None))]:
   with patch('pr_service.github.urllib.request.urlopen',side_effect=error) as call:
    with self.assertRaises(urllib.error.URLError):api.repo(method,'/test',{} if method=='POST' else None)
    self.assertEqual(call.call_count,1)
 def test_retries_are_bounded(self):
  import urllib.error
  api=GitHub('team/repo',1,2,'unused');api.token='token';api.expires=10**20
  with patch('pr_service.github.urllib.request.urlopen',side_effect=urllib.error.URLError('offline')) as call,patch('pr_service.github.time.sleep'):
   with self.assertRaises(urllib.error.URLError):api.repo('GET','/test')
   self.assertEqual(call.call_count,3)

class ApprovalMonitorTests(unittest.TestCase):
 def test_throttle_and_revocation(self):
  from pr_service.service import ApprovalMonitor
  now=[0];check=Mock(side_effect=[True,False]);monitor=ApprovalMonitor(check,clock=lambda:now[0])
  self.assertTrue(monitor())
  now[0]=29;self.assertTrue(monitor());self.assertEqual(check.call_count,1)
  now[0]=30;self.assertFalse(monitor());self.assertEqual(check.call_count,2)
 def test_error_does_not_refresh_approval(self):
  from pr_service.service import ApprovalMonitor
  check=Mock(side_effect=[ConnectionError('offline'),True]);monitor=ApprovalMonitor(check)
  with self.assertRaises(ConnectionError):monitor()
  self.assertTrue(monitor());self.assertEqual(check.call_count,2)

class ArtifactNetworkTests(unittest.TestCase):
 def test_retry_then_cache(self):
  import zipfile,urllib.error
  buffer=io.BytesIO()
  with zipfile.ZipFile(buffer,'w') as archive:archive.writestr('request.json',json.dumps({'review_id':7}))
  api=GitHub('team/repo',1,2,'unused');api.token='token';api.repo=Mock(side_effect=[{'workflow_runs':[{'id':1}]},{'artifacts':[{'name':'experiment-request','expired':False,'archive_download_url':'https://api.github.com/artifact'}]}])
  response=Mock();response.__enter__=Mock(return_value=io.BytesIO(buffer.getvalue()));response.__exit__=Mock(return_value=False)
  with patch('pr_service.github.urllib.request.build_opener') as opener,patch('pr_service.github.time.sleep'):
   opener.return_value.open.side_effect=[urllib.error.URLError('TLS EOF'),response]
   self.assertEqual(api.request_snapshot(7),{'review_id':7})
   self.assertEqual(api.request_snapshot(7),{'review_id':7})
   self.assertEqual(opener.return_value.open.call_count,2)
   self.assertEqual(api.repo.call_count,2)
 def test_artifact_failure_is_bounded(self):
  import urllib.error
  api=GitHub('team/repo',1,2,'unused');api.token='token';api.repo=Mock(side_effect=[{'workflow_runs':[{'id':1}]},{'artifacts':[{'name':'experiment-request','expired':False,'archive_download_url':'https://api.github.com/artifact'}]}])
  with patch('pr_service.github.urllib.request.build_opener') as opener,patch('pr_service.github.time.sleep'):
   opener.return_value.open.side_effect=urllib.error.URLError('offline')
   with self.assertRaises(urllib.error.URLError):api.request_snapshot(7)
   self.assertEqual(opener.return_value.open.call_count,3);self.assertEqual(api.snapshots,{})
