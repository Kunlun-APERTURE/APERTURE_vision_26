"""Post-inference common-observation pose errors for local-minimum ablation."""
import argparse
import json
import math
from pathlib import Path

import manage
from perception_study import quantile
from report import load,write
from sync_first_divergence_analysis import primary


def stats(values):return dict(n=len(values),p50=quantile(values,.5),p95=quantile(values,.95))


def analyze(root):
    import numpy as np
    from perception_attribution_analysis import timeline,match_truth
    c=load(root/'contract.json');s=load(root/'state.json');samples=[]
    for entry in c['sources']:
        source=manage.OPS/'experiments'/entry['run_id'];truth=timeline(source)
        baseline=next(r for r in s['replays'] if r['source_run']==entry['run_id'] and r['variant']=='BS4')
        candidate=next(r for r in s['replays'] if r['source_run']==entry['run_id'] and r['variant']=='M4')
        a,b=primary(Path(baseline['output'])),primary(Path(candidate['output']))
        for x,y in zip(a,b):
            assert x['frame']==y['frame'] and x['jpeg_sha256']==y['jpeg_sha256']
            f=x['frame'];pa=x['result'].get('observations',[]);pb=y['result'].get('observations',[])
            links=match_truth(f,pa,truth(f['stamp_ns']))
            for i,link in links.items():
                obs=pa[i];center=np.mean(obs['corners'],axis=0)
                near=[(float(np.linalg.norm(center-np.mean(z['corners'],axis=0))),j) for j,z in enumerate(pb)]
                if not near:continue
                dist,j=min(near)
                if dist>1e-5:continue
                gt=link['plate'];target=np.asarray(gt['position']);n=np.asarray(gt['normal']);yaw=math.atan2(-n[1],-n[0])
                record=dict(source=source.name,frame_seq=f['seq'],physical_plate=gt['id'],variants={})
                for name,result,ob in (('BS4',x['result'],obs),('M4',y['result'],pb[j])):
                    pose=next((d for d in result.get('continuity',{}).get('diagnosis_poses',[])
                      if np.linalg.norm(np.asarray(d['image_center'])-center)<1e-3),None)
                    if pose is None:break
                    p=np.asarray(pose['xyz'])+f['gimbal_position_map'];error=p-target
                    radial=(target-np.asarray(f['camera_in_map']['position']))[:2];radial/=np.linalg.norm(radial)
                    tangent=np.array([-radial[1],radial[0]])
                    record['variants'][name]=dict(xy=float(np.linalg.norm(error[:2])),radial=abs(float(error[:2]@radial)),
                      lateral=abs(float(error[:2]@tangent)),z=abs(float(error[2])),yaw=abs(math.remainder(pose['yaw']-yaw,2*math.pi)),rms=pose['rms'])
                if len(record['variants'])==2:samples.append(record)
    summary={v:{key:stats([r['variants'][v][key] for r in samples]) for key in ('xy','radial','lateral','z','yaw','rms')} for v in ('BS4','M4')}
    write(root/'common-pose-analysis.json',dict(summary=summary,samples=samples,
      scope='Same archived images and detected physical plate; pose state may feed different tracking histories. Two development paths only; not hit-rate evidence. Truth only after inference.'))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();analyze(args.output.resolve())
