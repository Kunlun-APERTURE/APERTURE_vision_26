"""Small GitHub App client. No credential is passed to candidate code."""
import base64
import json
import subprocess
import time
import urllib.request
from pathlib import Path

class ArtifactRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        from urllib.parse import urlsplit
        redirected=super().redirect_request(req,fp,code,msg,headers,newurl)
        if redirected and urlsplit(req.full_url).netloc!=urlsplit(newurl).netloc:
            redirected.remove_header('Authorization')
        return redirected


class GitHub:
    def __init__(self,repository,app_id,installation_id,key):
        self.repository=repository;self.app_id=str(app_id);self.installation_id=installation_id;self.key=Path(key)
        self.token=None;self.expires=0;self.check_ids={}

    def jwt(self):
        encode=lambda b:base64.urlsafe_b64encode(b).rstrip(b'=')
        now=int(time.time());body=b'.'.join([encode(b'{"alg":"RS256","typ":"JWT"}'),encode(json.dumps(dict(iat=now-60,exp=now+540,iss=self.app_id)).encode())])
        signature=subprocess.check_output(['openssl','dgst','-sha256','-sign',str(self.key)],input=body)
        return (body+b'.'+encode(signature)).decode()

    def call(self,method,path,data=None,app=False):
        if not app and time.time()>=self.expires:
            result=self.call('POST',f'/app/installations/{self.installation_id}/access_tokens',app=True)
            self.token=result['token'];self.expires=time.time()+3000
        request=urllib.request.Request('https://api.github.com'+path,data=json.dumps(data).encode() if data is not None else None,method=method,
            headers={'Authorization':'Bearer '+(self.jwt() if app else self.token),'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','Content-Type':'application/json','User-Agent':'aperture-experiment-service'})
        with urllib.request.urlopen(request,timeout=30) as response:
            raw=response.read();return json.loads(raw) if raw else None

    def repo(self,method,path,data=None):return self.call(method,'/repos/'+self.repository+path,data)

    def request_snapshot(self,review_id):
        import io,zipfile
        runs=self.repo('GET','/actions/workflows/experiment-request.yml/runs?status=success&per_page=100')['workflow_runs']
        for run in runs:
            artifacts=self.repo('GET',f"/actions/runs/{run['id']}/artifacts")['artifacts']
            for artifact in artifacts:
                if artifact['name']!='experiment-request' or artifact['expired']:continue
                request=urllib.request.Request(artifact['archive_download_url'],headers={'Authorization':'Bearer '+self.token,'Accept':'application/vnd.github+json'})
                with urllib.request.build_opener(ArtifactRedirect()).open(request,timeout=30) as response:raw=response.read(1024*1024+1)
                if len(raw)>1024*1024:raise ValueError('Oversized request artifact')
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    info=archive.getinfo('request.json')
                    if info.file_size>65536:raise ValueError('Oversized request')
                    data=json.loads(archive.read(info))
                if data.get('review_id')==review_id:return data
        return None

    def checks(self,sha):return self.repo('GET',f'/commits/{sha}/check-runs')['check_runs']

    def ci_passed(self,pr):
        # GitHub pull_request checks run on the synthetic merge commit, not always head.
        commits=list(dict.fromkeys([pr['head']['sha'],pr.get('merge_commit_sha')]))
        for commit in commits:
            if not commit:continue
            if any(c['name']=='development-tests' and c['conclusion']=='success' and c.get('app',{}).get('slug')=='github-actions' for c in self.checks(commit)):return True
        return False

    def update_check(self,request,payload):
        key=request.key()
        if key not in self.check_ids:
            created=self.repo('POST','/check-runs',dict(name='APERTURE experiments',head_sha=request.head,external_id=key,**payload))
            self.check_ids[key]=created['id']
            return created
        return self.repo('PATCH',f'/check-runs/{self.check_ids[key]}',payload)

    def status(self,request,stage):
        return self.update_check(request,dict(status='in_progress',output=dict(title=stage,summary='PR #'+str(request.number)+' · '+request.key())))

    def publish(self,request,result):
        text=score_table(result)
        check=self.update_check(request,dict(status='completed',conclusion='success' if result.get('valid') else 'failure',output=dict(title='PR experiment results',summary=text)))
        marker='<!-- aperture-experiment:'+request.key()+' -->'
        self.repo('POST',f'/issues/{request.number}/comments',dict(body=marker+'\n'+text+'\n[Experiment check]('+check['html_url']+')'))
        # The publisher workflow receives bounded plain text, never a local path.
        self.repo('POST','/actions/workflows/experiment-results.yml/dispatches',dict(ref='main',inputs=dict(request_id=request.key(),text=text[:20000],logs=result.get('log','')[-20000:])))


def score_table(result):
    rows=['PR experiment results','',f"Base `{result.get('base','')}` · Head `{result.get('head','')}`",'', '|Scene|Base exact|PR exact|Base weighted|PR weighted|Δ weighted|','|---|---:|---:|---:|---:|---:|']
    pct=lambda v:'N/A' if v is None else f'{100*v:.2f}%'
    for row in result.get('scores',[]):
        a,b=row['base'],row['candidate'];x,y=a.get('weighted'),b.get('weighted')
        rows.append('|'+row['scene']+'|'+pct(a.get('exact'))+'|'+pct(b.get('exact'))+'|'+pct(x)+'|'+pct(y)+'|'+('N/A' if x is None or y is None else f'{100*(y-x):+.2f} pp')+'|')
    rows+=['','|Scene|Base hits/s|PR hits/s|Base weighted/s|PR weighted/s|Base shots|PR shots|','|---|---:|---:|---:|---:|---:|---:|']
    for row in result.get('scores',[]):
        a,b=row['base'],row['candidate']
        rows.append('|'+row['scene']+'|'+ '|'.join(str(v) for v in (a.get('hits_per_s','N/A'),b.get('hits_per_s','N/A'),a.get('weighted_points_per_s','N/A'),b.get('weighted_points_per_s','N/A'),a.get('shots','N/A'),b.get('shots','N/A')))+'|')
    rows+=['', 'Execution: '+('valid' if result.get('valid') else 'invalid'),result.get('message','')]
    return '\n'.join(rows)
