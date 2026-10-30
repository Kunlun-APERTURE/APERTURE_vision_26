"""Versioned raw evidence retention and a bounded offline media queue."""
import argparse
import bisect
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from report import load,write,render


def lines(path):
    if not path.exists():return []
    with path.open() as stream:return [json.loads(line) for line in stream if line.strip()]


def sha(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda:stream.read(1024*1024),b''):digest.update(data)
    return digest.hexdigest()


def jpeg_size(data):
    """Read SOF dimensions without decompressing every captured image."""
    if data[:2]!=b'\xff\xd8' or data[-2:]!=b'\xff\xd9':raise ValueError('Incomplete JPEG')
    i=2
    while i+3<len(data):
        if data[i]!=255:raise ValueError('Malformed JPEG marker')
        while i<len(data) and data[i]==255:i+=1
        marker=data[i];i+=1
        if marker in (0xd8,0xd9,0x01) or 0xd0<=marker<=0xd7:continue
        length=int.from_bytes(data[i:i+2],'big')
        if length<2 or i+length>len(data):raise ValueError('Malformed JPEG length')
        if marker in (0xc0,0xc1,0xc2,0xc3,0xc5,0xc6,0xc7,0xc9,0xca,0xcb,0xcd,0xce,0xcf):
            return int.from_bytes(data[i+5:i+7],'big'),int.from_bytes(data[i+3:i+5],'big')
        if marker==0xda:break
        i+=length
    raise ValueError('JPEG has no dimensions')


def compact(folder):
    folder=Path(folder);manifest=load(folder/'manifest.json',{})
    if manifest.get('pipeline_version')!=2:return
    existing=load(folder/'evidence-policy.json',{})
    if existing.get('committed'):
        # Recover the final deletion after a crash between commit and unlink.
        if existing['level']=='sampled':(folder/'input.mjpg').unlink(missing_ok=True)
        return existing
    summary=load(folder/'summary.json',{});state=load(folder/'run-state.json',{})
    rows=lines(folder/'frames.jsonl');shots=lines(folder/'shots.jsonl')
    reasons=[]
    if manifest.get('stage')=='final':reasons.append('final')
    if manifest.get('evidence')=='full':reasons.append('explicit_full')
    if summary.get('verdict')=='invalid' or not state.get('complete'):reasons.append('invalid_or_interrupted')
    if any(not r['passed'] for r in summary.get('rules',[])):reasons.append('rule_anomaly')
    if manifest.get('mode')=='shoot' and (not shots or not any(s['outcome']=='target_armor' for s in shots)):
        reasons.append('no_shots_or_all_misses')
    if int(hashlib.sha256(folder.name.encode()).hexdigest()[:8],16)%20==0:reasons.append('audit_5_percent')
    raw=folder/'input.mjpg'
    if not raw.exists():raise ValueError('Unsealed evidence has no original JPEG archive')
    initial_bytes=raw.stat().st_size
    policy=dict(version=1,committed=True,level='full' if reasons else 'sampled',reasons=reasons,
        total_frames=len(rows),original_bytes=initial_bytes,original_sha256=sha(raw),
        original_index_sha256=sha(folder/'frames.jsonl') if (folder/'frames.jsonl').exists() else None,
        limitation='Only archived measurement images are available; model warmup and prior detector cache cannot be exactly replayed.')
    if reasons:
        policy.update(retained_frames=len(rows),retained_bytes=initial_bytes,archive='input.mjpg',index='frames.jsonl')
    else:
        keep=set();last_second=None
        stamps=[r['stamp_ns'] for r in rows]
        first=min(stamps,default=0);last=max(stamps,default=0)
        for i,row in enumerate(rows):
            second=(row['stamp_ns']-first)//1_000_000_000
            if second!=last_second or row['stamp_ns']-first<=500_000_000 or last-row['stamp_ns']<=500_000_000:keep.add(i)
            last_second=second
        for shot in shots:
            for stamp in (shot.get('fire_stamp_ns'),shot.get('terminal_stamp_ns')):
                if stamp is None or not stamps:continue
                i=max(0,min(len(rows)-1,bisect.bisect_right(stamps,stamp)-1))
                keep.update(j for j in (i-1,i,i+1) if 0<=j<len(rows))
        # Keep the forwarded fire-advice image even when no launch followed.
        forwarded=set()
        for packet in lines(folder/'telemetry.jsonl'):
            if packet['kind']=='observation':
                d=packet['data']
                if d.get('command_emitted') and d.get('result',{}).get('fire_advice'):
                    forwarded.add(d['frame']['stamp_ns'])
        for i,row in enumerate(rows):
            if row['stamp_ns'] in forwarded:keep.update(j for j in (i-1,i,i+1) if 0<=j<len(rows))
        retained=[]
        archive=folder/'retained.mjpg';temporary=folder/'retained.mjpg.tmp'
        with raw.open('rb') as source,temporary.open('wb') as target:
            for i in sorted(keep):
                row=rows[i];source.seek(row['jpeg_offset']);data=source.read(row['jpeg_bytes'])
                if len(data)!=row['jpeg_bytes']:raise ValueError('Truncated source JPEG archive')
                jpeg_size(data)
                retained.append(dict(original_video_index=row['video_index'],stamp_ns=row['stamp_ns'],
                    jpeg_offset=target.tell(),jpeg_bytes=len(data)))
                target.write(data)
            target.flush();os.fsync(target.fileno())
        temporary.replace(archive)
        write(folder/'retained-index.json',retained)
        policy.update(retained_frames=len(retained),retained_bytes=archive.stat().st_size,
            archive=archive.name,index='retained-index.json',archive_sha256=sha(archive),
            index_sha256=sha(folder/'retained-index.json'),
            sampling='1 Hz + first/last 0.5 s + forwarded/actual shots and adjacent frames + impact-adjacent frames',
            limitation=policy['limitation']+' Discarded images cannot be recovered. Sparse montage is not continuous video.')
    write(folder/'evidence-policy.json',policy)
    # Commit the archive, index and policy before dropping the only full copy.
    for name in (policy['archive'],policy['index'],'evidence-policy.json'):
        with (folder/name).open('rb') as stream:os.fsync(stream.fileno())
    descriptor=os.open(folder,os.O_RDONLY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)
    if policy['level']=='sampled':raw.unlink()
    return policy


