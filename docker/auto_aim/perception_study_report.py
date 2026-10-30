"""Report every stage, including failures that prevent UI acquisition."""
import argparse
import html
import json
import os
from pathlib import Path
import re
import subprocess
import time

import background_jobs as jobs
import manage
from decision_benchmark import sha,verify_bundle
from perception_study import freeze,protected,trial_metrics
from report import load,write


def render(root):
    c=load(root/'contract.json');s=load(root/'state.json')
    lines=['# 感知优化：后台优先实验报告','','## 当前结论','',
           '本轮实现角点矫正检查 A 与多装甲初始化一致性 B，均默认关闭。正式 v3、模型、模拟器、评分和时序保持冻结。',
           '',f"状态：{s['state']}；新闭环 {len(s['runs'])} 轮；原图回放 {len(s['replays'])} 段。",'',
           f"后台开发候选：{s.get('screen_variants',[])}；选中：{s.get('selection',{}).get('screen')}。",'',
           f"后台独立验证达到 UI 门槛：{s.get('ui_eligible',False)}；实际启动 UI：{s.get('ui_scheduled',False)}。",'',
           '[冻结定义](contract.json) · [恢复状态](state.json) · [验收审计](validation.json)','',
           '## 原图参数筛选','','| 候选 | 中心误差 P95 | 有效覆盖率 | 初始化延迟 P90 | 通过覆盖及延迟护栏 |',
           '|---|---:|---:|---:|---|']
    for name,r in s.get('replay_summaries',{}).items():
        err='无有效估计' if r['error_p95_m'] is None else f"{r['error_p95_m']:.4f} m"
        lines.append(f"| {name} | {err} | {r['coverage']:.2%} | {r['initial_latency_p90_s']:.4f} s | {r.get('eligible','基线')} |")
    if s.get('selection',{}).get('replay',{}).get('B') is None:
        lines+=['','多装甲一致性候选没有通过预定覆盖率／延迟护栏，未进入后台闭环，也没有与 A 组合。'
                '这表示当前拒绝初始化策略过于保守；不能把拒绝后剩余样本误差较低解释为修复成功。']
    lines+=['','A15/A25/A35 分别限制相对端点变化、灯条长度变化及左右长度比变化，拒绝时保留网络原始角点。'
            'B15/B25/B35 使用多装甲投影的一对一匹配，置信度只参与最后平局排序；没有一致支持时暂缓初始化。'
            '所有候选复用同一输入；目标真值只在推理退出后评分，回放没有闭环命中率。','',
            '## 后台闭环开发成绩','','| 候选 | 场景 | 发数 | 精确命中 | 加权命中 | 命中/秒 |',
            '|---|---|---:|---:|---:|---:|']
    screen=load(root/'screen-result.json',[])
    for r in screen:
        for scene,g in r['groups'].items():
            if scene=='all':continue
            for variant,z in [('baseline',g['baseline']),(r['variant'],g['candidate'])]:
                def pct(v):return '—' if v is None else f'{v:.2%}'
                lines.append(f"| {variant} | {scene} | {z['shots']} | {pct(z['target_hit_rate'])} | {pct(z['weighted_hit_rate'])} | {z['hits_per_s']:.3f} |")
    if screen and not any(r['passed'] for r in screen):
        lines+=['','没有候选通过开发阶段全部效果护栏，因此按预定规则结束：不追加独立种子，不运行 UI，不推广候选。',
                '回放中的几何误差改善并未自动转化为合格的闭环射击收益。','']
    lines+=['','## 预定门槛判定','']
    for stage,entries in [('screen',screen),('confirm',[load(root/'confirm-result.json')] if (root/'confirm-result.json').exists() else []),
                          ('ui',[load(root/'ui-result.json')] if (root/'ui-result.json').exists() else [])]:
        for r in entries:
            lines += [f"- {stage} / {r['variant']}：通过={r['passed']}；未通过项："
                      +', '.join(k for k,v in r['checks'].items() if not v)+f"；加权收益 95% 区间={r['weighted_gain_95']}。"]
    lines+=['','开发收益门槛：合并加权 +3pp，各场景加权与精确命中不下降超过 2pp，命中/秒至少保留 95%，'
            '初始化和首次有效命中 P90 增加不超过 100ms；无命中比例不得增加。独立验证另要求配对区间下界大于零。',
            '','后台开发种子 1901–1905；独立验证种子 2001–2010。不按分数追加样本。没有明确收益则不启动 UI。',
            '','## 范围与限制','','初始化使用已有 0.20m 半径先验，匹配时评估 ±0.01m 不确定度；未读取模拟器实测几何。'
            '多目标通过同类别、同优先级及局部投影范围隔离。四板小装甲以外保持原初始化。',
            '','独立构建没有覆盖正式二进制或默认配置，旧镜像和旧证据保留。不授予新的并发或 UI 等效资格。'
            '人工逐视频验收未进行；解码和哈希审计另列。','',
            '![原图误差与覆盖](replay-screen.png)','']
    text='\n'.join(lines);(root/'report.md').write_text(text)
    body=[];table=False
    def inline(x):
        x=html.escape(x);x=re.sub(r'!\[([^\]]*)\]\(([^)]+)\)',r'<img alt="\1" src="\2">',x)
        return re.sub(r'\[([^\]]*)\]\(([^)]+)\)',r'<a href="\2">\1</a>',x)
    for line in lines:
        if line.startswith('|'):
            if not table:body.append('<table>');table=True
            cells=[v.strip() for v in line.strip('|').split('|')]
            if all(re.fullmatch('[-:]+',v) for v in cells):continue
            body.append('<tr>'+''.join('<td>'+inline(v)+'</td>' for v in cells)+'</tr>');continue
        if table:body.append('</table>');table=False
        if line.startswith('#'):
            n=len(line)-len(line.lstrip('#'));body.append(f'<h{n}>'+inline(line[n:].strip())+f'</h{n}>')
        elif line:body.append('<p>'+inline(line)+'</p>')
    if table:body.append('</table>')
    (root/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>感知优化实验</title>'
        '<style>body{font:16px system-ui;line-height:1.6;max-width:1250px;margin:2em auto}'
        'table{border-collapse:collapse}td{border:1px solid #bbb;padding:.4em}img{max-width:100%}</style>'+''.join(body))


