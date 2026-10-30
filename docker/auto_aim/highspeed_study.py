#!/usr/bin/env python3
"""Resumable, frozen v3 flat-range short batch (48 formal runs)."""
import argparse,contextlib,fcntl,hashlib,json,time
from pathlib import Path
import experiments,manage
from highspeed_scenarios import HIGHSPEED
from highspeed_report import batch_report
from report import load,write


def signature():
    paths=[manage.ROOT/'sp_vision_25/build/simulator/sim_auto_aim',manage.ROOT/'sp_vision_25/configs/simulator.yaml',manage.ROOT/'sp_vision_25/tests/auto_aim_test.cpp']
    paths+=sorted(p for p in (manage.ROOT/'sp_vision_25/assets').iterdir() if p.suffix in ('.xml','.bin','.onnx'))
    paths+=sorted(p for p in manage.OPS.iterdir() if p.suffix in ('.py','.yml'))
    return dict(files={str(p.relative_to(manage.ROOT)):experiments.digest(p) for p in paths},simulator_image=manage.inspect(manage.IMAGE)['Id'],evaluator_image=manage.inspect('daedalus:rm26-evaluator')['Id'])


def run(manager,output,locked=False,preflight=False,focus=False):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    definition=dict(version=1,terrain='flat_range',profile='tracking_fire_v3',
        scenes=['hs_static','hs_cross_3','hs_spin_pos'] if preflight else [s for s in HIGHSPEED if s.startswith('hs_')],
        seeds=[2026102199] if preflight else [2026102104,2026102105],physics=['no-drag'] if preflight else ['no-drag','drag'])
    if focus:
        from focus_scenarios import FOCUS
        definition.update(version=2,study='near-range-focus',
            scenes=['hf_uniform_3','hf_reverse_3','hf_turn90_3','hf_spin_pos'] if preflight else list(FOCUS),
            seeds=[2026102198] if preflight else [2026102106,2026102107])
    frozen=signature();path=output/'highspeed-batch.json';batch=load(path)
    if batch and (batch['definition']!=definition or batch['signature']!=frozen):raise RuntimeError('Frozen batch differs; use a new output directory')
    batch=batch or dict(definition=definition,signature=frozen,runs=[],invalid_attempts=[],complete=False)
    expected=len(definition['scenes'])*len(definition['seeds'])*len(definition['physics'])
    started=time.monotonic();active_s=batch.get('timing',{}).get('active_wall_s',0.)
    write(path,batch)
    with contextlib.ExitStack() as stack:
        if not locked:
            lock=stack.enter_context((manager.RUNTIME/'operation.lock').open('a'));fcntl.flock(lock,fcntl.LOCK_EX)
        try:
            for i,seed in enumerate(definition['seeds']):
                scenes=definition['scenes'] if i==0 else list(reversed(definition['scenes']))
                for physics in definition['physics']:
                    pending=[s for s in scenes if not any(r['key']==[seed,physics,s] for r in batch['runs'])]
                    if not pending:continue
                    manager.stop();manager.start(argparse.Namespace(terrain='flat_range',scene='hs_static',control='off',view='off',replace=False,
                        fire='auto',physics=physics,bullet_speed=25.,reported_bullet_speed=25.,aim_origin='muzzle',gimbal_profile='feedforward',fire_policy='advice',fire_rate_hz=5.,
                        algorithm_profile='tracking_fire_v3',projectile_visible='on',command_script=''))
                    for scene in pending:
                        if signature()!=frozen:raise RuntimeError('Frozen inputs changed during batch')
                        for attempt in range(2):
                            folder=experiments.run_one(manager,scene,argparse.Namespace(mode='shoot',duration=None,seed=seed,label=('highspeed-focus' if focus else 'highspeed-v1')+('-preflight' if preflight else '')))
                            summary=load(folder/'summary.json',{})
                            row=dict(key=[seed,physics,scene],run_id=folder.name,scene=scene,verdict=summary.get('verdict'),wall_s=summary.get('metrics',{}).get('total_wall_s'))
                            (batch['invalid_attempts'] if row['verdict']=='invalid' else batch['runs']).append(row)
                            batch['timing']=dict(active_wall_s=active_s+time.monotonic()-started,measurement_s=sum(experiments.measurement_duration(r['scene'],argparse.Namespace(duration=None)) for r in batch['runs']),budget_note='Target 20–30 min excludes builds; each full run <=40 s')
                            write(path,batch);batch_report(output);experiments.publish_desktop(manager,output)
                            print(json.dumps(dict(progress=len(batch['runs']),expected=expected,**row),ensure_ascii=False),flush=True)
                            if row['verdict']!='invalid':break
                            if attempt:raise RuntimeError('Repeated invalid evidence; inspect last run')
            batch['complete']=len(batch['runs'])==expected;batch['final_signature']=signature();write(path,batch)
        finally:
            manager.stop()
            # Keep preview defaults; do not promote the experimental algorithm.
            saved=manager.profile();saved.update(scene='static',terrain='arena',fire='off',control='off',algorithm_profile='legacy');manager.save_profile(saved)
            batch['timing']=dict(batch.get('timing',{}),active_wall_s=active_s+time.monotonic()-started)
            write(path,batch);batch_report(output);experiments.publish_desktop(manager,output)
    return output


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--preflight',action='store_true');p.add_argument('--focus',action='store_true');a=p.parse_args()
    run(manage,a.output,preflight=a.preflight,focus=a.focus)
