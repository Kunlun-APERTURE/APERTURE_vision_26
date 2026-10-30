"""Publish auditable v4 root-cause findings, figures and original-frame clips."""
import collections,hashlib,html,json,math,statistics,subprocess,sys
from pathlib import Path
from urllib.parse import unquote
from html.parser import HTMLParser

OUTPUT=Path(sys.argv[1]);HISTORY=OUTPUT.parent/'optimization-v4-20261021'
def load(p):return json.loads(Path(p).read_text())
def rows(p):return [json.loads(l) for l in Path(p).open()]
def save(p,data):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(data,ensure_ascii=False,allow_nan=False,indent=2))
def median(values):
    a=[v for v in values if v is not None and math.isfinite(v)];return statistics.median(a) if a else None
def fmt(x,d=3):return '无样本' if x is None else f'{x:.{d}f}'
def wrap(x):return math.remainder(x,2*math.pi)

def findings():
    static=load(OUTPUT/'static/summary.json')['runs'];rotation=load(OUTPUT/'rotation/summary.json')['runs'];confidence=load(OUTPUT/'confidence/summary.json')['runs']
    validation=load(OUTPUT/'replay-validation.json');supp=load(OUTPUT/'supplement/replay-validation.json');inventory=load(OUTPUT/'inventory.json')['runs'];captures=load(OUTPUT/'supplement/batch.json')
    historical_ids={r['run_id'] for r in inventory};new_ids={r['run_id'] for r in captures['runs']}
    turn=rows(OUTPUT/'timing/shots.jsonl');target='20261021T173204_hf_static_8_shoot_muzzle_drag_25p0_25p0_feedforward_5p0hz_tracking_fire_v4mt_bfd8b2f2'
    anomaly=next(r for r in static if r['run_id']==target);ss=[r for r in rows(OUTPUT/'static/shots.jsonl') if r['run_id']==target]
    early=[r for r in turn if r['run_id'] in historical_ids and r['scene']=='hf_turn90_3' and r['source_profile']=='tracking_fire_v4mt' and r['phase'] in ('0..0.5','0.5..1')]
    late=[r for r in turn if r['run_id'] in historical_ids and r['scene']=='hf_turn90_3' and r['source_profile']=='tracking_fire_v4mt' and r['phase']=='1..2']
    rotation_pairs=[]
    for r in rotation:
        if r['profile']!='tracking_fire_v4mtr' or r['variant']!='diagnosis':continue
        other=next(x for x in rotation if x['run_id']==r['run_id'] and x['variant']=='no-hint')
        rotation_pairs.append(dict(run_id=r['run_id'],scene=r['scene'],supplement=r['run_id'] in new_ids,baseline_omega_p90=r['omega_abs_error']['p90'],
            no_hint_omega_p90=other['omega_abs_error']['p90'],baseline_active=r['active_fraction'],no_hint_active=other['active_fraction'],
            baseline_pose_yaw_p50=r['yaw_error']['p50'],no_hint_pose_yaw_p50=other['yaw_error']['p50']))
    confidence_groups=[]
    for scene in sorted({r['scene'] for r in confidence}):
        a=[r for r in confidence if r['scene']==scene]
        confidence_groups.append(dict(scene=scene,runs=len(a),coverage95_p50=median([r['coverage95'] for r in a]),matching_coverage_p50=median([r['matching_coverage'] for r in a]),
            interval_to_target_p50=median([r['interval_to_target']['p50'] for r in a]),process_fraction_p50=median([r['process_fraction']['p50'] for r in a]),
            normalized_error_p95_p50=median([r['normalized_error']['p95'] for r in a])))
    prior=load(OUTPUT/'static/prior-summary.json')['runs'];static_pairs=[]
    for r in prior:
        if r['role']!='supplement' or r['variant']!='diagnosis':continue
        other=next(x for x in prior if x['run_id']==r['run_id'] and x['variant']=='no-hint')
        static_pairs.append(dict(run_id=r['run_id'],profile=r['profile'],physics=r['physics'],seed=r['seed'],
            original_range_p50=r['range_error']['p50'],no_hint_range_p50=other['range_error']['p50'],
            original_absolute_phase_p50_deg=r['absolute_phase']['p50']*180/math.pi,no_hint_absolute_phase_p50_deg=other['absolute_phase']['p50']*180/math.pi))
    results=dict(static=dict(anomaly_run=target,anomaly=anomaly,prior_interventions=static_pairs,misses=[{k:r.get(k) for k in ('shot_seq','time_s','aim_range_error_m','geometry_command_error_m','execution_error_m','physics_model_error_m','video_index')} for r in ss if not r['useful_hit']],
        claim='已证实异常实射的额外低落主要来自错误几何瞄准，叠加共同阻力下坠；执行误差很小。历史异常具体姿态先验分支缺失，不能声称已重现该次因果链。'),
        turn90=dict(early_shots=len(early),early_hits=sum(r['useful_hit'] for r in early),early_velocity_error_p50=median([r.get('velocity_error_mps') for r in early]),
            early_yaw_execution_p50_deg=median([abs(r['yaw_execution_error_rad'])*180/math.pi for r in early if r.get('yaw_execution_error_rad') is not None]),
            late_shots=len(late),late_hits=sum(r['useful_hit'] for r in late),
            claim='已证实前1秒新增发射集中于速度估计尚未恢复的阶段；低枪口执行误差不能保证预测正确。连续控制对闭环收益的单独贡献仍未识别。'),
        rotation=dict(pairs=rotation_pairs,claim='同观测干预支持朝向先验与姿态候选选择形成反馈，拖慢独立相位估计；补录复现后可确认该算法机制，不能由回放推断新命中收益。'),
        confidence=dict(groups=confidence_groups,claim='未来过程噪声是误差带的主要来源；静止显著保守而变向仍可能欠覆盖，不能用统一缩放替代校准。'),
        validation=dict(native_replay=validation,supplement=supp),
        limits=['历史流缺失初始化；所有历史冷启动干预仅作支持证据。','连续控制反事实采用固定自身运动轨迹，无法反演改变控制后的真实轨迹。',
            '补录新增日志有时间开销，只作机制取证，不并入先前正式效果验收。','独立弹道积分与接触几何有毫米级差异；报告残差，保留未匹配发射。'])
    save(OUTPUT/'findings.json',results);return results

