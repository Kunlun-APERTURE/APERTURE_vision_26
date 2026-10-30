"""At most 16 diagnostic recordings, using frozen decisions and full session capture."""
import argparse,fcntl,json,time
from pathlib import Path
import yaml
import experiments,manage
from diagnosis_v4 import OUTPUT,HISTORY,digest,replay,verify
from report import load,write

def run():
    path=OUTPUT/'supplement/batch.json';config=manage.ROOT/'sp_vision_25/configs/simulator.yaml';original=config.read_bytes()
    path.parent.mkdir(parents=True,exist_ok=True)
    conditions=[]
    for seed in (2026102210,2026102211):
        for physics in ('no-drag','drag'):
            for profile in ('tracking_fire_v3','tracking_fire_v4mt'):conditions.append((seed,physics,profile,'hf_static_8'))
        for profile in ('tracking_fire_v3','tracking_fire_v4mt'):conditions.append((seed,'no-drag',profile,'hf_turn90_3'))
        for scene in ('hf_spin_pos','hf_spin_neg'):conditions.append((seed,'no-drag','tracking_fire_v4mtr',scene))
    batch=load(path,{}) or dict(conditions=conditions,runs=[],invalid_attempts=[],complete=False,
        reason='Historical streams begin after tracker initialization. Recover exact state history, original pose candidates and phase decisions.',
        algorithm_binary_sha256=digest(manage.ROOT/'sp_vision_25/build/simulator/sim_auto_aim'),maximum_runs=16)
    write(path,batch)
    with (manage.RUNTIME/'operation.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        try:
            for i,key in enumerate(conditions):
                if any(tuple(r['key'])==key for r in batch['runs']):continue
                seed,physics,profile,scene=key
                base=yaml.safe_load(original);base['diagnosis']={'enabled':True}
                base['optimization_v4'].update(mt_use_m=False,r_use_m=False,r_use_t=False)
                config.write_text(yaml.safe_dump(base,sort_keys=False))
                manage.stop();manage.start(argparse.Namespace(terrain='flat_range',scene='hs_static',control='off',view='off',replace=False,
                    fire='auto',physics=physics,bullet_speed=25.,reported_bullet_speed=25.,aim_origin='muzzle',gimbal_profile='feedforward',
                    fire_policy='advice',fire_rate_hz=5.,algorithm_profile=profile,projectile_visible='on',command_script=''))
                for attempt in range(2):
                    folder=experiments.run_one(manage,scene,argparse.Namespace(mode='shoot',duration=None,seed=seed,label='diagnosis-v4-only',diagnosis_reset=True))
                    summary=load(folder/'summary.json',{});row=dict(key=key,run_id=folder.name,verdict=summary.get('verdict'),wall_s=summary.get('metrics',{}).get('total_wall_s'))
                    if row['verdict']=='invalid':
                        batch['invalid_attempts'].append(row);write(path,batch)
                        if attempt:raise RuntimeError('Repeated invalid diagnosis capture')
                        continue
                    manifest=load(folder/'manifest.json')
                    row['inventory']=dict(run_id=folder.name,stage='mtr' if 'spin' in scene else 'mt',role='supplement',scene=scene,profile=profile,
                        seed=seed,physics=physics,telemetry_sha256=digest(folder/'telemetry.jsonl'),evidence_manifest_sha256=digest(folder/'evidence-sha256.json'),
                        configuration_sha256=manifest['configuration_sha256'])
                    batch['runs'].append(row);write(path,batch)
                    break
                print(json.dumps(dict(supplement=len(batch['runs']),expected=16,key=key,run_id=folder.name)),flush=True)
            batch['complete']=True;write(path,batch)
        finally:
            manage.stop();config.write_bytes(original)
            saved=manage.profile();saved.update(terrain='arena',scene='static',control='off',fire='off',algorithm_profile='legacy');manage.save_profile(saved)

if __name__=='__main__':run()
