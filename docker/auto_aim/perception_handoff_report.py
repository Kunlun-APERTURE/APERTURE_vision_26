"""Posthoc handoff error traces, backend scores and immutable report sealing."""
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
from decision_benchmark_report import aggregate
from perception_handoff import PARENT,verify
from perception_study import quantile
from report import load,write
from sync_first_divergence_analysis import primary,truth_centers


def analyze(root):
    c=load(root/'contract.json');s=load(root/'state.json');traces=[];early=[];guard=Counter()
    ps=load(PARENT/'state.json');pc=load(PARENT/'contract.json')
    for source in c['sources']:
        folder=manage.OPS/'experiments'/source['run_id'];gt=truth_centers(folder)
        start=load(folder/'run-state.json')['measurement_start_stamp_ns'];paths={}
        for r in s['replays']:
            if r['source_run']==source['run_id']:paths[r['variant']]=Path(r['output'])
        if source['engine']=='ui':
            paths['baseline']=Path(next(e['baseline_replay']['output'] for e in pc['sources'] if e['run_id']==source['run_id']))
            paths['Bfix']=Path(next(r['output'] for r in ps['replays'] if r['source_run']==source['run_id'] and r['variant']=='Bfix'))
        for v,path in paths.items():
            rr=primary(path)
            for i,p in enumerate(rr):
                f=p['frame'];r=p['result'];x=r.get('filter_state');g=gt(f['stamp_ns']);d=r.get('continuity',{})
                error=math.dist([x[0]+f['gimbal_position_map'][0],x[2]+f['gimbal_position_map'][1]],g[:2]) if x is not None and g is not None else None
                row=dict(source=folder.name,engine=source['engine'],source_variant=source['variant'],variant=v,
                    scene=source['scene'],sensor_seed=source['sensor_seed'],time_s=(f['stamp_ns']-start)*1e-9,
                    error_m=error,tracker=r.get('tracker'),handoff_remaining=d.get('filter',{}).get('initial_handoff_remaining',0))
                traces.append(row)
                if i<8:early.append(dict(row,frame_index=i,updates=d.get('filter',{}).get('updates',[])))
                if v=='H3':
                    for reject in d.get('handoff_rejections',[]):
                        guard['anchor_rejections']+=reject['anchor_only'];guard['nis_rejections']+=reject['high_innovation']
    groups=[]
    for engine in ('ui','backend'):
        for source_variant in (('baseline','Bfix') if engine=='backend' else ('baseline','A35','B35')):
            for scene in ('hf_uniform_3','hf_turn90_3'):
                for v in ('baseline','Bfix','H3'):
                    rr=[r for r in traces if (r['engine'],r['source_variant'],r['scene'],r['variant'])==(engine,source_variant,scene,v)]
                    if not rr:continue
                    errors=[r['error_m'] for r in rr if r['error_m'] is not None]
                    groups.append(dict(engine=engine,source_variant=source_variant,scene=scene,variant=v,
                        p50_m=statistics.median(errors) if errors else None,p95_m=quantile(errors,.95),coverage=len(errors)/len(rr)))
    (root/'handoff-traces.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in traces))
    (root/'early-updates.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in early))
    live=[]
    for entry in s['runs']:
        if entry['phase']!='screen':continue
        folder=manage.OPS/'experiments'/entry['run_id'];state=load(folder/'run-state.json');summary=load(folder/'summary.json')['metrics']
        large=[];resets=[];active=0
        for line in (folder/'session-evidence/events.jsonl').open():
            packet=json.loads(line)
            if packet['kind']!='result':continue
            data=packet['data'];f=data['frame'];r=data['result']
            if f['stamp_ns']>state['measurement_end_stamp_ns']:continue
            cont=r.get('continuity',{});fil=cont.get('filter') or {}
            remaining=fil.get('initial_handoff_remaining',0);active+=bool(remaining)
            if cont.get('reset_reason'):resets.append(dict(time_s=(f['stamp_ns']-state['measurement_start_stamp_ns'])*1e-9,reason=cont['reset_reason']))
            for u in fil.get('updates',[]):
                yaw=u.get('innovation',[0,0,0,0])[3]
                if u.get('accepted') and abs(yaw)>math.pi/4:
                    large.append(dict(seq=f['seq'],time_s=(f['stamp_ns']-state['measurement_start_stamp_ns'])*1e-9,
                        plate_id=u['plate_id'],yaw_innovation_rad=yaw,handoff_active=remaining>0))
        live.append(dict(run_id=entry['run_id'],variant=entry['variant'],scene=entry['scene'],sensor_seed=entry['sensor_seed'],
            center_error_p95_m=summary['center_xy_error_p95_m'],correct_tracking_fraction=summary['correct_tracking_fraction'],
            large_yaw_updates=large,active_handoff_frames=active,resets=resets,
            first_hit_s=entry['first_hit_s'],shots=entry['shots'],hits=entry['hits'],weight=entry['weight']))
    result=dict(completed=True,groups=groups,guard_rejections=dict(guard),live_diagnostics=live,
        regression_source=next(e['run_id'] for e in c['sources'] if e['variant']=='Bfix' and e['scene']=='hf_uniform_3' and e['sensor_seed']==2104))
    write(root/'analysis-result.json',result)


def plots(root):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    d=load(root/'analysis-result.json');traces=[json.loads(l) for l in (root/'handoff-traces.jsonl').open()]
    fig,axes=plt.subplots(1,2,figsize=(12,4),layout='constrained')
    for ax,limits,title in ((axes[0],(-1.05,-.65),'Recorded seed2104: initialization handoff'),(axes[1],(-1.1,5.1),'Same recorded camera path: full interval')):
        for v in ('Bfix','H3'):
            rr=[r for r in traces if r['source']==d['regression_source'] and r['variant']==v and r['error_m'] is not None]
            ax.plot([r['time_s'] for r in rr],[r['error_m'] for r in rr],label=v)
        ax.set(xlim=limits,title=title,xlabel='Recorded scenario time (s); negative = warmup',ylabel='Center XY error (m)');ax.grid(alpha=.2);ax.legend()
    fig.savefig(root/'handoff-regression.png',dpi=175);fig.savefig(root/'handoff-regression.svg');plt.close(fig)
    s=load(root/'state.json');rr=[r for r in s['runs'] if r['phase']=='screen']
    if not rr:return
    fig,axes=plt.subplots(1,2,figsize=(10,4),layout='constrained')
    for ax,scene in zip(axes,('hf_uniform_3','hf_turn90_3')):
        vv=s['screen_variants'];values=[aggregate([r for r in rr if r['variant']==v and r['scene']==scene]) for v in vv]
        ax.bar([i-.2 for i in range(len(vv))],[100*a['target_hit_rate'] if a['target_hit_rate'] is not None else 0 for a in values],width=.4,label='Exact hit rate')
        ax.bar([i+.2 for i in range(len(vv))],[100*a['weighted_hit_rate'] if a['weighted_hit_rate'] is not None else 0 for a in values],width=.4,label='Weighted hit rate')
        ax.set(xticks=range(len(vv)),xticklabels=vv,title=scene,ylabel='Percent of all shots');ax.legend(fontsize=8);ax.grid(axis='y',alpha=.2)
    fig.savefig(root/'backend-screen.png',dpi=170);plt.close(fig)


def render(root):
    c=load(root/'contract.json');s=load(root/'state.json');d=load(root/'analysis-result.json');v=load(root/'validation.json')
    early=[json.loads(line) for line in (root/'early-updates.jsonl').open()]
    n=lambda x:'—' if x is None else f'{x:.3f}'
    lines=['# 联合初始化交接修复与后台并行验收','',
      'H3 在 Bfix 联合初始化成功后的前三次更新中保护锚板 id0；候选须通过既有 NIS，预算在拒绝观测时也消耗，之后恢复原多板关联。默认关闭，未替换默认 v3。',
      '高置信度可绕过候选层的 NIS 条件，实际 Target 更新仍检查 NIS。上一轮的约 90° 错误更新本身通过了实际 NIS，本轮针对的是初始化交接时的板号关联。','',
      f'完成 {v["replays"]} 段固定输入回放、{v["frames"]} 帧、{v["backend_trials"]} 轮后台闭环；新增 UI 实验 0。',
      '[冻结定义](contract.json) · [证据校验](validation.json) · [回放结果](replay-result.json) · [实现与运行](implementation.md)','',
      '## 原失败例是否修好','',
      '来自上一轮 Bfix 匀速 seed2104 的同一录像。以下是逐帧中心 XY 误差，尚不等于新命中效果。','',
      '| 输入帧 | Bfix 误差 / m | H3 误差 / m | H3 交接剩余 |','|---|---:|---:|---:|']
    for i in range(8):
        a=next(r for r in early if r['source']==d['regression_source'] and r['variant']=='Bfix' and r['frame_index']==i)
        b=next(r for r in early if r['source']==d['regression_source'] and r['variant']=='H3' and r['frame_index']==i)
        remaining=b['handoff_remaining'] if i else '初始化'
        lines.append(f'| {i+1} | {n(a["error_m"])} | {n(b["error_m"])} | {remaining} |')
    lines+=['','H3 第 2 帧只更新锚板，避免了原先的板号 2 / 约 90° 朝向更新；第 5 帧已恢复多板。单板更新仍会使首帧联合位置变化，这个保护没有解决全部深度噪声。','',
      '![交接误差曲线](handoff-regression.png)','',
      '## 后台并行与成本','']
    p=load(root/'parallel-result.json')
    if p:
        lines+=[f'单/双路同输入行为和分数探针通过：{p["passed"]}；实际重叠：{p["actual_overlap"]}；效果实验使用 {p["effect_parallelism"]} 路。仅本轮负载，不登记全局容量。','',
          '| 配置 | 场景 | 图像/算法行为一致 | 分数一致 | 资源健康 | ≤40s |','|---|---|---|---|---|---|']
        for row in p['pairs']:lines.append('| '+' | '.join(str(row[k]) for k in ('variant','scene','same_behavior','same_scores','healthy','within_budget'))+' |')
        spans={}
        for phase in ('probe_p1','probe_p2','screen','confirm'):
            rr=[r for r in s['runs'] if r['phase']==phase]
            if not rr:continue
            span=max(r['attempt']['ended'] for r in rr)-min(r['attempt']['started'] for r in rr);spans[phase]=span
            lines+=['',f'{phase}：{len(rr)} 轮，批次跨度 {span:.1f}s，折算 {len(rr)/span*3600:.1f} 轮/小时。']
        if 'probe_p1' in spans and 'probe_p2' in spans:lines+=['',f'双路探针批次吞吐约为单路的 {spans["probe_p1"]/spans["probe_p2"]:.2f} 倍。']
        lines+=['',f'所有已完成单次最大用时 {max(r["wall_s"] for r in s["runs"]):.2f}s；媒体未请求，原始完整图像和数值已封存。']
    else:lines+=['回放护栏未通过，未启动新闭环。']
    for phase,label in (('screen','后台开发成绩'),('confirm','后台独立确认')):
        rr=[r for r in s['runs'] if r['phase']==phase]
        if not rr:continue
        lines+=['','## '+label,'',
          '| 场景 | 配置 | 命中/发射 | 精确命中率 | 加权命中率 | 命中/秒 | 首次命中 P90 / s |','|---|---|---:|---:|---:|---:|---:|']
        for scene in ('hf_uniform_3','hf_turn90_3','all'):
            for variant in ('baseline','Bfix','H3'):
                rows=[r for r in rr if r['variant']==variant and (scene=='all' or r['scene']==scene)]
                if not rows:continue
                a=aggregate(rows);exact=n(a['target_hit_rate']*100) if a['target_hit_rate'] is not None else '—';weight=n(a['weighted_hit_rate']*100) if a['weighted_hit_rate'] is not None else '—'
                lines.append(f'| {scene} | {variant} | {a["hits"]}/{a["shots"]} | {exact}% | {weight}% | {a["hits_per_s"]:.3f} | {n(quantile([r["first_hit_s"] for r in rows],.9))} |')
    if (root/'backend-screen.png').exists():lines+=['','![后台成绩](backend-screen.png)']
    if d.get('live_diagnostics'):
        lines+=['','## 交接后还剩什么问题','',
          '下表统计暖机到测量结束的实际被滤波器接受的 >45° 朝向创新；这表示大幅状态修正，不单凭角度阈值认定每次都是错误。','',
          '| 配置 | 后台记录数 | 大角度已接受更新 | 交接保护期内 |','|---|---:|---:|---:|']
        for variant in ('baseline','Bfix','H3'):
            rr=[r for r in d['live_diagnostics'] if r['variant']==variant]
            aa=[a for r in rr for a in r['large_yaw_updates']]
            lines.append(f'| {variant} | {len(rr)} | {len(aa)} | {sum(a["handoff_active"] for a in aa)} |')
        cases=[r for r in d['live_diagnostics'] if r['sensor_seed']==2301 and r['scene']=='hf_uniform_3']
        lines+=['','新种子 2301 的匀速闭环用于检验已定位的退化是否再次出现，非新增筛选样本。','',
          '| 配置 | 中心误差 P95 / m | 正确跟踪比例 | 首次命中 / s | >45° 已接受更新 |','|---|---:|---:|---:|---:|']
        for row in cases:lines.append(f'| {row["variant"]} | {n(row["center_error_p95_m"])} | {100*row["correct_tracking_fraction"]:.2f}% | {n(row["first_hit_s"])} | {len(row["large_yaw_updates"])} |')
        lines+=['','短期保护能阻止最初几帧错误交接，但退出后仍可能出现大幅姿态修正；原单板深度求解、持续板号关联及射击预测还没有整体解决。不能把某个首帧或首次命中改善当作高速射击稳定性已经通过。']
    lines+=['','## 判定','']
    for filename,label in (('screen-result.json','开发验收'),('confirm-result.json','独立确认')):
        result=load(root/filename)
        if not result:continue
        failures=[k for k,value in result['checks'].items() if not value]
        lines+=[f'{label}：通过={result["passed"]}。未通过项：'+(', '.join(failures) if failures else '无')+'。']
        if result.get('weighted_gain_95') is not None:lines+=['',f'独立配对加权收益 95% 区间：{result["weighted_gain_95"]}。']
    lines+=['',f'入选独立确认：{s.get("selected")}；独立后台通过：{s.get("backend_confirmed",False)}；UI 二次复核资格：{s.get("ui_eligible",False)}。未自动推广。','',
      '基线 v3 与 Bfix/H3 使用新开发种子 2301–2305；原失败 seed2104 仅作回放回归。所有射击统一 v2 加权评分，探针不进入效果统计。','',
      '## 同输入误差与限制','',
      '| 来源 | 原轨迹配置 | 场景 | 回放配置 | 中心 XY 中位 / m | P95 / m | 覆盖 |','|---|---|---|---|---:|---:|---:|']
    for row in d['groups']:
        if row['engine']=='ui' and row['source_variant']!='baseline':continue
        lines.append(f'| {row["engine"]} | {row["source_variant"]} | {row["scene"]} | {row["variant"]} | {n(row["p50_m"])} | {n(row["p95_m"])} | {row["coverage"]*100:.2f}% |')
    lines+=['',f'- {c["geometry_checks"]} 项 C++ 检查通过；20 条关闭交接保护的原生轨迹逐帧复现；5 项录像集成检查通过。',
      '- 三帧交接是本轮工程候选，没有标定为普适最优窗口；没有扫描其他持续时间。',
      '- 联合初始化仍沿用既有单板朝向、固定半径与同高先验，持续跟踪仍用原单板求解。',
      '- 固定录像只能比较同输入估计与命令，不能据此计算新命中率；UI 来源录像回放通过不等于 UI 闭环或实车验证通过。',
      '- 不按低分补跑，不把同一录像的相关帧当独立样本；未完成人工整段视频验收。','']
    markdown='\n'.join(lines);(root/'report.md').write_text(markdown)
    def inline(value):
        value=html.escape(value);value=re.sub(r'\[([^\]]+)\]\(([^)]+)\)',r'<a href="\2">\1</a>',value)
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
            level=len(line)-len(line.lstrip('#'));parts.append(f'<h{level}>'+inline(line[level:].strip())+f'</h{level}>')
        elif line:parts.append('<p>'+inline(line)+'</p>')
    if table:parts.append('</table>')
    (root/'report.html').write_text('<meta charset="utf-8"><title>交接修复后台验收</title><style>body{font:16px/1.6 sans-serif;max-width:1250px;margin:36px auto;padding:0 20px}table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:7px}img{max-width:100%}</style>'+''.join(parts))


def finish(root):
    c=load(root/'contract.json');s=load(root/'state.json')
    if s['state']!='completed':raise ValueError('Complete frozen experiment first')
    verify(root,c,s);analyze(root)
    meta=load(Path(c['bundles']['baseline'])/'bundle.json')
    command=['docker','run','--rm','--network','none','--cpus','2','--user',f'{os.getuid()}:{os.getgid()}',
        '-e','OPENBLAS_NUM_THREADS=1','-e','MPLCONFIGDIR=/tmp/matplotlib',
        '-v',str(manage.ROOT)+':'+str(manage.ROOT)+':ro','-v',str(root)+':'+str(root),
        '-w',str(manage.ROOT),'--entrypoint','python3',meta['images']['evaluator'],'-B',str(Path(__file__)),
        'plots','--output',str(root)]
    with (root/'plots.log').open('w') as log:subprocess.run(command,check=True,timeout=60,stdout=log,stderr=subprocess.STDOUT)
    (root/'implementation.md').write_text((manage.OPS/'PERCEPTION_HANDOFF.md').read_text())
    render(root)
    files=('report.md','report.html','validation.json','analysis-result.json','handoff-traces.jsonl','early-updates.jsonl',
        'replay-result.json','parallel-result.json','screen-result.json','confirm-result.json','repeatability.json',
        'handoff-regression.png','handoff-regression.svg','backend-screen.png','cpp-checks.log','replay-checks.log','implementation.md')
    write(root/'report-seal.json',dict(completed=True,files={name:sha(root/name) for name in files if (root/name).exists()}))
    from experiments import publish_desktop
    publish_desktop(manage,root)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('analyze','plots','render','finish'));p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();globals()[a.action](a.output.resolve())