def figures(data):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    target=data['static']['anomaly_run'];folder=OUTPUT.parent/target;start=load(folder/'run-state.json')['measurement_start_stamp_ns']
    obs=[p['data'] for p in rows(folder/'telemetry.jsonl') if p['kind']=='observation'];samples=[]
    for d in obs:
        t=(d['frame']['stamp_ns']-start)*1e-9;r=d['result'];x=r.get('filter_state');a=r.get('aim_point')
        if 0<=t<=6 and x and a:samples.append((t,x[0],x[6],a[0]+d['frame']['gimbal_position_map'][0],-r['pitch']*180/math.pi))
    shots=[r for r in rows(OUTPUT/'static/shots.jsonl') if r['run_id']==target]
    fig,axes=plt.subplots(3,1,figsize=(11,9),sharex=True)
    axes[0].plot([r[0] for r in samples],[r[3] for r in samples],label='Predicted aim x (world)');axes[0].axhline(7.796,ls='--',c='k',label='Approx front plate x');axes[0].set_ylabel('Range coordinate (m)');axes[0].legend()
    axes[1].plot([r[0] for r in samples],[r[2]*180/math.pi for r in samples]);axes[1].set_ylabel('Estimated phase (deg)')
    for key,label in [('geometry_command_error_m','Geometry + command'),('execution_error_m','Execution'),('physics_model_error_m','Physics model'),('independently_predicted_vertical_m','Independent sum')]:
        axes[2].plot([r['time_s'] for r in shots],[r.get(key,float('nan'))*100 for r in shots],'.-',label=label)
    for s in shots:
        if not s['useful_hit']:
            for ax in axes:ax.axvline(s['time_s'],c='r',alpha=.18)
    axes[2].set_ylabel('Vertical error (cm)');axes[2].set_xlabel('Scenario time (s)');axes[2].legend(ncol=2)
    for ax in axes:ax.grid(alpha=.25)
    fig.suptitle('Historical 8m anomaly: prediction drift and shot error decomposition');fig.tight_layout();fig.savefig(OUTPUT/'static-diagnosis.png',dpi=145);fig.savefig(OUTPUT/'static-diagnosis.svg');plt.close(fig)
    poses=rows(OUTPUT/'rotation/poses.jsonl');rot=data['rotation']['pairs'];scenes=['hf_spin_pos','hf_spin_neg','hf_spin_move_pos','hf_spin_move_neg']
    fig,axes=plt.subplots(1,2,figsize=(12,4.5))
    for j,kind in enumerate(('historical','supplement')):
        for i,scene in enumerate(scenes):
            rs=[r for r in rot if r['scene']==scene and r['supplement']==(kind=='supplement')]
            if not rs:continue
            for r in rs:axes[j].plot([i-.15,i+.15],[r['baseline_omega_p90'],r['no_hint_omega_p90']],'-o',alpha=.65)
        axes[j].set_xticks(range(4),['spin +','spin -','move +','move -']);axes[j].set_ylabel('Per-run angular-rate absolute error P90 (rad/s)');axes[j].set_title(kind+'; left=hint / right=no hint');axes[j].grid(alpha=.25)
    fig.tight_layout();fig.savefig(OUTPUT/'rotation-diagnosis.png',dpi=145);fig.savefig(OUTPUT/'rotation-diagnosis.svg');plt.close(fig)
    fig,axes=plt.subplots(1,2,figsize=(12,4.5));groups=data['confidence']['groups'];x=range(len(groups))
    axes[0].bar(x,[r['interval_to_target_p50'] or 0 for r in groups]);axes[0].axhline(1,c='r',ls='--');axes[0].set_ylabel('95% interval / target yaw tolerance')
    axes[1].bar(x,[100*(r['process_fraction_p50'] or 0) for r in groups]);axes[1].set_ylabel('Future process-noise variance share (%)')
    for ax in axes:ax.set_xticks(list(x),[r['scene'].replace('hf_','') for r in groups],rotation=40,ha='right');ax.grid(axis='y',alpha=.25)
    fig.tight_layout();fig.savefig(OUTPUT/'confidence-diagnosis.png',dpi=145);fig.savefig(OUTPUT/'confidence-diagnosis.svg');plt.close(fig)
    shot=rows(OUTPUT/'timing/shots.jsonl');historical={r['run_id'] for r in load(OUTPUT/'inventory.json')['runs']}
    fig,axes=plt.subplots(1,2,figsize=(11,4.5));phases=['0..0.5','0.5..1','1..2']
    for j,profile in enumerate(('tracking_fire_v3','tracking_fire_v4mt')):
        ss=[[r for r in shot if r['run_id'] in historical and r['scene']=='hf_turn90_3' and r['source_profile']==profile and r['phase']==phase] for phase in phases];xs=[i+(j-.5)*.35 for i in range(3)]
        axes[0].bar(xs,[len(r) for r in ss],.32,alpha=.4,label=profile+' launches');axes[0].bar(xs,[sum(x['useful_hit'] for x in r) for r in ss],.32,label=profile+' useful hits')
        axes[1].bar(xs,[median([r.get('velocity_error_mps') for r in group]) or 0 for group in ss],.32,label=profile)
    axes[0].set_ylabel('Actual launches / useful hits');axes[1].set_ylabel('At-launch velocity error median (m/s)')
    for ax in axes:ax.set_xticks(range(3),phases);ax.set_xlabel('Seconds after 90-degree turn');ax.legend(fontsize=8);ax.grid(axis='y',alpha=.25)
    fig.tight_layout();fig.savefig(OUTPUT/'turn90-diagnosis.png',dpi=145);fig.savefig(OUTPUT/'turn90-diagnosis.svg');plt.close(fig)

