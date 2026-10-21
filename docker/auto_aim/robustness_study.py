#!/usr/bin/env python3
"""Resumable paired robustness experiments with frozen source and explicit seeds."""
import argparse,contextlib,fcntl,hashlib,json,subprocess,sys,time
from pathlib import Path
import experiments,manage
from scenario_catalog import EXTRA
from report import load,write

BASE=('move_ref','line','ego_sweep','ego_sweep_reverse','spin')
DEVELOP=('line','ego_sweep_reverse','random_both_05','random_both_10','range_in_out','spin_move_ego_15')


def signature():
    paths=[manage.ROOT/'sp_vision_25/build/simulator/sim_auto_aim',manage.ROOT/'sp_vision_25/configs/simulator.yaml']
    paths+=[manage.OPS/name for name in ('manage.py','experiments.py','evaluator.py','report.py','shots.py','bridge.py','scenario_catalog.py','algorithm_runner.py','command_script.py','robustness_study.py')]
    paths+=[manage.OPS/'compose.yml']
    paths+=sorted((manage.ROOT/'sp_vision_25/tasks/auto_aim').glob('*.hpp'))+sorted((manage.ROOT/'sp_vision_25/tasks/auto_aim').glob('*.cpp'))
    paths+=sorted((manage.ROOT/'sp_vision_25/tasks/auto_aim/yolos').glob('*.[ch]pp'))
    paths+=[manage.ROOT/'sp_vision_25/src/sim_auto_aim.cpp']
    paths+=sorted(p for p in (manage.ROOT/'sp_vision_25/assets').iterdir() if p.suffix in ('.xml','.bin','.onnx'))
    return dict(files={str(p.relative_to(manage.ROOT)):experiments.digest(p) for p in paths},simulator_image=manage.inspect(manage.IMAGE)['Id'])


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--phase',choices=['development','validation','display','preflight'],default='development')
    p.add_argument('--scenes',nargs='+');p.add_argument('--profiles',nargs='+',default=['tracking_v2','tracking_v3','tracking_fire_v3'])
    p.add_argument('--physics',nargs='+',default=['no-drag','drag']);p.add_argument('--repeats',type=int);p.add_argument('--command-script',default='');p.add_argument('--seed-start',type=int)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True);batch_path=a.output/(a.phase+'-batch.json')
    scenes=a.scenes or (list(BASE+EXTRA) if a.phase=='validation' else list(DEVELOP))
    n=a.repeats or (3 if a.phase=='validation' else 1)
    if not 1<=n<=30:p.error('repeats must be within 1..30')
    start=a.seed_start if a.seed_start is not None else (2026102101 if a.phase=='validation' else 2026102001)
    if a.phase=='validation' and (start!=2026102101 or n!=3):p.error('Formal validation requires exactly three fixed seeds 2026102101..2026102103')
    if a.phase!='validation' and (start not in (2026102001,2026102002) or start+n-1>2026102002):p.error('Development seeds must stay within 2026102001..2026102002')
    seeds=[start+i for i in range(n)]
    display=['on','off'] if a.phase=='display' else ['on']
    definition=dict(phase=a.phase,scenes=scenes,profiles=a.profiles,physics=a.physics,seeds=seeds,display=display,command_script_sha256=experiments.digest(Path(a.command_script)) if a.command_script else None)
    old=load(batch_path,{})
    frozen=signature()
    if old and (old['definition']!=definition or old['signature']!=frozen):raise RuntimeError('Frozen study differs; use another output directory')
    batch=old or dict(definition=definition,signature=frozen,runs=[],invalid_attempts=[],complete=False)
    expected=len(scenes)*len(a.profiles)*len(a.physics)*n*len(display)
    write(batch_path,batch)
    with (manage.RUNTIME/'operation.lock').open('a') as lock:
      fcntl.flock(lock,fcntl.LOCK_EX)
      try:
       for repeat,seed in enumerate(seeds,1):
        profiles=a.profiles if repeat%2 else list(reversed(a.profiles))
        for physics in a.physics:
         for visible in display:
          for profile in profiles:
           pending=[s for s in scenes if not any(r['key']==[seed,physics,visible,profile,s] for r in batch['runs'])]
           if not pending:continue
           manage.stop();manage.start(argparse.Namespace(scene='static',control='off',view='off',replace=False,fire='auto',physics=physics,
             bullet_speed=25.,reported_bullet_speed=25.,aim_origin='muzzle',gimbal_profile='feedforward',fire_policy='advice',fire_rate_hz=5.,
             algorithm_profile=profile,projectile_visible=visible,command_script=a.command_script))
           for scene in pending:
            for attempt in range(2):
             args=argparse.Namespace(mode='shoot',duration=None,seed=seed,label='robustness-v3-'+a.phase)
             folder=experiments.run_one(manage,scene,args);summary=load(folder/'summary.json',{})
             row=dict(key=[seed,physics,visible,profile,scene],run_id=folder.name,repeat=repeat,verdict=summary.get('verdict'),metrics=summary.get('metrics'),
                failed_rules=[r for r in summary.get('rules',[]) if not r['passed']])
             if summary.get('verdict')=='invalid':batch['invalid_attempts'].append(row)
             else:batch['runs'].append(row)
             write(batch_path,batch)
             print(json.dumps(dict(progress=len(batch['runs']),expected=expected,**row),ensure_ascii=False),flush=True)
             if summary.get('verdict')!='invalid':break
             if attempt:raise RuntimeError('Repeated invalid evidence; inspect last run')
       batch['complete']=len(batch['runs'])==expected;write(batch_path,batch)
      finally:manage.stop()

if __name__=='__main__':main()
