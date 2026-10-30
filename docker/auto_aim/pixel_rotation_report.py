"""Joint image-space rigid rotation and plate retention evidence review."""
import argparse
from collections import Counter
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
from decision_benchmark_report import aggregate,unified_score,behavior
from perception_study import protected,quantile
from report import load,write
from pixel_rotation_study import initialize,VARIANTS,CANDIDATES,OPTIONS,evaluate,regression_gate


def analysis(root):
    import numpy as np
    c=initialize(root);s=load(root/'state.json');contrasts=[]
    for phase in ('screen','regression','confirm'):
        rows=[r for r in s['runs'] if r['phase']==phase]
        for scene in ('all',*sorted({r['scene'] for r in rows})):
            for reference,variant in (('baseline','BS4'),('baseline','L4'),('baseline','R4'),('baseline','LR4'),('BS4','L4'),('BS4','R4'),('BS4','LR4'),('L4','LR4'),('R4','LR4')):
                a=[r for r in rows if r['variant']==reference and (scene=='all' or r['scene']==scene)]
                b=[r for r in rows if r['variant']==variant and (scene=='all' or r['scene']==scene)]
                if not a or not b:continue
                keys=lambda rows:{(r['sensor_seed'],r['scene']) for r in rows}
                assert keys(a)==keys(b)
                seeds=sorted({r['sensor_seed'] for r in a});rng=np.random.default_rng(c['bootstrap']['random_seed'])
                totals=lambda rows:np.asarray([[sum(r[k] for r in rows if r['sensor_seed']==seed) for k in ('shots','hits','weight','duration_s')] for seed in seeds],float)
                aa,bb=totals(a),totals(b);draw=rng.integers(0,len(seeds),size=(10000,len(seeds)))
                ax,bx=aa[draw].sum(axis=1),bb[draw].sum(axis=1);av,bv=aa.sum(axis=0),bb.sum(axis=0);metrics={}
                for name,num,den in (('weighted',2,0),('exact',1,0),('hits_per_s',1,3)):
                    value=float(bv[num]/bv[den]-av[num]/av[den]) if av[den]>0 and bv[den]>0 else None
                    ci=np.quantile(bx[:,num]/bx[:,den]-ax[:,num]/ax[:,den],[.025,.975]).tolist() if np.all(ax[:,den]>0) and np.all(bx[:,den]>0) else None
                    metrics[name]=dict(delta=value,ci95=ci)
                contrasts.append(dict(phase=phase,scene=scene,reference=reference,variant=variant,seeds=len(seeds),metrics=metrics))
    write(root/'paired-effects.json',dict(contrasts=contrasts,unit='paired sensor seed, both scenes retained together for all',
      samples=10000,development_multiple_comparisons='Descriptive, not adjusted; independent confirmation is separate'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(2,3,figsize=(15,8),layout='constrained')
    for ax,(phase,scene,title) in zip(axes.flat,[('screen','hf_uniform_3','Uniform 3 m/s'),('screen','hf_turn90_3','90-degree turns'),
      ('regression','hf_static_5','Static'),('regression','hf_spin_pos','Positive spin'),('regression','hf_spin_neg','Negative spin'),('regression','ego_right_centered','Ego motion')]):
        values=[aggregate([r for r in s['runs'] if r['phase']==phase and r['scene']==scene and r['variant']==v]) for v in VARIANTS]
        ax.bar([i-.18 for i in range(5)],[100*x['target_hit_rate'] if x['target_hit_rate'] is not None else 0 for x in values],width=.36,label='Exact hit %',color='#3568a8')
        ax.bar([i+.18 for i in range(5)],[100*x['weighted_hit_rate'] if x['weighted_hit_rate'] is not None else 0 for x in values],width=.36,label='Weighted hit %',color='#2d9a89')
        ax.set(xticks=range(5),xticklabels=[v+'\nn='+str(x['shots']) for v,x in zip(VARIANTS,values)],ylim=(0,105),title=title,ylabel='Percent of shots');ax.grid(axis='y',alpha=.2);ax.set_axisbelow(True)
    axes.flat[0].legend(loc='lower left',fontsize=8);fig.savefig(root/'shooting-comparison.png',dpi=150);plt.close(fig)


def diagnostics(root):
    s=load(root/'state.json');rows=[]
    for entry in s['runs']:
        folder=manage.OPS/'experiments'/entry['run_id'];state=load(folder/'run-state.json');counts=Counter();previous=None;max_jump=0
        for line in (folder/'session-evidence/events.jsonl').open():
            packet=json.loads(line)
            if packet['kind']!='result':continue
            p=packet['data'];f,r=p['frame'],p['result']
            if not state['measurement_start_stamp_ns']<=f['stamp_ns']<=state['measurement_end_stamp_ns']:continue
            counts['frames']+=1;counts['valid']+=bool(r.get('valid'));counts['advice']+=bool(r.get('fire_advice'))
            counts['tracker_'+str(r.get('tracker'))]+=1
            continuity=r.get('continuity') or {}
            if continuity.get('reset_reason'):counts['reset_'+str(continuity['reset_reason'])]+=1
            fire=r.get('fire_check') or {}
            if fire and not r.get('fire_advice'):
                reason='invalid_tracking' if not fire.get('valid') else 'pose' if not fire.get('pose_valid') else 'alignment' if not fire.get('aligned') else 'stability_or_other'
                counts['fire_block_'+reason]+=1
            diag=(r.get('diagnosis') or {}).get('aimer') or {};ret=diag.get('aim_retention')
            pixel=r.get('continuity',{}).get('pixel_rotation')
            if pixel and pixel.get('state'):
                counts['pixel_model_frames']+=1;counts['pixel_active_frames']+=pixel['active']
                for update in pixel.get('updates',[]):
                    if update['accepted']:
                        counts['pixel_accepted']+=1;assert update['nis']<=26.125 and update['rms_px']<=8
            if ret:
                counts['retention_frames']+=1;selected=ret['selected'];counts['held_over_area_preference']+=selected>=0 and selected!=ret['preferred']
                previous_id=ret['previous']
                can_hold=0<=previous_id<len(ret['capture_fresh']) and ret['capture_fresh'][previous_id] and ret['facing'][previous_id]>=.5
                if can_hold:
                    assert selected==previous_id;counts['actual_hold_frames']+=1
                elif ret.get('release_to_preferred'):
                    assert selected==ret['preferred'];counts['release_to_preferred_frames']+=1
                    counts['released_same_id_frames']+=selected==previous_id
                elif selected>=0:assert ret['capture_fresh'][selected] and ret['facing'][selected]>=.5
            if r.get('valid'):
                if previous and previous['track_generation']==r['track_generation']:
                    counts['plate_switches']+=r['selected_plate_id']!=previous['selected_plate_id']
                    import math
                    jump=abs(math.remainder(r['yaw']-previous['yaw'],2*math.pi));max_jump=max(max_jump,jump)
                previous=r
            else:previous=None
        if OPTIONS[entry['variant']]['retain']:assert counts['retention_frames']>0
        else:assert counts['retention_frames']==0
        if OPTIONS[entry['variant']]['pixel']:assert counts['pixel_model_frames']>0
        else:assert counts['pixel_model_frames']==0
        shots=[json.loads(l) for l in (root/'normalized-scores'/entry['run_id']/'shots.jsonl').open()]
        rows.append(dict(phase=entry['phase'],variant=entry['variant'],scene=entry['scene'],sensor_seed=entry['sensor_seed'],
          run_id=entry['run_id'],counts=dict(counts),max_consecutive_yaw_jump_rad=max_jump,
          first_fire_s=shots[0]['fire_time_s'] if shots else None,first_hit_s=entry['first_hit_s']))
    write(root/'live-diagnostics.json',rows)


def verify(root):
    c=initialize(root);s=load(root/'state.json');assert protected()==c['protected'] and len(s['replays'])==10
    counts=Counter(r['phase'] for r in s['runs']);assert counts['screen']==50 and counts['regression']==60 and counts['probe_p1']==counts['probe_p2']==10
    for p,h in c['historical_files'].items():assert sha(Path(p))==h,p
    for p,h in c['source_sha256'].items():assert sha(root/'build-source/sp_vision_25'/p)==h
    for path in c['bundles'].values():verify_bundle(Path(path))
    for row in s['replays']:
        assert not any(row['differences'].values()) and sha(Path(row['output'])/'events.jsonl')==row['output_sha256']
    pilot=load(root/'rotation-pilot.json');assert pilot['passed'] and not pilot['truth_used'] and len(pilot['rows'])==4
    for row in pilot['rows']:assert sha(Path(row['output'])/'events.jsonl')==row['output_sha256']
    unique=set();evidence={}
    for r in s['runs']:
        key=(r['phase'],r['variant'],r['scene'],r['sensor_seed'],r['repeat_id']);assert key not in unique;unique.add(key)
        p=manage.OPS/'experiments'/r['run_id'];m=load(p/'manifest.json');meta=load(Path(c['bundles'][r['variant']])/'bundle.json')
        assert m['algorithm_binary_sha256']==c['candidate_binary_sha256'] and m['configuration_sha256']['algorithm.yaml']==meta['files']['sp_vision_25/configs/simulator.yaml']
        for role in ('simulator','bridge','evaluator','algorithm'):
            assert m['containers'][role+'_image_id']==meta['images']['simulator' if role=='bridge' else role]
        for runtime_path,h in c['runtime_files'].items():
            key='ops/'+Path(runtime_path).name
            if key in m['configuration_sha256']:assert m['configuration_sha256'][key]==h
        assert all(m.get(k)==v for k,v in dict(scene=r['scene'],sensor_seed=r['sensor_seed'],terrain='flat_range',input_contract='aligned-v1',warmup_frames=30).items())
        assert all(m['execution'].get(k)==v for k,v in dict(mode='lockstep',timing_profile=c['backend_timing_profile'],clock_domain='simulation',detector_history='session').items())
        assert all(m['shooting'].get(k)==v for k,v in dict(bullet_speed=25.,reported_bullet_speed=25.,physics='no-drag',aim_origin='muzzle',fire_policy='advice',fire_rate_limit_hz=5.,gimbal_profile='feedforward',algorithm_profile='tracking_fire_v3').items())
        assert r['health']['healthy'] and r['wall_s']<=40 and load(p/'session-evidence-audit.json')['passed']
        for name,h in r['source_hashes'].items():assert sha(p/name)==h
        with jobs.connect() as db:
            attempts=[dict(a) for a in db.execute('SELECT a.* FROM attempts a JOIN items i ON a.item_id=i.id WHERE i.job_id=?',(r['job_id'],))]
        assert len(attempts)==1 and attempts[0]['state']=='finished' and attempts[0]['verdict']!='invalid'
        scored=unified_score(p,root/'normalized-scores'/r['run_id'],c)['metrics']
        assert (scored['shots_fired'],scored['target_armor_hits'])==(r['shots'],r['hits']) and abs(scored['weighted_hit_score_sum']-r['weight'])<1e-10
        evidence[r['run_id']]={name:sha(p/name) for name in ('manifest.json','summary.json','run-state.json','telemetry.jsonl','session-evidence/events.jsonl')}
    for phase,scenes,seeds in (('screen',('hf_uniform_3','hf_turn90_3'),c['screen_seeds']),('regression',c['regression_scenes'],c['regression_seeds'])):
        assert {(r['variant'],r['scene'],r['sensor_seed']) for r in s['runs'] if r['phase']==phase}=={(v,sc,seed) for v in VARIANTS for sc in scenes for seed in seeds}
    screen=[r for r in s['runs'] if r['phase']=='screen'];reg=[r for r in s['runs'] if r['phase']=='regression']
    effects=[evaluate(screen,v,c,ref) for v in CANDIDATES for ref in ('baseline','BS4')];regressions=[regression_gate(reg,v) for v in CANDIDATES]
    assert effects==load(root/'screen-result.json') and regressions==load(root/'regression-result.json')
    eligible=[v for v in CANDIDATES if all(r['passed'] for r in effects if r['variant']==v) and next(r['passed'] for r in regressions if r['variant']==v)]
    selected=max(eligible,key=lambda v:aggregate([r for r in screen if r['variant']==v])['weighted_hit_rate']) if eligible else None
    assert selected==s['selected']
    if selected:
        assert counts['confirm']==60
        confirmation=[evaluate([r for r in s['runs'] if r['phase']=='confirm'],selected,c,ref,True) for ref in ('baseline','BS4')]
        assert confirmation==load(root/'confirm-result.json')
        assert s.get('backend_confirmed',False)==all(x['passed'] for x in confirmation)
        assert {(r['variant'],r['scene'],r['sensor_seed']) for r in s['runs'] if r['phase']=='confirm'}=={(v,sc,seed) for v in ('baseline','BS4',selected) for sc in ('hf_uniform_3','hf_turn90_3') for seed in c['confirm_seeds']}
    else:assert counts['confirm']==0
    if s.get('backend_confirmed'):
        assert counts['repeat']==6
        pairs=[]
        for row in (x for x in s['runs'] if x['phase']=='repeat'):
            original=next(x for x in s['runs'] if x['phase']=='confirm' and all(x[k]==row[k] for k in ('variant','scene','sensor_seed')))
            pairs.append(dict(variant=row['variant'],scene=row['scene'],same_scores=all(original[k]==row[k] for k in ('shots','hits','weight')),
              same_behavior=behavior(manage.OPS/'experiments'/original['run_id'])==behavior(manage.OPS/'experiments'/row['run_id'])))
        repeat_passed=all(x['same_scores'] and x['same_behavior'] for x in pairs)
        write(root/'repeatability.json',dict(passed=repeat_passed,pairs=pairs))
    else:
        assert counts['repeat']==0;repeat_passed=None
    diagnostics(root)
    write(root/'run-evidence-hashes.json',evidence)
    write(root/'validation.json',dict(passed=True,backend_trials=len(s['runs']),trial_counts=dict(counts),replays=14,
      frozen_selection_verified=True,repeatability_passed=repeat_passed,protected_unchanged=True,historical_evidence_unchanged=True,raw_scores_recomputed=True,
      live_retention_and_facing_freshness_verified=True,live_image_rotation_model_verified=True,maximum_complete_trial_s=max(r['wall_s'] for r in s['runs']),
      ui_acquisition=0,automatic_promotion=False,source_sha256={p.name:sha(p) for p in (manage.OPS/'pixel_rotation_study.py',Path(__file__))}))


def render(root):
    c=load(root/'contract.json');s=load(root/'state.json');a=load(root/'paired-effects.json');v=load(root/'validation.json');live=load(root/'live-diagnostics.json')
    pct=lambda x:'—' if x is None else f'{100*x:.3f}%';num=lambda x:'—' if x is None else f'{x:.3f}'
    lines=['# 图像角点联合旋转模型与目标保持','',
      '本轮围绕既有短补偿收益修复目标保持：仅在物理板有新鲜量测且朝向仍位于60°范围内时保持，失去新鲜性或离开视角时交回原优先板选择，不另加无解门控。不跨板插值，不放宽开火窗口，默认关闭。',
      '[上游锁定策略源码](https://github.com/TongjiSuperPower/sp_vision_25/blob/bd9f5e798fa3c6dd3b483ae6627796afb41c608d/tasks/auto_aim/aimer.cpp#L160)提供了保持目标的机制参考；本地新实现额外要求捕获时新鲜性。',
      'R在独立后验中联合估计车体中心、速度、相位、角速度及几何，直接更新图像角点；不以PnP深度作为该模型的测量。连续三帧有可信旋转且角速度标准差低于0.5rad/s后才接管几何预测，低速或长时间无更新时释放。所有量测来自原图像，不使用真值。',
      '', '| 配置 | 感知 | 补偿 | 物理板保持 |','|---|---|---|---|',
      '| baseline | v3 | 原策略 | 关闭 |','| BS4 | BS | 4.329533ms | 关闭 |','| L4 | BS | 4.329533ms | 保持，释放交回原逻辑 |','| R4 | BS＋图像联合旋转 | 4.329533ms | 不保持 |','| LR4 | BS＋图像联合旋转 | 4.329533ms | 保持，释放交回原逻辑 |','',
      '[冻结定义](contract.json) · [验收校验](validation.json) · [实际保持诊断](live-diagnostics.json) · [配对区间](paired-effects.json)','',
      f'完成{len(s["runs"])}轮后台射击；单次最大完整用时{v["maximum_complete_trial_s"]:.2f}s。正式二进制与配置保持不变，新增UI采集0。','',
      '![射击对照](shooting-comparison.png)','']
    for phase,title in (('screen','开发集'),('regression','回归'),('confirm','独立确认')):
        rows=[r for r in s['runs'] if r['phase']==phase]
        if not rows:continue
        lines+=['## '+title,'','| 场景 | 配置 | 命中/发射 | 精确命中 | 加权命中 | 命中/秒 | 首次命中P90/s |','|---|---|---:|---:|---:|---:|---:|']
        for scene in [*sorted({r['scene'] for r in rows}),*(['all'] if phase!='regression' else [])]:
            for variant in VARIANTS:
                rr=[r for r in rows if r['variant']==variant and (scene=='all' or r['scene']==scene)]
                if not rr:continue
                aa=aggregate(rr);lines.append(f'| {scene} | {variant} | {aa["hits"]}/{aa["shots"]} | {pct(aa["target_hit_rate"])} | {pct(aa["weighted_hit_rate"])} | {num(aa["hits_per_s"])} | {num(quantile([r["first_hit_s"] for r in rr],.9))} |')
        if phase=='regression':lines+=['','回归每格三个种子，额外目录场景仍属探索，不继承主场景单/双路或UI效果资格；各场景分开验收。']
    lines+=['','## 配对效果','', '| 阶段/场景 | 候选/对照 | 加权差/pp | 配对95%区间/pp |','|---|---|---:|---|']
    for row in a['contrasts']:
        if row['scene']!='all' or row['phase']=='regression':continue
        m=row['metrics']['weighted'];ci=m['ci95'];interval='无有效区间' if ci is None else f'[{100*ci[0]:+.2f}, {100*ci[1]:+.2f}]'
        lines.append('| '+row['phase']+'/'+row['scene']+' | '+row['variant']+'/'+row['reference']+' | '+num(None if m['delta'] is None else 100*m['delta'])+' | '+interval+' |')
    lines+=['','配对单位是种子，合并主场景时共同抽样；开发多重比较仅作描述，不把弹丸或帧当独立样本。','', '## 冻结验收','']
    for row in load(root/'screen-result.json'):
        lines+=['',row['variant']+'/'+row['reference']+' 通过='+str(row['passed'])+'；未通过='+str([k for k,x in row['checks'].items() if not x])+'。']
    for row in load(root/'regression-result.json'):
        lines+=['',row['variant']+' 回归通过='+str(row['passed'])+'；未通过='+str([k for k,x in row['checks'].items() if not x])+'。']
    for row in load(root/'confirm-result.json',[]):lines+=['','独立 '+row['variant']+'/'+row['reference']+' 通过='+str(row['passed'])+'；加权收益95%区间='+str(row['weighted_gain_95'])+'。']
    lines+=['',f'开发入选={s.get("selected")}；后台独立通过={s.get("backend_confirmed",False)}。',
      '重复性结果='+str(v.get('repeatability_passed'))+'。',
      '验收要求相对v3总体加权至少+5pp、各场景精确/加权不明显退化、命中/秒和首次命中约束通过；相对BS4保留非劣约束；四场景回归必须通过才进入独立确认。未入选不增加种子。','',
      '## 实际机制启用','', '| 主场景配置 | 保持诊断帧 | 压过面积优先的帧 | 同代有效帧间换板次数 |','|---|---:|---:|---:|']
    for variant in VARIANTS:
        rows=[x for x in live if x['phase']=='screen' and x['variant']==variant];cc=sum((Counter(x['counts']) for x in rows),Counter())
        lines.append('| '+variant+' | '+str(cc['retention_frames'])+' | '+str(cc['held_over_area_preference'])+' | '+str(cc['plate_switches'])+' |')
    lines+=['','| 场景/配置 | 图像模型帧 | 接管帧 | 接受角点更新 | 暂失帧 | 超时重置 |','|---|---:|---:|---:|---:|---:|']
    for scene in ('hf_uniform_3','hf_turn90_3',*c['regression_scenes']):
        for variant in ('R4','LR4'):
            cc=sum((Counter(x['counts']) for x in live if x['scene']==scene and x['variant']==variant and x['phase'] in ('screen','regression')),Counter())
            lines.append('| '+scene+'/'+variant+' | '+str(cc['pixel_model_frames'])+' | '+str(cc['pixel_active_frames'])+' | '+str(cc['pixel_accepted'])+' | '+str(cc['tracker_temp_lost'])+' | '+str(cc['reset_trusted_update_timeout'])+' |')
    lines+=['','换板计数与保持次数不是精度收益；不同候选闭环相机轨迹不同。保持要求新鲜性，释放后交回原优先板逻辑，不为原板制造新观测，也不改变下游射击新鲜性门槛。图像联合模型的每次接受量测核查创新与像素残差门槛；旋转状态与激活次数另存诊断，不将激活本身当作收益。',
      '模型使用独立角点噪声近似，尚未建模外参和云台姿态误差造成的量测相关性；接管后的四板几何共享当前联合后验的新鲜性，并不意味着每块板都在本帧可见。',
      '固定逻辑时序不代替实车实时计算截止时间，4.329533ms不直接迁移硬件；原始输入、所有发射与负面结果保留，未补做人工整段视频验收。']
    text='\n'.join(lines)+'\n';(root/'report.md').write_text(text)
    def inline(x):return re.sub(r'\[([^\]]+)\]\(([^)]+)\)',r'<a href="\2">\1</a>',html.escape(x))
    parts=[];table=False
    for line in lines:
        if table and not line.startswith('|'):parts.append('</table>');table=False
        if line.startswith('|'):
            if re.fullmatch(r'[|:\- ]+',line):continue
            tag='td' if table else 'th'
            if not table:parts.append('<table>');table=True
            parts.append('<tr>'+''.join('<'+tag+'>'+inline(x.strip())+'</'+tag+'>' for x in line.strip('|').split('|'))+'</tr>')
        elif line.startswith('!['):
            m=re.fullmatch(r'!\[([^\]]*)\]\(([^)]+)\)',line)
            if m:parts.append('<img style="max-width:100%" src="'+html.escape(m[2])+'" alt="'+html.escape(m[1])+'">')
        elif line.startswith('#'):
            n=len(line)-len(line.lstrip('#'));parts.append(f'<h{n}>'+inline(line[n:].strip())+f'</h{n}>')
        elif line:parts.append('<p>'+inline(line)+'</p>')
    if table:parts.append('</table>')
    (root/'report.html').write_text('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>目标保持验收</title><style>body{max-width:1300px;margin:32px auto;padding:0 20px;font:16px/1.65 sans-serif}table{border-collapse:collapse;width:100%;font-size:14px;margin:20px 0}td,th{padding:7px;border:1px solid #ccd5df;text-align:right}th{background:#edf2f8}td:first-child,th:first-child{text-align:left}a{color:#175cb5}</style>'+''.join(parts))


def finish(root):
    c=initialize(root);s=load(root/'state.json');assert len(s['runs'])>=130
    meta=load(Path(c['bundles']['baseline'])/'bundle.json')
    subprocess.run(['docker','run','--rm','--network','none','--cpus','2','--user',f'{os.getuid()}:{os.getgid()}',
      '-v',str(manage.ROOT)+':'+str(manage.ROOT)+':ro','-v',str(root)+':'+str(root),'-w',str(manage.ROOT),
      '--entrypoint','python3',meta['images']['evaluator'],'-B',str(Path(__file__)),'analyze','--output',str(root)],check=True,timeout=90)
    verify(root);s=load(root/'state.json');s.update(state='completed',report_completed=time.time());s.pop('error',None);write(root/'state.json',s);render(root)
    write(root/'report-seal.json',{p.name:sha(p) for p in root.iterdir() if p.is_file() and p.suffix!='.log' and p.name not in ('report-seal.json','controller.lock')})
    print(json.dumps(dict(completed=True,trials=len(s['runs']),selected=s.get('selected'),independently_confirmed=s.get('backend_confirmed',False))),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('analyze','finish'));parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();root=args.output.resolve()
    if args.action=='analyze':analysis(root)
    else:finish(root)
