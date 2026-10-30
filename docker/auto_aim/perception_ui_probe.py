"""Explicitly requested, bounded UI feature probe after backend screening failure."""
import argparse
import fcntl
import html
import json
import os
from pathlib import Path
import subprocess
import time

import background_jobs as jobs
import experiments
import manage
from decision_benchmark import sha,verify_bundle
from decision_benchmark_report import aggregate
from perception_study import protected,trial_metrics
from report import load,write
from sync_contract_study import settings
from sync_history_study import matched_ui

PARENT=manage.OPS/'experiments/perception-optimization-20261023'
VARIANTS=('baseline','A35','B35')
SCENES=('hf_uniform_3','hf_turn90_3')
SEEDS=(1901,1902)


def initialize(root):
    root.mkdir(parents=True,exist_ok=True)
    if (root/'contract.json').exists():
        c=load(root/'contract.json')
        if sha(root/'contract.json')!=load(root/'state.json')['contract_sha256']:raise ValueError('Probe contract changed')
        if protected()!=c['protected']:raise ValueError('Production artifacts changed')
        return c
    parent=load(PARENT/'state.json');original=load(PARENT/'contract.json')
    if parent['state']!='completed' or parent['selection']['screen'] is not None:
        raise ValueError('Expected the completed, unpromoted backend screen')
    cases=[]
    for i,seed in enumerate(SEEDS):
        for scene in SCENES if i%2==0 else SCENES[::-1]:
            for variant in VARIANTS if i%2==0 else VARIANTS[::-1]:
                cases.append(dict(phase='ui_probe',engine='ui',variant=variant,scene=scene,sensor_seed=seed,repeat_id=i))
    backend=[r for r in parent['runs'] if r['phase']=='screen' and r['sensor_seed'] in SEEDS and r['variant'] in VARIANTS]
    if len(backend)!=12:raise ValueError('Missing paired backend source cases')
    bundles={v:parent['bundles'][v] for v in VARIANTS}
    for b in bundles.values():verify_bundle(Path(b))
    c=dict(version=1,created=time.time(),parent=str(PARENT),parent_contract_sha256=sha(PARENT/'contract.json'),
        bundles=bundles,cases=cases,backend=backend,protected=protected(),original_profile=manage.profile(),
        runtime_files=original['runtime_files'],backend_timing_profile=original['backend_timing_profile'],
        budget=dict(maximum_ui_trials=12,maximum_trial_wall_s=40,no_sample_extension=True),
        purpose='User-requested low-cost characteristic check of both changes on UI, overriding the prior conditional UI policy '
                'for this separate diagnostic only. Original study remains read-only and unpromoted.',
        limitations='Two reused development sensor seeds, selected after backend scores were known. '
                    'Descriptive paired observations only, no independent validation, statistical acceptance, or UI equivalence.',
        backend_source_hashes={r['run_id']:{p:sha(manage.OPS/'experiments'/r['run_id']/p) for p in
                              ('manifest.json','summary.json','run-state.json')} for r in backend})
    write(root/'contract.json',c)
    write(root/'state.json',dict(state='frozen',runs=[],contract_sha256=sha(root/'contract.json'),
        ui_equivalence_certified=False,candidate_promoted=False,capacity_registration=False))
    return c


def key(e):return tuple(e[k] for k in ('variant','scene','sensor_seed'))


