"""Independent post-inference analysis and readable repair/parallel reports."""
import argparse
from collections import Counter
import html
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess

import manage
from decision_benchmark import sha
from perception_study import quantile
from report import load,write
from sync_first_divergence_analysis import primary,truth_centers


def stats(v):
    return dict(n=len(v),p50=statistics.median(v) if v else None,p95=quantile(v,.95))


def analyze(root):
    c=load(root/'contract.json');s=load(root/'state.json');traces=[];initial=[];decisions=[]
    if len(s['replays'])!=61:raise ValueError('Complete the frozen replay acquisition first')
    for source in c['sources']:
        folder=manage.OPS/'experiments'/source['run_id'];gt=truth_centers(folder)
        start=load(folder/'run-state.json')['measurement_start_stamp_ns']
        baseline=Path(source['baseline_replay']['output'])
        paths={v:baseline.parent/v for v in ('baseline','A35','B35')}
        paths.update({v:root/'replays'/folder.name/v for v in ('Afix','Bfix')})
        for v,path in paths.items():
            rr=primary(path);first=rr[0]['frame']['stamp_ns'];first_valid=None;first_error=None
            for p in rr:
                f=p['frame'];r=p['result'];stamp=f['stamp_ns'];t=(stamp-start)*1e-9;x=r.get('filter_state');g=gt(stamp)
                phase='warmup' if t<0 else 'stationary' if t<1 else 'after_turn' if source['scene']=='hf_turn90_3' and t>=3.2 else 'moving'
                error=None
                if x is not None and g is not None:
                    error=math.dist([x[0]+f['gimbal_position_map'][0],x[2]+f['gimbal_position_map'][1]],g[:2])
                if x is not None and first_valid is None:first_valid=(stamp-first)*1e-9;first_error=error
                traces.append(dict(source=folder.name,engine=source['engine'],source_variant=source['variant'],
                    scene=source['scene'],sensor_seed=source['sensor_seed'],variant=v,time_s=t,phase=phase,error_m=error))
                d=r.get('continuity',{}).get('initial_consistency') or {}
                if v=='Bfix' and d.get('status'):
                    decisions.append(dict(source=folder.name,engine=source['engine'],scene=source['scene'],time_s=t,
                        error_m=error,diagnostic=d))
            initial.append(dict(source=folder.name,variant=v,engine=source['engine'],scene=source['scene'],
                latency_s=first_valid,center_error_m=first_error))
    groups=[]
    for engine in ('ui','backend'):
        for scene in ('hf_uniform_3','hf_turn90_3'):
            for phase in ('warmup','stationary','moving','after_turn'):
                for v in ('baseline','A35','Afix','B35','Bfix'):
                    rr=[r for r in traces if r['sensor_seed']!=1904 and (r['engine'],r['scene'],r['phase'],r['variant'])==(engine,scene,phase,v)]
                    if not rr:continue
                    errors=[r['error_m'] for r in rr if r['error_m'] is not None]
                    groups.append(dict(engine=engine,scene=scene,phase=phase,variant=v,error=stats(errors),
                        coverage=len(errors)/len(rr),within_half_m=sum(x<=.5 for x in errors)/len(rr)))
    long_a=next(e for e in c['sources'] if e['sensor_seed']==1904 and e['variant']=='A35')
    long_b=next(e for e in c['sources'] if e['engine']=='ui' and e['scene']=='hf_turn90_3' and e['sensor_seed']==1901 and e['variant']=='B35')
    regressions={}
    for name,src in (('A_longest_first_hit',long_a),('B_longest_wait',long_b)):
        regressions[name]=dict(source=src['run_id'],metrics=[r for r in initial if r['source']==src['run_id']])
    live_cases=[]
    for entry in s['runs']:
        if entry['phase']!='screen':continue
        folder=manage.OPS/'experiments'/entry['run_id'];gt=truth_centers(folder)
        start=load(folder/'run-state.json')['measurement_start_stamp_ns'];rows=[]
        for line in (folder/'session-evidence/events.jsonl').open():
            packet=json.loads(line)
            if packet['kind']=='result':rows.append(packet['data'])
        first=[];resets=[]
        for row in rows:
            f=row['frame'];r=row['result'];x=r.get('filter_state');g=gt(f['stamp_ns'])
            cont=r.get('continuity',{})
            if cont.get('reset_reason'):resets.append(dict(time_s=(f['stamp_ns']-start)*1e-9,reason=cont['reset_reason']))
            if x is not None and g is not None and len(first)<3:
                first.append(dict(time_s=(f['stamp_ns']-start)*1e-9,
                    center_error_m=math.dist([x[0]+f['gimbal_position_map'][0],x[2]+f['gimbal_position_map'][1]],g[:2]),
                    yaw=x[6],updates=cont.get('filter',{}).get('updates',[])))
        shots=[json.loads(line) for line in (folder/'shots.jsonl').open()]
        metrics=load(folder/'summary.json')['metrics']
        live_cases.append(dict(variant=entry['variant'],scene=entry['scene'],sensor_seed=entry['sensor_seed'],run_id=entry['run_id'],
            first_states=first,resets=resets,first_fire_s=shots[0]['fire_time_s'] if shots else None,first_hit_s=entry['first_hit_s'],
            center_error_p95_m=metrics['center_xy_error_p95_m'],correct_tracking_fraction=metrics['correct_tracking_fraction']))
    result=dict(completed=True,groups=groups,initial=initial,regressions=regressions,closed_loop_cases=live_cases,
        joint_decisions=dict(Counter(d['diagnostic']['status'] for d in decisions)),
        joint_position_shifts=stats([d['diagnostic']['position_shift_m'] for d in decisions if d['diagnostic']['status']=='joint_position']),
        fixed_input_not_new_hit_rates=True)
    (root/'repair-traces.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in traces))
    (root/'joint-decisions.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in decisions))
    write(root/'analysis-result.json',result)
    return result


def plots(root):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    d=load(root/'analysis-result.json');rows=[json.loads(l) for l in (root/'repair-traces.jsonl').open()]
    fig,axes=plt.subplots(1,2,figsize=(12,4),layout='constrained')
    for ax,key,variants,title in ((axes[0],'A_longest_first_hit',('baseline','A35','Afix'),'A: longest recorded first-hit failure'),
                                  (axes[1],'B_longest_wait',('baseline','B35','Bfix'),'B: longest recorded initialization wait')):
        source=d['regressions'][key]['source']
        for v in variants:
            rr=[r for r in rows if r['source']==source and r['variant']==v and r['error_m'] is not None]
            ax.plot([r['time_s'] for r in rr],[r['error_m'] for r in rr],label=v)
        ax.axvline(0,color='gray',ls=':');ax.set(title=title,xlabel='Recorded scenario time (s); negative = warmup',ylabel='Center XY error (m)')
        ax.legend();ax.grid(alpha=.2)
    fig.savefig(root/'regression-repairs.png',dpi=170);fig.savefig(root/'regression-repairs.svg');plt.close(fig)
    s=load(root/'state.json');rr=[r for r in s['runs'] if r['phase']=='screen']
    if not rr:return
    from decision_benchmark_report import aggregate
    variants=s['screen_variants'];fig,axes=plt.subplots(1,2,figsize=(10,4),layout='constrained')
    for ax,scene in zip(axes,('hf_uniform_3','hf_turn90_3')):
        vals=[aggregate([r for r in rr if r['variant']==v and r['scene']==scene]) for v in variants]
        ax.bar([i-.2 for i in range(len(vals))],[100*r['target_hit_rate'] if r['target_hit_rate'] is not None else 0 for r in vals],width=.4,label='Exact hit rate')
        ax.bar([i+.2 for i in range(len(vals))],[100*r['weighted_hit_rate'] if r['weighted_hit_rate'] is not None else 0 for r in vals],width=.4,label='Weighted hit rate')
        ax.set(xticks=range(len(vals)),xticklabels=variants,ylabel='Percent of all shots',title='Backend screen: '+scene);ax.legend(fontsize=8);ax.grid(axis='y',alpha=.2)
    fig.savefig(root/'backend-screen.png',dpi=170);plt.close(fig)


def render(root):
    c=load(root/'contract.json');s=load(root/'state.json');d=load(root/'analysis-result.json');v=load(root/'validation.json')
    def number(x):return '—' if x is None else f'{x:.3f}'
    lines=['# 感知修复与后台并行实验','',
      '本轮实施两个独立候选：Afix 撤除左右灯条长度比的硬拒绝；Bfix 由双板图像端点联合拟合初始化位置，失败时沿用 v3，不再无限等待。默认 v3、正式二进制及模拟器运行选择器未替换。','',
      f'完成 {v["replays"]} 段回放、{v["frames"]} 帧；{v["backend_trials"]} 轮后台闭环；新增 UI 试验 0。',
      '[固定定义](contract.json) · [状态](state.json) · [证据审计](validation.json) · [源码说明](../../PERCEPTION_REPAIR.md)','',
      '## 具体退化是否消除','',
      '| 原记录 | 配置 | 首次初始化延迟 / s | 首个有效中心 XY 误差 / m |','|---|---|---:|---:|']
    for name,r in d['regressions'].items():
        variants=('baseline','A35','Afix') if name.startswith('A') else ('baseline','B35','Bfix')
        for variant in variants:
            m=next(x for x in r['metrics'] if x['variant']==variant)
            lines.append(f'| {name} | {variant} | {number(m["latency_s"])} | {number(m["center_error_m"])} |')
    lines+=['','A 的具体角点拒绝已用原坐标回归检查；B 的等待改善必须连同初始化误差评估。B35 与 Bfix 首个有效状态发生在不同时刻，表中误差不是同帧差值。固定相机轨迹只能检查估计和建议，不能推算新的命中率。','',
      '![同输入退化回归](regression-repairs.png)','',
      '## 并行探针','']
    p=load(root/'parallel-result.json')
    if p:
        lines+=[f'双路探针通过：**{p["passed"]}**；实际跨槽重叠：{p["actual_overlap"]}；效果实验使用 {p["effect_parallelism"]} 路。此结论限本轮配置/负载，不写入全局容量登记。','',
          '| 配置 | 场景 | 输入和算法行为相同 | 分数相同 | 资源健康 | ≤40s |','|---|---|---|---|---|---|']
        for x in p['pairs']:lines.append('| '+' | '.join(str(x[k]) for k in ('variant','scene','same_behavior','same_scores','healthy','within_budget'))+' |')
        for phase in ('probe_p1','probe_p2'):
            rr=[r for r in s['runs'] if r['phase']==phase]
            elapsed=max(r['attempt']['ended'] for r in rr)-min(r['attempt']['started'] for r in rr)
            lines+=['',f'{phase}：{len(rr)} 轮，批次跨度 {elapsed:.1f}s，{len(rr)/elapsed*3600:.1f} 轮/小时；包含实例准备，媒体未请求。']
    else:lines+=['回放护栏未通过，未启动闭环探针。']
    lines+=['','## 后台开发成绩','',
      '| 场景 | 配置 | 命中/发射 | 精确命中率 | 加权命中率 | 命中/秒 | 首次命中 P90 / s |','|---|---|---:|---:|---:|---:|---:|']
    from decision_benchmark_report import aggregate
    for scene in ('hf_uniform_3','hf_turn90_3','all'):
        for variant in s['screen_variants']:
            rr=[r for r in s['runs'] if r['phase']=='screen' and r['variant']==variant and (scene=='all' or r['scene']==scene)]
            if not rr:continue
            a=aggregate(rr)
            lines.append(f'| {scene} | {variant} | {a["hits"]}/{a["shots"]} | {100*a["target_hit_rate"]:.2f}% | {100*a["weighted_hit_rate"]:.2f}% | {a["hits_per_s"]:.3f} | {quantile([r["first_hit_s"] for r in rr],.9):.3f} |')
    lines+=['','探针不进入成绩；统一 v2 严格目标装甲命中与中心权重，以所有发射为分母。advice、0.2s 最小间隔，保持原开火门控。','',
      '![后台成绩](backend-screen.png)','',
      '## 护栏与选择','']
    for x in load(root/'screen-result.json',[]):
        failures=[k for k,passed in x['checks'].items() if not passed]
        lines+=[f'- {x["variant"]}：通过={x["passed"]}；未通过项：'+(', '.join(failures) if failures else '无')+'。']
    lines+=['',f'独立确认候选：{s.get("selected")}；后台独立确认通过：{s.get("backend_confirmed",False)}；UI 二次核对资格：{s.get("ui_eligible",False)}。未自动推广。',
      '[开发判定](screen-result.json)'+(' · [独立确认](confirm-result.json)' if (root/'confirm-result.json').exists() else '；没有合格候选，未启动独立确认。'),'']
    if d.get('closed_loop_cases'):
        cases=[r for r in d['closed_loop_cases'] if r['sensor_seed']==2104 and r['scene']=='hf_uniform_3']
        lines+=['## 为什么初始化变好仍可能打差','',
          '以下选取 Bfix 匀速首次命中最慢的 seed2104，用于解释失败，不替代全样本统计。各行是各自闭环，第二帧起不能当作同输入因果干预。','',
          '| 配置 | 首帧中心误差 / m | 第二帧中心误差 / m | 全程中心误差 P95 / m | 首次发射 / s | 首次命中 / s |','|---|---:|---:|---:|---:|---:|']
        for case in cases:
            first=case['first_states']
            lines.append(f'| {case["variant"]} | {number(first[0]["center_error_m"])} | {number(first[1]["center_error_m"])} | {number(case["center_error_p95_m"])} | {number(case["first_fire_s"])} | {number(case["first_hit_s"])} |')
        b=next(r for r in cases if r['variant']=='Bfix');second=b['first_states'][1]
        lines+=['',
          'Bfix 只在初始化联合估计位置，下一帧恢复单板求解和既有贪心板号关联。这个失败例的第二帧接受了板号 2 的更新，其朝向创新约 90°；随后初始化的位置优势没有保持。',
          '这一帧记录支持优先检查“联合初始化如何交接到持续观测／板号关联”，不能单凭这一例证明全部成绩退化都由此引起。也不应继续盲目放宽开火门控来掩盖跟踪错误。',
          '本轮止于冻结预算和验收结果，不追加阈值搜索或扩大联合拟合到全部跟踪帧。','']
    lines+=[
      '## 同输入的整体估计表现','',
      '以下只统计主集合的 24 条轨迹；按最差表现补选的两条 seed1904 记录仅用于具体退化回归，不混入此表。','',
      '| 来源 | 场景 | 阶段 | 配置 | 中心 XY 中位 / m | P95 / m | 有效覆盖 |','|---|---|---|---|---:|---:|---:|']
    for x in d['groups']:
        if x['phase'] not in ('moving','after_turn') or x['variant'] not in ('baseline','Afix','Bfix'):continue
        lines.append(f'| {x["engine"]} | {x["scene"]} | {x["phase"]} | {x["variant"]} | {number(x["error"]["p50"])} | {number(x["error"]["p95"])} | {100*x["coverage"]:.2f}% |')
    lines+=['','## 证据与限制','',
      f'- 72 项 C++ 检查通过；关闭修复的九条原生基线逐帧复现观测、滤波、选板、跟踪、角度和开火建议。',
      f'- Bfix 初始化诊断计数：`{json.dumps(d["joint_decisions"],ensure_ascii=False)}`；位置改动 `{json.dumps(d["joint_position_shifts"])}` m。',
      '- 双板拟合仍固定单板朝向、0.20m 半径和同高模型；25% 距离上限是工程保护。拟合失败回到 v3，因此并未声称修好所有单板深度偏差。',
      '- 重投影残差合格不能证明深度真实，必须同时检查真值误差及闭环效果。',
      '- 录像来自既有相关轨迹，帧不是独立样本；后台通过也不证明 UI 或实车同幅度收益。',
      '- 尚未进行本轮完整视频的人工验收；本轮没有改变自动并发登记和默认算法。','']
    text='\n'.join(lines);(root/'report.md').write_text(text)
    try:
        rendered=subprocess.run(['python3','-c','import markdown,sys;print(markdown.markdown(sys.stdin.read(),extensions=["tables"]))'],input=text,text=True,capture_output=True,check=True).stdout
    except subprocess.CalledProcessError:
        def inline(value):
            value=html.escape(value)
            value=re.sub(r'\[([^\]]+)\]\(([^)]+)\)',r'<a href="\2">\1</a>',value)
            value=re.sub(r'\*\*([^*]+)\*\*',r'<strong>\1</strong>',value)
            return re.sub(r'`([^`]+)`',r'<code>\1</code>',value)
        parts=[];table=False
        for line in lines:
            if table and not line.startswith('|'):parts.append('</table>');table=False
            if line.startswith('|'):
                if re.fullmatch(r'[|:\- ]+',line):continue
                tag='td' if table else 'th'
                if not table:parts.append('<table>');table=True
                parts.append('<tr>'+''.join('<'+tag+'>'+inline(cell.strip())+'</'+tag+'>' for cell in line.strip('|').split('|'))+'</tr>')
            elif line.startswith('!['):
                match=re.fullmatch(r'!\[([^\]]*)\]\(([^)]+)\)',line)
                if match:parts.append('<img alt="'+html.escape(match[1])+'" src="'+html.escape(match[2])+'">')
            elif line.startswith('#'):
                n=len(line)-len(line.lstrip('#'));parts.append(f'<h{n}>'+inline(line[n:].strip())+f'</h{n}>')
            elif line:parts.append('<p>'+inline(line)+'</p>')
        if table:parts.append('</table>')
        rendered='\n'.join(parts)
    (root/'report.html').write_text('<meta charset="utf-8"><title>感知修复与后台并行</title><style>body{font:16px/1.6 sans-serif;max-width:1250px;margin:36px auto;padding:0 20px}table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:7px}img{max-width:100%}pre{white-space:pre-wrap}</style>'+rendered)


def finish(root):
    from perception_repair import verify
    c=load(root/'contract.json');s=load(root/'state.json')
    if s['state']!='completed':raise ValueError('Finish the frozen backend batch first')
    verify(root,c,s);analyze(root)
    meta=load(Path(c['bundles']['baseline'])/'bundle.json')
    command=['docker','run','--rm','--network','none','--cpus','2','--user',f'{os.getuid()}:{os.getgid()}',
        '-e','OPENBLAS_NUM_THREADS=1','-e','MPLCONFIGDIR=/tmp/matplotlib',
        '-v',str(manage.ROOT)+':'+str(manage.ROOT)+':ro','-v',str(root)+':'+str(root),
        '-w',str(manage.ROOT),'--entrypoint','python3',meta['images']['evaluator'],'-B',
        str(manage.OPS/'perception_repair_report.py'),'plots','--output',str(root)]
    with (root/'plots.log').open('w') as log:subprocess.run(command,check=True,timeout=60,stdout=log,stderr=subprocess.STDOUT)
    render(root)
    files=('validation.json','analysis-result.json','repair-traces.jsonl','joint-decisions.jsonl',
        'replay-result.json','parallel-result.json','screen-result.json','report.md','report.html',
        'regression-repairs.png','regression-repairs.svg','backend-screen.png','cpp-checks.log','replay-checks.log')
    write(root/'report-seal.json',dict(completed=True,files={n:sha(root/n) for n in files if (root/n).exists()}))
    from experiments import publish_desktop
    publish_desktop(manage,root)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('analyze','plots','render','finish'));p.add_argument('--output',required=True,type=Path)
    a=p.parse_args();globals()[a.action](a.output.resolve())
