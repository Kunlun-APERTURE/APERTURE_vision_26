#!/usr/bin/env python3
"""Verify frozen paired studies and all published experiment evidence."""
import argparse,collections,hashlib,json,re,subprocess
from pathlib import Path
from continuity_replay import OPS,ROOT
from report import load,write


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def main():
    p=argparse.ArgumentParser();p.add_argument('--study',type=Path,required=True);p.add_argument('--decode-videos',action='store_true');a=p.parse_args();study=a.study.resolve()
    failures=[];files=0;frames=0;video_frames=0;runs=0;rate=[];walls=[];batch_checks=[]
    for batch_path in sorted(study.rglob('*-batch.json')):
        batch=load(batch_path);seen=set()
        for run in batch['runs']:
            key=tuple(run['key'])
            if key in seen:failures.append(dict(batch=str(batch_path),duplicate=key))
            seen.add(key);folder=OPS/'experiments'/run['run_id'];manifest=load(folder/'manifest.json');summary=load(folder/'summary.json')
            expected=dict(algorithm_binary_sha256=batch['signature']['files']['sp_vision_25/build/simulator/sim_auto_aim'],
                simulator_image=batch['signature']['simulator_image'])
            if manifest['algorithm_binary_sha256']!=expected['algorithm_binary_sha256'] or manifest['containers']['simulator_image_id']!=expected['simulator_image']:failures.append(dict(run=folder.name,issue='frozen binary/image mismatch'))
            for name,sha in load(folder/'evidence-sha256.json').items():
                path=folder/name;files+=1
                if not path.is_file() or digest(path)!=sha:failures.append(dict(run=folder.name,file=name,issue='hash mismatch'))
            indices=[json.loads(s) for s in (folder/'frames.jsonl').open()];frames+=len(indices)
            if indices:
                if [r['video_index'] for r in indices]!=list(range(len(indices))):failures.append(dict(run=folder.name,issue='video index discontinuity'))
                with (folder/'input.mjpg').open('rb') as stream:
                    for r in indices:
                        stream.seek(r['jpeg_offset']);data=stream.read(r['jpeg_bytes'])
                        if len(data)!=r['jpeg_bytes'] or data[:2]!=b'\xff\xd8' or data[-2:]!=b'\xff\xd9':failures.append(dict(run=folder.name,seq=r['seq'],issue='JPEG span invalid'))
            if a.decode_videos and indices:
                info=subprocess.run(['ffprobe','-v','error','-count_frames','-select_streams','v:0','-show_entries','stream=nb_read_frames','-of','json',str(folder/'annotated.mp4')],capture_output=True,text=True,check=True)
                n=int(json.loads(info.stdout)['streams'][0]['nb_read_frames']);video_frames+=n
                if n!=len(indices):failures.append(dict(run=folder.name,issue='decoded video/index frame mismatch'))
            spacing=summary['metrics'].get('minimum_launch_spacing_s');wall=summary['metrics'].get('total_wall_s')
            if spacing is not None:
                rate.append(spacing)
                if spacing<.2-1e-8:failures.append(dict(run=folder.name,issue='fire interval below 200ms',actual=spacing))
            walls.append(wall)
            if wall is None or wall>40:failures.append(dict(run=folder.name,issue='40s wall budget',actual=wall))
            # All mutable configuration snapshots are tied to the run manifest.
            for name,sha in manifest['configuration_sha256'].items():
                if digest(folder/'configuration'/name)!=sha:failures.append(dict(run=folder.name,issue='configuration hash mismatch',file=name))
            runs+=1
        definition=batch['definition'];expected=len(definition['scenes'])*len(definition['profiles'])*len(definition['physics'])*len(definition['seeds'])*len(definition['display'])
        complete=len(seen)==expected
        if batch['complete']!=complete:failures.append(dict(batch=str(batch_path),issue='completeness flag mismatch'))
        batch_checks.append(dict(batch=str(batch_path.relative_to(study)),complete=complete,expected=expected,actual=len(seen),invalid=len(batch['invalid_attempts'])))
    before=load(study/'before.json');name='sp_vision_25/tests/auto_aim_test.cpp';user_test_unchanged=digest(ROOT/name)==before['files'][name]
    if not user_test_unchanged:failures.append(dict(issue='User test file changed'))
    result=dict(passed=not failures,runs=runs,hashed_evidence_files=files,indexed_frames=frames,decoded_video_frames=video_frames if a.decode_videos else None,
      minimum_launch_spacing_s=min(rate,default=None),maximum_total_wall_s=max(walls,default=None),user_test_unchanged=user_test_unchanged,
      batches=batch_checks,failures=failures,visual_review='pending',baseline_promoted=False)
    write(study/'integrity.json',result);print(json.dumps({k:v for k,v in result.items() if k not in ('batches','failures')},ensure_ascii=False))

if __name__=='__main__':main()