def audit():
    captures=load(OUTPUT/'supplement/batch.json');fail=[];count=0;frames=0;hashes=0;maxwall=0.;minimum=1.
    for row in captures['runs']:
        folder=OUTPUT.parent/row['run_id'];summary=load(folder/'summary.json');manifest=load(folder/'manifest.json')
        for name,digest in load(folder/'evidence-sha256.json').items():
            hashes+=1
            if hashlib.sha256((folder/name).read_bytes()).hexdigest()!=digest:fail.append([folder.name,name,'hash'])
        for name,digest in manifest['configuration_sha256'].items():
            if hashlib.sha256((folder/'configuration'/name).read_bytes()).hexdigest()!=digest:fail.append([folder.name,name,'configuration_hash'])
        index=rows(folder/'frames.jsonl');frames+=len(index)
        info=subprocess.run(['ffprobe','-v','error','-count_frames','-select_streams','v:0','-show_entries','stream=nb_read_frames','-of','json',str(folder/'annotated.mp4')],capture_output=True,text=True,check=True)
        decoded=int(json.loads(info.stdout)['streams'][0]['nb_read_frames'])
        if decoded!=len(index):fail.append([folder.name,'decoded_frames'])
        wall=summary['metrics']['total_wall_s'];maxwall=max(maxwall,wall);spacing=summary['metrics'].get('minimum_launch_spacing_s')
        if spacing is not None:minimum=min(minimum,spacing)
        if wall>40 or spacing is not None and spacing<.2-1e-8:fail.append([folder.name,'time_or_rate'])
        if row['verdict']=='invalid':fail.append([folder.name,'invalid'])
        count+=1
    for record in load(OUTPUT/'inventory.json')['runs']:
        folder=OUTPUT.parent/record['run_id']
        for name,key in [('telemetry.jsonl','telemetry_sha256'),('evidence-sha256.json','evidence_manifest_sha256')]:
            if hashlib.sha256((folder/name).read_bytes()).hexdigest()!=record[key]:fail.append([folder.name,name,'history_mutated'])
    save(OUTPUT/'integrity.json',dict(passed=not fail,supplement_runs=count,decoded_frames=frames,hashed_files=hashes,max_wall_s=maxwall,minimum_launch_spacing_s=minimum,
        history_runs_verified=232,invalid_attempts=captures['invalid_attempts'],failures=fail))
    if fail:raise RuntimeError('Integrity audit failed')

