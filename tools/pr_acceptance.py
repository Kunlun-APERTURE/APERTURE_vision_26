#!/usr/bin/env python3
"""Run the real PR executor locally; replace only GitHub approval/report transport."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'docker/auto_aim'))
from pr_service.executor import Executor
from pr_service.lifecycle import Lifecycle
from pr_service.resources import DockerResources
from pr_service.request import Request


def main():
 p=argparse.ArgumentParser();p.add_argument('--repository',required=True);p.add_argument('--base',required=True);p.add_argument('--head',required=True)
 p.add_argument('--output',type=Path,required=True);p.add_argument('--attempt',type=int,default=1);p.add_argument('--upload-failure',action='store_true');args=p.parse_args()
 root=args.output.resolve();request=Request(args.repository,1,args.head,args.base,args.attempt,'local-acceptance',('basic',),attempt=args.attempt)
 executor=Executor({'experiment_selection':{'scenes':['hf_static_5','adv_variable_pos'],'seeds':[9301]}})
 lifecycle=Lifecycle(root/'scratch',root/'logs',DockerResources())
 def publish(req,result):
  if args.upload_failure:raise ConnectionError('Acceptance-injected upload failure')
  print(json.dumps(result),flush=True)
 status=lifecycle.run(request,executor,publish)
 print(json.dumps(dict(status=status,scratch_absent=not lifecycle.workspace(request.key()).exists())),flush=True)
 if status!='completed' and not args.upload_failure:raise SystemExit(1)

if __name__=='__main__':main()
