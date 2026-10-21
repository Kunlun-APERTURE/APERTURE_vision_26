#!/usr/bin/env python3
"""Select continuity noise on old observations; held-out repeats never tune it.

Only observation packets are sent to the C++ algorithm. Truth remains here.
This is an estimator replay, not a new closed-loop shooting measurement.
"""
import argparse
import bisect
import collections
import hashlib
import json
import math
from pathlib import Path
import statistics
import subprocess
import uuid

OPS=Path(__file__).resolve().parent
ROOT=OPS.parent.parent


def load(p):return json.loads(p.read_text())
def write(p,v):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v,ensure_ascii=False,allow_nan=False,indent=2))
def q(values,f=.5):
    s=sorted(v for v in values if v is not None and math.isfinite(v))
    # Empirical CDF / nearest rank: at least f of the observations lie at or
    # below this value. In particular, P90 of two events is the slower event.
    return s[max(0,min(len(s)-1,math.ceil(len(s)*f)-1))] if s else None
def wrap(x):return math.remainder(x,math.tau)


class Interpolate:
    def __init__(self,rows):self.rows=sorted(rows,key=lambda r:r[0]);self.times=[r[0] for r in self.rows]
    def at(self,t):
        i=bisect.bisect_left(self.times,t)
        if i<len(self.times) and self.times[i]==t:return self.rows[i][1]
        if i==0 or i==len(self.times):return None
        a,b=self.rows[i-1],self.rows[i]
        if max(t-a[0],b[0]-t)>100_000_000:return None
        f=(t-a[0])/(b[0]-a[0]);return [x+(y-x)*f for x,y in zip(a[1],b[1])]


def prepare(folder):
    state=load(folder/'run-state.json');manifest=load(folder/'manifest.json')
    start,end=state['measurement_start_stamp_ns'],state['measurement_end_stamp_ns']
    observations=[];robots=[];centers=[]
    for line in (folder/'telemetry.jsonl').open():
        p=json.loads(line);d=p['data']
        if p['kind']=='observation':observations.append(line)
        elif p['kind']=='robots':
            by_id={r['id']:r for r in d['robots']}
            if 0 in by_id and 1 in by_id:
                target,ego=by_id[1],by_id[0]
                relative=[v-u for v,u in zip(target['velocity'],ego['velocity'])]
                robots.append((d['stamp_ns'],relative+[target['yaw_rate']]))
        elif p['kind']=='armors':
            plates=[a['position'] for a in d['armors'] if a['robot_id']==1]
            if plates:centers.append((d['stamp_ns'],[sum(p[i] for p in plates)/len(plates) for i in range(3)]))
    metadata={}
    for line in observations:
        obs=json.loads(line)['data'];f=obs['frame'];metadata[f['stamp_ns']]=f
    return ''.join(observations),{'start':start,'end':end,'duration':manifest['duration_s'],
        'scene':manifest['scene'],'metadata':metadata,'velocity':Interpolate(robots),'center':Interpolate(centers)}


def steady(scene,t):
    if scene=='line':return t>=1 and not (t>=4 and t%4<1)
    if scene.startswith('ego_sweep'):return any(a<=t<=b for a,b in [(3,5.5),(7,9.5),(11,13.5)])
    return True


def recovery(rows,truth,start,end):
    events=[];previous=None
    for stamp,vel in truth.rows:
        if not start<=stamp<=end:continue
        if previous and previous[1][1]*vel[1]<0:
            # Candidate zero crossing precedes the first significant opposite
            # sample. Keep a bracketed time, then confirm motion either side.
            before,after=truth.at(stamp-200_000_000),truth.at(stamp+200_000_000)
            confirmed=(before is not None and after is not None and before[1]*after[1]<0
                and abs(before[1])>.15 and abs(after[1])>.15)
            if confirmed and (not events or stamp-events[-1]>500_000_000):events.append(stamp)
        if abs(vel[1])>.001:previous=(stamp,vel)
    recovered=[]
    for stamp in events:
        origin=None;delay=None
        for r in rows:
            t=r['stamp_ns']
            if t<stamp:continue
            if t-stamp>2_000_000_000:break
            v=truth.at(t);state=r.get('filter_state')
            ok=v is not None and abs(v[1])>.1 and state and state[3]*v[1]>0 and abs(state[3]-v[1])<=.15
            if ok:
                if origin is None:origin=t
                if t-origin>=100_000_000:delay=(origin-stamp)*1e-9;break
            else:origin=None
        recovered.append({'stamp_ns':stamp,'recovery_s':delay,'censored_at_s':2. if delay is None else None})
    return recovered


def metrics(rows,context):
    frames=[];xy_errors=[];velocity_errors=[];correct=0;angular=[];jumps=0;previous=None;previous_rate=None
    for r in rows:
        stamp=r['stamp_ns'];meta=context['metadata'].get(stamp)
        if not context['start']<=stamp<=context['end'] or meta is None:continue
        t=(stamp-context['start'])*1e-9;r['time_s']=t
        center=context['center'].at(stamp);v=context['velocity'].at(stamp);state=r.get('filter_state')
        if state and center:
            error=math.hypot(state[0]+meta['gimbal_position_map'][0]-center[0],state[2]+meta['gimbal_position_map'][1]-center[1])
            xy_errors.append(error)
            correct+=r['tracker']=='tracking' and error<=.5
            if v:velocity_errors.append(math.hypot(state[1]-v[0],state[3]-v[1]))
        if r['valid']:
            if previous:
                dt=(stamp-previous['stamp_ns'])*1e-9
                if .015<=dt<=.1:
                    dy=wrap(r['yaw']-previous['yaw']);dp=r['pitch']-previous['pitch']
                    if previous_rate and steady(context['scene'],t):
                        residual=math.degrees(math.hypot(wrap(dy-previous_rate[0]*dt),dp-previous_rate[1]*dt))
                        angular.append(residual);jumps+=residual>1
                    previous_rate=(dy/dt,dp/dt)
                else:previous_rate=None
            previous=r
        else:previous=None;previous_rate=None
        frames.append(r)
    events=recovery(frames,context['velocity'],context['start'],context['end'])
    return {'quantile_method':'nearest_rank','frames':len(frames),'correct_tracking_fraction':correct/len(frames) if frames else 0,
        'center_error_p95_m':q(xy_errors,.95),'velocity_error_p95_mps':q(velocity_errors,.95),
        'steady_angle_residual_p95_deg':q(angular,.95),'steady_angle_gt_1deg_frames':jumps,
        'steady_angle_samples':len(angular),'recovery_events':events,
        'recovery_p90_s':q([e['recovery_s'] if e['recovery_s'] is not None else 2 for e in events],.9)}


