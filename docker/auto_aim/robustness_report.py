#!/usr/bin/env python3
"""Paired robustness report. Read-only scoring over immutable run evidence."""
import argparse,collections,csv,hashlib,html,json,math
from pathlib import Path
from continuity_replay import prepare,metrics,q,OPS,steady
from continuity_compare import steady_seconds
from report import load,write
from scenario_catalog import EXTRA,semantics
from robustness_study import BASE
from robustness_prediction import evaluate as evaluate_prediction



def command_change_times(plan):
    changes=set()
    for legs in plan.get('segments',[]):
        previous=None
        for segment in legs:
            speed=float(segment.get('v',0)) if segment['type']!='static' else 0.
            heading=math.radians(segment.get('dir_deg',0))
            command=(speed*math.cos(heading),speed*math.sin(heading),float(segment.get('omega',0)))
            if previous is not None and any(abs(a-b)>1e-7 for a,b in zip(command,previous)) and 2<=segment['t0']<16:
                changes.add(segment['t0'])
            previous=command
    return sorted(changes)


def score(folder,prediction_output=None):
    manifest=load(folder/'manifest.json');summary=load(folder/'summary.json');source,context=prepare(folder)
    observed=[json.loads(s)['data'] for s in source.splitlines()]
    results=[]
    for o in observed:r=dict(o['result']);r['stamp_ns']=o['frame']['stamp_ns'];results.append(r)
    m=metrics(results,context)
    shots=[json.loads(s) for s in (folder/'shots.jsonl').open()]
    plates=[]
    for line in (folder/'telemetry.jsonl').open():
        packet=json.loads(line)
        if packet['kind']=='armors':plates=packet['data']['armors'];break
    front_id=next((p['id'] for p in plates if p['robot_id']==1 and p['slot']==0),None)
    target=lambda s:s['outcome']=='target_armor'
    front=lambda s:target(s) and s['armor_id']==front_id
    useful=target if semantics(manifest['scene'])['hit_evaluation']=='target_armor' else front
    hit_count=sum(useful(s) for s in shots);total=len(shots)
    steady_shots=[s for s in shots if steady(manifest['scene'],s['fire_time_s'])]
    phase_seconds=steady_seconds(manifest['scene'],manifest['duration_s'])
    changes=[]
    plan=load(folder/'configuration/scenario-plan.json',{})
    if plan:
        times=command_change_times(plan)
        changes=[dict(stamp_ns=context['start']+round(t*1e9),time=t) for t in times]
    elif manifest['scene'] in ('line','ego_sweep','ego_sweep_reverse'):
        changes=[dict(stamp_ns=e['stamp_ns']) for e in m['recovery_events']]
    if not changes:changes=[dict(stamp_ns=context['start'])]
    first=[];vector_recovery=[]
    for i,event in enumerate(changes):
        stamp=event['stamp_ns'];until=min(context['end'],stamp+2_000_000_000,changes[i+1]['stamp_ns'] if i+1<len(changes) else context['end'])
        times=[(s['terminal_stamp_ns']-stamp)*1e-9 for s in shots if useful(s) and s['fire_stamp_ns']>=stamp and s.get('terminal_stamp_ns') is not None and s['terminal_stamp_ns']<=until]
        first.append(dict(stamp_ns=stamp,delay_s=min(times) if times else None,censor_s=(until-stamp)*1e-9))
        origin=None;recovery=None;identity=None;previous=None
        for result in results:
            tick=result['stamp_ns']
            if tick<stamp:continue
            if tick>=until:break
            truth=context['velocity'].at(tick);state=result.get('filter_state')
            current=(result.get('track_generation'),result.get('selected_plate_id'))
            valid=(truth is not None and state and result.get('tracker')=='tracking' and
                math.hypot(*truth[:2])>.1 and math.hypot(state[1]-truth[0],state[3]-truth[1])<=.15 and
                state[1]*truth[0]+state[3]*truth[1]>0)
            if previous is not None and (tick-previous>100_000_000 or identity!=current):origin=None
            if valid:
                if origin is None:origin=tick
                if tick-origin>=100_000_000:recovery=(origin-stamp)*1e-9;break
            else:origin=None
            previous=tick;identity=current
        vector_recovery.append(dict(stamp_ns=stamp,recovery_s=recovery,censor_s=(until-stamp)*1e-9,
           reference='relative centre velocity; valid for nonrotating target only'))
    durations=manifest['duration_s'];frames=[json.loads(s) for s in (folder/'frames.jsonl').open()]
    jumps=m['steady_angle_gt_1deg_frames']
    prediction=evaluate_prediction(folder,observed,context,prediction_output)
    return dict(prediction=prediction,binary_sha256=manifest['algorithm_binary_sha256'],control_source=manifest.get('control_source','algorithm'),run_id=folder.name,scene=manifest['scene'],seed=manifest['random_seed'],physics=manifest['shooting']['physics'],
        visible_frames=summary['counts']['visible_frames'],correct_tracking_frames=summary['counts']['strict_correct_tracking_frames'],
        steady_shots=len(steady_shots),steady_hits=sum(useful(s) for s in steady_shots),steady_seconds=phase_seconds,profile=manifest['algorithm_profile'],visible=manifest.get('projectile_visible','on'),duration_s=durations,shots=total,hits=hit_count,
        hit_rate=hit_count/total if total else 0,useful_hits_per_s=hit_count/durations,hit_evaluation=semantics(manifest['scene'])['hit_evaluation'],
        correct_tracking_fraction=summary['metrics'].get('correct_tracking_fraction'),recovery_p90_s=m['recovery_p90_s'],
        recovery_events=m['recovery_events'],vector_recovery_events=vector_recovery if 'rotation' not in semantics(manifest['scene'])['target_motion'] else [],first_hit_events=first,first_hit_p90_s=q([e['delay_s'] for e in first],.9),
        first_hit_censor_fraction=sum(e['delay_s'] is None for e in first)/len(first) if first else None,
        jump_frames=jumps,selected_plate_velocity_error_p95_mps=summary['metrics'].get('selected_plate_velocity_error_p95_mps'),
        target_visibility=summary['metrics'].get('visible_frame_fraction'),target_speed_p95_mps=summary['metrics'].get('target_speed_p95_mps'),wall_s=summary['metrics']['total_wall_s'],
        failed_rules=[r for r in summary['rules'] if not r['passed']],frame_count=len(frames))


