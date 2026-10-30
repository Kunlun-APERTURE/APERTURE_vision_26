#!/usr/bin/env python3
"""Single-frame prior intervention using frozen Solver sources and recorded priors."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import subprocess
import yaml

from diagnosis_v4_analysis import Evidence, norm, sub, add
from background_alignment_diagnosis import stats
import experiments
import manage
from report import load, write


def run(study,output):
    state=load(study/'alignment.json');probe=[];context={};excluded=Counter();config=None
    for mode,entry in state['modes'].items():
        for run_id in entry['runs']:
            folder=manage.OPS/'experiments'/run_id;m=load(folder/'manifest.json')
            if m['scene']!='hf_static_8':continue
            ev=Evidence(dict(run_id=run_id))
            for d in ev.observations:
                f=d['frame'];r=d['result'];stamp=f['stamp_ns']
                if not ev.measured(stamp):continue
                observations=r.get('observations',[]);diagnostic=r.get('continuity',{}).get('filter',{})
                prior=diagnostic.get('state_before',[]);updates=diagnostic.get('updates',[])
                if len(observations)!=1 or len(prior)!=11 or len(updates)!=1 or not updates[0].get('accepted') or updates[0].get('plate_id')!=0:
                    excluded['not_single_accepted_front_plate']+=1;continue
                o=observations[0];target=ev.front(stamp)
                if not o.get('position') or not target:excluded['no_pose_or_truth']+=1;continue
                if config is None:
                    config=yaml.safe_load((folder/'configuration/runtime-calibration.yaml').read_text())
                    config.update(algorithm_profile='tracking_fire_v3',diagnosis={'enabled':True})
                    config.update(camera_matrix=f['calibration']['k'],distort_coeffs=f['calibration']['d'],
                        R_camera2gimbal=f['calibration']['r_camera2gimbal'],t_camera2gimbal=f['calibration']['t_camera2gimbal'])
                identity=run_id+':'+str(stamp)
                probe.append(dict(id=identity,frame=f,observation=o,hint=prior[6]))
                predicted=[prior[0]-prior[8]*math.cos(prior[6]),prior[2]-prior[8]*math.sin(prior[6]),prior[4]]
                expected=add(predicted,updates[0]['position_residual_m'])
                context[identity]=dict(mode=mode,run_id=run_id,seed=m['random_seed'],time_s=ev.time(stamp),
                    truth_center=target[1][:3],origin=f['gimbal_position_map'],muzzle=f['muzzle_position_map'],
                    raw_xyz=o['position'],expected_tracker_measurement_xyz=expected,hint=prior[6],
                    video_index=ev.index.get(stamp,{}).get('video_index'))
    (output/'pose-probe-input.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in probe))
    (output/'pose-probe-config.yaml').write_text(yaml.safe_dump(config))
    metadata=load(output/'probe-build.json')
    command=['docker','run','--rm','--network','none','--user','1000:1000','-v',str(output)+':/output',
        '--workdir','/output','--entrypoint','/output/diagnostic_pose_prior',metadata['image'],
        '/output/pose-probe-config.yaml','/output/pose-probe-input.jsonl','/output/pose-probe-output.jsonl']
    result=subprocess.run(command,capture_output=True,text=True,timeout=120)
    (output/'pose-probe.log').write_text(result.stdout+result.stderr);result.check_returncode()
    records=[]
    for line in (output/'pose-probe-output.jsonl').open():
        row=json.loads(line);c=context[row['id']];a=row['unhinted'];b=row['hinted']
        def range_error(xyz):return norm(sub(add(xyz,c['origin']),c['muzzle']))-norm(sub(c['truth_center'],c['muzzle']))
        d=b['diagnosis']
        records.append(dict(id=row['id'],mode=c['mode'],run_id=c['run_id'],seed=c['seed'],time_s=c['time_s'],video_index=c['video_index'],
            hint_deg=math.degrees(c['hint']),unhinted_range_error_m=range_error(a['xyz']),hinted_range_error_m=range_error(b['xyz']),
            unhinted_raw_reproduction_error_m=norm(sub(a['xyz'],c['raw_xyz'])),
            hinted_measurement_reproduction_error_m=norm(sub(b['xyz'],c['expected_tracker_measurement_xyz'])),
            unhinted_yaw_deg=math.degrees(a['yaw']),hinted_yaw_deg=math.degrees(b['yaw']),
            hinted_cost_excess_px=d['chosen_cost']-d['best_cost'],hinted_xyz=b['xyz'],unhinted_xyz=a['xyz']))
    groups=[]
    for mode,entry in state['modes'].items():
        for run_id in entry['runs']:
            rr=[r for r in records if r['run_id']==run_id]
            if not rr:continue
            groups.append(dict(mode=mode,run_id=run_id,frames=len(rr),
                **{key:stats([r[key] for r in rr]) for key in ('unhinted_range_error_m','hinted_range_error_m',
                    'unhinted_raw_reproduction_error_m','hinted_measurement_reproduction_error_m','hinted_cost_excess_px')}))
    (output/'pose-prior-intervention.jsonl').write_text(''.join(json.dumps(r,allow_nan=False)+'\n' for r in records))
    write(output/'pose-prior-summary.json',dict(frames=len(records),excluded=dict(excluded),runs=groups,
        raw_reproduced_fraction=sum(r['unhinted_raw_reproduction_error_m']<1e-6 for r in records)/len(records),
        tracker_measurement_reproduced_fraction=sum(r['hinted_measurement_reproduction_error_m']<1e-5 for r in records)/len(records),
        input_sha256=experiments.digest(output/'pose-probe-input.jsonl'),binary_sha256=experiments.digest(output/'diagnostic_pose_prior'),
        method='For a single accepted front-plate observation, state_before gives the actual predicted yaw prior. The same frozen Solver receives identical corners and calibration with that prior or no prior. No target truth is passed to the probe.',
        limits='An intervention on one pose solve with the recorded prior; not a full tracker counterfactual or a new closed-loop hit measurement. Reproduction errors quantify inference of the original prior/measurement.'))
    print(json.dumps(dict(frames=len(records),groups=len(groups))),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--study',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();run(args.study.resolve(),args.output.resolve())
