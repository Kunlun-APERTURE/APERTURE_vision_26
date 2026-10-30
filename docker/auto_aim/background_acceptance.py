#!/usr/bin/env python3
"""Resumable desktop/isolated-serial/isolated-dual infrastructure acceptance."""
import argparse
import fcntl
import html
import json
import re
from pathlib import Path
import statistics
import time
import tomllib

import background_jobs as jobs
import experiments
import manage
from report import load, write

SCENES=['hf_static_5','hf_static_8','hf_uniform_3','hf_reverse_3','hf_turn90_3','hf_spin_pos','hf_spin_neg']
SEEDS=[2026102201,2026102202,2026102203]
DEFAULTS=dict(terrain='flat_range',mode='shoot',matrix='single',algorithm_profile='tracking_fire_v3',
    gimbal_profile='feedforward',physics='no-drag',fire_policy='advice',fire_rate_hz=5.,
    bullet_speed=25.,reported_bullet_speed=25.,aim_origin='muzzle',projectile_visible='on')


def collect(output):
    state=load(output/'acceptance.json',{})
    state['comparison_notes']=[
        'Desktop legacy orchestration reuses the algorithm process and resets its session; background attempts start a fresh algorithm process. Detector caches can therefore differ.',
        'All modes retain uncapped rendering. Render-loop rates are recorded; the feedforward gimbal profile includes Update-driven control. These are end-to-end comparisons, not isolated attribution of score changes to concurrency.',
        'Rule checks and video decoding do not replace human visual review. Run verdicts remain pending_visual_review until explicitly reviewed.'
    ]
    modes={}
    expected=21+len(state.get('definition',{}).get('supplemental_cases',[]))
    for mode,entry in state.get('modes',{}).items():
        runs=list(entry.get('runs',[]))
        if entry.get('job_id'):
            with jobs.connect() as db:
                tasks=[jobs.status(db,job_id) for job_id in [entry['job_id'],*entry.get('extra_jobs',[])]]
                task=tasks[-1]
                runs=[a['run_id'] for task_entry in tasks for a in task_entry['attempts'] if a['state']=='finished' and a['verdict']!='invalid' and a['run_id']]
                entry['state']=task['state']
                entry['runs']=runs
                # Include provisioning and report work, but exclude queue waiting.
                entry['active_wall_s']=0
                for task_entry in tasks:
                    finished=[a for a in task_entry['attempts'] if a['ended']]
                    if finished:entry['active_wall_s']+=max(a['ended'] for a in finished)-min(a['started'] for a in finished)
        rows=[]
        for run_id in runs:
            folder=manage.OPS/'experiments'/run_id
            summary=load(folder/'summary.json',{})
            manifest=load(folder/'manifest.json',{})
            fps=[float(v) for v in re.findall(r'fps\s+:\s+([0-9.]+)',(folder/'simulator.log').read_text())]
            rows.append(dict(run_id=run_id,scene=manifest.get('scene'),seed=manifest.get('random_seed'),
                verdict=summary.get('verdict'),metrics=summary.get('metrics',{}),
                simulator_fps_recent_median=statistics.median(fps[-12:]) if fps else None,
                provenance=dict(binary=manifest.get('algorithm_binary_sha256'),
                    algorithm=manifest.get('configuration_sha256',{}).get('algorithm.yaml'),
                    weights=manifest.get('algorithm_weights_sha256'),simulator=manifest.get('containers',{}).get('simulator_image_id'),
                    scoring={name:manifest.get('source_files',{}).get(name) for name in ('report.py','shots.py','impact_quality.py','evaluator.py')}),
                failed_rules=[r for r in summary.get('rules',[]) if not r['passed']]))
        modes[mode]=dict(rows=rows,valid_runs=len(rows),active_wall_s=entry.get('active_wall_s'),
            processed_hz_median=statistics.median(r['metrics']['processed_hz'] for r in rows) if rows else None,
            age_p95_max_ms=max(r['metrics']['age_p95_ms'] for r in rows) if rows else None,
            render_fps_median=statistics.median(r['simulator_fps_recent_median'] for r in rows if r['simulator_fps_recent_median'] is not None) if rows else None,
            valid_runs_per_hour=len(rows)*3600/entry['active_wall_s'] if entry.get('active_wall_s') else None)
    state['results']=modes
    checks=[]
    def check(name,passed,**values):checks.append(dict(name=name,passed=bool(passed),**values))
    for mode,data in modes.items():
        check(mode+'_complete',data['valid_runs']==expected,actual=data['valid_runs'],expected=expected)
        rows=data['rows']
        check(mode+'_validity',bool(rows) and all(r['verdict']!='invalid' and not any(x['category']=='validity' for x in r['failed_rules']) for r in rows))
        check(mode+'_performance',bool(rows) and all(r['metrics'].get('processed_hz',0)>=20 and r['metrics'].get('age_p95_ms',float('inf'))<=150 and r['metrics'].get('total_wall_s',float('inf'))<=40 for r in rows))
    if all(m in modes for m in ('desktop','serial','dual')):
        all_rows=[r for data in modes.values() for r in data['rows']]
        check('same_frozen_algorithm_models_images_and_scoring',len({json.dumps(r['provenance'],sort_keys=True) for r in all_rows})==1)
        configs=[]
        scenes={}
        for row in all_rows:
            folder=manage.OPS/'experiments'/row['run_id']/'configuration'
            config=tomllib.loads((folder/'simulator.toml').read_text())
            for section,key in [('window','headless'),('preview','enabled'),('debug','egui'),('debug','inspector'),('camera','pip_enabled')]:
                config.get(section,{}).pop(key,None)
            configs.append(json.dumps(config,sort_keys=True))
            scenes.setdefault((row['scene'],row['seed']),set()).add(experiments.digest(folder/'scenario.toml'))
        check('same_capture_physics_and_gimbal_settings',len(set(configs))==1)
        check('same_scenarios_per_seed',all(len(hashes)==1 for hashes in scenes.values()))
        serial,dual=modes['serial'],modes['dual']
        ratio=(dual['valid_runs_per_hour'] or 0)/(serial['valid_runs_per_hour'] or float('inf'))
        check('dual_throughput_gain_30pct',ratio>=1.3,ratio=ratio)
        for scene in SCENES:
            for base,candidate in (('desktop','serial'),('serial','dual')):
                for key in ('target_hit_rate','weighted_hit_rate'):
                    def aggregate(mode):
                        relevant=[r['metrics'] for r in modes[mode]['rows'] if r['scene']==scene]
                        shots=sum(r.get('shots_fired',0) for r in relevant)
                        numerator=sum(r.get('target_armor_hits',0) if key=='target_hit_rate' else r.get('weighted_hit_score_sum',0) for r in relevant)
                        return numerator/shots if shots else None
                    b,c=aggregate(base),aggregate(candidate)
                    check(f'{scene}_{candidate}_vs_{base}_{key}',b is not None and c is not None and c>=b-.05,baseline=b,candidate=c)
    state['checks']=checks
    state['parallel_qualified']=len(modes)==3 and bool(checks) and all(c['passed'] for c in checks)
    quality={}
    for scene in SCENES:
        quality[scene]={}
        for mode,data in modes.items():
            rows=[r for r in data['rows'] if r['scene']==scene]
            shots=sum(r['metrics'].get('shots_fired',0) for r in rows)
            hits=sum(r['metrics'].get('target_armor_hits',0) for r in rows)
            weight=sum(r['metrics'].get('weighted_hit_score_sum',0) for r in rows)
            quality[scene][mode]=dict(shots=shots,hits=hits,hit_rate=hits/shots if shots else None,
                weighted_hit_rate=weight/shots if shots else None)
    state['quality_by_scene']=quality
    write(output/'acceptance.json',state)
    enabled='已启用' if state['parallel_qualified'] else '未启用，正式任务按串行排队'
    body='<h1>后台实验与双路并行验收</h1><p>正式双路：'+enabled+'</p>'
    body+='<p>桌面入口复用算法进程并重置会话；后台每次尝试启动新进程。渲染主循环未限帧，'
    body+='因此内部缓存与采样节奏可能不同。本报告比较迁移后的整体效果，不能直接将评分差异归因于并行。</p>'
    body+='<table><tr><th>运行方式</th><th>有效实验</th><th>墙钟秒</th><th>有效实验/小时</th><th>处理 Hz 中位数</th><th>帧龄 P95 最坏值 ms</th><th>主循环 FPS 中位数</th></tr>'
    for mode,data in modes.items():
        values=({'desktop':'桌面串行','serial':'后台串行','dual':'后台双路'}[mode],data['valid_runs'],
            data['active_wall_s'],data['valid_runs_per_hour'],data['processed_hz_median'],data['age_p95_max_ms'],data['render_fps_median'])
        body+='<tr>'+''.join('<td>'+html.escape(f'{v:.2f}' if isinstance(v,float) else str(v))+'</td>' for v in values)+'</tr>'
    body+='</table><h2>分场景评分</h2><p>每格为精确命中率 / 加权命中率；括号中为命中 / 发射数。红色表示相比前一种运行方式下降超过 5 个百分点。</p><table><tr><th>场景</th><th>桌面串行</th><th>后台串行</th><th>后台双路</th></tr>'
    for scene,conditions in quality.items():
        body+='<tr><td>'+scene+'</td>';previous=None
        for mode in ('desktop','serial','dual'):
            q=conditions.get(mode,{})
            if q.get('hit_rate') is None:cell='—'
            else:cell=f'{q["hit_rate"]:.1%} / {q["weighted_hit_rate"]:.1%} ({q["hits"]}/{q["shots"]})'
            regression=previous is not None and q.get('hit_rate') is not None and any(q[k]<previous[k]-.05 for k in ('hit_rate','weighted_hit_rate'))
            body+='<td'+(' style="color:#b00020"' if regression else '')+'>'+cell+'</td>';previous=q if q.get('hit_rate') is not None else None
        body+='</tr>'
    body+='</table><h2>检查</h2><p>通过 '+str(sum(c['passed'] for c in checks))+' / '+str(len(checks))+' 项。</p><details><summary>全部检查与数值</summary><pre>'+html.escape(json.dumps(checks,ensure_ascii=False,indent=2))+'</pre></details><h2>实验报告</h2>'
    for mode,data in modes.items():
        body+='<h3>'+mode+'</h3><ul>'+''.join('<li><a href="../'+r['run_id']+'/report.html">'+r['run_id']+'</a> '+str(r['verdict'])+'</li>' for r in data['rows'])+'</ul>'
    if (output/'evidence-audit.json').exists():
        body+='<h2>证据完整性</h2><a href="evidence-audit.json">完整摘要、逐帧 JPEG 解码与 MP4 解码结果</a>'
    if (output/'functional.json').exists():
        body+='<h2>后台功能验收</h2><a href="functional-report.html">冻结、桌面独立性、暂停、取消、故障与恢复</a>'
    if (output/'image-retention.json').exists():
        body+='<h2>镜像保留验收</h2><a href="image-retention.json">提交后重建三种镜像，再恢复任务，确认执行原冻结镜像</a>'
    (output/'report.html').write_text('<!doctype html><meta charset="utf-8"><style>body{font:15px system-ui;margin:2em}table{border-collapse:collapse}td,th{border:1px solid #aaa;padding:.5em}</style>'+body)
    experiments.publish_desktop(manage,output)
    runtime_signature=None
    if state.get('modes',{}).get('dual',{}).get('job_id'):
        with jobs.connect() as db:
            bundle=Path(jobs.get_job(db,state['modes']['dual']['job_id'])['bundle'])
        runtime_signature=jobs.stack_signature(load(bundle/'bundle.json'))
    write(jobs.STATE/'qualification.json',dict(parallel_qualified=state['parallel_qualified'],runtime_signature=runtime_signature,report=str(output/'report.html'),updated=time.time()))
    return state


