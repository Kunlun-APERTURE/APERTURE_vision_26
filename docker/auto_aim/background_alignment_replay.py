#!/usr/bin/env python3
"""Replay identical raw images in desktop/independent runtimes after live trials."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import background_jobs as jobs
import experiments
import manage
from report import load, write


def inside(path):
    return '/home/openvino/'+str(path.relative_to(manage.DESKTOP))


LIBRARY_PROBE = '''
import glob,hashlib,json,os,re,subprocess,sys
exe=sys.argv[1]
output=subprocess.check_output(['ldd',exe],text=True)
paths=set(re.findall(r'(/[^\\s]+) \\(0x',output))
paths.update(glob.glob('/opt/intel/openvino/runtime/lib/intel64/libopenvino*.so*'))
def digest(path):
 h=hashlib.sha256()
 with open(path,'rb') as f:
  for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
 return h.hexdigest()
libraries={os.path.basename(p):digest(p) for p in sorted(paths) if os.path.isfile(p)}
print(json.dumps(dict(binary=digest(exe),libraries=libraries,
 threads={key:os.getenv(key) for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS')})))
'''


def run(output):
    state=load(output/'alignment.json')
    with jobs.connect() as db:
        bundle=Path(jobs.get_job(db,state['modes']['background']['job_id'])['bundle'])
    metadata=load(bundle/'bundle.json')
    desktop_experiments=manage.DESKTOP/'.local/share/rm26-sim/experiments'
    destination=desktop_experiments/output.name/'runtime-replay';destination.mkdir(parents=True,exist_ok=True)
    snapshot=destination/'snapshot';snapshot.mkdir(exist_ok=True)
    def frozen_copy(source,target):
        if Path(target).exists():
            if experiments.digest(source)!=experiments.digest(target):raise RuntimeError('Replay snapshot differs: '+str(target))
            return target
        return shutil.copy2(source,target)
    for name in ('assets','configs'):
        shutil.copytree(bundle/'sp_vision_25'/name,snapshot/name,dirs_exist_ok=True,copy_function=frozen_copy)
    binary=snapshot/'sim_auto_aim'
    frozen_copy(bundle/'sp_vision_25/build/simulator/sim_auto_aim',binary)
    for mode in ('ui','background'):
        work=destination/mode;work.mkdir(exist_ok=True)
        for name in ('assets','configs'):
            link=work/name
            if not link.is_symlink():link.symlink_to('../snapshot/'+name)
    # The same frozen binary, configs and weights are visible at identical paths.
    common=['docker','run','--rm','--network','none','--user',f'{os.getuid()}:{os.getgid()}',
        '--volume',str(desktop_experiments)+':'+inside(desktop_experiments)+':ro',
        '--volume',str(destination)+':'+inside(destination)+':rw',
        '--workdir',inside(destination/'background')]
    prefixes={
        'ui':['docker','exec','--user','1000:1000','--workdir',inside(destination/'ui'),manage.DESKTOP_CONTAINER],
        'background':common+['--entrypoint',inside(binary),metadata['images']['algorithm']]}
    probes={}
    for mode in prefixes:
        command=(prefixes[mode]+['python3','-c',LIBRARY_PROBE,inside(binary)] if mode=='ui' else
            common+['--entrypoint','python3',metadata['images']['algorithm'],'-c',LIBRARY_PROBE,inside(binary)])
        probes[mode]=json.loads(subprocess.check_output(command,text=True,timeout=60))
    a=probes['ui']['libraries'];b=probes['background']['libraries']
    mismatches={k:dict(ui=a.get(k),background=b.get(k)) for k in a.keys()|b.keys() if a.get(k)!=b.get(k)}
    write(output/'runtime-libraries.json',dict(results=probes,mismatches=mismatches,
        same_binary=probes['ui']['binary']==probes['background']['binary'],
        same_dependencies=not mismatches,method='ldd dependencies plus OpenVINO plugin file SHA256; does not measure scheduling'))
    results=[]
    for scene in ('hf_static_8','hf_turn90_3'):
        run_id=next(r for r in state['modes']['ui']['runs'] if load(manage.OPS/'experiments'/r/'manifest.json')['scene']==scene)
        folder=manage.OPS/'experiments'/run_id
        if load(folder/'evidence-policy.json',{}).get('level')=='sampled':
            raise RuntimeError('Sparse evidence cannot be used as a complete detector replay; rerun with --evidence full')
        source=destination/(scene+'-observations.jsonl')
        observations=[];source_count=0
        indexed_stamps={json.loads(line)['stamp_ns'] for line in (folder/'frames.jsonl').read_text().splitlines()}
        with (folder/'telemetry.jsonl').open() as input_file:
            for line in input_file:
                record=json.loads(line)
                if record['kind']=='observation':
                    source_count+=1
                    if record['data']['frame']['stamp_ns'] in indexed_stamps:observations.append(line)
        source.write_text(''.join(observations))
        rows={};timings={}
        for mode,prefix in prefixes.items():
            result_file=destination/(scene+'-'+mode+'.jsonl')
            options=['--config='+inside(snapshot/'configs/simulator.yaml'),
                '--algorithm-profile=tracking_fire_v3','--view=false','--fire=off','--aim-origin=muzzle',
                '--replay='+inside(source),'--raw-replay='+inside(desktop_experiments/run_id),
                '--replay-output='+inside(result_file),
                '--runtime='+inside(destination/(scene+'-'+mode+'-runtime.yaml'))]
            command=prefix+([inside(binary)] if mode=='ui' else [])+options
            started=time.monotonic()
            completed=subprocess.run(command,capture_output=True,text=True,timeout=120)
            (destination/(scene+'-'+mode+'.log')).write_text(completed.stdout+completed.stderr)
            completed.check_returncode();timings[mode]=time.monotonic()-started
            rows[mode]=[json.loads(line) for line in result_file.read_text().splitlines()]
        keys=('observations','detector_summary','valid','tracker','yaw','pitch','aim_point','filter_state')
        differences={key:sum(a.get(key)!=b.get(key) for a,b in zip(rows['ui'],rows['background'])) for key in keys}
        count=len(observations)
        full_differences=sum(a!=b for a,b in zip(rows['ui'],rows['background']))
        passed=count>0 and all(len(values)==count and all(r.get('raw_replayed') for r in values) for values in rows.values()) and full_differences==0
        result=dict(scene=scene,run_id=run_id,input_frames=count,source_observations=source_count,
            excluded_without_archived_jpeg=source_count-count,
            jpeg_sha256=experiments.digest(folder/'input.mjpg'),observations_sha256=experiments.digest(source),
            output_frames={m:len(v) for m,v in rows.items()},different_frames_by_field=differences,
            exact_all_fields_different_frames=full_differences,
            max_yaw_difference_rad=max((abs(a['yaw']-b['yaw']) for a,b in zip(rows['ui'],rows['background'])),default=0),
            max_pitch_difference_rad=max((abs(a['pitch']-b['pitch']) for a,b in zip(rows['ui'],rows['background'])),default=0),
            wall_s=timings,passed=passed)
        results.append(result);print(json.dumps(result),flush=True)
        write(output/'runtime-replay.json',dict(results=results,passed=all(r['passed'] for r in results),
            binary_sha256=experiments.digest(binary),frozen_bundle=str(bundle),
            limitation='Only observations with an archived original JPEG are fed, in original order. Same-image offline numerical comparison does not establish closed-loop hit-rate equivalence or identical warm detector caches.'))
    archive=output/'replay';archive.mkdir(exist_ok=True)
    for source in destination.iterdir():
        if source.is_file():shutil.copy2(source,archive/source.name)
    experiments.publish_desktop(manage,output)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    run(p.parse_args().output.resolve())