def run(root):
    c=initialize(root)
    with (root/'controller.lock').open('a') as controller:
        fcntl.flock(controller,fcntl.LOCK_EX|fcntl.LOCK_NB)
        s=load(root/'state.json')
        if s['state']=='completed':return
        if s.get('current_case'):raise RuntimeError('Interrupted attempt retained; inspect before resuming')
        with jobs.connect() as db:
            if db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running','pausing')").fetchone()[0]:
                raise RuntimeError('Other experiments active')
            if db.execute("SELECT count(*) FROM artifacts WHERE state IN ('queued','running')").fetchone()[0]:
                raise RuntimeError('Other offline media active')
        with (manage.RUNTIME/'operation.lock').open('a') as operation:
            fcntl.flock(operation,fcntl.LOCK_EX|fcntl.LOCK_NB)
            try:
                s['state']='running';write(root/'state.json',s)
                for case in c['cases']:
                    if key(case) in {key(r) for r in s['runs']}:continue
                    if protected()!=c['protected']:raise ValueError('Production artifacts changed')
                    with matched_ui(Path(c['bundles'][case['variant']])):
                        try:
                            manage.stop()
                            manage.start(argparse.Namespace(**dict(settings('static',case['sensor_seed']),
                                control='off',view='off',replace=False,fire='auto')))
                            s['current_case']=case;write(root/'state.json',s)
                            args=argparse.Namespace(**dict(settings(case['scene'],case['sensor_seed']),repeat_id=case['repeat_id'],
                                label='ui-feature-probe-'+case['variant']))
                            folder=experiments.run_one(manage,case['scene'],args)
                            entry=dict(case,run_id=folder.name);s['runs'].append(entry)
                            s.pop('current_case',None);write(root/'state.json',s)
                            if trial_metrics(root,entry,c)['wall_s']>40:raise ValueError('UI trial budget exceeded')
                            print(json.dumps(dict(completed=len(s['runs']),maximum=12,**entry)),flush=True)
                        finally:manage.stop()
                s.update(state='completed',ended=time.time());write(root/'state.json',s)
            except BaseException as e:
                s.update(state='failed',error=repr(e));write(root/'state.json',s);raise
            finally:manage.save_profile(c['original_profile'])


def summarize(root):
    c=initialize(root);s=load(root/'state.json')
    if s['state']!='completed' or len(s['runs'])!=12:raise ValueError('Incomplete probe')
    rows=[trial_metrics(root,r,c) for r in [*s['runs'],*c['backend']]]
    for r in rows:
        folder=manage.OPS/'experiments'/r['run_id'];m=load(folder/'manifest.json')
        b=verify_bundle(Path(c['bundles'][r['variant']]))
        checks=dict(config=m['configuration_sha256']['algorithm.yaml']==b['files']['sp_vision_25/configs/simulator.yaml'],
            binary=m['algorithm_binary_sha256']==b['files']['sp_vision_25/build/simulator/sim_auto_aim'],
            scene=m['scene']==r['scene'],seed=m['sensor_seed']==r['sensor_seed'],
            scene_seed=m['random_seed']==2026102207,input=m['input_contract']=='aligned-v1',warmup=m['warmup_frames']==30,
            simulator=m['containers']['simulator_image_id']==b['images']['simulator'],
            evaluator=m['containers']['evaluator_image_id']==b['images']['evaluator'])
        if not all(checks.values()):raise ValueError('Probe contract mismatch: '+repr(checks))
        r['contract_checks']=checks
    scenarios={}
    for r in rows:
        m=load(manage.OPS/'experiments'/r['run_id']/'manifest.json')
        value=(m['configuration_sha256']['scenario.toml'],m['duration_s'],m['shooting']['bullet_speed'],
               m['shooting']['physics'],m['shooting']['fire_policy'],m['shooting']['fire_rate_limit_hz'])
        if r['scene'] in scenarios and scenarios[r['scene']]!=value:
            raise ValueError('UI/backend scenario or shooting settings differ')
        scenarios[r['scene']]=value
    groups=[];deltas=[]
    for engine in ('backend','ui'):
        for scene in SCENES:
            for variant in VARIANTS:
                rr=[r for r in rows if (r['engine'],r['scene'],r['variant'])==(engine,scene,variant)]
                if len(rr)!=2:raise ValueError('Incomplete paired cell')
                groups.append(dict(engine=engine,scene=scene,variant=variant,**aggregate(rr),
                    initial_latency_max_s=max(r['initial_latency_s'] for r in rr),
                    first_hit_max_s=max(r['first_hit_s'] for r in rr)))
            a=next(g for g in groups if (g['engine'],g['scene'],g['variant'])==(engine,scene,'baseline'))
            for variant in VARIANTS[1:]:
                b=next(g for g in groups if (g['engine'],g['scene'],g['variant'])==(engine,scene,variant))
                deltas.append(dict(engine=engine,scene=scene,variant=variant,
                    weighted_delta=b['weighted_hit_rate']-a['weighted_hit_rate'] if a['weighted_hit_rate'] is not None and b['weighted_hit_rate'] is not None else None,
                    exact_delta=b['target_hit_rate']-a['target_hit_rate'] if a['target_hit_rate'] is not None and b['target_hit_rate'] is not None else None,
                    hits_per_s_delta=b['hits_per_s']-a['hits_per_s'],
                    initial_latency_max_delta_s=b['initial_latency_max_s']-a['initial_latency_max_s'],
                    first_hit_max_delta_s=b['first_hit_max_s']-a['first_hit_max_s']))
    for run,hashes in c['backend_source_hashes'].items():
        if any(sha(manage.OPS/'experiments'/run/p)!=h for p,h in hashes.items()):raise ValueError('Backend source changed')
    paired=[]
    for engine in ('backend','ui'):
        for scene in SCENES:
            for seed in SEEDS:
                a=next(r for r in rows if (r['engine'],r['scene'],r['sensor_seed'],r['variant'])==(engine,scene,seed,'baseline'))
                for variant in VARIANTS[1:]:
                    b=next(r for r in rows if (r['engine'],r['scene'],r['sensor_seed'],r['variant'])==(engine,scene,seed,variant))
                    aw=a['weight']/a['shots'] if a['shots'] else None;bw=b['weight']/b['shots'] if b['shots'] else None
                    paired.append(dict(engine=engine,scene=scene,sensor_seed=seed,variant=variant,
                        baseline_weighted=aw,candidate_weighted=bw,
                        weighted_delta=bw-aw if aw is not None and bw is not None else None,
                        initial_wait_delta_s=b['initial_latency_s']-a['initial_latency_s'],
                        first_hit_delta_s=b['first_hit_s']-a['first_hit_s'],
                        first_hit_censored=[a['first_hit_censored'],b['first_hit_censored']]))
    result=dict(completed=True,groups=groups,deltas=deltas,paired_seeds=paired,trials=rows,independent_validation=False,
                qualification='Descriptive characteristic probe, two seeds; no statistical acceptance.',
                algorithm_promoted=False,ui_equivalence_certified=False)
    write(root/'result.json',result)
    return result


