"""Passive complete-session input/result archive, including unscored warmup.

Disk IO runs outside the control path. Overflow is reported as invalid evidence;
it never blocks realtime control or silently drops an input from the archive.
"""
import hashlib
import json
import os
from pathlib import Path
import queue
import threading
import time


def digest(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):value.update(block)
    return value.hexdigest()


class SessionArchive:
    def __init__(self,root,session):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.session=session;self.pending=queue.Queue(maxsize=64)
        self.lock=threading.Lock();self.errors=[];self.closed=False;self.sequence=0
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
        self.record('session_start',dict(session=session,wall_ns=time.time_ns()))

    def record(self,kind,data,jpeg=b''):
        # Serialize now: callers subsequently mutate command and frame objects.
        with self.lock:
            if self.closed:return
            try:
                self.sequence+=1
                packet=dict(kind=kind,data=data,event_index=self.sequence,wall_ns=time.time_ns())
                encoded=json.dumps(packet,allow_nan=False,separators=(',',':')).encode()
                self.pending.put_nowait((encoded,jpeg))
            except (queue.Full,ValueError,TypeError) as error:
                self.errors.append(type(error).__name__+': '+str(error))

    def run(self):
        try:
            with (self.root/'input.mjpg').open('wb') as images,(self.root/'events.jsonl').open('wb') as events:
                while True:
                    item=self.pending.get()
                    if item is None:break
                    encoded,jpeg=item;packet=json.loads(encoded)
                    if packet['kind']=='input':
                        packet.update(jpeg_offset=images.tell(),jpeg_bytes=len(jpeg),
                            jpeg_sha256=hashlib.sha256(jpeg).hexdigest())
                        images.write(jpeg)
                    events.write(json.dumps(packet,separators=(',',':')).encode()+b'\n')
                for stream in (images,events):stream.flush();os.fsync(stream.fileno())
        except BaseException as error:
            self.errors.append(type(error).__name__+': '+str(error))

    def close(self):
        with self.lock:
            if self.closed:return
            self.closed=True
        # All earlier records precede this sentinel. Seal only after draining.
        deadline=time.monotonic()+10
        while self.thread.is_alive() and time.monotonic()<deadline:
            try:self.pending.put(None,timeout=.1);break
            except queue.Full:continue
        self.thread.join(max(0,deadline-time.monotonic()))
        if self.thread.is_alive():self.errors.append('Archive writer did not finish')
        value=dict(version=1,session=self.session,sealed=not self.thread.is_alive(),
            errors=self.errors,records_requested=self.sequence,
            files={p.name:digest(p) for p in (self.root/'input.mjpg',self.root/'events.jsonl')
                if p.exists() and not self.thread.is_alive()},
            scoring='Warmup is retained for replay and excluded from measurement scores.',
            boundary='Starts at the fresh bridge/algorithm session; an in-flight final input may be explicitly unmatched at stop.')
        (self.root/'manifest.json').write_text(json.dumps(value,indent=2))