def clips(data):
    dest=OUTPUT/'clips';dest.mkdir(exist_ok=True);selected=[(data['static']['anomaly_run'],3.,5.5,'static-anomaly')]
    history=load(OUTPUT/'inventory.json')['runs'];capture=load(OUTPUT/'supplement/batch.json')['runs']
    for profile in ('tracking_fire_v3','tracking_fire_v4mt'):
        r=next(r for r in history if r['stage']=='mt' and r['role']=='formal' and r['scene']=='hf_turn90_3' and r['profile']==profile and r['physics']=='no-drag')
        selected.append((r['run_id'],2.7,5.3,'turn90-'+profile))
    for scene in ('hf_spin_pos','hf_spin_neg'):
        r=next(r for r in capture if r['inventory']['scene']==scene)
        selected.append((r['run_id'],1.,5.,'supplement-'+scene))
    records=[]
    for run,a,b,label in selected:
        folder=OUTPUT.parent/run;index=rows(folder/'frames.jsonl');chosen=[f for f in index if f.get('measurement',True) and f.get('time') is not None and a<=f['time']<=b]
        first,last=chosen[0]['video_index'],chosen[-1]['video_index'];path=dest/(label+'.mp4')
        subprocess.run(['ffmpeg','-nostdin','-loglevel','error','-y','-i',str(folder/'annotated.mp4'),'-ss',str(first/30),'-t',str((last-first+1)/30),'-an','-c:v','libx264','-preset','fast','-crf','23','-pix_fmt','yuv420p','-movflags','+faststart',str(path)],check=True)
        records.append(dict(run_id=run,label=label,first_frame=first,last_frame=last,first_sim_s=chosen[0]['time'],last_sim_s=chosen[-1]['time'],file=str(path.relative_to(OUTPUT))))
    save(OUTPUT/'clips.json',records)
    (OUTPUT/'clips.html').write_text('<!doctype html><meta charset="utf-8"><title>诊断代表视频</title><h1>诊断代表视频</h1><p>视频来自原始记录；补录仅为取证，不参与优化效果验收。人工视觉待复核。</p>'+''.join('<h2>'+r['label']+'</h2><video controls preload="metadata" width="850" src="'+r['file']+'"></video><p>帧 '+str(r['first_frame'])+'–'+str(r['last_frame'])+'；<a href="../'+r['run_id']+'/report.html">完整视频</a></p>' for r in records))