def replay(folder,profile,translation,rotation,output):
    dest=output/(profile+f'_q{translation:g}_{rotation:g}')/folder.name
    signature=hashlib.sha256((folder/'telemetry.jsonl').read_bytes()).hexdigest()
    binary_signature=hashlib.sha256((ROOT/'sp_vision_25/build/simulator/sim_auto_aim').read_bytes()).hexdigest()
    config_signature=hashlib.sha256((ROOT/'sp_vision_25/configs/simulator.yaml').read_bytes()).hexdigest()
    metrics_signature=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if (dest/'metrics.json').exists():
        old=load(dest/'metrics.json')
        if (old.get('telemetry_sha256')==signature and old.get('binary_sha256')==binary_signature
            and old.get('algorithm_config_sha256')==config_signature
            and old.get('metrics_code_sha256')==metrics_signature):return old
    source,context=prepare(folder)
    runtime='build/sim/replay-'+uuid.uuid4().hex+'.yaml'
    command=['docker','exec','-i','--user','1000:1000','--workdir','/workspace/sp_vision_25','sp_vision_sunshine',
        'build/simulator/sim_auto_aim','--replay=/dev/stdin','--replay-output=/dev/stdout',
        '--runtime='+runtime,'--algorithm-profile='+profile,
        '--translation-variance='+str(translation),'--rotation-variance='+str(rotation)]
    try:result=subprocess.run(command,input=source,capture_output=True,text=True,check=True)
    finally:(ROOT/'sp_vision_25'/runtime).unlink(missing_ok=True)
    rows=[json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
    dest.mkdir(exist_ok=True,parents=True);(dest/'results.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    m=metrics(rows,context);m.update(run_id=folder.name,scene=context['scene'],telemetry_sha256=signature,
        binary_sha256=binary_signature,algorithm_config_sha256=config_signature,metrics_code_sha256=metrics_signature)
    write(dest/'metrics.json',m);return m


def aggregate(rows):
    events=[e for r in rows for e in r['recovery_events']]
    return {'quantile_method':'nearest_rank','correct_tracking_fraction':sum(r['correct_tracking_fraction']*r['frames'] for r in rows)/sum(r['frames'] for r in rows),
        'steady_angle_gt_1deg_frames':sum(r['steady_angle_gt_1deg_frames'] for r in rows),
        'steady_angle_samples':sum(r['steady_angle_samples'] for r in rows),
        'center_error_p95_m_median':q([r['center_error_p95_m'] for r in rows]),
        'velocity_error_p95_mps_median':q([r['velocity_error_p95_mps'] for r in rows]),
        'recovery_p90_s':q([e['recovery_s'] if e['recovery_s'] is not None else 2 for e in events],.9),
        'recovery_events':len(events),'recovery_failures':sum(e['recovery_s'] is None for e in events)}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--batch',type=Path,nargs='+',required=True)
    p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    runs=[]
    for batch in args.batch:
        runs.extend((batch.parent/r['run_id'],r['repeat']) for r in load(batch)['runs'] if r['verdict']!='invalid')
    selection=[f for f,n in runs if n in (1,2)];holdout=[f for f,n in runs if n==3]
    baseline=aggregate([replay(f,'legacy',30,10,args.output) for f in selection]);write(args.output/'baseline.json',baseline)
    candidates=[]
    for translation in (10,30,100):
        for rotation in (1,10,100):
            m=aggregate([replay(f,'tracking_v2',translation,rotation,args.output) for f in selection])
            eligible=(m['correct_tracking_fraction']>=baseline['correct_tracking_fraction']-.02
                and m['recovery_p90_s'] is not None and m['recovery_p90_s']<=.3
                and m['recovery_p90_s']<=baseline['recovery_p90_s']+.05)
            c={'translation':translation,'rotation':rotation,'metrics':m,'eligible':eligible};candidates.append(c)
            write(args.output/'grid.json',candidates);print(json.dumps(c),flush=True)
    eligible=[c for c in candidates if c['eligible']]
    pool=eligible or candidates
    best=min(pool,key=lambda c:(c['metrics']['steady_angle_gt_1deg_frames'],c['metrics']['center_error_p95_m_median'],c['translation'],c['rotation']))
    validation={profile:aggregate([replay(f,profile,best['translation'],best['rotation'],args.output) for f in holdout]) for profile in ('legacy','tracking_v2')}
    write(args.output/'selection.json',{'selected':best,'selection_gate_passed':bool(eligible),
        'selection_runs':[f.name for f in selection],'held_out_runs':[f.name for f in holdout],
        'validation':validation,'policy':'Repeats 1/2 select; repeat 3 is held out; no hit outcome used in selection. If all gates fail, selected is diagnostic only and must not be promoted.'})
    print('Selected',json.dumps(best),'gate',bool(eligible),flush=True)


if __name__=='__main__':main()