def audit(root,start=None,end=None,experiment=None):
    """Check full history and byte identity, without needing ROS or target truth."""
    from background_pipeline import jpeg_size
    root=Path(root);errors=[]
    manifest=json.loads((root/'manifest.json').read_text())
    if not manifest.get('sealed') or manifest['errors']:errors+=manifest['errors'] or ['Unsealed archive']
    for name,expected in manifest['files'].items():
        if digest(root/name)!=expected:errors.append('Hash mismatch: '+name)
    inputs={};results={};events=[];offset=0;previous_seq=None;previous_stamp=None;index=0;latest_receipt=None
    with (root/'input.mjpg').open('rb') as images,(root/'events.jsonl').open() as source:
        for line in source:
            packet=json.loads(line);index+=1
            if packet['event_index']!=index:errors.append('Discontinuous event index')
            data=packet['data'];kind=packet['kind']
            if kind=='input':
                key=(data['session'],data['seq'],data['stamp_ns'])
                if key in inputs:errors.append('Duplicate input')
                if data['session']!=manifest['session']:errors.append('Mixed algorithm sessions')
                if previous_seq is not None and (data['seq']<=previous_seq or data['stamp_ns']<=previous_stamp):
                    errors.append('Nonmonotonic algorithm input')
                previous_seq=data['seq'];previous_stamp=data['stamp_ns']
                if packet['jpeg_offset']!=offset:errors.append('Discontinuous JPEG archive')
                jpeg=images.read(packet['jpeg_bytes']);offset+=len(jpeg)
                if hashlib.sha256(jpeg).hexdigest()!=packet['jpeg_sha256']:errors.append('Input JPEG mismatch')
                try:
                    if jpeg_size(jpeg)!=(data['calibration']['width'],data['calibration']['height']):errors.append('Input dimensions changed')
                except ValueError as error:errors.append(str(error))
                inputs[key]=packet
            elif kind=='result':
                frame=data['frame'];key=(frame['session'],frame['seq'],frame['stamp_ns'])
                if key not in inputs or key in results:errors.append('Unmatched or duplicate result')
                results[key]=packet
                if 'receipt_ns' in data:latest_receipt=max(latest_receipt or data['receipt_ns'],data['receipt_ns'])
            else:
                events.append(packet)
                if kind=='warmup_park' and data.get('receipt_boundary'):
                    if latest_receipt is None or latest_receipt>data['stamp_ns']:
                        errors.append('Warmup parked before final logical receipt')
                    if data['pipeline'].get('pending_command_receipt_ns') is not None:
                        errors.append('Warmup parked before final command acknowledgement')
        if images.read(1):errors.append('Unindexed image bytes')
    if index!=manifest['records_requested']:errors.append('Missing archive records')
    if not inputs:errors.append('No algorithm inputs archived')
    # A stop can interrupt the final request. Missing warmup/measured results
    # remain invalid; only the explicitly reported post-measurement tail may end.
    unmatched=[list(k) for k in inputs if k not in results]
    if start is not None:
        if not any(k[2]<start for k in inputs):errors.append('Missing warmup input history')
        if end is None:errors.append('Missing measurement end boundary')
        elif any(k[2]<=end for k in inputs if k not in results):errors.append('Missing result before measurement end')
    else:errors.append('Missing measurement start boundary')
    observed=0
    if experiment is not None:
        with (Path(experiment)/'telemetry.jsonl').open() as stream:
            for line in stream:
                packet=json.loads(line)
                if packet['kind']!='observation':continue
                data=packet['data'];frame=data['frame'];key=(frame['session'],frame['seq'],frame['stamp_ns'])
                if start is None or end is None or not start<=frame['stamp_ns']<=end:continue
                observed+=1
                if key not in results:errors.append('Measured observation absent from full history');continue
                recorded=results[key]['data']
                if any(recorded.get(k)!=data.get(k) for k in ('frame','result','receipt_ns','fire_forwarded')):
                    errors.append('Measured observation differs from session archive')
        if not observed:errors.append('No measured observations verified')
        experiment=Path(experiment)
        config=json.loads((experiment/'manifest.json').read_text())
        if config.get('input_contract')=='aligned-v1':
            contract=[p['data'] for p in events if p['kind']=='warmup_contract']
            if len(contract)!=1:errors.append('Missing or duplicate counted warmup boundary')
            else:
                warm=contract[0];count=config['warmup_frames']
                ordered=list(inputs.items())
                if warm['completed_frames']!=count or len(warm['inputs'])!=count:errors.append('Warmup count differs from declared contract')
                if [(v['data']['seq'],v['data']['stamp_ns']) for k,v in ordered[:count]] != [(p['seq'],p['stamp_ns']) for p in warm['inputs']]:
                    errors.append('Hidden or missing input before counted warmup')
                if [p.get('sensor',{}).get('index') for p in warm['inputs']]!=list(range(count)):
                    errors.append('Warmup did not consume the contiguous native sensor prefix')
                sensors=[v['data'].get('sensor') for k,v in ordered]
                if any(not s or s.get('seed')!=config['sensor_seed'] or s.get('reset_id')!=manifest['session'] for s in sensors):
                    errors.append('Missing or mismatched native sensor stream')
                elif sensors:
                    if sensors[0]['index']!=0:errors.append('First native sensor input was skipped')
                    if any(b['index']<=a['index'] for a,b in zip(sensors,sensors[1:])):errors.append('Nonmonotonic native sensor input index')
                scored={json.loads(line)['stamp_ns'] for line in (experiment/'frames.jsonl').open()
                    if json.loads(line).get('measurement')}
                measured={k[2] for k in results if start<=k[2]<=end}
                if scored!=measured:errors.append('Physical measurement input/score coverage mismatch')
                for p in results.values():
                    data=p['data'];f=data['frame'];r=data['result']
                    clocks=r.get('evaluation_clocks',{})
                    if r.get('valid') and not all(type(clocks.get(k)) is int for k in ('aim_now_ns','fire_now_ns','pose_now_ns')):
                        errors.append('Valid result lacks actual per-stage clocks');break
    return dict(passed=not errors,errors=errors,inputs=len(inputs),results=len(results),
        warmup_inputs=sum(k[2]<start for k in inputs) if start is not None else None,
        unmatched_tail=unmatched,events=events,session=manifest['session'],measurement_observations_verified=observed)