def render(root,result):
    pct=lambda x:'—' if x is None else f'{x:.2%}'
    lines=['# 低成本 UI 特征核对','','本轮由用户明确要求，在后台候选未通过后另开诊断批次。'
           '原实验结论不变；没有调参、追加样本或推广候选。', '',
           '基线、A35、B35 × 匀速／转向 × 1901、1902 两个种子，共 12 轮 UI。'
           '与同种子既有后台 12 轮配对，不再新增后台实验。统一评分 v2。', '',
           '**样本少且复用了已知开发种子，仅用于核对特征，不是独立验证或正式验收。**', '',
           '[冻结定义](contract.json) · [逐轮及变化结果](result.json) · [证据审计](validation.json)', '',
           '| 模式 | 场景 | 配置 | 命中/发数 | 精确命中 | 加权命中 | 命中/秒 | 初始化最大等待 | 首次命中最大时间 |',
           '|---|---|---|---:|---:|---:|---:|---:|---:|']
    for g in result['groups']:
        lines.append(f"| {g['engine']} | {g['scene']} | {g['variant']} | {g['hits']}/{g['shots']} | "
            f"{pct(g['target_hit_rate'])} | {pct(g['weighted_hit_rate'])} | {g['hits_per_s']:.3f} | "
            f"{g['initial_latency_max_s']:.3f}s | {g['first_hit_max_s']:.3f}s |")
    lines+=['','## 相对各自基线的变化','','| 模式 | 场景 | 候选 | 加权变化 | 精确变化 | 命中/秒变化 |',
            '|---|---|---|---:|---:|---:|']
    for d in result['deltas']:
        pp=lambda v:'—' if v is None else f'{v*100:+.2f}pp'
        lines.append(f"| {d['engine']} | {d['scene']} | {d['variant']} | {pp(d['weighted_delta'])} | {pp(d['exact_delta'])} | {d['hits_per_s_delta']:+.3f} |")
    lines+=['','## 每个种子的变化','','| 模式 | 场景 | 种子 | 候选 | 加权变化 | 初始化等待变化 | 首次命中时间变化 |',
            '|---|---|---:|---|---:|---:|---:|']
    for d in result['paired_seeds']:
        gain='—' if d['weighted_delta'] is None else f"{d['weighted_delta']*100:+.2f}pp"
        lines.append(f"| {d['engine']} | {d['scene']} | {d['sensor_seed']} | {d['variant']} | {gain} | "
                     f"{d['initial_wait_delta_s']:+.3f}s | {d['first_hit_delta_s']:+.3f}s |")
    lines+=['','首次命中缺失时以测量窗口长度记作截断值；逐轮结果同时保留截断标记。', '',
            '## 预算与范围','',
            f"UI 单轮最大用时 {max(r['wall_s'] for r in result['trials'] if r['engine']=='ui'):.2f} 秒，"
            '总采集固定 12 轮，无低分替换或追加。', '']
    lines+=['','## 原始视频','']
    for r in result['trials']:
        if r['engine']=='ui':
            lines.append(f"- {r['scene']} / {r['sensor_seed']} / {r['variant']}："
                         f"[报告](../{r['run_id']}/report.html) · [视频](../{r['run_id']}/annotated.mp4)")
    lines+=['','两轮中的最大延迟只描述已测样本，不估计总体 P90。不能用不同模式的绝对分差替代候选相对基线的变化。',
            '','正式二进制、默认配置和原始后台证据保持不变；UI 使用 VT8。完整人工视频验收未进行。','']
    (root/'report.md').write_text('\n'.join(lines))
    body=[];table=False
    import re
    def inline(s):return re.sub(r'\[([^\]]+)\]\(([^)]+)\)',r'<a href="\2">\1</a>',html.escape(s))
    for line in lines:
        if line.startswith('|'):
            if not table:body.append('<table>');table=True
            cells=[v.strip() for v in line.strip('|').split('|')]
            if all(re.fullmatch('[-:]+',v) for v in cells):continue
            body.append('<tr>'+''.join('<td>'+inline(v)+'</td>' for v in cells)+'</tr>');continue
        if table:body.append('</table>');table=False
        if line:body.append('<p>'+inline(line)+'</p>')
    if table:body.append('</table>')
    (root/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>UI 特征核对</title>'
        '<style>body{font:16px system-ui;max-width:1300px;margin:2em auto;line-height:1.6}table{border-collapse:collapse}td{border:1px solid #bbb;padding:.4em}</style>'+''.join(body))


