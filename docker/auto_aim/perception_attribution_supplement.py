"""Replay the omitted longest first-hit failure and its paired baseline only."""
import argparse
import json
from pathlib import Path
import time

import manage
from decision_benchmark import sha
from perception_attribution import replay,VARIANTS
from report import load,write


def run(root):
    parent=load(root/'contract.json');out=root/'longest-A';out.mkdir(exist_ok=True)
    path=out/'contract.json'
    if path.exists():c=load(path)
    else:
        prior=manage.OPS/'experiments/perception-optimization-20261023';s=load(prior/'state.json')
        candidates=[e for e in s['runs'] if e['variant']=='A35' and e['scene']=='hf_uniform_3']
        scored=[(load(manage.OPS/'experiments'/e['run_id']/'summary.json')['metrics']['first_hit_s'],e) for e in candidates]
        worst=max((p for p in scored if p[0] is not None),key=lambda p:p[0])
        paired=next(e for e in s['runs'] if e['variant']=='baseline' and (e['scene'],e['sensor_seed'])==(worst[1]['scene'],worst[1]['sensor_seed']))
        sources=[]
        for e in (paired,worst[1]):
            f=manage.OPS/'experiments'/e['run_id']
            sources.append(dict(e,duration_s=load(f/'manifest.json')['duration_s'],source_hashes={n:sha(f/n) for n in
                ('manifest.json','run-state.json','summary.json','telemetry.jsonl','session-evidence/events.jsonl','session-evidence/input.mjpg')}))
        c=dict(parent,sources=sources,maximum_replays=6,created=time.time(),
            purpose='Mechanism analysis of longest actually recorded A35 uniform first-hit delay, plus its same-seed baseline. '
                    'Selected by maximum delay, not as independent performance sample. No new live experiment.',
            selected_delay_s=worst[0])
        write(path,c);write(out/'state.json',dict(state='frozen',contract_sha256=sha(path),replays=[],new_closed_loop_trials=0))
    state=load(out/'state.json')
    if state['contract_sha256']!=sha(path):raise ValueError('Supplement definition changed')
    for e in c['sources']:
        for v in VARIANTS:
            if any(x['source_run']==e['run_id'] and x['variant']==v for x in state['replays']):continue
            r=replay(out,c,e,v);state['replays'].append(r);write(out/'state.json',state)
            print(json.dumps(dict(supplement_replays=len(state['replays']),variant=v)),flush=True)
    state['state']='completed';write(out/'state.json',state)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();run(a.output.resolve())
