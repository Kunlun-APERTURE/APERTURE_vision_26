"""Frozen behavioural timing. This file contains no scene truth or commands."""
import hashlib
import json
import math
from pathlib import Path
import statistics

SCENES=('hf_static_8','hf_uniform_3','hf_turn90_3','hf_spin_neg')


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate(value):
    if not isinstance(value,dict) or value.get('version')!=1 or value.get('kind') not in ('fixed','trace'):
        raise ValueError('Expected timing profile version 1, fixed or trace')
    allowed={'version','kind','name','warmup_frames','control_hz','input_delay_ns','processing_delay_ns',
        'return_delay_ns','pose_delay_ns','preparation_reset','continuous_measurement','warmup_receipt_boundary','scenes','provenance',
        'capture_interval_ns','preparation_release_home'}
    if set(value)-allowed:raise ValueError('Unexpected timing profile field; timing profiles cannot contain commands or truth')
    if 'preparation_reset' in value and type(value['preparation_reset']) is not bool:raise ValueError('Invalid preparation reset')
    if 'preparation_release_home' in value and type(value['preparation_release_home']) is not bool:raise ValueError('Invalid home release')
    if 'continuous_measurement' in value and type(value['continuous_measurement']) is not bool:raise ValueError('Invalid continuous measurement')
    if 'warmup_receipt_boundary' in value:
        if type(value['warmup_receipt_boundary']) is not bool:raise ValueError('Invalid warmup receipt boundary')
        if value['warmup_receipt_boundary'] and not value.get('continuous_measurement'):raise ValueError('Receipt boundary requires continuous measurement')
    for k in ('input_delay_ns','processing_delay_ns'):
        if type(value.get(k)) is not int or not 0<=value[k]<=200_000_000:raise ValueError('Invalid '+k)
    for k in ('return_delay_ns','pose_delay_ns'):
        if k in value and (type(value[k]) is not int or not 0<=value[k]<=200_000_000):raise ValueError('Invalid '+k)
    if type(value.get('warmup_frames')) is not int or not 1<=value['warmup_frames']<=300:raise ValueError('Invalid warmup count')
    hz=value.get('control_hz')
    if isinstance(hz,bool) or not isinstance(hz,(int,float)) or not math.isfinite(hz) or not 120<=hz<=2000:raise ValueError('Invalid control frequency')
    if 'capture_interval_ns' in value and (type(value['capture_interval_ns']) is not int
            or not 8_333_333<=value['capture_interval_ns']<=100_000_000):raise ValueError('Invalid capture interval')
    if value['kind']=='trace':
        if set(value.get('scenes',{}))!=set(SCENES):raise ValueError('Trace requires all four calibration scenes')
        for scene,trace in value['scenes'].items():
            if set(trace)-{'control_intervals_ns','capture_intervals_ns','input_delays_ns','processing_delays_ns',
                    'return_delays_ns','pose_delays_ns','source_run'}:
                raise ValueError('Trace can contain timing only')
            for name in ('control_intervals_ns','capture_intervals_ns','input_delays_ns','processing_delays_ns'):
                rows=trace.get(name)
                if not isinstance(rows,list) or not rows or len(rows)>100000:raise ValueError('Empty or excessive trace '+name)
                if any(type(v) is not int or v<0 or v>250_000_000 for v in rows):raise ValueError('Invalid timing value')
                if name.endswith('intervals_ns') and any(v==0 for v in rows):raise ValueError('Zero interval')
            for name in ('return_delays_ns','pose_delays_ns'):
                if name in trace and (not isinstance(trace[name],list) or not trace[name] or len(trace[name])>100000
                        or any(type(v) is not int or not 0<=v<=250_000_000 for v in trace[name])):
                    raise ValueError('Invalid timing value: '+name)
    return value


def load_profile(path):return validate(json.loads(Path(path).read_text()))


class Schedule:
    def __init__(self,profile,scene):
        self.profile=validate(profile);self.trace=profile.get('scenes',{}).get(scene,{})
        self.step_index=self.image_index=0

    def at(self,name,index,default):
        rows=self.trace.get(name)
        return rows[index%len(rows)] if rows else default

    def control_delta(self):
        n=self.step_index;self.step_index+=1
        # Rational accumulation avoids a drifting 120/30 Hz clock.
        fixed=round((n+1)*1e9/self.profile['control_hz'])-round(n*1e9/self.profile['control_hz'])
        return self.at('control_intervals_ns',n,fixed)

    def image_interval(self):
        n=self.image_index;self.image_index+=1
        fixed=self.profile.get('capture_interval_ns',round((n+1)*1e9/30)-round(n*1e9/30))
        return self.at('capture_intervals_ns',n,fixed)

    def delays(self,index):
        return (self.at('input_delays_ns',index,self.profile['input_delay_ns']),
            self.at('processing_delays_ns',index,self.profile['processing_delay_ns']))

    def return_delay(self,index):
        return self.at('return_delays_ns',index,self.profile.get('return_delay_ns',0))

    def pose_delay(self,index):
        return self.at('pose_delays_ns',index,self.profile.get('pose_delay_ns',0))


