#!/usr/bin/env python3
"""Replay original JPEGs through the real C++ detector without simulator output."""
import argparse, collections, hashlib, json, os, subprocess
from pathlib import Path
from continuity_replay import prepare, metrics, write, ROOT, OPS


def replay(folder, output, profile='tracking_v2', diagnostics=True, config=None):
    output.mkdir(parents=True,exist_ok=True)
    shared=ROOT/'docker/sunshine/home/.local/share/rm26-sim/replay-input'/folder.name
    shared.mkdir(parents=True,exist_ok=True)
    for name in ('input.mjpg','frames.jsonl'):
        target=shared/name
        if not target.exists(): os.link(folder/name,target)
    source,context=prepare(folder)
    command=['docker','exec','-i','--user','1000:1000','--workdir','/workspace/sp_vision_25','sp_vision_sunshine',
        'build/simulator/sim_auto_aim','--replay=/dev/stdin','--replay-output=/dev/stdout',
        '--runtime=build/sim/raw-replay.yaml','--algorithm-profile='+profile,
        '--raw-replay=/home/openvino/.local/share/rm26-sim/replay-input/'+folder.name,
        '--detector-diagnostics='+str(diagnostics).lower()]
    if config:command.append('--config='+str(config))
    result=subprocess.run(command,input=source,capture_output=True,text=True,check=True)
    rows=[json.loads(s) for s in result.stdout.splitlines() if s.startswith('{')]
    (output/'results.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    old={json.loads(s)['data']['frame']['stamp_ns']:json.loads(s)['data']['result']['observations']
         for s in source.splitlines()}
    mismatches=[];reasons=collections.Counter();refine=collections.Counter()
    for r in rows:
        if not r['raw_replayed']:continue
        actual=r['observations']; expected=[{k:d[k] for k in ('corners','confidence','name','color','type')} for d in old[r['stamp_ns']]]
        if actual!=expected:mismatches.append({'seq':r['seq'],'old':expected,'new':actual})
        diagnostic=r.get('detector') or {}
        for stage in (diagnostic,diagnostic.get('tile_pass') or {}):
            for c in stage.get('candidates',[]):
                reasons[c['reason']]+=1
                if 'refinement_reason' in c:refine[c['refinement_reason']]+=1
    write(output/'summary.json',dict(run_id=folder.name,profile=profile,frames=len(rows),raw_frames=sum(r['raw_replayed'] for r in rows),
        fallback_warmup_frames=sum(not r['raw_replayed'] for r in rows),detection_mismatch_frames=len(mismatches),
        reasons=dict(reasons),refinement=dict(refine),metrics=metrics(rows,context),
        config_path=str(config or 'configs/simulator.yaml'),
        config_sha256=hashlib.sha256((ROOT/'sp_vision_25'/(config or 'configs/simulator.yaml')).read_bytes()).hexdigest(),
        binary_sha256=hashlib.sha256((ROOT/'sp_vision_25/build/simulator/sim_auto_aim').read_bytes()).hexdigest()))
    write(output/'detection-differences.json',mismatches)
    return json.loads((output/'summary.json').read_text())


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run');p.add_argument('--event-controls',action='store_true')
    p.add_argument('--output',type=Path,required=True);p.add_argument('--profile',default='tracking_v2');p.add_argument('--no-diagnostics',action='store_true');p.add_argument('--config')
    a=p.parse_args()
    if a.event_controls:
        pairs=json.loads((OPS/'experiments/reversal-diagnosis-20261020/event-pairs.json').read_text())
        runs=sorted({row[key].rsplit('_e',1)[0] for row in pairs for key in ('slow_event_id','normal_event_id')})
    elif a.run:runs=[a.run]
    else:p.error('Supply --run or --event-controls')
    for run in runs:
        summary=replay(OPS/'experiments'/run,a.output/run,a.profile,not a.no_diagnostics,a.config)
        print(json.dumps({k:summary[k] for k in ('run_id','frames','raw_frames','detection_mismatch_frames','reasons')},ensure_ascii=False),flush=True)

if __name__=='__main__':main()