def archived_frames(folder):
    """One reader for full and compact archives; original indices never change."""
    policy=load(folder/'evidence-policy.json',{})
    rows=lines(folder/'frames.jsonl')
    if policy.get('level')=='sampled':
        mapping={r['original_video_index']:r for r in load(folder/'retained-index.json',[])}
        archive=folder/'retained.mjpg'
    else:
        mapping={r['video_index']:r for r in rows};archive=folder/'input.mjpg'
    with archive.open('rb') as stream:
        for row in rows:
            entry=mapping.get(row['video_index'])
            if entry is None:continue
            stream.seek(entry['jpeg_offset']);data=stream.read(entry['jpeg_bytes'])
            if len(data)!=entry['jpeg_bytes']:raise ValueError('Truncated archived frame')
            yield row,data


def make_media(folder):
    import cv2
    import numpy as np
    from report import plots
    folder=Path(folder);started=time.time();cv2.setNumThreads(1)
    manifest=load(folder/'manifest.json',{})
    if manifest.get('pipeline_version')!=2:raise ValueError('Legacy task has no frozen deferred renderer; retain its existing media')
    source_hashes={n:sha(folder/n) for n in ('frames.jsonl','telemetry.jsonl','evidence-policy.json')}
    status=dict(state='running',started=started,evidence_level=load(folder/'evidence-policy.json',{}).get('level'),
        renderer_sha256=sha(Path(__file__)),source_hashes=source_hashes)
    write(folder/'media-status.json',status)
    process=None;temporary=folder/'annotated.tmp.mp4';count=0;gaps=[];last=None
    try:
        for row,data in archived_frames(folder):
            frame=cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_COLOR)
            if frame is None:raise ValueError('Archived JPEG decode failed')
            h,w=frame.shape[:2]
            for name,args in row.get('drawings',[]):
                if name not in ('polylines','putText','drawMarker','circle','rectangle'):raise ValueError('Unknown drawing operation')
                if name=='polylines':args=[ [np.asarray(p,np.int32) for p in args[0]],*args[1:] ]
                getattr(cv2,name)(frame,*args)
            if status['evidence_level']=='sampled':
                cv2.putText(frame,f'SPARSE EVIDENCE | source t={row.get("time")} | gaps omitted',
                    (12,h-20),cv2.FONT_HERSHEY_SIMPLEX,.6,(0,0,255),2)
            if last is not None and row['video_index']!=last+1:gaps.append([last+1,row['video_index']-1])
            last=row['video_index']
            if process is None:
                process=subprocess.Popen(['ffmpeg','-nostdin','-loglevel','error','-y','-f','rawvideo',
                    '-pix_fmt','bgr24','-s',f'{w}x{h}','-r','30','-i','pipe:0','-an','-c:v','libx264',
                    '-threads','1','-preset','ultrafast','-crf','25','-pix_fmt','yuv420p','-movflags','+faststart',str(temporary)],stdin=subprocess.PIPE)
            process.stdin.write(frame.tobytes());count+=1
        if process is None:raise ValueError('No archived frames available')
        process.stdin.close()
        if process.wait(timeout=30):raise ValueError('FFmpeg failed')
        temporary.replace(folder/'annotated.mp4')
        rows=lines(folder/'frames.jsonl');plots(folder,rows)
        motion=load(folder/'motion-analysis.json')
        if motion:
            from highspeed_report import figures
            figures(folder,motion['samples'],lines(folder/'shots.jsonl'),load(folder/'configuration/scenario-plan.json',{}))
        status.update(state='completed',ended=time.time(),frames=count,omitted_index_ranges=gaps,
            timeline='Nominal 30 FPS; timestamps in frames.jsonl. Sparse archives omit gaps, not continuous playback.',
            derived_sha256={p.name:sha(p) for p in folder.iterdir() if p.suffix in ('.mp4','.png','.svg')})
    except BaseException as error:
        if process and process.poll() is None:
            process.kill();process.wait()
        status.update(state='failed',ended=time.time(),error=type(error).__name__+': '+str(error))
        raise
    finally:
        temporary.unlink(missing_ok=True)
        write(folder/'media-status.json',status)
        render(folder,load(folder/'summary.json'))


