#!/usr/bin/env python3
"""Finite pose/noise ablation on fixed original images, never a new hit estimate."""
import argparse,hashlib,json
from pathlib import Path
from robustness_replay import replay
from continuity_replay import ROOT,OPS,q,write


def main():
    p=argparse.ArgumentParser();p.add_argument('--study',type=Path,required=True);a=p.parse_args();a.study=a.study.resolve()
    pairs=json.loads((OPS/'experiments/reversal-diagnosis-20261020/event-pairs.json').read_text())
    runs={r[k].rsplit('_e',1)[0] for r in pairs for k in ('slow_event_id','normal_event_id')}
    novel=json.loads((a.study/'preflight-c/preflight-batch.json').read_text())
    runs.update(r['run_id'] for r in novel['runs'] if r['key'][4] in ('distance_8','spin_move_ego_15','random_both_10'))
    base=(ROOT/'sp_vision_25/configs/simulator.yaml').read_text();configuration=ROOT/'sp_vision_25/build/sim/pose-ablation';configuration.mkdir(exist_ok=True)
    binary=hashlib.sha256((ROOT/'sp_vision_25/build/simulator/sim_auto_aim').read_bytes()).hexdigest();summaries=[]
    for constrained,radial in ((False,3.),(True,.3),(True,1.),(True,3.)):
        label=f'constraint-{str(constrained).lower()}_radial-{radial:g}';config=configuration/(label+'.yaml')
        text=base.replace('  constrained_pose: false','  constrained_pose: '+str(constrained).lower()).replace('  radial_acceleration_variance: 3.0','  radial_acceleration_variance: '+str(radial))
        config.write_text(text);digest=hashlib.sha256(text.encode()).hexdigest();rows=[]
        for number,run in enumerate(sorted(runs),1):
            dest=a.study/'pose-ablation'/label/run;old=dest/'summary.json';result=json.loads(old.read_text()) if old.exists() else {}
            if result.get('binary_sha256')!=binary or result.get('config_sha256')!=digest:
                result=replay(OPS/'experiments'/run,dest,'tracking_v3',True,'build/sim/pose-ablation/'+config.name)
            rows.append(result)
            print(json.dumps(dict(candidate=label,run=number,total=len(runs),run_id=run,recovery=result['metrics']['recovery_p90_s'],position=result['metrics']['center_error_p95_m']),ensure_ascii=False),flush=True)
        events=[e['recovery_s'] if e['recovery_s'] is not None else 2. for r in rows for e in r['metrics']['recovery_events'] if not r['run_id'] in {x['run_id'] for x in novel['runs']}]
        summary=dict(label=label,constrained_pose=constrained,radial=radial,runs=len(rows),original_event_recovery_p90_s=q(events,.9),
          original_event_slow=sum(t>.3 for t in events),jump_frames=sum(r['metrics']['steady_angle_gt_1deg_frames'] for r in rows),
          median_position_p95_m=q([r['metrics']['center_error_p95_m'] for r in rows]),
          median_velocity_p95_mps=q([r['metrics']['velocity_error_p95_mps'] for r in rows]),
          novelty={r['run_id']:r['metrics'] for r in rows if r['run_id'] in {x['run_id'] for x in novel['runs']}},
          config_sha256=digest,binary_sha256=binary)
        summaries.append(summary);write(a.study/'pose-ablation-summary.json',summaries)
        print(json.dumps(summary,ensure_ascii=False),flush=True)
    eligible=[r for r in summaries if r['constrained_pose'] and r['original_event_recovery_p90_s'] is not None and r['original_event_recovery_p90_s']<=.3]
    pool=eligible or [r for r in summaries if r['constrained_pose']]
    selected=min(pool,key=lambda r:(r['jump_frames'],r['median_position_p95_m'],r['median_velocity_p95_mps'],r['radial']))
    write(a.study/'pose-selection.json',dict(selected=selected,selection_gate_passed=bool(eligible),
      policy='Fixed-image development selection. Existing thresholds retained. Novel live-C recordings used only for development; independent seed validation remains required.',
      runs=sorted(runs)))

if __name__=='__main__':main()
