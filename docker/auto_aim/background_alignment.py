#!/usr/bin/env python3
"""Ordered UI/headless comparisons. Never promotes dual-worker qualification."""
import argparse
from collections import Counter
import fcntl
import html
import json
from pathlib import Path
import statistics
import time
import tomllib

import background_jobs as jobs
from background_acceptance import DEFAULTS, SCENES
import experiments
import manage
from report import load, write
from impact_quality import quantile


def settings(mode):
    cap=float(mode.split('-')[-1]) if mode.split('-')[-1].isdigit() else None
    return dict(DEFAULTS,max_fps=cap,algorithm_lifecycle='fresh' if '-cold' in mode else 'session')


def ui(output,mode,state):
    entry=state['modes'].setdefault(mode,dict(runs=[],processes=[]))
    # A partial warm sequence cannot resume from its middle with an empty cache.
    if entry.get('state')=='completed':return
    if entry['runs']:raise RuntimeError('Partial UI sequence: use a new output to replay the entire ordered sequence')
    opts=settings(mode)
    started=time.monotonic()
    with (manage.RUNTIME/'operation.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            manage.stop()
            manage.start(argparse.Namespace(**opts,scene='static',control='off',view='off',replace=False,fire='auto',command_script=''))
            for case in state['definition']['cases']:
                if opts['algorithm_lifecycle']=='fresh' and entry['runs']:
                    manage.algorithm('stop')
                    manage.algorithm('start',view='off',fire='auto',aim_origin=opts['aim_origin'],
                        algorithm_profile=opts['algorithm_profile'],detach=True)
                    deadline=time.monotonic()+45
                    while True:
                        process=manage.algorithm('status');health=manage.rpc('status')
                        if process['running'] and health.get('connected') and health.get('frame_age_ms',999) is not None and health['frame_age_ms']<250:break
                        if time.monotonic()>deadline:raise RuntimeError('Fresh UI algorithm readiness exceeded deadline')
                        time.sleep(.2)
                args=argparse.Namespace(**opts,**case,duration=None,label='alignment-'+mode)
                entry['processes'].append(manage.algorithm('status'))
                folder=experiments.run_one(manage,case['scene'],args)
                entry['runs'].append(folder.name)
                write(output/'alignment.json',state)
                if load(folder/'summary.json',{}).get('verdict')=='invalid':
                    raise RuntimeError('Invalid UI experiment: '+folder.name)
            entry['state']='completed'
        finally:
            manage.stop()
            saved=manage.profile();saved.update(scene='static',terrain='arena',fire='off',control='off',
                algorithm_profile='legacy',gimbal_profile='legacy',max_fps=None)
            manage.save_profile(saved)
            entry['active_wall_s']=time.monotonic()-started
            write(output/'alignment.json',state)


def background(output,mode,state):
    if mode not in state['modes']:
        spec=output/(mode+'-spec.json')
        write(spec,dict(version=1,defaults=dict(settings(mode),label='alignment-'+mode),
            cases=state['definition']['cases'],parallelism=1))
        task=jobs.submit(manage.parser().parse_args(['experiment','submit','--spec',str(spec)]))
        state['modes'][mode]=dict(job_id=task['job_id'],runs=[])
        write(output/'alignment.json',state)
    entry=state['modes'][mode]
    while True:
        with jobs.connect() as db:task=jobs.status(db,entry['job_id'])
        entry['state']=task['state']
        entry['attempts']=task['attempts']
        entry['runs']=[a['run_id'] for a in task['attempts'] if a['state']=='finished' and a['verdict']!='invalid' and a['run_id']]
        done=[a for a in task['attempts'] if a['ended']]
        if done:entry['active_wall_s']=max(a['ended'] for a in done)-min(a['started'] for a in done)
        write(output/'alignment.json',state)
        print(json.dumps(dict(mode=mode,state=task['state'],items=task['items'])),flush=True)
        if task['state'] in ('completed','paused','cancelled'):
            if task['state']!='completed':raise RuntimeError('Background task stopped: '+entry['job_id'])
            return
        time.sleep(10)


def collect(output):
    state=load(output/'alignment.json');quality={};checks=[];provenance=set();configs={}
    def check(name,passed,**values):checks.append(dict(name=name,passed=bool(passed),**values))
    for mode,entry in state['modes'].items():
        rows=[]
        for run in entry['runs']:
            folder=manage.OPS/'experiments'/run;m=load(folder/'manifest.json');s=load(folder/'summary.json')
            traces=[];capture_stamps=[];result_stamps=[];rois=Counter()
            measurement=load(folder/'run-state.json',{})
            start=measurement.get('measurement_start_stamp_ns',0);end=start+int(m['duration_s']*1e9)
            with (folder/'telemetry.jsonl').open() as source:
                for line in source:
                    record=json.loads(line)
                    if record['kind']=='gimbal_trace' and start<=record['data']['stamp_ns']<=end:
                        traces.append(record['data'])
                    if record['kind']=='observation' and start<=record['data']['frame']['stamp_ns']<=end:
                        capture_stamps.append(record['data']['frame']['stamp_ns']);result_stamps.append(record['received_ns'])
                        roi=(record['data']['result'].get('detector_summary') or {}).get('tile_roi')
                        rois[json.dumps(roi)]+=1
            capture_intervals=[(b-a)/1e6 for a,b in zip(capture_stamps,capture_stamps[1:])]
            result_intervals=[(b-a)/1e6 for a,b in zip(result_stamps,result_stamps[1:])]
            control_hz=None
            if len(traces)>1:
                span=(traces[-1]['stamp_ns']-traces[0]['stamp_ns'])/1e9
                if span>0:control_hz=(traces[-1]['control_steps']-traces[0]['control_steps'])/span
            rows.append(dict(run_id=run,scene=m['scene'],seed=m['random_seed'],metrics=s['metrics'],verdict=s['verdict'],
                provisioning=m.get('background',{}).get('provisioning'),
                control_hz=control_hz,control_dt_median_ms=statistics.median(t['dt_s']*1000 for t in traces) if traces else None,
                control_dt_p95_ms=quantile([t['dt_s']*1000 for t in traces],.95),
                capture_interval_p50_ms=quantile(capture_intervals,.5),capture_interval_p95_ms=quantile(capture_intervals,.95),
                result_interval_p95_ms=quantile(result_intervals,.95),
                detector_roi_counts=dict(rois),
                scenario_sha256=m['configuration_sha256']['scenario.toml']))
            provenance.add(json.dumps(dict(binary=m['algorithm_binary_sha256'],config=m['configuration_sha256']['algorithm.yaml'],
                weights=m['algorithm_weights_sha256'],image=m['containers']['simulator_image_id'],
                scoring={name:m['source_files'][name] for name in ('report.py','shots.py','impact_quality.py','evaluator.py')}),sort_keys=True))
            config=tomllib.loads((folder/'configuration/simulator.toml').read_text())
            for section,key in [('window','headless'),('preview','enabled'),('debug','egui'),('debug','inspector'),('camera','pip_enabled')]:
                config.get(section,{}).pop(key,None)
            configs.setdefault(settings(mode)['max_fps'],set()).add(json.dumps(config,sort_keys=True))
        entry['rows']=rows
        check(mode+'_ordered_cases',[(r['scene'],r['seed']) for r in rows]==[(c['scene'],c['seed']) for c in state['definition']['cases']])
        check(mode+'_valid',bool(rows) and all(r['verdict']!='invalid' for r in rows))
        check(mode+'_performance',bool(rows) and all(r['metrics'].get('processed_hz',0)>=20 and
            r['metrics'].get('age_p95_ms',999)<=150 and r['metrics'].get('total_wall_s',999)<=40 for r in rows))
        if mode.startswith('background') and rows:
            ids=[r['provisioning']['algorithm_container_id'] for r in rows]
            cold=settings(mode)['algorithm_lifecycle']=='fresh'
            expected_reused=[False]+[not cold]*(len(rows)-1)
            check(mode+'_lifecycle', [r['provisioning']['algorithm_reused'] for r in rows]==expected_reused and
                (len(set(ids))==len(ids) if cold else len(set(ids))==1))
        elif rows:
            check(mode+'_lifecycle',len({(p['pid'],p['starttime']) for p in entry['processes']})==
                (len(rows) if settings(mode)['algorithm_lifecycle']=='fresh' else 1))
        for scene in state['definition']['scenes']:
            rr=[r['metrics'] for r in rows if r['scene']==scene]
            n=sum(r.get('shots_fired',0) for r in rr);hits=sum(r.get('target_armor_hits',0) for r in rr)
            quality.setdefault(scene,{})[mode]=dict(shots=n,hits=hits,hit_rate=hits/n if n else None,
                weighted_hit_rate=sum(r.get('weighted_hit_score_sum',0) for r in rr)/n if n else None)
        entry['performance']=dict(processed_hz_median=statistics.median(r['metrics']['processed_hz'] for r in rows) if rows else None,
            control_hz_median=statistics.median(r['control_hz'] for r in rows if r['control_hz'] is not None) if any(r['control_hz'] is not None for r in rows) else None,
            age_p95_max_ms=max((r['metrics']['age_p95_ms'] for r in rows),default=None),
            wall_max_s=max((r['metrics']['total_wall_s'] for r in rows),default=None))
    check('same_algorithm_models_scoring_and_simulator_image',len(provenance)==1)
    check('same_config_except_presentation_at_each_cap',all(len(v)==1 for v in configs.values()))
    comparisons={}
    pairs=[]
    for mode in state['modes']:
        if mode.startswith('background'):
            base=mode.replace('background','ui')
            pairs.append((base,mode))
            if base!='ui':pairs.append(('ui',mode))
        elif mode!='ui':pairs.append(('ui',mode))
    for base,mode in pairs:
        if base not in state['modes']:continue
        paired=[]
        for scene,q in quality.items():
            for metric in ('hit_rate','weighted_hit_rate'):
                b=q[base][metric];c=q[mode][metric]
                passed=b is not None and c is not None and c>=b-.05
                paired.append(passed)
                check(scene+'_'+mode+'_vs_'+base+'_'+metric,passed,
                    baseline=b,candidate=c,delta=None if b is None or c is None else c-b)
        same=([(r['scene'],r['seed'],r['scenario_sha256']) for r in state['modes'][mode]['rows']]==
            [(r['scene'],r['seed'],r['scenario_sha256']) for r in state['modes'][base]['rows']])
        check(mode+'_vs_'+base+'_same_scenarios',same)
        comparisons[mode+'_vs_'+base]=dict(passed=all(paired) and same,baseline=base,candidate=mode)
    coverage_complete=set(state['definition']['scenes'])==set(SCENES) and len(set(state['definition']['seeds']))>=3
    screen=bool(comparisons) and all(c['passed'] for c in checks)
    qualified_profiles=[]
    for candidate in ('background','background-240','background-480','background-cold'):
        if candidate not in state['modes'] or 'ui' not in state['modes']:continue
        base='ui' if candidate=='background' else candidate.replace('background','ui')
        required=[candidate+'_vs_'+base]
        if base!='ui':required += [candidate+'_vs_ui',base+'_vs_ui']
        relevant=[c for c in checks if c['name'].startswith((candidate+'_',base+'_','ui_','same_')) and '_vs_' not in c['name']]
        if coverage_complete and all(comparisons.get(k,{}).get('passed',False) for k in required) and all(c['passed'] for c in relevant):
            qualified_profiles.append(candidate)
    state.update(checks=checks,quality_by_scene=quality,coverage_complete=coverage_complete,
        comparisons=comparisons,qualified_profiles=qualified_profiles,
        regression_screen_passed=screen,serial_aligned=bool(qualified_profiles),
        review_status='pending_visual_review',
        notes=['Paired cases share order, seed, algorithm, models, scoring and physics.',
            'UI runs inside Sunshine with host IPC/default DDS; background uses its independent runtime, private IPC and explicit UDP. Process lifecycle and FPS controls do not isolate every runtime difference.',
            'Five percentage points per scene is a regression screen, not a statistical equivalence proof.',
            'Capped UI variants do not replace the original uncapped UI baseline.',
            'This study never qualifies dual-worker execution.'])
    write(output/'alignment.json',state)
    body='<h1>后台与 UI 对齐实验</h1><p>逐场景回退检查：'+('通过' if screen else '尚未全部通过')+'；完整场景覆盖：'+str(coverage_complete)+'</p>'
    body+='<p>精确命中率 / 加权命中率（命中/射击）。同种子、同顺序、同算法；单场景允许回退最多 5 个百分点。规则检查不代表人工视觉验收。</p>'
    body+='<p>完整覆盖后通过的运行配置：'+html.escape(', '.join(qualified_profiles) or '暂无')+'</p>'
    body+='<p><a href="alignment.json">逐次评分、控制时序与检测器 ROI 分布 JSON</a></p>'
    body+='<table><tr><th>场景</th>'+''.join('<th>'+html.escape(m)+'</th>' for m in state['modes'])+'</tr>'
    for scene,q in quality.items():
        body+='<tr><td>'+scene+'</td>'
        for mode in state['modes']:
            v=q[mode];cell='—' if v['hit_rate'] is None else f'{v["hit_rate"]:.1%} / {v["weighted_hit_rate"]:.1%} ({v["hits"]}/{v["shots"]})'
            body+='<td>'+cell+'</td>'
        body+='</tr>'
    body+='</table><h2>检查与性能</h2><pre>'+html.escape(json.dumps(dict(checks=checks,
        performance={m:e['performance'] for m,e in state['modes'].items()}),ensure_ascii=False,indent=2))+'</pre><h2>原始报告</h2>'
    for mode,entry in state['modes'].items():
        body+='<h3>'+mode+'</h3><ul>'+''.join('<li><a href="../'+r+'/report.html">'+r+'</a></li>' for r in entry['runs'])+'</ul>'
    for name,label in [('evidence-audit.json','完整证据与视频解码检查'),('lifecycle-functional.json','冷启动兼容与空闲回收检查'),
            ('runtime-libraries.json','动态库摘要对照'),('runtime-replay.json','相同原始图像跨容器逐帧回放'),
            ('verification-summary.json','实现验收汇总')]:
        if (output/name).exists():body+='<p><a href="'+name+'">'+label+'</a></p>'
    (output/'report.html').write_text('<!doctype html><meta charset="utf-8"><style>body{font:15px system-ui;margin:2em}td,th{border:1px solid #aaa;padding:.5em}table{border-collapse:collapse}</style>'+body)
    experiments.publish_desktop(manage,output)
    return state


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    p.add_argument('--scenes',default=','.join(SCENES));p.add_argument('--seeds',default='2026102207,2026102208,2026102209')
    p.add_argument('--modes',default='ui,background');p.add_argument('--report-only',action='store_true')
    args=p.parse_args();output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    modes=args.modes.split(',')
    if any(m not in ('ui','background','ui-cold','background-cold','ui-240','background-240','ui-480','background-480') for m in modes):
        p.error('Unsupported comparison mode')
    scenes=args.scenes.split(',');seeds=[int(s) for s in args.seeds.split(',')]
    if any(s not in SCENES for s in scenes):p.error('Unsupported alignment scene')
    if not seeds or any(not 0<=seed<=4294967295 for seed in seeds):p.error('Seeds must be unsigned 32-bit integers')
    definition=dict(scenes=scenes,seeds=seeds,cases=[dict(scene=scene,seed=seed) for seed in seeds
        for scene in (scenes if seed%2 else list(reversed(scenes)))],defaults=DEFAULTS,tolerance_pp=5)
    state=load(output/'alignment.json',dict(version=1,definition=definition,modes={}))
    if state['definition']!=definition and not args.report_only:p.error('Existing study definition differs; use a new output')
    write(output/'alignment.json',state)
    if not args.report_only:
        for mode in modes:
            if mode.startswith('ui'):ui(output,mode,state)
            else:background(output,mode,state)
            collect(output)
    result=collect(output)
    print(json.dumps(dict(serial_aligned=result['serial_aligned'],report=str(output/'report.html'))),flush=True)


if __name__=='__main__':main()
