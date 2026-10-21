#!/usr/bin/env python3
"""Compare the real C++ gate on identical recorded commands; no hit-rate inference."""
import argparse,json,subprocess
from pathlib import Path
from continuity_replay import ROOT,OPS,write
from reversal_diagnose import gate_inputs


def main():
    p=argparse.ArgumentParser();p.add_argument('--study',type=Path,required=True);a=p.parse_args();a.study=a.study.resolve()
    base=(ROOT/'sp_vision_25/configs/simulator.yaml').read_text().replace('algorithm_profile: legacy','algorithm_profile: tracking_fire_v3').replace('auto_fire: false','auto_fire: true')
    summaries=[]
    for duration in (.02,.04,.06):
        import re
        config=ROOT/'sp_vision_25/build/sim/fire-select.yaml';config.write_text(re.sub(r'fire_stable_time_s: [0-9.]+',f'fire_stable_time_s: {duration}',base))
        eligible=requests=frames=0
        for folder in sorted((a.study/'radial-ablation'/'3.0').iterdir()):
            result={r['stamp_ns']:r for r in map(json.loads,(folder/'results.jsonl').read_text().splitlines())}
            observations=[]
            for line in (OPS/'experiments'/folder.name/'telemetry.jsonl').open():
                packet=json.loads(line)
                if packet['kind']=='observation':
                    data=packet['data'];data['result']={**data['result'],**result[data['frame']['stamp_ns']]};observations.append(data)
            inputs=gate_inputs(observations)
            process=subprocess.run(['docker','exec','-i','--user','1000','sp_vision_sunshine','/workspace/sp_vision_25/build/diagnostic_fire_replay/diagnostic_fire_replay','/workspace/sp_vision_25/build/sim/fire-select.yaml'],input=''.join(json.dumps(r)+'\n' for r in inputs),capture_output=True,text=True,check=True)
            rows=[json.loads(l) for l in process.stdout.splitlines()];last=None;count=0
            for row in rows:
                if row['eligible']:
                    eligible+=1
                    if last is None or row['stamp_ns']-last>=200_000_000:last=row['stamp_ns'];count+=1
            requests+=count;frames+=len(rows)
            dest=a.study/'fire-ablation'/str(duration)/folder.name;dest.mkdir(parents=True,exist_ok=True)
            (dest/'results.jsonl').write_text(process.stdout)
        summaries.append(dict(stable_time_s=duration,frames=frames,eligible_frames=eligible,candidate_requests=requests))
        write(a.study/'fire-ablation.json',summaries);print(json.dumps(summaries[-1]),flush=True)

if __name__=='__main__':main()
