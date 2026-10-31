"""Poll approved requests without an inbound port or a self-hosted Actions runner."""
import argparse
from dataclasses import replace
import json
import time
from pathlib import Path
from .request import authorize,selection
from .github import GitHub
from .lifecycle import Lifecycle
from .resources import DockerResources


class ApprovalMonitor:
    def __init__(self,check,interval=30,clock=time.monotonic):
        self.check=check;self.interval=interval;self.clock=clock;self.last=None;self.value=False

    def __call__(self):
        now=self.clock()
        if self.last is None or now-self.last>=self.interval:
            # A failed query is never cached as approval.
            self.value=self.check();self.last=self.clock()
        return self.value


class Service:
    def __init__(self,api,lifecycle,execute):self.api=api;self.lifecycle=lifecycle;self.execute=execute

    def approved(self,pr):
        reviews=self.api.repo('GET',f"/pulls/{pr['number']}/reviews?per_page=100")
        latest={}
        for r in reviews:
            if r['state']!='COMMENTED':latest[r['user']['login']]=r
        for review in sorted(latest.values(),key=lambda r:r.get('submitted_at',''),reverse=True):
            permission=self.api.repo('GET',f"/collaborators/{review['user']['login']}/permission")['permission']
            try:
                request=authorize(self.api.repository,pr,review,permission,pr.get('_selection_changed_at'))
                snapshot=self.api.request_snapshot(review['id'])
                if not snapshot or snapshot['head']!=request.head or snapshot['base']!=request.base or selection(snapshot['body'])!=request.choices:continue
                # Only the named trusted CI check is accepted, never arbitrary success.
                if not self.api.ci_passed(pr):return None
                return request
            except ValueError:continue
        return None

    def poll(self):
        for listing in self.api.repo('GET','/pulls?state=open&per_page=100'):
            pr=self.api.repo('GET',f"/pulls/{listing['number']}")
            if not selection(pr.get('body')):continue
            request=self.approved(pr)
            if not request:continue
            original=request
            previous=self.lifecycle.logs/(request.key()+'.log')
            if previous.exists():
                text=previous.read_text(errors='replace')
                first=text.splitlines()[0]
                old=__import__('json').loads(first.removeprefix('request '))
                if 'finished; score delivered' in text or request.review_id<=old['review_id']:continue
                request=replace(request,attempt=request.review_id)
            def check_approval():
                current=self.api.repo('GET',f'/pulls/{request.number}')
                approved=self.approved(current)
                return approved is not None and approved.key()==original.key()
            valid=ApprovalMonitor(check_approval)
            if sum(p.stat().st_size for p in self.lifecycle.logs.glob('*.log'))>10*1024**3:raise RuntimeError('Log storage threshold reached; operator action required')
            if (self.lifecycle.logs/(request.key()+'.log')).exists():continue
            self.api.status(request,'Queued for workstation')
            def execute(req,work,validate):
                self.api.status(req,'Building and running paired experiments')
                return self.execute(req,work,validate)
            self.lifecycle.run(request,execute,self.api.publish,valid)


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=('check','once','serve'))
    p.add_argument('--config',type=Path,required=True);a=p.parse_args();c=json.loads(a.config.read_text())
    api=GitHub(c['repository'],c['app_id'],c['installation_id'],c['private_key'])
    from .executor import Executor
    lifecycle=Lifecycle(Path(c['scratch']),Path(c['logs']),DockerResources())
    if a.action=='check':
        print(json.dumps(dict(repository=api.repo('GET','')['full_name'],credentials='ok',enabled=c.get('enabled',False))))
        return
    if not c.get('enabled'):raise ValueError('Service disabled; finish deployment checks first')
    lifecycle.recover();service=Service(api,lifecycle,Executor(c))
    while True:
        try:service.poll()
        except Exception as e:print(type(e).__name__+': '+str(e),flush=True)
        if a.action=='once':break
        time.sleep(c.get('poll_seconds',30))

if __name__=='__main__':main()