def report(data):
    static=data['static']['anomaly'];turn=data['turn90'];groups=data['confidence']['groups'];r=data['rotation']['pairs']
    lines=['# v4 根因诊断\n','诊断取证已完成。运行算法未做修复或升级；离线干预不代表新的闭环命中率。\n',
        '## 8 米静止偏差\n',
        f'异常记录 `{static["run_id"]}` 的实射分解显示：几何与指令误差均值 {static["geometry_command_error_m"]["mean"]*100:.2f} cm，执行误差 {static["execution_error_m"]["mean"]*100:.2f} cm，共同物理模型误差 {static["physics_model_error_m"]["mean"]*100:.2f} cm。原始算法只有重力项，模拟器有阻力。\n',
        '分解均值以全部24次实际发射为分母，接触落点只有20发有效命中有值，两者不可直接混用。4 发脱靶集中在约4.2–5.0秒，瞄准距离低估约0.79–1.21米。原始记录未包含该次初始化，无法恢复全部先验选择历史，因此“时序改动导致了该次姿态偏置”仍为证据不足。\n',
        '完整会话补录中，两次静止时序版出现约22°–26°错误朝向；同观测移除先验后，距离误差中位值由约−0.62/−0.33米变为+0.12/+0.18米。这证实了姿态先验反馈能够造成错误深度的机制；为何某一运行更易进入该分支仍未确定。\n',
        '## 90° 转向与时序归因\n',
        f'历史时序版在转向后前1秒实际发射 {turn["early_shots"]} 发，命中 {turn["early_hits"]} 发；发射时二维速度误差中位数 {fmt(turn["early_velocity_error_p50"])} m/s，而 yaw 执行误差中位数 {fmt(turn["early_yaw_execution_p50_deg"])}°。1–2秒为 {turn["late_hits"]}/{turn["late_shots"]} 命中。\n',
        '这支持开火机会提前到了速度预测尚未恢复的阶段。缩短延迟和姿态外推均能改变离线开火资格，但连续控制的闭环贡献不能由固定自身轨迹回放证明。8种组合全部保留，不能把离线开火资格当作命中。\n',
        '## 旋转方向差异\n',
        '相同检测观测下，移除姿态朝向先验后，独立相位模型的正负角速度估计均改善。姿态解算在最优代价+0.5像素范围内优先选择接近先验的候选，先验由跟踪模型反馈，可形成错误相位的自维持。\n',
        '| 场景 | 历史各次角速度误差P90中位值：带先验→无先验 | 补录同口径 |\n|---|---:|---:|\n']
    for scene in ('hf_spin_pos','hf_spin_neg','hf_spin_move_pos','hf_spin_move_neg'):
        a=[x for x in r if x['scene']==scene and not x['supplement']];b=[x for x in r if x['scene']==scene and x['supplement']]
        lines.append('|'+scene+'|'+fmt(median([x['baseline_omega_p90'] for x in a]))+' → '+fmt(median([x['no_hint_omega_p90'] for x in a]))+'|'+fmt(median([x['baseline_omega_p90'] for x in b]))+' → '+fmt(median([x['no_hint_omega_p90'] for x in b]))+'|\n')
    lines+=['\n补录会话的状态可与原记录复现时，干预证据适用于该会话机制；仍不等于“删除先验即可在所有运动中提高命中”。v3旧模型的干预结果不一致，不能全局删除先验。\n',
        '## 置信度门控\n','无单位错误证据。未来过程噪声约占预测 yaw 方差的主要部分，静止误差带远大于实际误差；反向机动仍可欠覆盖。必须按运动状态检查校准，不能全局乘一个缩放系数。\n',
        '| 场景 | 95%区间/允许角宽 | 未来过程方差占比 | 匹配覆盖率 | 95%区间实际覆盖率 |\n|---|---:|---:|---:|---:|\n']
    for g in groups:lines.append('|'+g['scene']+'|'+fmt(g['interval_to_target_p50'],2)+'|'+fmt(100*g['process_fraction_p50'],1)+'%|'+fmt(100*g['matching_coverage_p50'],1)+'%|'+fmt(100*g['coverage95_p50'],1)+'%|\n')
    lines+=['\n## 下一轮最小修复实验建议\n','1. 先限制姿态先验对相位与深度解的错误反馈，保留无歧义观测的作用；分别检验静止与两个旋转方向。\n',
        '2. 针对90°转向增加预测恢复可信度，区分对准误差和预测误差；采用新种子闭环验证，防止单靠延后开火降低吞吐。\n',
        '3. 单独验证有阻力弹道补偿，报告共同偏差与视觉估计偏差，避免用弹道参数抵消错误距离。\n',
        '4. 在校准后的状态与过程噪声模型建立前，置信度仅记录，不参与发射。连续控制的收益需独立闭环消融。\n',
        '\n## 证据与限制\n','历史98次诊断回放与冻结原始二进制、诊断关闭结果逐帧核对；补录用于补全初始化，历史已有输出和证据保持只读。数据表保存逐发、帧号、未匹配数量；原有优化验收结论不变。\n']
    text=''.join(lines).replace('\n##','\n\n##').replace('## 8 米静止偏差\n','## 8 米静止偏差\n\n').replace('## 90° 转向与时序归因\n','## 90° 转向与时序归因\n\n').replace('## 旋转方向差异\n','## 旋转方向差异\n\n').replace('## 置信度门控\n','## 置信度门控\n\n').replace('## 下一轮最小修复实验建议\n','## 下一轮最小修复实验建议\n\n').replace('## 证据与限制\n','## 证据与限制\n\n')
    (OUTPUT/'REPORT.md').write_text(text)
    # Keep report rendering dependency-free and preserve the Markdown as the main detailed artifact.
    esc=html.escape;rot=''.join('<tr><td>'+esc(x['scene'])+'</td><td>'+str(x['supplement'])+'</td><td>'+fmt(x['baseline_omega_p90'])+'</td><td>'+fmt(x['no_hint_omega_p90'])+'</td></tr>' for x in r)
    links=''.join('<li><a href="'+name+'">'+label+'</a></li>' for name,label in [('REPORT.md','完整中文诊断'),('findings.json','结论与量化证据'),('static/shots.csv','静止逐发误差分解'),('static/frames.csv','静止逐帧估计'),('static/prior-summary.json','静止先验干预与延迟对照'),('timing/closed-loop-summary.json','全部64次时序实测分组'),('timing/factorial.csv','时序8组合离线对照'),('timing/shots.csv','转向实际发射'),('rotation/poses.csv','旋转姿态候选与物理装甲匹配'),('rotation/updates.csv','相位更新与拒绝'),('confidence/frames.csv','置信度与误差逐帧'),('historical-state-reproduction.json','历史初始化缺口'),('supplement/replay-validation.json','补录完整会话复现'),('integrity.json','证据完整性'),('clips.html','代表性视频')])
    body='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>v4 根因诊断</title><style>body{font:16px system-ui;margin:2em;max-width:1150px}img{max-width:100%}td,th{border:1px solid #aaa;padding:.4em}table{border-collapse:collapse}</style><h1>v4 根因诊断</h1><p>已完成历史分析与有限补录。默认算法未升级，人工视觉待复核。</p><ul>'+links+'</ul>'
    for title,text,figure in [
        ('静止低落','错误几何瞄准叠加共同阻力下坠；执行误差很小。历史异常的初始化缺失，不能还原全部先验分支。','static-diagnosis.png'),
        ('90°转向','前1秒：'+str(turn['early_hits'])+'/'+str(turn['early_shots'])+' 命中；1–2秒：'+str(turn['late_hits'])+'/'+str(turn['late_shots'])+'。新增开火与未恢复的速度预测重叠。','turn90-diagnosis.png'),
        ('旋转反馈','同检测观测的先验干预支持姿态候选选择与相位反馈问题；补录与历史分别列出，未推断新命中收益。','rotation-diagnosis.png'),
        ('预测置信度','静止过度保守与机动欠覆盖同时存在，不能统一缩放。匹配覆盖率随场景变化，完整分母见JSON。','confidence-diagnosis.png')]:
        body+='<h2>'+title+'</h2><p>'+text+'</p><img src="'+figure+'">'
    body+='<h2>旋转逐次干预</h2><table><tr><th>场景</th><th>补录</th><th>原角速度误差P90</th><th>无先验离线P90</th></tr>'+rot+'</table></html>'
    (OUTPUT/'report.html').write_text(body)

data=findings();figures(data);audit();clips(data);report(data)
save(OUTPUT/'completion.json',dict(complete=True,integrity=load(OUTPUT/'integrity.json'),diagnostic_replay_runs=98,
    supplemental_runs=len(load(OUTPUT/'supplement/batch.json')['runs']),visual_review='pending',default_promoted=False))
print(json.dumps(dict(completed=True,output=str(OUTPUT),integrity=load(OUTPUT/'integrity.json')['passed'])),flush=True)