def desktop(output):
    state=load(output/'acceptance.json',dict(version=1,definition=dict(scenes=SCENES,seeds=SEEDS,defaults=DEFAULTS),modes={}))
    entry=state['modes'].setdefault('desktop',dict(runs=[],active_wall_s=0))
    done={(load(manage.OPS/'experiments'/r/'manifest.json')['scene'],load(manage.OPS/'experiments'/r/'manifest.json')['random_seed']) for r in entry['runs']}
    started=time.monotonic();previous=entry['active_wall_s']
    with (manage.RUNTIME/'operation.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            manage.stop()
            manage.start(argparse.Namespace(**DEFAULTS,scene='static',control='off',view='off',replace=False,fire='auto',command_script=''))
            cases=[(seed,scene) for seed in SEEDS for scene in (SCENES if seed%2 else list(reversed(SCENES)))]
            cases += [(case['seed'],case['scene']) for case in state['definition'].get('supplemental_cases',[])]
            for seed,scene in cases:
                if (scene,seed) in done:continue
                for attempt in range(2):
                    args=argparse.Namespace(**DEFAULTS,seed=seed,duration=None,label='background-acceptance-desktop')
                    folder=experiments.run_one(manage,scene,args)
                    summary=load(folder/'summary.json',{})
                    if summary.get('verdict')!='invalid':
                        entry['runs'].append(folder.name);break
                    state.setdefault('invalid_attempts',[]).append(folder.name)
                    if attempt:raise RuntimeError('Desktop control repeatedly invalid: '+folder.name)
                entry['active_wall_s']=previous+time.monotonic()-started
                write(output/'acceptance.json',state)
                print(json.dumps(dict(mode='desktop',progress=len(entry['runs']),run_id=folder.name)),flush=True)
        finally:
            manage.stop()
            saved=manage.profile();saved.update(scene='static',terrain='arena',fire='off',control='off',algorithm_profile='legacy');manage.save_profile(saved)
            entry['active_wall_s']=previous+time.monotonic()-started
            entry['state']='completed' if len(entry['runs'])==21+len(state['definition'].get('supplemental_cases',[])) else 'paused'
            write(output/'acceptance.json',state)
    collect(output)


def background(output,mode):
    state=load(output/'acceptance.json',dict(version=1,definition=dict(scenes=SCENES,seeds=SEEDS,defaults=DEFAULTS),modes={}))
    if mode not in state['modes']:
        spec=output/(mode+'-spec.json')
        write(spec,dict(version=1,defaults=dict(DEFAULTS,label='background-acceptance-'+mode),
            matrix=dict(scene=SCENES,seed=SEEDS),parallelism=1 if mode=='serial' else 2))
        args=manage.parser().parse_args(['experiment','submit','--spec',str(spec),'--exploratory'])
        # Submit returns structured output to the caller via the new task row.
        task=jobs.submit(args)
        state['modes'][mode]=dict(job_id=task['job_id'],runs=[],state='queued')
        write(output/'acceptance.json',state)
    job_id=state['modes'][mode]['job_id']
    while True:
        with jobs.connect() as db:task=jobs.status(db,job_id)
        print(json.dumps(dict(mode=mode,job_id=job_id,state=task['state'],progress=task['items'])),flush=True)
        if task['state'] in ('completed','paused','cancelled'):
            collect(output)
            if task['state']!='completed':raise RuntimeError('Acceptance task stopped: '+job_id)
            return
        time.sleep(15)


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    p.add_argument('--mode',choices=['desktop','serial','dual','report','confirm','all'],default='all')
    args=p.parse_args();output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    if args.mode in ('desktop','all'):desktop(output)
    if args.mode in ('serial','all'):background(output,'serial')
    if args.mode in ('dual','all'):background(output,'dual')
    if args.mode=='report':collect(output)
    if args.mode=='confirm':confirm(output)


def confirm(output):
    state=collect(output)
    affected=[scene for scene in SCENES if any(not c['passed'] and c['name'].startswith(scene+'_') and '_vs_' in c['name'] for c in state['checks'])]
    if not affected:return
    seeds=[2026102204,2026102205,2026102206]
    # Only affected scenarios receive three additional seeds, in every mode.
    cases=[dict(scene=scene,seed=seed) for scene in affected for seed in seeds]
    state['definition']['supplemental_cases']=cases
    write(output/'acceptance.json',state)
    desktop(output)
    for mode in ('serial','dual'):
        state=load(output/'acceptance.json');entry=state['modes'][mode]
        if entry.get('extra_jobs'):
            job_id=entry['extra_jobs'][-1]
        else:
            spec=output/(mode+'-confirmation-spec.json')
            write(spec,dict(version=1,defaults=dict(DEFAULTS,label='background-confirmation-'+mode),
                matrix=dict(scene=affected,seed=seeds),parallelism=1 if mode=='serial' else 2))
            task=jobs.submit(manage.parser().parse_args(['experiment','submit','--spec',str(spec),'--exploratory']))
            job_id=task['job_id'];entry.setdefault('extra_jobs',[]).append(job_id)
            write(output/'acceptance.json',state)
        while True:
            with jobs.connect() as db:task=jobs.status(db,job_id)
            print(json.dumps(dict(mode=mode,confirmation=True,state=task['state'],progress=task['items'])),flush=True)
            if task['state'] in ('completed','paused','cancelled'):
                collect(output)
                if task['state']!='completed':raise RuntimeError('Confirmation stopped: '+job_id)
                break
            time.sleep(15)


if __name__=='__main__':main()
