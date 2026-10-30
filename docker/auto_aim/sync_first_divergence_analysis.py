"""Causal checks over unchanged JPEGs; simulator truth is read only afterwards."""
import argparse
import bisect
import json
import math
from pathlib import Path
import statistics

import manage
from report import load,write
from sync_first_divergence import STATE_FIELDS,archive


def primary(folder):
    return [p for line in (folder/'events.jsonl').open() if (p:=json.loads(line))['stage']=='primary']


def differences(left,right):
    if len(left)!=len(right):raise ValueError('Different replay lengths')
    return {name:sum(a['result'].get(name)!=b['result'].get(name) for a,b in zip(left,right)) for name in STATE_FIELDS}


def first_divergence(left,right):
    for index,(a,b) in enumerate(zip(left,right)):
        different=[name for name in STATE_FIELDS if a['result'].get(name)!=b['result'].get(name)]
        if different:
            return dict(frame_index=index,seq=a['frame']['seq'],stamp_ns=a['frame']['stamp_ns'],fields=different,
                left_roi=a['result'].get('detector_summary'),right_roi=b['result'].get('detector_summary'))
    return None


def truth_centers(source):
    stamps=[];centers=[]
    for line in (source/'telemetry.jsonl').open():
        packet=json.loads(line)
        if packet['kind']!='armors':continue
        data=packet['data'];target=[r['position'] for r in data['armors'] if r['robot_id']==1]
        if not target:continue
        stamps.append(data['stamp_ns']);centers.append([sum(p[k] for p in target)/len(target) for k in range(3)])
    def at(stamp):
        index=bisect.bisect_left(stamps,stamp)
        if index<len(stamps) and stamps[index]==stamp:return centers[index]
        if index==0 or index==len(stamps):return None
        a,b=stamps[index-1:index+1]
        if b-a>100_000_000:return None
        fraction=(stamp-a)/(b-a)
        return [x+(y-x)*fraction for x,y in zip(centers[index-1],centers[index])]
    return at


def error_trace(source,records):
    start=load(source/'run-state.json')['measurement_start_stamp_ns'];truth=truth_centers(source);result=[]
    measured={r['stamp_ns']:r for line in (source/'frames.jsonl').open() if (r:=json.loads(line)).get('measurement')}
    for packet in records:
        frame=packet['frame'];r=packet['result'];scored=measured.get(frame['stamp_ns'])
        if scored is None:continue
        t=scored['time']
        center=truth(frame['stamp_ns']);track=r.get('track_estimate')
        if not 0<=t<2 or center is None or track is None:continue
        xy=[track['center'][k]+frame['gimbal_position_map'][k] for k in (0,1)]
        error=math.hypot(xy[0]-center[0],xy[1]-center[1])
        result.append(dict(stamp_ns=frame['stamp_ns'],time_s=t,center_xy_error_m=error,
            tracker=r['tracker'],correct_center=error<=.5,
            recorded_center_error_m=measured.get(frame['stamp_ns'],{}).get('center_xy_error_m')))
    return result


