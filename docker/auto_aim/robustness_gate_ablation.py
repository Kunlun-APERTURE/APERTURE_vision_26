#!/usr/bin/env python3
"""Compare the original and independent-stability C++ gates on exact live inputs."""
import argparse,json,re,subprocess
from pathlib import Path
from continuity_replay import ROOT,OPS,write
from reversal_diagnose import gate_inputs


def main():
    p=argparse.ArgumentParser();p.add_argument('--study',type=Path,required=True);a=p.parse_args();a.study=a.study.resolve()
    batch=json.loads((a.study/'development-e-selected/development-batch.json').read_text());runs=[r for r in batch['runs'] if r['key'][3]=='tracking_fire_v3']
    base=(ROOT/'sp_vision_25/configs/simulator.yaml').read_text().replace('algorithm_profile: legacy','algorithm_profile: tracking_fire_v3').replace('auto_fire: false','auto_fire: true')
    result=[]
    for aligned_required in (True,False):
        config=ROOT/'sp_vision_25/build/sim/gate-independent.yaml';config.write_text(base+f'  fire_stability_requires_alignment: {str(aligned_required).lower()}\n')
        rows=[]
        for run in runs:
            folder=OPS/'experiments'/run['run_id'];observations=[p['data'] for p in map(json.loads,(folder/'telemetry.jsonl').read_text().splitlines()) if p['kind']=='observation']
            state=json.loads((folder/'run-state.json').read_text());start=state['measurement_start_stamp_ns'];end=state['measurement_end_stamp_ns']
            inputs=gate_inputs(observations,exact=True)
            for row,o in zip(inputs,observations):
                fire=o['result'].get('fire_check') or {}
                row['target_half_width_rad']=fire.get('target_half_width_rad',-1.)
                row['other_visible_plates']=fire.get('other_visible_plates',[])
            command=['docker','exec','-i','--user','1000','sp_vision_sunshine','/workspace/sp_vision_25/build/diagnostic_fire_replay/diagnostic_fire_replay','/workspace/sp_vision_25/build/sim/gate-independent.yaml']
            process=subprocess.run(command,input=''.join(json.dumps(r)+'\n' for r in inputs),capture_output=True,text=True,check=True)
            output=list(map(json.loads,process.stdout.splitlines()));last=None;requests=0;eligible=0;compared=0;differences=0;sync=False
            for r,o in zip(output,observations):
                expected=o['result'].get('fire_check') or {};actual=r.get('fire_check') or {}
                if expected.get('stable_frames')==actual.get('stable_frames')==0:sync=True
                if not start<=r['stamp_ns']<=end:continue
                if r['eligible']:
                    eligible+=1
                    if last is None or r['stamp_ns']-last>=200_000_000:requests+=1;last=r['stamp_ns']
                if aligned_required and sync and expected:
                    compared+=1;differences+=any(actual.get(k)!=expected.get(k) for k in ('valid','aligned','discontinuity','stable_frames','eligible'))
            dest=a.study/'gate-independent-ablation'/str(aligned_required).lower()/folder.name;dest.mkdir(parents=True,exist_ok=True);(dest/'results.jsonl').write_text(process.stdout)
            rows.append(dict(run_id=folder.name,scene=run['key'][-1],eligible_frames=eligible,candidate_requests=requests,live_comparison_frames=compared,live_mismatch_frames=differences))
        value=dict(stability_requires_alignment=aligned_required,runs=rows,total_candidate_requests=sum(r['candidate_requests'] for r in rows),
                   exact_original_gate_match=all(r['live_mismatch_frames']==0 for r in rows) if aligned_required else None)
        result.append(value);write(a.study/'gate-independent-ablation.json',result);print(json.dumps(value,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