def finish(root):
    c=initialize(root);result=summarize(root);image=verify_bundle(Path(c['bundles']['baseline']))['images']['evaluator']
    subprocess.run(['docker','run','--rm','--network','none','--cpus','2','--user',f'{os.getuid()}:{os.getgid()}',
        '-v',str(manage.ROOT)+':'+str(manage.ROOT)+':ro','-v',str(root)+':'+str(root),'-w',str(manage.ROOT),
        '--entrypoint','python3',image,'-B','docker/auto_aim/decision_benchmark_validate.py','--audit-only','--output',str(root)],
        check=True,timeout=300)
    audit=load(root/'evidence-audit.json')
    if not audit['passed']:raise ValueError('UI evidence audit failed')
    if protected()!=c['protected'] or manage.profile()!=c['original_profile']:raise ValueError('Production state was not restored')
    ui=[r for r in result['trials'] if r['engine']=='ui'];maximum=max(r['wall_s'] for r in ui)
    if maximum>40:raise ValueError('Probe budget exceeded')
    write(root/'validation.json',dict(passed=True,ui_trials=12,reused_backend_trials=12,maximum_ui_trial_wall_s=maximum,
        evidence={k:v for k,v in audit.items() if k!='results'},production_unchanged=True,backend_originals_unchanged=True,
        independent_validation=False,candidate_promoted=False,human_video_acceptance=False,
        vt=Path('/sys/class/tty/tty0/active').read_text().strip(),source_sha256=sha(Path(__file__))))
    render(root,result);experiments.publish_desktop(manage,root)
    print(json.dumps(result['deltas'],indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['run','finish']);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();root=a.output.resolve()
    if a.action=='run':run(root)
    else:finish(root)