def analyze(root):
    pilot=load(root/'study.json');confirmation=load(root/'confirmation/confirmation.json')
    verification=load(root/'verification/verification.json')
    if not all(x.get('complete') for x in (pilot,confirmation,verification)):raise ValueError('Complete all causal stages first')
    cold_reproduced=[r for r in pilot['results'] if r['case'].startswith('backend-cold')]
    assert len(cold_reproduced)==2 and all(all(v['different_frames']==0 for v in r['differences_from_recorded'].values()) for r in cold_reproduced)
    restored=[];clock_deltas=[]
    for entry in confirmation['results']:
        if not entry['case'].endswith('_primed'):continue
        cold=next(r for r in confirmation['results'] if r['source_run']==entry['source_run'] and r['case'].endswith('_cold'))
        assert entry['primary_input_sha256']==cold['primary_input_sha256']
        c=primary(root/'confirmation'/cold['case']);p=primary(root/'confirmation'/entry['case'])
        restored_fields=('observations','tracker','continuity','filter_state','track_estimate','selected_plate_id','fire_advice')
        assert all(entry['differences_from_recorded'][name]['different_frames']==0 for name in restored_fields),entry['case']
        restored.append(dict(scene=entry['scene'],repeat_id=entry['repeat_id'],frames=entry['frames'],
            roi=entry['prime_tile'],restored_fields=list(restored_fields),
            cold_filter_different_frames=cold['differences_from_recorded']['filter_state']['different_frames'],
            first_cache_intervention_difference=first_divergence(c,p)))
        source=manage.OPS/'experiments'/entry['source_run'];_,original=archive(source)
        clock_deltas.append(dict(case=entry['case'],
            max_yaw_error_deg=max(abs(a['result']['yaw']-b['result']['yaw'])*180/math.pi for a,(_,b) in zip(p,original)),
            max_prediction_horizon_delta_us=max(abs(a['result'].get('prediction_horizon_s',0)-b['result'].get('prediction_horizon_s',0))*1e6 for a,(_,b) in zip(p,original))))
    assert len(restored)==6
    reset_proofs=[];default_compatibility=[]
    for current in verification['results']:
        if not current['case'].endswith('_session-cold'):continue
        base=current['case'].removesuffix('session-cold')
        other=next(r for r in verification['results'] if r['case']==base+'session-primed')
        ordinary=next(r for r in verification['results'] if r['case']==base+'process-cold')
        assert current['primary_input_sha256']==other['primary_input_sha256']==ordinary['primary_input_sha256']
        cold=primary(root/'verification'/current['case']);primed=primary(root/'verification'/other['case'])
        process=primary(root/'verification'/ordinary['case'])
        d=differences(cold,primed);legacy=differences(cold,process)
        assert not any(d.values()) and not any(legacy.values()),(current['case'],d,legacy)
        assert cold[0]['result']['detector_reset'] and primed[0]['result']['detector_reset']
        assert all(not r['result']['detector_reset'] for r in cold[1:]+primed[1:])
        reset_proofs.append(dict(case=base,frames=len(cold),input_sha256=current['primary_input_sha256'],
            history_erased=d,cold_algorithm_unchanged=legacy))
        if current['case'].startswith('ui_'):
            old=next(r for r in confirmation['results'] if r['source_run']==current['source_run'] and r['case'].endswith('_cold'))
            prior=primary(root/'confirmation'/old['case']);dd=differences(prior,process)
            assert not any(dd.values()),(current['case'],dd)
            default_compatibility.append(dict(case=base,frames=len(process),differences=dd))
        else:
            assert not any(v['different_frames'] for v in ordinary['differences_from_recorded'].values())
            default_compatibility.append(dict(case=base,frames=len(process),differences='Exact recorded backend output reproduced'))
    assert len(reset_proofs)==8
    error_effect=[]
    for name in ('backend-cold-0','backend-primed-0'):
        case=load(root/name/'result.json');source=manage.OPS/'experiments'/case['source_run']
        trace=error_trace(source,primary(root/name));write(root/(name+'-error.json'),trace)
        for a,b in ((0,1),(1,2)):
            rr=[r for r in trace if a<=r['time_s']<b]
            error_effect.append(dict(case=name,time_range_s=[a,b],frames=len(rr),
                median_center_xy_error_m=statistics.median(r['center_xy_error_m'] for r in rr),
                correct_center_fraction=sum(r['correct_center'] for r in rr)/len(rr),
                limitation='Fixed recorded camera trajectory; counterfactual output is not fed back to simulator.'))
    live=load(root/'history-live.json');live_rows=[]
    if live:
        for mode,entry in live['modes'].items():
            for r in entry['runs']:
                folder=manage.OPS/'experiments'/r['run_id'];audit=load(folder/'session-evidence-audit.json')
                archive_rows=[p for line in (folder/'session-evidence/events.jsonl').open() if (p:=json.loads(line))['kind']=='result']
                first=archive_rows[0]['data']['result'];m=load(folder/'manifest.json')
                reset_ok=(audit.get('detector_session_reset_verified') and first.get('detector_reset') is True
                    and not first['detector_summary']['cached_tile'] and m['execution']['detector_history']=='session'
                    and sum(p['data']['result'].get('detector_reset') is True for p in archive_rows)==1)
                assert reset_ok,r
                live_rows.append(dict(mode=mode,**r,reset_verified=True,warmup_inputs=audit['warmup_inputs'],
                    initial_roi=first['detector_summary']['tile_roi'],metrics=load(folder/'summary.json')['metrics']))
    result=dict(passed=True,recorded_backend_exact_replays=len(cold_reproduced),restored_ui_runs=restored,
        restored_ui_frames=sum(r['frames'] for r in restored),session_reset_proofs=reset_proofs,
        default_compatibility=default_compatibility,clock_reconstruction=clock_deltas,
        fixed_input_cache_effect=error_effect,live=live_rows,
        cause='Detector ROI/search history survives a tracker session reset. Fresh backend processes and reused UI processes begin with different detector history.',
        fix='Explicit detector-history=session clears ROI, failure count and scan index at input-session change; model and numerical estimation are unchanged.',
        scope='Cache-origin initialization divergence is isolated and fixed under the explicit contract. Complete UI equivalence and all hit-rate residuals are not proven.')
    write(root/'causal-result.json',result);return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    result=analyze(args.output.resolve());print(json.dumps({k:result[k] for k in ('passed','restored_ui_frames','fixed_input_cache_effect')},indent=2))
