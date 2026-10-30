#!/usr/bin/env python3
"""Offline v4 evidence acquisition; never starts or controls the simulator."""
import argparse,hashlib,json,shutil,subprocess,time
from pathlib import Path
import yaml
from report import load,write

OPS=Path(__file__).resolve().parent
ROOT=OPS.parent.parent
HISTORY=OPS/'experiments/optimization-v4-20261021'
OUTPUT=OPS/'experiments/diagnosis-v4-20261022'
ALGORITHM=ROOT/'sp_vision_25'

def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def inventory():
    rows=[]
    for stage in ('m','mt','mtr'):
        batch=load(HISTORY/('stage-'+stage)/'batch.json')
        for row in batch['runs']:
            folder=HISTORY.parent/row['run_id'];manifest=load(folder/'manifest.json')
            rows.append(dict(run_id=folder.name,stage=stage,role='formal',scene=manifest['scene'],profile=manifest['algorithm_profile'],
                seed=manifest['random_seed'],physics=manifest['shooting']['physics'],telemetry_sha256=digest(folder/'telemetry.jsonl'),
                evidence_manifest_sha256=digest(folder/'evidence-sha256.json'),configuration_sha256=manifest['configuration_sha256']))
        for row in load(HISTORY/('ablation-'+stage)/'results.json')['runs']:
            folder=HISTORY.parent/row['metrics']['run_id'];manifest=load(folder/'manifest.json')
            rows.append(dict(run_id=folder.name,stage=stage,role='ablation',variant=row['variant'],scene=manifest['scene'],profile=manifest['algorithm_profile'],
                seed=manifest['random_seed'],physics=manifest['shooting']['physics'],telemetry_sha256=digest(folder/'telemetry.jsonl'),
                evidence_manifest_sha256=digest(folder/'evidence-sha256.json'),configuration_sha256=manifest['configuration_sha256']))
    write(OUTPUT/'inventory.json',dict(runs=rows,formal=sum(r['role']=='formal' for r in rows),ablation=sum(r['role']=='ablation' for r in rows),
        history=str(HISTORY),policy='Read-only source evidence; new analysis writes only into this diagnosis directory.'))
    return rows

def source(folder):
    return ''.join(line for line in (folder/'telemetry.jsonl').open() if json.loads(line)['kind']=='observation')

def replay(row,variant='diagnosis',original=False,enabled=True,delay='profile',suppress=False):
    folder=HISTORY.parent/row['run_id'];dest=OUTPUT/'replay'/variant/folder.name
    signature=dict(telemetry_sha256=row['telemetry_sha256'],binary_sha256=digest(OUTPUT/'before/sp_vision_25/build/simulator/sim_auto_aim' if original else ALGORITHM/'build/simulator/sim_auto_aim'),
        enabled=enabled,delay=delay,suppress_hint=suppress,config_sha256=digest(folder/'configuration/algorithm.yaml'),adapter_sha256=digest(Path(__file__)))
    old=load(dest/'manifest.json',{})
    if old.get('signature')==signature and (dest/'results.jsonl').exists():return dest
    config=ALGORITHM/'build/sim/diagnosis-config.yaml';runtime='build/sim/diagnosis-runtime.yaml'
    base=yaml.safe_load((folder/'configuration/algorithm.yaml').read_text())
    base['diagnosis']={'enabled':enabled}
    config.write_text(yaml.safe_dump(base,sort_keys=False))
    exe='build/simulator/sim_auto_aim'
    if original:
        exe='build/sim/diagnosis-original';shutil.copy2(OUTPUT/'before/sp_vision_25/build/simulator/sim_auto_aim',ALGORITHM/exe)
    command=['docker','exec','-i','--user','1000:1000','--workdir','/workspace/sp_vision_25','sp_vision_sunshine',exe,
        '--replay=/dev/stdin','--replay-output=/dev/stdout','--runtime='+runtime,'--config=build/sim/diagnosis-config.yaml','--algorithm-profile='+row['profile']]
    if not original:command+=['--diagnosis='+str(enabled).lower(),'--replay-delay-mode='+delay,'--replay-no-yaw-hint='+str(suppress).lower()]
    result=subprocess.run(command,input=source(folder),capture_output=True,text=True,check=True)
    rows=[json.loads(l) for l in result.stdout.splitlines() if l.startswith('{')]
    dest.mkdir(parents=True,exist_ok=True);(dest/'results.jsonl').write_text(''.join(json.dumps(r,allow_nan=False,separators=(',',':'))+'\n' for r in rows))
    write(dest/'manifest.json',dict(signature=signature,frames=len(rows),command=command,results_sha256=digest(dest/'results.jsonl'),
        role='Fixed-observation counterfactual, not a new closed-loop hit experiment' if suppress or delay!='profile' else 'Diagnostic fixed-observation replay'))
    return dest

