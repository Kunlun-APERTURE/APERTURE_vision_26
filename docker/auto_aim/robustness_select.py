#!/usr/bin/env python3
"""Select finite replay candidates; all GT remains in the offline scorer."""
import argparse,collections,json,subprocess
from pathlib import Path
from continuity_replay import OPS,ROOT,prepare,metrics,q,write


def main():
    p=argparse.ArgumentParser();p.add_argument('--study',type=Path,required=True);p.add_argument('--bootstrap',action='store_true');a=p.parse_args();a.study=a.study.resolve(); suffix='bootstrap' if a.bootstrap else 'no-bootstrap'
    source_config=(ROOT/'sp_vision_25/configs/simulator.yaml').read_text()
    shared=ROOT/'sp_vision_25/build/sim/robustness-select';shared.mkdir(exist_ok=True)
    results=[]
    for radial in (.3,3.,10.,30.):
        config=shared/f'radial-{radial}.yaml';config.write_text(source_config+f'\nrobustness_v3:\n  radial_acceleration_variance: {radial}\n  velocity_bootstrap: {str(a.bootstrap).lower()}\n')
        group=[]
        for raw in sorted((a.study/'raw-v3').glob('*/results.jsonl')):
            run=raw.parent.name;folder=OPS/'experiments'/run;source,context=prepare(folder)
            observed={r['stamp_ns']:r for r in map(json.loads,raw.read_text().splitlines())}
            packets=[]
            for line in source.splitlines():
                packet=json.loads(line);r=observed[packet['data']['frame']['stamp_ns']]
                if r['raw_replayed']:packet['data']['result']['observations']=r['observations']
                packets.append(json.dumps(packet))
            command=['docker','exec','-i','--user','1000:1000','--workdir','/workspace/sp_vision_25','sp_vision_sunshine','build/simulator/sim_auto_aim',
              '--config=build/sim/robustness-select/'+config.name,'--runtime=build/sim/select.yaml','--algorithm-profile=tracking_v3','--replay=/dev/stdin','--replay-output=/dev/stdout']
            process=subprocess.run(command,input='\n'.join(packets)+'\n',capture_output=True,text=True,check=True)
            rows=[json.loads(s) for s in process.stdout.splitlines() if s.startswith('{')]
            dest=a.study/('radial-ablation-'+suffix)/str(radial)/run;dest.mkdir(parents=True,exist_ok=True)
            (dest/'results.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows));m=metrics(rows,context);write(dest/'metrics.json',m);group.append(m)
        events=[e['recovery_s'] if e['recovery_s'] is not None else 2. for m in group for e in m['recovery_events']]
        value=dict(radial=radial,runs=len(group),recovery_p90_s=q(events,.9),recovery_max_s=max(events),slow_events=sum(e>.3 for e in events),
          jump_frames=sum(m['steady_angle_gt_1deg_frames'] for m in group),velocity_error_p95_mps=q([m['velocity_error_p95_mps'] for m in group],.5))
        results.append(value);write(a.study/('radial-ablation-'+suffix+'.json'),results);print(json.dumps(value),flush=True)

if __name__=='__main__':main()
