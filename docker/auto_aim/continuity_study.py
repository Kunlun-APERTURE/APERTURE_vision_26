#!/usr/bin/env python3
"""Alternate algorithm profiles under frozen conditions and retain every run."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import uuid

import experiments
import manage
from report import load,write

SCENES=('move_ref','line','ego_sweep','ego_sweep_reverse','spin')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--phase',choices=['tracking','fire'],required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--resume',type=Path)
    p.add_argument('--repeat',type=int,default=3)
    p.add_argument('--selection',type=Path)
    p.add_argument('--diagnostic',action='store_true',help='Retain a failed selection as a diagnostic comparison; cannot pass acceptance')
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    profiles=('legacy','tracking_v2') if args.phase=='tracking' else ('tracking_v2','tracking_fire_v2')
    selection=load(args.selection,{}) if args.selection else {}
    if args.phase=='tracking' and not selection.get('selection_gate_passed') and not args.diagnostic:
        raise RuntimeError('Offline selection failed or is missing; use --diagnostic to collect evidence without acceptance')
    if args.phase=='fire':
        acceptance=load(args.output/'tracking-acceptance.json',{})
        if not acceptance.get('passed'):raise RuntimeError('Tracking acceptance must pass before formal fire comparison')
    resumed=load(args.resume,{}) if args.resume else {}
    records=[r for r in resumed.get('runs',[]) if r.get('verdict')!='invalid']
    attempts=resumed.get('invalid_attempts',[])+[r for r in resumed.get('runs',[]) if r.get('verdict')=='invalid']
    batch=args.output/(args.phase+'_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'_'+uuid.uuid4().hex[:6]+'.json')
    binary=manage.ROOT/'sp_vision_25/build/simulator/sim_auto_aim'
    signature=experiments.digest(binary)
    image=manage.inspect(manage.IMAGE)['Id']
    configs=experiments.digest(manage.ROOT/'sp_vision_25/configs/simulator.yaml')
    if resumed and (resumed.get('phase')!=args.phase or resumed.get('profiles')!=list(profiles)
        or resumed.get('algorithm_binary_sha256')!=signature or resumed.get('simulator_image_id')!=image
        or resumed.get('algorithm_yaml_sha256')!=configs):
        raise RuntimeError('Resume batch does not match frozen phase, binary, image or config')
    def persist(complete=False):
        write(batch,{'phase':args.phase,'profiles':profiles,'complete':complete,'resumed_from':str(args.resume) if args.resume else None,
            'diagnostic_only':args.diagnostic,'offline_selection':str(args.selection) if args.selection else None,
            'offline_selection_passed':selection.get('selection_gate_passed',False),
            'offline_selection_sha256':experiments.digest(args.selection) if args.selection else None,
            'algorithm_binary_sha256':signature,'simulator_image_id':image,'algorithm_yaml_sha256':configs,
            'runs':records,'invalid_attempts':attempts})
    persist();print('Study batch:',batch,flush=True)
    run_args=argparse.Namespace(mode='shoot',duration=None,label='continuity-'+args.phase,
        aim_origin='muzzle',gimbal_profile='feedforward',fire_policy='advice',fire_rate_hz=5)
    try:
        for repeat in range(1,args.repeat+1):
            for physics in ('no-drag','drag'):
                for scene in SCENES:
                    order=profiles if (repeat+(physics=='drag'))%2 else profiles[::-1]
                    for profile in order:
                        if any(r['repeat']==repeat and r['scene']==scene and r['shooting']['physics']==physics
                            and r['algorithm_profile']==profile and r['verdict']!='invalid' for r in records):continue
                        if experiments.digest(binary)!=signature or manage.inspect(manage.IMAGE)['Id']!=image or experiments.digest(manage.ROOT/'sp_vision_25/configs/simulator.yaml')!=configs:
                            raise RuntimeError('Frozen algorithm, config or image changed during study')
                        manage.stop()
                        manage.start(argparse.Namespace(scene='static',control='off',view='off',replace=False,fire='auto',
                            fire_policy='advice',fire_rate_hz=5,physics=physics,bullet_speed=25.,reported_bullet_speed=25.,
                            aim_origin='muzzle',gimbal_profile='feedforward',algorithm_profile=profile))
                        run_args.algorithm_profile=profile
                        existing=set((manage.OPS/'experiments').iterdir())
                        try:
                            folder=experiments.run_one(manage,scene,run_args)
                        except BaseException as error:
                            for failed in set((manage.OPS/'experiments').iterdir())-existing:
                                if not failed.is_dir() or not (failed/'manifest.json').exists():continue
                                manifest=load(failed/'manifest.json',{})
                                attempts.append({'run_id':failed.name,'scene':scene,'repeat':repeat,
                                    'algorithm_profile':profile,'shooting':manifest.get('shooting'),
                                    'verdict':'invalid','reason':type(error).__name__+': '+str(error)})
                            persist()
                            raise
                        summary=load(folder/'summary.json',{})
                        entry={'run_id':folder.name,'scene':scene,'repeat':repeat,'algorithm_profile':profile,
                            'shooting':summary.get('shooting'),'verdict':summary.get('verdict','invalid'),'metrics':summary.get('metrics')}
                        (attempts if entry['verdict']=='invalid' else records).append(entry)
                        persist()
                        if summary.get('verdict')=='invalid':raise RuntimeError('Invalid evidence/environment: '+folder.name)
        persist(True)
    finally:
        manage.stop()
    print('Completed:',batch,flush=True)


if __name__=='__main__':
    manage.RUNTIME.mkdir(exist_ok=True,parents=True)
    with (manage.RUNTIME/'operation.lock').open('a') as task_lock:
        try:manage.fcntl.flock(task_lock,manage.fcntl.LOCK_EX|manage.fcntl.LOCK_NB)
        except BlockingIOError:raise SystemExit('Another simulator operation is running')
        main()
