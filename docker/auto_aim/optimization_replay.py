"""Observation-only development selection; truth stays in the evaluator."""
import argparse,hashlib,json,math,subprocess
from pathlib import Path
import yaml
from continuity_replay import prepare,q,ROOT
from report import load,write

OPS=Path(__file__).resolve().parent
OUTPUT=OPS/'experiments/optimization-v4-20261021'

def replay(folder, name, profile, options=None, old=False):
    dest=OUTPUT/'development'/name/folder.name
    dest.mkdir(parents=True,exist_ok=True)
    config=ROOT/'sp_vision_25/build/sim'/('optimization-'+name+'.yaml')
    base=yaml.safe_load((OUTPUT/'baseline/sp_vision_25/configs/simulator.yaml').read_text())
    base['optimization_v4']=options or {}
    config.write_text(yaml.safe_dump(base))
    source,context=prepare(folder)
    executable=('/workspace/rm26/docker/auto_aim/experiments/optimization-v4-20261021/baseline/sp_vision_25/build/simulator/sim_auto_aim' if old else 'build/simulator/sim_auto_aim')
    # Sunshine mounts the whole workspace at /workspace/rm26 as well as the algorithm.
    if old:
        executable='build/sim/optimization-original'
        import shutil
        shutil.copy2(OUTPUT/'baseline/sp_vision_25/build/simulator/sim_auto_aim',ROOT/'sp_vision_25'/executable)
    command=['docker','exec','-i','--user','1000:1000','--workdir','/workspace/sp_vision_25','sp_vision_sunshine',executable,
        '--replay=/dev/stdin','--replay-output=/dev/stdout','--runtime=build/sim/optimization-replay-runtime.yaml',
        '--config='+str(config.relative_to(ROOT/'sp_vision_25')),'--algorithm-profile='+profile]
    result=subprocess.run(command,input=source,capture_output=True,text=True,check=True)
    rows=[json.loads(l) for l in result.stdout.splitlines() if l.startswith('{')]
    (dest/'results.jsonl').write_text(''.join(json.dumps(r,allow_nan=False)+'\n' for r in rows))
    plan=load(folder/'configuration/scenario-plan.json',{})
    events=[]
    for change in plan.get('motion_change_times_s',[]):
        origin=None;delay=None
        reference=[v for stamp,v in context['velocity'].rows if change-.2<=(stamp-context['start'])*1e-9<change]
        departure=change
        if reference:
            vx=sum(v[0] for v in reference)/len(reference);vy=sum(v[1] for v in reference)/len(reference)
            candidates=[(stamp,v) for stamp,v in context['velocity'].rows if change<=(stamp-context['start'])*1e-9<=change+2]
            for i,(stamp,v) in enumerate(candidates):
                following=candidates[i:i+3]
                if len(following)==3 and all(math.hypot(w[0]-vx,w[1]-vy)>=.3 for _,w in following):
                    departure=(stamp-context['start'])*1e-9;break
        for r in rows:
            t=(r['stamp_ns']-context['start'])*1e-9
            if t<departure:continue
            if t>change+2:break
            state=r.get('filter_state');truth=context['velocity'].at(r['stamp_ns'])
            good=state and truth and r['tracker']=='tracking' and math.hypot(state[1]-truth[0],state[3]-truth[1])<=.3
            if good:
                if origin is None:origin=t
                if t-origin>=.1:delay=origin-change;break
            else:origin=None
        events.append(dict(change_s=change,recovery_s=delay,censored=delay is None))
    measured=[r for r in rows if context['start']<=r['stamp_ns']<=context['end']]
    suspects=[r for r in measured if r.get('prediction_suspect')]
    false=sum(bool(r.get('prediction_suspect')) for r in measured) if 'static' in context['scene'] else 0
    sigmas=[r['prediction_yaw_sigma_rad'] for r in measured if r.get('valid') and r.get('prediction_yaw_sigma_rad',-1)>=0]
    metrics=dict(scene=context['scene'],run_id=folder.name,profile=profile,options=options,frames=len(measured),
        events=events,recovery_p90_s=q([e['recovery_s'] if not e['censored'] else 2 for e in events],.9),
        censored=sum(e['censored'] for e in events),static_false_frames=false,suspect_frames=len(suspects),
        yaw_sigma_p50_rad=q(sigmas),circular_frames=sum(r.get('circular_prediction',False) for r in measured),
        source_sha256=hashlib.sha256((folder/'telemetry.jsonl').read_bytes()).hexdigest())
    write(dest/'metrics.json',metrics)
    return rows,metrics

def run():
    batch=load(OPS/'experiments/highspeed-focus-20261021/highspeed-batch.json')
    runs=[r for r in batch['runs'] if r['key'][0]==2026102106 and r['key'][1]=='no-drag']
    result=[];regressions=[]
    for r in runs:
        folder=OPS/'experiments'/r['run_id']
        old,_=replay(folder,'original','tracking_fire_v3',old=True)
        new,m=replay(folder,'v3-regression','tracking_fire_v3')
        # Added profile metadata is excluded. Every numerical and existing diagnostic field must match.
        clean=lambda row:{k:v for k,v in row.items() if k!='algorithm_profile'}
        differences=sum(clean(a)!=clean(b) for a,b in zip(old,new))
        regressions.append(dict(scene=r['scene'],old_frames=len(old),new_frames=len(new),different_frames=differences,
            passed=len(old)==len(new) and differences==0))
        result.append(m)
        for sigma in (2.,3.):
            for frames in (2,3):
                _,m=replay(folder,f'm-s{sigma:g}-n{frames}','tracking_fire_v4m',dict(maneuver_sigma=sigma,maneuver_frames=frames))
                result.append(m)
        print(json.dumps(dict(scene=r['scene'],regression=regressions[-1])),flush=True)
    rankings=[]
    for sigma in (2.,3.):
        for frames in (2,3):
            selected=[r for r in result if r['profile']=='tracking_fire_v4m' and r['options']==dict(maneuver_sigma=sigma,maneuver_frames=frames)]
            events=[e for r in selected for e in r['events']]
            rankings.append(dict(sigma=sigma,frames=frames,censored=sum(e['censored'] for e in events),
                recovery_p90_s=q([e['recovery_s'] if not e['censored'] else 2 for e in events],.9),
                static_false_frames=sum(r['static_false_frames'] for r in selected)))
    rankings.sort(key=lambda r:(r['censored'],r['recovery_p90_s'] if r['recovery_p90_s'] is not None else 2,r['static_false_frames'],-r['sigma'],-r['frames']))
    output=dict(regressions=regressions,rankings=rankings,selected=rankings[0],runs=result,
        truth_isolation='Only recorded detections, calibration and capture/receipt timestamps enter the algorithm. Truth evaluates output here.',
        selection='censored count; 2D velocity recovery P90; static false frames; conservative tie break')
    write(OUTPUT/'development/selection.json',output)
    if not all(r['passed'] for r in regressions):raise RuntimeError('v3 observation replay changed')
    return output

if __name__=='__main__':run()
