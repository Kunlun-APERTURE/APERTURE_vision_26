#!/usr/bin/env python3
"""Export timestamp-indexed failure clips from the final paired comparison."""
import argparse,csv,html,json,math,subprocess
from pathlib import Path
from continuity_replay import OPS


def main():
    p=argparse.ArgumentParser();p.add_argument('--study',type=Path,required=True);a=p.parse_args();study=a.study.resolve();data=json.loads((study/'result-summary.json').read_text())
    comparisons=[c for c in data['comparisons'] if 'validation' in c['batch'] and c['profile']=='tracking_fire_v3' and c['baseline']]
    if not comparisons:comparisons=[c for c in data['comparisons'] if c['profile']=='tracking_fire_v3' and c['baseline']]
    comparisons.sort(key=lambda c:(c['candidate']['useful_hits_per_s']-c['baseline']['useful_hits_per_s'],c['candidate']['coverage']-c['baseline']['coverage']))
    conditions=[];seen=set()
    for c in comparisons:
        key=(c['scene'],c['physics'])
        if key in seen:continue
        seen.add(key);conditions.append(c)
        if len(conditions)==6:break
    clips=[];out=study/'failure-clips';out.mkdir(exist_ok=True)
    for c in conditions:
        rows=[r for r in data['runs'] if r['study_batch']==c['batch'] and r['scene']==c['scene'] and r['physics']==c['physics'] and r['profile']=='tracking_fire_v3']
        worst=min(rows,key=lambda r:(r['useful_hits_per_s'],r['correct_tracking_fraction'] or 0));seed=worst['seed']
        paired=next((r for r in data['runs'] if r['study_batch']==c['batch'] and r['scene']==c['scene'] and r['physics']==c['physics'] and r['profile']=='tracking_v2' and r['seed']==seed),None)
        source=OPS/'experiments'/worst['run_id'];indices=[json.loads(s) for s in (source/'frames.jsonl').open()]
        measured=[r for r in indices if r.get('measurement') and r.get('time') is not None]
        if not measured:continue
        center=max(measured,key=lambda r:r.get('center_xy_error_m') or 0)['time']
        failures=[e for e in worst['first_hit_events'] if e['delay_s'] is None and e['censor_s']>=.5]
        if failures:
            start=json.loads((source/'run-state.json').read_text())['measurement_start_stamp_ns'];center=(failures[0]['stamp_ns']-start)*1e-9
        begin=max(0,center-.75);end=min(worst['duration_s'],center+2)
        for run in (worst,paired):
            if run is None:continue
            source=OPS/'experiments'/run['run_id'];indices=[json.loads(s) for s in (source/'frames.jsonl').open()]
            selected=[r for r in indices if r.get('time') is not None and begin<=r['time']<=end and r.get('measurement')]
            if not selected:continue
            first,last=selected[0]['video_index'],selected[-1]['video_index'];folder=out/(c['scene']+'_'+c['physics']+'_'+run['profile']);folder.mkdir(exist_ok=True)
            subprocess.run(['ffmpeg','-v','error','-y','-i',str(source/'annotated.mp4'),'-vf',f'trim=start_frame={first}:end_frame={last+1},setpts=PTS-STARTPTS',
               '-an','-c:v','libx264','-preset','ultrafast','-crf','23',str(folder/'clip.mp4')],check=True)
            output=subprocess.check_output(['ffprobe','-v','error','-count_frames','-select_streams','v:0','-show_entries','stream=nb_read_frames','-of','json',str(folder/'clip.mp4')],text=True)
            count=int(json.loads(output)['streams'][0]['nb_read_frames']);assert count==last-first+1
            keys=list(dict.fromkeys(k for r in selected for k in r))
            with (folder/'frames.csv').open('w') as f:
                w=csv.DictWriter(f,keys);w.writeheader();w.writerows({k:json.dumps(v) if isinstance(v,(dict,list)) else v for k,v in row.items()} for row in selected)
            value=dict(scene=c['scene'],physics=c['physics'],profile=run['profile'],run_id=run['run_id'],seed=seed,
                selected_scenario_time_s=center,start_time_s=begin,end_time_s=end,first_source_video_index=first,last_source_video_index=last,
                decoded_frames=count,precise_timing='frames.csv capture timestamps and scenario times; clip nominal30fps',selection_reason='Failed first-hit window if available; otherwise maximum centre-position error in the weakest useful-hit repeat.',
                failed_rules=[k for k,v in c['checks'].items() if not v],relative_path=str(folder.relative_to(study)))
            (folder/'index.json').write_text(json.dumps(value,ensure_ascii=False,indent=2));clips.append(value)
    (study/'failure-clips.json').write_text(json.dumps(dict(clips=clips,decoded_frame_checks_passed=True),ensure_ascii=False,indent=2))
    body=''.join(f'<section><h2>{html.escape(c["scene"]+" / "+c["physics"]+" / "+c["profile"])}</h2><p>{html.escape(c["run_id"])}，种子 {c["seed"]}，t={c["selected_scenario_time_s"]:.3f}s</p><p>{html.escape(", ".join(c["failed_rules"]))}</p><video controls preload="none" src="{c["relative_path"]}/clip.mp4"></video><p><a href="{c["relative_path"]}/frames.csv">精确逐帧时间</a> · <a href="../{c["run_id"]}/report.html">完整报告</a></p></section>' for c in clips)
    (study/'failure-clips.html').write_text('<!doctype html><meta charset="utf-8"><title>鲁棒性失败片段</title><style>body{font:16px sans-serif;margin:24px}video{width:min(900px,100%)}</style><h1>鲁棒性失败片段与相同种子对照</h1><p>片段由规则和误差选取，人工视觉验收待确认。精确时间以CSV为准。</p>'+body)
    print(json.dumps(dict(clips=len(clips),decoded_frame_checks_passed=True)))

if __name__=='__main__':main()