def complete_timing(observation):
    """Recover decision and receipt timing without reading target state.

    pipeline_ms starts AFTER JPEG decode. The firing diagnostic's pose age was
    measured on the shared host clock at the decision, so its timestamp plus
    age recovers that event and includes decode/transport overhead as well.
    """
    frame=observation['frame'];result=observation['result']
    sent=frame['sent_ns'];pose=frame['latest_muzzle_pose']['stamp_ns']
    age=result['fire_check']['pose_age_s']
    if not isinstance(age,(int,float)) or not math.isfinite(age) or not 0<=age<=.25:
        raise ValueError('Missing or invalid decision-time pose age')
    decision=pose+round(age*1e9);receipt=observation['receipt_ns']
    if not frame['stamp_ns']<=sent<=decision<=receipt or pose>sent:
        raise ValueError('Calibration timestamps are not causally ordered')
    return dict(input=sent-frame['stamp_ns'],processing=decision-sent,
        return_delay=receipt-decision,pose_delay=sent-pose,
        excluded_from_pipeline=decision-sent-round(result['pipeline_ms']*1e6))


def calibrate(folders,destination,*,name_suffix='v1',complete_processing=False):
    """Generate both candidates using times only; scores cannot affect selection."""
    from report import load,write
    groups={s:[] for s in SCENES};all_input=[];all_processing=[];all_hz=[];warm=[]
    all_return=[];all_pose=[];all_excluded=[]
    for folder in map(Path,folders):
        manifest=load(folder/'manifest.json');scene=manifest['scene']
        if scene not in groups:continue
        state=load(folder/'run-state.json');start=state['measurement_start_stamp_ns'];end=state['measurement_end_stamp_ns']
        observations=[];traces=[]
        with (folder/'telemetry.jsonl').open() as stream:
            for line in stream:
                packet=json.loads(line)
                if packet['kind']=='observation':observations.append(packet['data'])
                if packet['kind']=='gimbal_trace' and start<=packet['data']['stamp_ns']<=end:traces.append(packet['data'])
        measured=[o for o in observations if start<=o['frame']['stamp_ns']<=end]
        if len(measured)<30:raise ValueError('Insufficient calibration image samples: '+folder.name)
        control=[]
        exact=folder/'control-timing.jsonl'
        if exact.exists():
            stamps=[json.loads(l)['stamp_ns'] for l in exact.open()]
            control=[b-a for a,b in zip(stamps,stamps[1:]) if start<=a<b<=end and b-a<50_000_000]
        if not control:
            for a,b in zip(traces,traces[1:]):
                steps=b['control_steps']-a['control_steps'];dt=b['stamp_ns']-a['stamp_ns']
                if 0<steps<200 and 0<dt<100_000_000:control.extend([round(dt/steps)]*steps)
        if not control:raise ValueError('Missing control timing: '+folder.name)
        stamps=[o['frame']['stamp_ns'] for o in measured]
        processing=[max(0,round(o['result'].get('pipeline_ms',0)*1e6)) for o in measured]
        inputs=[max(0,o['frame'].get('sent_ns',o['receipt_ns']-processing[i])-o['frame']['stamp_ns']) for i,o in enumerate(measured)]
        returns=[];poses=[]
        if complete_processing:
            complete=[complete_timing(o) for o in measured]
            inputs=[r['input'] for r in complete];processing=[r['processing'] for r in complete]
            returns=[r['return_delay'] for r in complete];poses=[r['pose_delay'] for r in complete]
            all_return+=returns;all_pose+=poses;all_excluded += [r['excluded_from_pipeline'] for r in complete]
        hz=1e9/statistics.mean(control);all_hz.append(hz);all_input+=inputs;all_processing+=processing
        warm.append(sum(o['frame']['stamp_ns']<start for o in observations))
        trace=dict(control_intervals_ns=control,capture_intervals_ns=[b-a for a,b in zip(stamps,stamps[1:])],
            input_delays_ns=inputs,processing_delays_ns=processing,source_run=folder.name)
        if complete_processing:trace.update(return_delays_ns=returns,pose_delays_ns=poses)
        groups[scene].append(dict(hz=hz,age=statistics.median(inputs),trace=trace,
            provenance=dict(run=folder.name,manifest_sha256=digest(folder/'manifest.json'),
                telemetry_sha256=digest(folder/'telemetry.jsonl'),control_source='exact_tf' if exact.exists() else 'interpolated_control_counter')))
    if any(len(rows)<3 for rows in groups.values()):raise ValueError('Need three UI calibration runs per scene')
    base=dict(version=1,kind='fixed',name='ui-fixed-'+name_suffix,control_hz=max(120,round(statistics.median(all_hz)/120)*120),
        input_delay_ns=round(statistics.median(all_input)),processing_delay_ns=round(statistics.median(all_processing)),
        warmup_frames=max(30,min(120,round(statistics.median(warm)) or 60)),
        provenance=dict(selection='Timing-only calibration; independent UI repeats required for effect acceptance',
            runs=[r['provenance'] for rows in groups.values() for r in rows]))
    if complete_processing:
        base.update(return_delay_ns=round(statistics.median(all_return)),pose_delay_ns=round(statistics.median(all_pose)))
        base['provenance'].update(processing_clock='Own-pose stamp + recorded decision-time pose age; includes JPEG decode',
            return_clock='Bridge receipt minus decision time',pose_clock='Send time minus own-pose stamp',
            missing_pipeline_median_ns=round(statistics.median(all_excluded)))
    traced=dict(base,kind='trace',name='ui-trace-'+name_suffix,scenes={})
    for scene,rows in groups.items():
        center=statistics.median(r['hz'] for r in rows);age=statistics.median(r['age'] for r in rows)
        selected=min(rows,key=lambda r:(abs(r['hz']-center),abs(r['age']-age),r['trace']['source_run']))
        traced['scenes'][scene]=selected['trace']
    destination=Path(destination);destination.mkdir(parents=True,exist_ok=True)
    for profile in (base,traced):validate(profile);write(destination/(profile['name']+'.json'),profile)
    return [destination/(p['name']+'.json') for p in (base,traced)]
