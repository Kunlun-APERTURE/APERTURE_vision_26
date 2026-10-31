"""Isolated process entry: all mutable queue/evidence paths live under scratch."""
import json
import os
from pathlib import Path
import subprocess
import sys


def run(root,binary,key,choices,settings_override=None):
    ops=root/'docker/auto_aim';sys.path.insert(0,str(ops))
    import manage
    import background_jobs as jobs
    from benchmark.artifacts import freeze,verify
    from benchmark.algorithms import get
    from benchmark.core import EnvironmentSpec
    from benchmark.scenarios import selection
    from background_runtime import Worker
    manage.ROOT=root;manage.OPS=ops;manage.RUNTIME=ops/'.runtime';manage.RUNTIME.mkdir(parents=True)
    manage.DESKTOP=root/'disabled-desktop';jobs.STATE=manage.RUNTIME/'jobs';jobs.DB=jobs.STATE/'queue.sqlite3'
    settings_override=settings_override or {}
    names=[]
    if 'basic' in choices:names+=selection('basic')
    advanced=[v for v in choices if v not in ('basic','pressure')]
    if advanced:names+=selection('advanced',advanced,'pressure' in choices)
    names=settings_override.get('scenes',names)
    bundle=freeze('pr_'+key,EnvironmentSpec(),get('B1'),binary)
    metadata=verify(bundle)
    groups=[];all_valid=True
    try:
        for i,scene in enumerate(names):
            shots=hits=weight=seconds=0;maximum_wall_s=0.
            for seed in settings_override.get('seeds',(8101,8102,8103)):
                settings=jobs.validate(dict(EnvironmentSpec().settings(scene,seed,2026102207),algorithm_profile='tracking_fire_v3'))
                settings.update(execution_profile='lockstep-ui-aligned-v7-clock',repeat_id=0)
                runtime=jobs.STATE/'workers'/str(i);runtime.mkdir(parents=True,exist_ok=True)
                request=dict(job_id='pr_'+key,item_id=i,attempt='pr',slot=40,settings=settings,bundle=str(bundle),ops=str(ops),desktop=str(manage.DESKTOP),runtime=str(runtime),result=str(runtime/'result.json'),parallelism=1,exploratory=False,protocol=2)
                path=runtime/'request.json';path.write_text(json.dumps(request))
                env=dict(os.environ,RM26_PR_TASK=key)
                subprocess.run([sys.executable,str(bundle/'docker/auto_aim/background_runtime.py'),str(path)],env=env,check=True,timeout=180)
                result=json.loads((runtime/'result.json').read_text());folder=ops/'experiments'/result['run_id'];summary=json.loads((folder/'summary.json').read_text());m=summary['metrics']
                all_valid &= summary['verdict']!='invalid'
                maximum_wall_s=max(maximum_wall_s,json.loads((folder/'timing.json').read_text()).get('wall_seconds',0))
                shots+=m['shots_fired'];hits+=m['target_armor_hits'];weight+=m.get('weighted_hit_score_sum') or 0;seconds+=json.loads((folder/'manifest.json').read_text())['duration_s']
                all_valid &= json.loads((folder/'session-evidence-audit.json').read_text())['passed']
                subprocess.run(['docker','compose','--project-name','rm26-worker-40','--file',str(runtime/'compose.json'),'down','--volumes','--remove-orphans'],check=True)
            groups.append(dict(maximum_wall_s=maximum_wall_s,within_40s=maximum_wall_s<=40,scene=scene,shots=shots,hits=hits,exact=hits/shots if shots else None,weighted=weight/shots if shots else None,hits_per_s=hits/seconds,weighted_points_per_s=weight/seconds))
        (root/'scores.json').write_text(json.dumps(dict(groups=groups,valid=all_valid)))
    finally:
        # Remove only task-owned tags; never remove shared image digests.
        for tag in metadata['retained_images'].values():subprocess.run(['docker','image','rm',tag],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

if __name__=='__main__':run(Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3],json.loads(sys.argv[4]),json.loads(sys.argv[5]) if len(sys.argv)>5 else None)
