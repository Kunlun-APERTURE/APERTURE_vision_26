#!/usr/bin/env python3
"""Full evidence hash/JPEG/video audit, run after performance measurements."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess

import cv2
import numpy as np


def digest(path):
    checksum=hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):checksum.update(block)
    return checksum.hexdigest()


def audit(folder):
    manifest=json.loads((folder/'manifest.json').read_text())
    evidence=json.loads((folder/'evidence-sha256.json').read_text())
    errors=[]
    for name,expected in evidence.items():
        if digest(folder/name)!=expected:errors.append('Evidence SHA256 mismatch: '+name)
    for name,expected in manifest['configuration_sha256'].items():
        if digest(folder/'configuration'/name)!=expected:errors.append('Configuration SHA256 mismatch: '+name)
    rows=[json.loads(line) for line in (folder/'frames.jsonl').read_text().splitlines() if line]
    decoded=0;offset=0;shape=None
    policy=json.loads((folder/'evidence-policy.json').read_text()) if (folder/'evidence-policy.json').exists() else {}
    if policy:
        from background_pipeline import archived_frames
        entries=archived_frames(folder)
    else:
        def old_entries():
            with (folder/'input.mjpg').open('rb') as source:
                for row in rows:
                    source.seek(row['jpeg_offset']);yield row,source.read(row['jpeg_bytes'])
        entries=old_entries()
    for index,(row,data) in enumerate(entries):
            if policy.get('level')!='sampled' and (row['video_index']!=index or row['jpeg_offset']!=offset):errors.append('Discontinuous JPEG index: '+str(index))
            frame=cv2.imdecode(np.frombuffer(data,dtype=np.uint8),cv2.IMREAD_COLOR)
            if frame is None:errors.append('Undecodable JPEG: '+str(index))
            else:
                decoded+=1
                if shape is None:shape=frame.shape
                if shape!=frame.shape:errors.append('JPEG dimensions changed')
            offset=row['jpeg_offset']+row['jpeg_bytes']
    if policy.get('level')!='sampled' and offset!=(folder/'input.mjpg').stat().st_size:errors.append('Raw JPEG archive has unindexed bytes')
    if policy and decoded!=policy['retained_frames']:errors.append('Retention index/frame count mismatch')
    command=['ffprobe','-v','error','-threads','1','-select_streams','v:0','-count_frames',
        '-show_entries','stream=nb_read_frames,codec_name,width,height','-of','json',str(folder/'annotated.mp4')]
    if not (folder/'annotated.mp4').exists() and manifest.get('pipeline_version')==2:
        return dict(run_id=folder.name,passed=not errors,errors=errors,frames=len(rows),decoded_jpegs=decoded,
            video={},media_state='not_rendered',evidence_files=len(evidence),configuration_files=len(manifest['configuration_sha256']))
    result=subprocess.run(command,capture_output=True,text=True,timeout=120)
    video=json.loads(result.stdout)['streams'][0] if result.returncode==0 else {}
    if result.returncode or result.stderr.strip():errors.append('MP4 decode: '+result.stderr)
    if int(video.get('nb_read_frames',0))!=decoded:errors.append('MP4/JPEG frame count mismatch')
    return dict(run_id=folder.name,passed=not errors,errors=errors,frames=len(rows),
        decoded_jpegs=decoded,video=video,evidence_files=len(evidence),configuration_files=len(manifest['configuration_sha256']))


def main():
    p=argparse.ArgumentParser();p.add_argument('--input',type=Path,required=True)
    p.add_argument('--root',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();state=json.loads(args.input.read_text())
    runs=[run for mode in state['modes'].values() for run in mode['runs']]
    cv2.setNumThreads(1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=[]
        for row in pool.map(audit,(args.root/run for run in runs)):
            results.append(row)
            print(json.dumps(dict(progress=len(results),expected=len(runs),run_id=row['run_id'],passed=row['passed'])),flush=True)
    summary=dict(passed=all(r['passed'] for r in results),runs=len(results),
        decoded_jpegs=sum(r['decoded_jpegs'] for r in results),
        decoded_mp4_frames=sum(int(r['video'].get('nb_read_frames',0)) for r in results),results=results)
    args.output.write_text(json.dumps(summary,indent=2))
    if not summary['passed']:raise SystemExit(1)


if __name__=='__main__':main()