def plot(root):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    d=load(root/'state.json')['replay_summaries'];names=list(d)
    fig,axes=plt.subplots(1,2,figsize=(12,4),layout='constrained')
    axes[0].bar(names,[d[n]['error_p95_m'] or 0 for n in names]);axes[0].set(ylabel='Center error P95 (m)',title='Frozen native input replay')
    axes[1].bar(names,[100*d[n]['coverage'] for n in names]);axes[1].set(ylabel='Valid state + truth coverage (%)',ylim=(0,100),title='Coverage includes warmup')
    for ax in axes:ax.grid(axis='y',alpha=.2)
    for ext in ('png','svg'):fig.savefig(root/('replay-screen.'+ext),dpi=170)
    plt.close(fig)


def finish(root):
    c=freeze(root);state=load(root/'state.json')
    if state['state']!='completed':raise ValueError('Complete all conditional stages before sealing')
    own={j['id'] for j in state['jobs']}
    from background_pipeline import enqueue
    with jobs.connect() as db:
        for r in state['runs']:
            if r['engine']=='backend':enqueue(db,r['job_id'],state['bundles'][r['variant']],[r['run_id']])
    deadline=time.monotonic()+600
    while True:
        with jobs.connect() as db:media=[dict(r) for r in db.execute('SELECT * FROM artifacts') if r['job_id'] in own]
        if any(r['state'] in ('failed','cancelled') for r in media):raise RuntimeError('Media failure retained')
        if len(media)==sum(r['engine']=='backend' for r in state['runs']) and all(r['state']=='completed' for r in media):break
        if time.monotonic()>deadline:raise RuntimeError('Media deadline exceeded')
        time.sleep(2)
    image=verify_bundle(Path(c['base_bundle']))['images']['evaluator']
    subprocess.run(['docker','run','--rm','--network','none','--cpus','2','--user',f'{os.getuid()}:{os.getgid()}',
        '-e','MPLCONFIGDIR=/tmp/matplotlib','-v',str(manage.ROOT)+':'+str(manage.ROOT)+':ro',
        '-v',str(root)+':'+str(root),'-w',str(manage.ROOT),'--entrypoint','python3',image,
        '-B','docker/auto_aim/perception_study_report.py','audit','--output',str(root)],check=True,timeout=300)
    audit=load(root/'evidence-audit.json')
    if not audit['passed']:raise ValueError('Evidence audit failed')
    metrics=[trial_metrics(root,r,c) for r in state['runs']]
    for replay in state['replays']:
        baseline=next(r for r in state['replays'] if r['variant']=='baseline' and r['run_id']==replay['run_id'])
        if baseline['input_sha256']!=replay['input_sha256']:
            raise ValueError('A replay variant received a different original input')
        plan=load(Path(replay['replay'])/'plan.json')
        original=manage.OPS/'experiments'/replay['run_id']/'session-evidence/input.mjpg'
        if sha(original)!=plan['source_archive_sha256']:
            raise ValueError('Original replay input archive changed')
        if replay['variant']=='baseline' and any(replay['baseline_differences'][k]['different_frames'] for k in
                                                  ('observations','filter_state','selected_plate_id')):
            raise ValueError('Disabled algorithm compatibility failed')
    scenario_hashes={};trial_contracts=[]
    runtime_checks=dict(refinement_accepted=0,refinement_rejected=0,pending_initialization_frames=0,
                        consistency_initializations=0)
    for r in state['runs']:
        folder=manage.OPS/'experiments'/r['run_id'];m=load(folder/'manifest.json')
        meta=verify_bundle(Path(state['bundles'][r['variant']]))
        checks=dict(binary=m['algorithm_binary_sha256']==meta['files']['sp_vision_25/build/simulator/sim_auto_aim'],
            config=m['configuration_sha256']['algorithm.yaml']==meta['files']['sp_vision_25/configs/simulator.yaml'],
            simulator=m['containers']['simulator_image_id']==meta['images']['simulator'],
            evaluator=m['containers']['evaluator_image_id']==meta['images']['evaluator'],
            input_contract=m['input_contract']=='aligned-v1',warmup=m['warmup_frames']==30,
            sensor_seed=m['sensor_seed']==r['sensor_seed'],scene=m['scene']==r['scene'],
            scene_seed=m['random_seed']==2026102207,terrain=m['terrain']=='flat_range',
            physics=m['shooting']['physics']=='no-drag',advice=m['shooting']['fire_policy']=='advice',
            frequency=m['shooting']['fire_rate_limit_hz']==5.,
            execution=m['execution']['mode']==('lockstep' if r['engine']=='backend' else 'realtime'))
        if r['engine']=='backend':
            checks['timing']=load(folder/'configuration/timing-profile.json')['name']==c['backend_timing_profile']
            checks['one_lane']=m['background']['effective_parallelism']==1
        scenario=m['configuration_sha256']['scenario.toml'];key=(r['scene'],m['duration_s'])
        if key in scenario_hashes:checks['same_scenario']=scenario==scenario_hashes[key]
        scenario_hashes[key]=scenario
        if not all(checks.values()):raise ValueError('Frozen trial contract violation: '+repr((r['run_id'],checks)))
        trial_contracts.append(dict(run_id=r['run_id'],checks=checks))
        for line in (folder/'session-evidence/events.jsonl').open():
            packet=json.loads(line)
            if packet['kind']!='result':continue
            actual=packet['data']['result']
            for observed in actual.get('observations',[]):
                guard=observed.get('refinement')
                if guard is None:continue
                runtime_checks['refinement_accepted' if guard['accepted'] else 'refinement_rejected']+=1
                if not guard['accepted'] and 'raw' in guard and observed['corners']!=guard['raw']:
                    raise ValueError('Rejected refinement did not preserve original corners')
            initial=actual.get('continuity',{}).get('initial_consistency')
            if initial is None:continue
            if initial['status']=='pending_inconsistent':
                runtime_checks['pending_initialization_frames']+=1
                if actual.get('filter_state') is not None or actual.get('fire_advice'):
                    raise ValueError('Pending initialization emitted a track or firing advice')
            elif initial['status']=='consistent':
                runtime_checks['consistency_initializations']+=1
                if not initial['hypotheses'] or initial['hypotheses'][0]['support']<2:
                    raise ValueError('Consistency initialization has no dual-plate support')
    with jobs.connect() as db:
        attempts=[dict(r) for r in db.execute('SELECT a.*,i.job_id FROM attempts a JOIN items i ON i.id=a.item_id') if r['job_id'] in own]
    if len(attempts)!=sum(r['engine']=='backend' for r in state['runs']) or any(r['verdict']=='invalid' for r in attempts):
        raise ValueError('Failed or repeated attempts require explicit inspection')
    if protected()!=c['protected']:raise ValueError('Production artifacts changed')
    for b in state['bundles'].values():verify_bundle(Path(b))
    checks=(root/'python-checks.log').read_text();cpp=(root/'cpp-checks.log').read_text()
    if not checks.rstrip().endswith('OK') or 'PASSED' not in cpp:raise ValueError('Unit checks incomplete')
    validation=dict(passed=True,live_trials=len(metrics),replays=len(state['replays']),
        replay_frames=sum(r['frames'] for r in state['replays']),protected_production_unchanged=True,
        frozen_contract_sha256=sha(root/'contract.json'),all_scoring_version=2,
        max_trial_wall_s=max((r['wall_s'] for r in metrics),default=None),
        ui_scheduled=state['ui_scheduled'],ui_eligible=state.get('ui_eligible',False),
        implementation_and_evidence_passed=True,
        screen_effect_passed=any(r['passed'] for r in load(root/'screen-result.json',[])),
        independent_backend_effect_passed=load(root/'confirm-result.json',{}).get('passed'),
        ui_effect_passed=load(root/'ui-result.json',{}).get('passed'),
        selected=state['selection'],capacity_registration=False,human_video_acceptance=False,
        evidence={k:v for k,v in audit.items() if k!='results'},
        trial_contracts=trial_contracts,
        runtime_behavior_checks=runtime_checks,
        sources={p.name:sha(p) for p in manage.OPS.glob('perception_study*.py')},
        source_metrics=metrics)
    write(root/'validation.json',validation);render(root)
    from experiments import publish_desktop
    publish_desktop(manage,root)
    print(json.dumps({k:v for k,v in validation.items() if k not in ('source_metrics','sources','trial_contracts')},indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['finish','audit']);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();root=a.output.resolve()
    if a.action=='audit':
        from decision_benchmark_validate import evidence_audit
        evidence_audit(root);plot(root)
    else:finish(root)
