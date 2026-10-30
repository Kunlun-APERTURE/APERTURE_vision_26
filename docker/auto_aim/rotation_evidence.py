"""Offline, causal phase-fit feasibility. Truth is read only after fitting."""
import bisect
from collections import defaultdict,deque
import json
import math
from pathlib import Path

OPS=Path(__file__).resolve().parent
ROOT=OPS/'experiments/rotation-evidence-20261027'


def stat(values):
    v=sorted(values)
    return dict(n=len(v),p50=v[max(0,math.ceil(.5*len(v))-1)] if v else None,
                p95=v[max(0,math.ceil(.95*len(v))-1)] if v else None)


def main():
    c=json.loads((ROOT/'contract.json').read_text());traces=[]
    for source in c['sources']:
        folder=OPS/'experiments'/source['run_id'];state=json.loads((folder/'run-state.json').read_text())
        history=deque();last=None;phase=None;results=[]
        for line in (folder/'session-evidence/events.jsonl').open():
            packet=json.loads(line)
            if packet['kind']!='result':continue
            frame,result=packet['data']['frame'],packet['data']['result'];t=frame['stamp_ns']*1e-9
            poses=[x for x in result.get('continuity',{}).get('diagnosis_poses',[]) if x.get('pose_valid') and x.get('rms',99)<=8 and 'yaw' in x]
            value=None;concentration=0
            if poses:
                cs=sum(math.cos(4*x['yaw']) for x in poses)/len(poses);sn=sum(math.sin(4*x['yaw']) for x in poses)/len(poses)
                concentration=math.hypot(cs,sn)
                if concentration>=c['minimum_phase_concentration']:value=math.atan2(sn,cs)/4
            if last is not None and t-last>c['max_gap_s']:history.clear();phase=None
            fit=None;rms=None
            if value is not None:
                phase=value if phase is None else phase+math.remainder(value-phase,math.pi/2)
                history.append((t,phase));last=t
                while history and t-history[0][0]>c['window_s']:history.popleft()
                if len(history)>=c['minimum_samples'] and t-history[0][0]>=c['minimum_span_s']:
                    mt=sum(x[0]-t for x in history)/len(history);mp=sum(x[1] for x in history)/len(history)
                    den=sum((x[0]-t-mt)**2 for x in history)
                    fit=sum((x[0]-t-mt)*(x[1]-mp) for x in history)/den
                    rms=math.sqrt(sum((x[1]-mp-fit*(x[0]-t-mt))**2 for x in history)/len(history))
            if state['measurement_start_stamp_ns']<=frame['stamp_ns']<=state['measurement_end_stamp_ns']:
                results.append(dict(stamp_ns=frame['stamp_ns'],fit_w=fit,rms=rms,qualified=fit is not None and rms<=c['maximum_fit_rms_rad'],
                  ekf_w=(result.get('filter_state') or [None]*8)[7],concentration=concentration))
        truth=[]
        for line in (folder/'telemetry.jsonl').open():
            packet=json.loads(line)
            if packet['kind']=='robots':
                x=next(x for x in packet['data']['robots'] if x['id']==1);truth.append((packet['data']['stamp_ns'],x['yaw_rate']))
        times=[x[0] for x in truth]
        for row in results:
            i=bisect.bisect_left(times,row['stamp_ns']);near=min((j for j in (i-1,i) if 0<=j<len(times)),key=lambda j:abs(times[j]-row['stamp_ns']))
            if abs(times[near]-row['stamp_ns'])>100_000_000:continue
            row.update(source=source['run_id'],scene=source['scene'],variant=source['variant'],truth_w=truth[near][1]);traces.append(row)
    groups=[]
    for variant in ('baseline','BS4'):
        for scene in sorted({r['scene'] for r in traces}):
            rr=[r for r in traces if r['scene']==scene and r['variant']==variant];qualified=[r for r in rr if r['qualified']]
            groups.append(dict(variant=variant,scene=scene,frames=len(rr),qualified=len(qualified),
              fit_error=stat([abs(r['fit_w']-r['truth_w']) for r in qualified]),
              ekf_error_on_same_frames=stat([abs(r['ekf_w']-r['truth_w']) for r in qualified if r['ekf_w'] is not None]),
              strong_rotation_fraction=sum(abs(r['fit_w'])>=c['strong_rotation_radps'] for r in qualified)/len(qualified) if qualified else None))
    (ROOT/'traces.json').write_text(json.dumps(traces,indent=2)+'\n')
    (ROOT/'result.json').write_text(json.dumps(dict(groups=groups,no_live_trials=True,truth_only_after_estimation=True),indent=2)+'\n')
    for row in groups:print(json.dumps(row),flush=True)


if __name__=='__main__':main()