def report(study):
    batches=list(study.rglob('*-batch.json'));rows=[];invalid=[];completeness=[]
    cache=study/'report-cache';cache.mkdir(exist_ok=True)
    exp=OPS/'experiments'
    for batch_path in batches:
        batch=load(batch_path)
        completeness.append(dict(batch=str(batch_path.relative_to(study)),complete=batch['complete'],runs=len(batch['runs'])))
        invalid+=batch['invalid_attempts']
        for r in batch['runs']:
            f=exp/r['run_id'];dest=cache/(f.name+'.json')
            digest=hashlib.sha256(Path(__file__).read_bytes()+Path(__file__).with_name('robustness_prediction.py').read_bytes()).hexdigest()
            cached=load(dest,{})
            if cached.get('scorer_sha256')!=digest:cached=score(f,study/'prediction'/(f.name+'.jsonl'));cached['scorer_sha256']=digest;write(dest,cached)
            rows.append(dict(cached,study_batch=str(batch_path.relative_to(study))))
    notes=load(study/'design-notes.json',{})
    for row in rows:row['design_note']=notes.get(row['run_id'])
    groups=collections.defaultdict(list)
    for r in rows:groups[r['study_batch'],r['scene'],r['physics'],r['profile'],r['visible']].append(r)
    paired=[]
    def aggregate(rs):
        shots=sum(r['shots'] for r in rs);seconds=sum(r['duration_s'] for r in rs)
        return dict(runs=len(rs),shots=shots,hits=sum(r['hits'] for r in rs),hit_rate=sum(r['hits'] for r in rs)/shots if shots else 0,
          useful_hits_per_s=sum(r['hits'] for r in rs)/seconds if seconds else 0,
          coverage=sum(r['correct_tracking_frames'] for r in rs)/max(1,sum(r['visible_frames'] for r in rs)),
          steady_shots=sum(r['steady_shots'] for r in rs),steady_hits=sum(r['steady_hits'] for r in rs),
          steady_hit_rate=sum(r['steady_hits'] for r in rs)/max(1,sum(r['steady_shots'] for r in rs)),
          steady_useful_hits_per_s=sum(r['steady_hits'] for r in rs)/max(1e-9,sum(r['steady_seconds'] for r in rs)),
          steady_seconds=sum(r['steady_seconds'] for r in rs),
          recovery_p90_s=q([e['recovery_s'] if e['recovery_s'] is not None else 2 for r in rs for e in r['recovery_events']],.9),
          first_hit_p90_s=q([e['delay_s'] for r in rs for e in r['first_hit_events']],.9),
          vector_recovery_p90_s=q([e['recovery_s'] for r in rs for e in r['vector_recovery_events']],.9),
          vector_recovery_censor_fraction=sum(e['recovery_s'] is None for r in rs for e in r['vector_recovery_events'])/max(1,sum(len(r['vector_recovery_events']) for r in rs)),
          first_hit_censor_fraction=sum(e['delay_s'] is None for r in rs for e in r['first_hit_events'])/max(1,sum(len(r['first_hit_events']) for r in rs)),
          max_wall_s=max(r['wall_s'] for r in rs),jump_frames=sum(r['jump_frames'] for r in rs))
    for (batch_name,scene,physics,profile,visible),rs in sorted(groups.items()):
        baseline=groups.get((batch_name,scene,physics,'tracking_v2',visible),[])
        matched=[r for r in rs if any(b['seed']==r['seed'] for b in baseline)]
        peers=[b for b in baseline if any(r['seed']==b['seed'] for r in matched)]
        b=aggregate(peers) if peers else None;a=aggregate(matched or rs)
        checks={}
        if b and profile!='tracking_v2':
            checks['useful_hits_per_s_improved']=a['useful_hits_per_s']>=b['useful_hits_per_s'] if scene in BASE else a['useful_hits_per_s']>b['useful_hits_per_s']
            checks['coverage_nonregression']=a['coverage']>=b['coverage']-.02
            checks['first_hit_censor_nonregression']=a['first_hit_censor_fraction']<=b['first_hit_censor_fraction']
            if a['first_hit_p90_s'] is not None and b['first_hit_p90_s'] is not None:checks['first_hit_nonregression']=a['first_hit_p90_s']<=b['first_hit_p90_s']+.05
        if scene in BASE and profile!='tracking_v2':
            if scene in ('line','ego_sweep','ego_sweep_reverse'):
                checks['original_recovery_gate']=a['recovery_p90_s'] is not None and a['recovery_p90_s']<=.3
                checks['moving_hit_rate']=a['steady_hit_rate']>=.8
                if b:checks['steady_useful_hits_nonregression']=a['steady_useful_hits_per_s']>=b['steady_useful_hits_per_s']
                checks['first_hit_absolute']=a['first_hit_p90_s'] is not None and a['first_hit_p90_s']<=.8 and a['first_hit_censor_fraction']==0
            if scene=='move_ref':checks['static_hit_rate']=a['hit_rate']>=.95
            if scene=='spin' and b:checks['spin_hit_nonregression']=a['hit_rate']>=b['hit_rate']-.05
            legacy=load(OPS/'experiments/continuity-v2-20261020/tracking-acceptance.json',{}).get('groups',{}).get('legacy/'+scene+'/'+physics)
            if legacy and scene in ('line','ego_sweep','ego_sweep_reverse'):
                checks['legacy_jump_reduction']=a['jump_frames']/a['steady_seconds']<=legacy['jump_hz']*.5
                checks['legacy_coverage_nonregression']=a['coverage']>=legacy['correct_tracking_fraction']-.02
        paired.append(dict(batch=batch_name,control_source=rs[0]['control_source'],scene=scene,physics=physics,profile=profile,visible=visible,candidate=a,baseline=b,checks=checks,
                           stress=semantics(scene)['stress']))
    value=dict(complete=any('validation' in b['batch'] for b in completeness) and all(b['complete'] for b in completeness if 'validation' in b['batch']),batches=completeness,runs=rows,comparisons=paired,
      invalid_attempts=invalid,visual_review='pending',baseline_promoted=False,
      limitations=['Development recordings are not held-out validation.','First-hit events in random motion are censored at the next command change; censor rate is reported separately.',
        'Original recovery uses world-Y relative velocity for comparability; rotating plate CV velocity is not whole-car centre velocity.','Visibility is geometric. Effective hit rate uses the full measurement duration.'])
    validation=[c for c in paired if 'validation' in c['batch'] and c['profile']=='tracking_fire_v3']
    value['rule_acceptance_passed']=bool(validation) and value['complete'] and all(all(c['checks'].values()) for c in validation if not c['stress']) and all(not r['failed_rules'] for r in rows if 'validation' in r['study_batch'])
    value['acceptance_note']='New normal conditions require useful hits/s improvement and coverage/first-hit nonregression. Original conditions retain absolute recovery/hit gates. Stress conditions describe the boundary and cannot override normal-condition failures.'
    value['failed_acceptance_conditions']=[dict(scene=c['scene'],physics=c['physics'],rules=[k for k,v in c['checks'].items() if not v]) for c in validation if not c['stress'] and not all(c['checks'].values())]
    write(study/'result-summary.json',value)
    def fmt(v):return '—' if v is None else f'{v:.3f}'
    body=[]
    for c in paired:
        r=c['candidate'];failed=', '.join(k for k,v in c['checks'].items() if not v)
        body.append('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in (c['batch']+' / '+c['scene'],c['physics'],c['profile'],c['visible'],r['runs'],f"{r['hits']}/{r['shots']}",fmt(r['useful_hits_per_s']),fmt(r['coverage']),fmt(r['recovery_p90_s']),fmt(r['first_hit_p90_s']),fmt(r['first_hit_censor_fraction']),failed or '—'))+'</tr>')
    videos=''.join(f'<details><summary>{html.escape(r["run_id"])}</summary><a href="../{r["run_id"]}/report.html">完整报告</a><video controls preload="none" src="../{r["run_id"]}/annotated.mp4"></video></details>' for r in rows)
    text=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>自瞄鲁棒性 v3</title><style>body{{font:15px sans-serif;margin:24px}}table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:6px}}video{{width:min(900px,100%)}}input{{padding:8px}}</style>