def without_diagnosis(value):
    if isinstance(value,dict):return {k:without_diagnosis(v) for k,v in value.items() if k!='diagnosis' and not k.startswith('diagnosis_')}
    if isinstance(value,list):return [without_diagnosis(v) for v in value]
    return value

def difference(a,b,path=''):
    if isinstance(a,dict) and isinstance(b,dict):
        if a.keys()!=b.keys():return path+':keys'
        for k in a:
            d=difference(a[k],b[k],path+'/'+k)
            if d:return d
        return None
    if isinstance(a,list) and isinstance(b,list):
        if len(a)!=len(b):return path+':length'
        for i,(x,y) in enumerate(zip(a,b)):
            d=difference(x,y,path+'/'+str(i))
            if d:return d
        return None
    if isinstance(a,(float,int)) and not isinstance(a,bool) and isinstance(b,(float,int)) and not isinstance(b,bool):
        return None if abs(a-b)<=1e-9 else path+':number'
    return None if a==b else path+':value'

def verify(row,diagnostic):
    original=replay(row,'original',original=True,enabled=False)
    disabled=replay(row,'disabled',enabled=False)
    paths=[original/'results.jsonl',disabled/'results.jsonl',diagnostic/'results.jsonl']
    streams=[[without_diagnosis(json.loads(l)) for l in p.open()] for p in paths]
    diffs=[]
    if len({len(s) for s in streams})!=1:diffs.append('frame_count')
    for i,(a,b,c) in enumerate(zip(*streams)):
        for name,x in [('disabled',b),('enabled',c)]:
            d=difference(a,x)
            if d:diffs.append(dict(frame=i,mode=name,field=d))
    return dict(run_id=row['run_id'],profile=row['profile'],frames=len(streams[0]),passed=not diffs,differences=diffs[:20])

def run():
    OUTPUT.mkdir(parents=True,exist_ok=True);rows=inventory()
    selected=[r for r in rows if r['role']=='formal' and (
        r['stage']=='mt' and r['scene'] in ('hf_static_8','hf_uniform_3','hf_reverse_3','hf_turn90_3') or
        r['stage']=='mtr' and 'spin' in r['scene'] or r['stage']=='m' and r['profile']=='tracking_fire_v4m')]
    selected += [r for r in rows if r['role']=='ablation' and r['stage']=='m' and r['variant']=='estimator']
    checks=[];began=time.monotonic()
    for i,row in enumerate(selected):
        dest=replay(row)
        check=verify(row,dest);checks.append(check)
        write(OUTPUT/'replay-validation.json',dict(complete=False,passed=all(r['passed'] for r in checks),runs=checks))
        if not check['passed']:raise RuntimeError('Diagnostic output changed decisions: '+row['run_id'])
        if row['stage']=='mt' and row['scene'] in ('hf_uniform_3','hf_reverse_3','hf_turn90_3'):
            for delay in ('original','measured'):replay(row,'delay-'+delay,delay=delay)
        if row['stage']=='mtr' and 'spin' in row['scene']:replay(row,'no-hint',suppress=True)
        print(json.dumps(dict(replayed=i+1,total=len(selected),run_id=row['run_id'],validated_frames=check['frames'],elapsed_s=time.monotonic()-began)),flush=True)
    write(OUTPUT/'replay-validation.json',dict(complete=True,passed=all(r['passed'] for r in checks),runs=checks,total_frames=sum(r['frames'] for r in checks),
        tolerance=1e-9,excluded_fields='Only newly added diagnosis namespaces',truth_inputs=False))
    write(OUTPUT/'acquisition.json',dict(complete=True,runs=len(selected),elapsed_s=time.monotonic()-began),)

if __name__=='__main__':run()