def final_selection(db,job_id):
    import background_jobs as jobs
    groups={};anomalies=set()
    for attempt in jobs.status(db,job_id)['attempts']:
        if not attempt['run_id']:continue
        folder=jobs.OPS/'experiments'/attempt['run_id'];summary=load(folder/'summary.json',{})
        if not summary:continue
        score=summary.get('metrics',{}).get('weighted_hit_rate')
        groups.setdefault(summary['scene'],[]).append((score if score is not None else -1,folder.name))
        if any(not r['passed'] for r in summary.get('rules',[])):anomalies.add(folder.name)
    for group in groups.values():
        group.sort();anomalies.update([group[0][1],group[len(group)//2][1]])
    return sorted(anomalies)


def enqueue(db,job_id,bundle,run_ids):
    import background_jobs as jobs
    for run_id in run_ids:
        folder=jobs.OPS/'experiments'/run_id
        if load(folder/'manifest.json',{}).get('pipeline_version')!=2:
            raise ValueError('Legacy runs retain their original media; deferred renderer is unavailable')
        db.execute('INSERT INTO artifacts(run_id,job_id,bundle,state,requested) VALUES(?,?,?,?,?) '
            "ON CONFLICT(run_id) DO UPDATE SET state=CASE WHEN artifacts.state IN ('failed','cancelled') THEN 'queued' ELSE artifacts.state END,error=NULL",
            (run_id,job_id,str(bundle),'queued',time.time()))
        current=load(folder/'media-status.json',{})
        if current.get('state') not in ('running','completed'):write(folder/'media-status.json',dict(state='queued',requested=time.time()))


def enqueue_final(db,job_id,row_bundle):
    pending=db.execute("SELECT count(*) FROM items WHERE job_id=? AND state NOT IN ('completed','cancelled')",(job_id,)).fetchone()[0]
    if pending:return
    final=any(json.loads(r['settings']).get('stage')=='final' for r in db.execute('SELECT settings FROM items WHERE job_id=?',(job_id,)))
    if final:
        selection=final_selection(db,job_id)
        enqueue(db,job_id,row_bundle,selection)
        import background_jobs as jobs
        directory=jobs.OPS/'experiments'/job_id;directory.mkdir(exist_ok=True)
        write(directory/'review-list.json',dict(run_ids=selection,reason='Per-scene median, worst and flagged anomalies; only viewed runs may be reviewed.'))


def pump_media(db,active):
    import background_jobs as jobs
    if active:
        process,run_id,output=active
        item=db.execute('SELECT * FROM artifacts WHERE run_id=?',(run_id,)).fetchone()
        cancelled=jobs.get_job(db,item['job_id'])['state']=='cancelled'
        if cancelled and process.poll() is None:
            subprocess.run(['docker','rm','-f','rm26-media-'+run_id[-8:]],capture_output=True,text=True,timeout=8)
            process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait()
        if process.poll() is None:return active
        output.close();state='cancelled' if cancelled else 'completed' if process.returncode==0 else 'failed'
        db.execute('UPDATE artifacts SET state=?,ended=?,error=? WHERE run_id=?',
            (state,time.time(),None if state=='completed' else 'Offline renderer failed; jobs render retries without rerunning physics',run_id))
        artifact=db.execute('SELECT * FROM artifacts WHERE run_id=?',(run_id,)).fetchone()
        import experiments
        experiments.publish_desktop(jobs.manage,jobs.OPS/'experiments'/run_id)
        jobs.report(db,artifact['job_id'])
    row=db.execute("SELECT * FROM artifacts WHERE state='queued' ORDER BY requested LIMIT 1").fetchone()
    if not row:return None
    folder=jobs.OPS/'experiments'/row['run_id'];bundle=Path(row['bundle']);meta=load(bundle/'bundle.json')
    for name,expected in meta['files'].items():
        if name.startswith('docker/auto_aim/') and sha(bundle/name)!=expected:
            db.execute("UPDATE artifacts SET state='failed',ended=?,error=? WHERE run_id=?",(time.time(),'Frozen renderer checksum mismatch: '+name,row['run_id']))
            return None
    output=(folder/'media.log').open('a',buffering=1)
    command=['docker','run','--rm','--init','--network','none','--user',f'{os.getuid()}:{os.getgid()}',
        '--name','rm26-media-'+row['run_id'][-8:],'--label','rm26.media=true','--label','rm26.workspace='+str(jobs.manage.ROOT),
        '--cpus','1','--cpu-shares','128','--env','OPENBLAS_NUM_THREADS=1','--env','OMP_NUM_THREADS=1','--env','MPLCONFIGDIR=/tmp/matplotlib',
        '--volume',str(bundle/'docker/auto_aim')+':/opt/rm26:ro','--volume',str(folder)+':/evidence',
        '--entrypoint','python3',meta['images']['evaluator'],'-u','/opt/rm26/background_pipeline.py','render','/evidence']
    process=subprocess.Popen(command,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
    try:os.setpriority(os.PRIO_PROCESS,process.pid,10)
    except PermissionError:pass
    db.execute("UPDATE artifacts SET state='running',started=?,ended=NULL,error=NULL WHERE run_id=?",(time.time(),row['run_id']))
    return process,row['run_id'],output


def command(db,args):
    import background_jobs as jobs
    job=jobs.get_job(db,args.job_id);status=jobs.status(db,args.job_id)
    available={r['run_id'] for r in status['attempts'] if r['run_id'] and r['state']!='running'}
    if args.jobs_action=='render':
        selected=[args.run_id] if args.run_id else sorted(available) if args.all else final_selection(db,args.job_id)
        if not set(selected)<=available:raise ValueError('Run is not a completed attempt of this task')
        enqueue(db,args.job_id,job['bundle'],selected)
        print(json.dumps(dict(queued=selected),ensure_ascii=False));return
    selected=args.runs
    if not set(selected)<=available:raise ValueError('Only completed runs from this task may be reviewed')
    if not args.note.strip():raise ValueError('Review requires an observation note')
    review=dict(decision=args.decision,note=args.note,run_ids=selected,
        reviewed_utc=datetime.now(timezone.utc).isoformat(),reviewer=os.environ.get('USER','rm'))
    import experiments
    for run_id in selected:
        folder=jobs.OPS/'experiments'/run_id
        if load(folder/'media-status.json',{}).get('state')!='completed':raise ValueError('Render and view requested evidence before reviewing '+run_id)
    for run_id in selected:
        folder=jobs.OPS/'experiments'/run_id
        audit=folder/'reviews';audit.mkdir(exist_ok=True)
        write(audit/(str(time.time_ns())+'.json'),review);write(folder/'visual-review.json',review)
        summary=load(folder/'summary.json');summary.update(visual_review=review,visual_status=args.decision)
        write(folder/'summary.json',summary);render(folder,summary);experiments.publish_desktop(jobs.manage,folder)
    jobs.report(db,args.job_id)
    print(json.dumps(review,ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['render']);parser.add_argument('folder',type=Path)
    options=parser.parse_args();make_media(options.folder)