<h1>自瞄鲁棒性 v3</h1><p>已记录 {len(rows)} 次；独立验证完整：{value['complete']}；规则验收：{value['rule_acceptance_passed']}；人工视觉验收：待确认；基线未升级。</p>
<p>命中使用完整测量时间。随机/组合场景的首次命中在下一次指令变化处截断，同时展示截断率；不能只比较成功样本延迟。</p>
<p><a href="display-audit.json">显示开关对照与调度误差</a> · <a href="design-notes.json">场景设计问题记录</a> · <a href="result-summary.json">全部结果与规则</a> · <a href="candidate-selection.json">离线候选选择</a> · <a href="diagnostic-invariance.json">诊断无行为影响</a></p>
<p>原开发批次的部分距离路线存在建筑遮挡，详见场景设计记录；不作为纯距离性能结论。随机组须对照实际速度，2m/s指令不等于实际达到2m/s。</p><img loading="lazy" style="max-width:100%" src="display-contact-sheet.png"><input id="filter" placeholder="筛选场景 / 算法 / 弹道" oninput="document.querySelectorAll('tbody tr').forEach(r=>r.hidden=!r.textContent.includes(this.value))">
<table><thead><tr>{''.join('<th>'+x+'</th>' for x in ['场景','弹道','算法','弹丸显示','次数','命中/发射','有效命中/秒','跟踪覆盖','速度恢复P90','首次命中P90','截断率','未通过检查'])}</tr></thead><tbody>{''.join(body)}</tbody></table><h2>对照图</h2><a href="robustness-comparison.svg"><img loading="lazy" style="max-width:100%" src="robustness-comparison.png"></a><h2>实验录像</h2>{videos}</html>'''
    (study/'report.html').write_text(text)
    return dict(runs=len(rows),complete=value['complete'],comparisons=len(paired))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--study',type=Path,required=True);a=p.parse_args();print(json.dumps(report(a.study.resolve())))
