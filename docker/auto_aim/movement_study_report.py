"""Difficulty-first tier report; capability baselines do not claim algorithm tuning."""
import argparse
from collections import Counter
import html
from pathlib import Path
import time
import background_jobs as jobs
import manage
from decision_benchmark import sha,verify_bundle
from decision_benchmark_report import aggregate,unified_score
from movement_analysis import analyze
from perception_study import protected
from report import load,write
from movement_scenarios import NEW_SCENES

def diagnostics(folder):
    state=load(folder/'run-state.json');counts=Counter();last=None;last_active=False
    for line in (folder/'session-evidence/events.jsonl').open():
        p=__import__('json').loads(line)
        if p['kind']!='result':continue
        f,r=p['data']['frame'],p['data']['result']
        if not state['measurement_start_stamp_ns']<=f['stamp_ns']<=state['measurement_end_stamp_ns']:continue
        counts['frames']+=1;counts['temp_lost_frames']+=r.get('tracker')=='temp_lost'
        active=bool((r.get('continuity',{}).get('pixel_rotation') or {}).get('active'))
        counts['model_active_frames']+=active;counts['model_takeovers']+=active and not last_active;counts['model_releases']+=last_active and not active;last_active=active
        if r.get('valid') and last and last.get('track_generation')==r.get('track_generation'):
            counts['plate_switches']+=last.get('selected_plate_id')!=r.get('selected_plate_id')
            import math
            jump=abs(math.remainder(r['yaw']-last['yaw'],2*math.pi))
            counts['command_jumps_over_1deg']+=jump>math.pi/180
        last=r if r.get('valid') else None
    return dict(counts)

def verify(root):
    c,s=load(root/'contract.json'),load(root/'state.json')
    assert sha(root/'contract.json')==s['contract_sha256'] and protected()==c['protected']
    for path,h in c['historical_files'].items():assert sha(Path(path))==h
    for bundle in c['bundles'].values():verify_bundle(Path(bundle))
    for name,h in c['suite_source_hashes'].items():assert sha(Path(c['bundles']['baseline'])/'docker/auto_aim'/name)==h
    actual_overlap={}
    for scene,level in s['parallelism_by_scene'].items():
        attempts=[r['attempt'] for r in s['runs'] if r['phase']=='probe_p2' and r['scene']==scene]
        overlap=len(attempts)==2 and attempts[0]['slot']!=attempts[1]['slot'] and attempts[0]['started']<attempts[1]['ended'] and attempts[1]['started']<attempts[0]['ended']
        actual_overlap[scene]=overlap
        if level==2:assert overlap,'Parallel qualification requires actual simultaneous work'
    write(root/'parallel-overlap-audit.json',actual_overlap)
    seen=set();details=[];evidence={};comparisons=[]
    for r in s['runs']:
        key=(r['phase'],r['scene'],r['variant'],r['sensor_seed']);assert key not in seen;seen.add(key)
        folder=manage.OPS/'experiments'/r['run_id'];m=load(folder/'manifest.json');meta=load(Path(c['bundles'][r['variant']])/'bundle.json')
        assert m['algorithm_binary_sha256']==meta['files']['sp_vision_25/build/simulator/sim_auto_aim']
        assert m['configuration_sha256']['algorithm.yaml']==meta['files']['sp_vision_25/configs/simulator.yaml']
        assert m['terrain']=='flat_range' and m['input_contract']=='aligned-v1' and m['warmup_frames']==30
        assert m['shooting']['physics']=='no-drag' and m['shooting']['fire_rate_limit_hz']==5 and m['shooting']['fire_policy']=='advice'
        assert m['sensor_seed']==r['sensor_seed'] and load(folder/'session-evidence-audit.json')['passed']
        assert r['wall_s']<=40 and r.get('complete_wall_s',0)<=40
        for name,h in r['source_hashes'].items():assert sha(folder/name)==h
        score=unified_score(folder,root/'normalized-scores'/r['run_id'],c)['metrics']
        assert (score['shots_fired'],score['target_armor_hits'])==(r['shots'],r['hits'])
        assert abs(score['weighted_hit_score_sum']-r['weight'])<1e-10
        if r['engine']=='backend':
            with jobs.connect() as db:attempts=list(db.execute('SELECT a.* FROM attempts a JOIN items i ON a.item_id=i.id WHERE i.job_id=?',(r['job_id'],)))
            assert len(attempts)==1 and attempts[0]['state']=='finished' and attempts[0]['verdict']!='invalid'
        motion=None
        if r['scene'] in NEW_SCENES:
            import json
            shots=[json.loads(x) for x in (root/'normalized-scores'/r['run_id']/'shots.jsonl').open()]
            motion=analyze(folder,root/'motion'/r['run_id'],shots)
            start=load(folder/'run-state.json')['measurement_start_stamp_ns'];duration=r['duration_s']
            hit_times=sorted((x['terminal_stamp_ns']-start)*1e-9 for x in shots if x['useful_hit'] and x.get('terminal_stamp_ns') is not None and 0<=(x['terminal_stamp_ns']-start)*1e-9<=duration)
            edges=[0.,*hit_times,duration]
            motion['longest_no_hit_interval_s']=max(b-a for a,b in zip(edges,edges[1:]))
            motion['first_hit_s']=r['first_hit_s'];motion['first_hit_censored']=r['first_hit_censored']
            write(root/'motion'/r['run_id']/'motion-analysis.json',motion)
        details.append(dict(run_id=r['run_id'],phase=r['phase'],engine=r['engine'],variant=r['variant'],scene=r['scene'],
            sensor_seed=r['sensor_seed'],motion=motion,diagnostics=diagnostics(folder)))
        evidence[r['run_id']]={n:sha(folder/n) for n in ('manifest.json','run-state.json','summary.json','frames.jsonl','telemetry.jsonl','session-evidence/events.jsonl')}
    assert {(r['scene'],r['variant'],r['sensor_seed']) for r in s['runs'] if r['phase']=='audit'}=={(sc,v,seed) for sc in c['scenes'] for v in c['variants'] for seed in c['sensor_seeds']}
    if c['tier']=='advanced':
        from movement_scenarios import BASIC_COMBINED
        assert {(r['scene'],r['variant'],r['sensor_seed']) for r in s['runs'] if r['phase']=='basic_regression' and r['scene'] in BASIC_COMBINED}=={(scene,v,seed) for scene in BASIC_COMBINED for v in c['variants'] for seed in c['sensor_seeds']}
        addendum=load(root/'execution-addendum.json') or {}
        references=addendum.get('basic_references',c.get('basic_references',[]))
        assert len(references)==80
        parent=Path(c['parent']);assert load(parent/'validation.json')['passed']
        if addendum:
            assert sha(parent/'report-seal.json')==addendum['parent_seal_sha256']
            assert sha(root/'controller-resume-source.py')==addendum['resumed_controller_sha256']
        for row in references:
            folder=manage.OPS/'experiments'/row['run_id']
            for name,h in row['source_hashes'].items():assert sha(folder/name)==h
            m=load(folder/'manifest.json');meta=load(Path(c['bundles'][row['variant']])/'bundle.json')
            assert m['algorithm_binary_sha256']==meta['files']['sp_vision_25/build/simulator/sim_auto_aim']
            assert m['configuration_sha256']['algorithm.yaml']==meta['files']['sp_vision_25/configs/simulator.yaml']
        write(root/'foundation-reference-results.json',dict(source=str(parent),historical_only=True,not_new_samples=True,runs=references))
    assert len([r for r in s['runs'] if r['phase']=='ui'])==2*len(c['expected_ui_scenes'])
    import random
    for scene in c['scenes']:
        paired={v:{r['sensor_seed']:r for r in s['runs'] if r['phase']=='audit' and r['scene']==scene and r['variant']==v} for v in c['variants']}
        assert set(paired['baseline'])==set(paired['R4'])
        seeds=sorted(paired['baseline']);rng=random.Random(20261027);draws=[]
        for _ in range(10000):
            draw=rng.choices(seeds,k=len(seeds))
            a=aggregate([paired['baseline'][seed] for seed in draw]);b=aggregate([paired['R4'][seed] for seed in draw])
            draws.append(b['weighted_hit_rate']-a['weighted_hit_rate'] if a['weighted_hit_rate'] is not None and b['weighted_hit_rate'] is not None else None)
        values=sorted(x for x in draws if x is not None)
        ci=[values[249],values[9749]] if len(values)==10000 else None
        comparisons.append(dict(scene=scene,seeds=seeds,weighted_gain_95=ci,descriptive_only=True,failed_resamples=sum(x is None for x in draws)))
    write(root/'paired-effects.json',dict(contrasts=comparisons,samples=10000,unit='paired sensor seed within scene',not_independent_optimization_confirmation=True))
    write(root/'difficulty-results.json',details);write(root/'run-evidence-hashes.json',evidence)
    # Preserve the first interrupted preflight as separate history, never mix it into effects.
    previous=root.with_name(root.name.removesuffix('-r2')) if root.name.endswith('-r2') else None
    if previous and (previous/'validation-diagnosis.json').exists():
        history=load(previous/'validation-diagnosis.json')
        for run_id,files in history.get('original_failed_evidence_hashes',{}).items():
            for name,h in files.items():assert sha(manage.OPS/'experiments'/run_id/name)==h
        write(root/'preflight-history.json',history)
    write(root/'validation.json',dict(passed=True,backend_trials=sum(r['engine']=='backend' for r in s['runs']),ui_trials=sum(r['engine']=='ui' for r in s['runs']),
        maximum_complete_trial_s=max(max(r['wall_s'],r.get('complete_wall_s',0)) for r in s['runs']),
        protected_unchanged=True,algorithm_binary_config_unchanged=True,original_reports_unchanged=True,raw_scores_recomputed=True,
        difficulty_is_separate=True,ui_equivalence_certified=False,candidate_promoted=False,baseline_reference_trials=80 if c['tier']=='advanced' else 0,new_samples_exclude_historical=True,report_generator_sha256=sha(Path(__file__))))

def render(root):
    c,s=load(root/'contract.json'),load(root/'state.json');details=load(root/'difficulty-results.json');lookup={r['run_id']:r for r in details}
    pct=lambda v:'—' if v is None else f'{100*v:.2f}%'
    lines=['# 分档移动旋转能力评估','',
        '本轮只新增测试，v3与R4二进制、模型和算法配置保持冻结。基础、进阶、压力独立解释；运动强度达标、实验有效性和射击效果分别判断。不以得分低为理由补种子或改参数。',
        '',f'选定档次：{c["tier"]}；共{len(s["runs"])}轮，'+str(load(root/'validation.json')['ui_trials'])+'轮UI。完整单次最长'+f'{load(root/"validation.json")["maximum_complete_trial_s"]:.2f}s。',
        '', '[配对清单](contract.json) · [证据校验](validation.json) · [完整难度诊断](difficulty-results.json) · [单/双路复现](parallel-result.json)','']
    for phase,title in (('audit','本档后台三种子'),('basic_regression','本轮基础组合补测与已发生的额外基础采集'),('ui','UI固定特征检查')):
        rr=[r for r in s['runs'] if r['phase']==phase]
        if not rr:continue
        lines+=['## '+title,'','| 场景 | 配置 | 命中/发射 | 精确 | 加权 | 命中/秒 | 难度达标轮数 | 联合达标发射数 |','|---|---|---:|---:|---:|---:|---:|---:|']
        for scene in dict.fromkeys(r['scene'] for r in rr):
            for variant in c['variants']:
                rows=[r for r in rr if (r['scene'],r['variant'])==(scene,variant)];a=aggregate(rows)
                motions=[lookup[r['run_id']]['motion'] for r in rows if lookup[r['run_id']]['motion']]
                achieved=str(sum(m['intensity_achieved'] for m in motions))+'/'+str(len(motions)) if motions else '旧基础场景'
                lines.append(f'| {scene} | {variant} | {a["hits"]}/{a["shots"]} | {pct(a["target_hit_rate"])} | {pct(a["weighted_hit_rate"])} | {a["hits_per_s"]:.3f} | {achieved} | '+str(sum(m['at_requested_motion']['shots'] for m in motions))+' |')
        if phase=='ui':lines+=['','单种子UI与后台分开，不构成引擎等价认证。']
    if (root/'foundation-reference-results.json').exists():
        refs=load(root/'foundation-reference-results.json')['runs']
        lines+=['','## 沿用的六项已封存基础结果','','这些记录来自上一轮R4独立验收，二进制与配置一致；全部80轮保留，属于历史参照，不并入本轮独立样本。当前已发生的额外基础采集完整列在前表；本轮新增四项3/4m/s组合均另跑三个种子。','','| 场景 | 配置 | 命中/发射 | 精确 | 加权 | 命中/秒 |','|---|---|---:|---:|---:|---:|']
        for scene in dict.fromkeys(x['scene'] for x in refs):
            for variant in c['variants']:
                a=aggregate([x for x in refs if x['scene']==scene and x['variant']==variant])
                lines.append(f'| {scene} | {variant} | {a["hits"]}/{a["shots"]} | {pct(a["target_hit_rate"])} | {pct(a["weighted_hit_rate"])} | {a["hits_per_s"]:.3f} |')
    lines+=['','## 跟踪、切板与命中间断诊断（后台三种子）','','| 场景/配置 | 模型接管/退出 | 暂失帧 | 切板次数 | >1°命令跳变 | 最长未命中/s |','|---|---:|---:|---:|---:|---:|']
    for scene in c['scenes']:
        for variant in c['variants']:
            rr=[x for x in details if x['phase']=='audit' and x['scene']==scene and x['variant']==variant]
            cc=sum((Counter(x['diagnostics']) for x in rr),Counter());gaps=[x['motion']['longest_no_hit_interval_s'] for x in rr if x['motion']]
            lines.append('| '+scene+'/'+variant+' | '+str(cc['model_takeovers'])+'/'+str(cc['model_releases'])+' | '+str(cc['temp_lost_frames'])+' | '+str(cc['plate_switches'])+' | '+str(cc['command_jumps_over_1deg'])+' | '+('—' if not gaps else f'{max(gaps):.3f}')+' |')
    lines+=['','未命中间断按实际碰撞时刻计算，包含计分起止边界；仅统计计分内已发生的有用命中，计分末尾仍在飞行的命中保留于最终射击评分。模型激活次数、切板次数不直接等于效果收益。']
    if (root/'preflight-history.json').exists():
        lines+=['','首次预检因旧全程角速度中位数规则误判旋转启停而中止，保留12轮成功及两条无效记录；本修订批次只在预声明旋转阶段沿用原阈值验证实际旋转。脚本、物理、算法和射击评分不变，旧记录未回写，见[中断审计](preflight-history.json)。']
    lines+=['','## 实际运动难度（后台R4，不用脚本指令冒充真值）','','| 场景/种子 | 速度中位/m/s | 角速中位/rad/s | 距离P50/P95/m | 联合达标时长/s | 实际变向响应比例 | 未达标原因 |','|---|---:|---:|---:|---:|---:|---|']
    for row in details:
        m=row['motion']
        if not m or row['phase']!='audit' or row['variant']!='R4':continue
        num=lambda x:'—' if x is None else f'{x:.2f}'
        lines.append('| '+row['scene']+'/'+str(row['sensor_seed'])+' | '+num(m['speed_p50_mps'])+' | '+num(m['omega_p50_radps'])+' | '+num(m['range_p50_m'])+'/'+num(m['range_p95_m'])+' | '+num(m['joint_motion']['good_s'])+' | '+pct(m['turn_response_fraction'])+' | '+', '.join(m['failure_reasons'])+' |')
    lines+=['','## 同时达标时段的射击成绩（后台）','','| 场景 | 配置 | 达标发射数 | 达标精确命中 | 达标加权命中 | 达标命中/秒 |','|---|---|---:|---:|---:|---:|']
    for scene in c['scenes']:
        for variant in c['variants']:
            rr=[x['motion'] for x in details if x['phase']=='audit' and x['scene']==scene and x['variant']==variant and x['motion']]
            if not rr:continue
            n=sum(x['at_requested_motion']['shots'] for x in rr);h=sum(x['at_requested_motion']['useful_armor_hits'] for x in rr)
            weight=sum(x['at_requested_motion']['weighted_hit_score_sum'] or 0 for x in rr);seconds=sum(x['joint_motion']['good_s'] for x in rr)
            lines.append('| '+scene+' | '+variant+' | '+str(n)+' | '+pct(h/n if n else None)+' | '+pct(weight/n if n else None)+' | '+('—' if not seconds else f'{h/seconds:.3f}')+' |')
    lines+=['','## 配对区间（描述性）','','| 场景 | 加权差95%区间/pp |','|---|---:|']
    for x in load(root/'paired-effects.json')['contrasts']:
        ci=x['weighted_gain_95'];lines.append('| '+x['scene']+' | '+('无有效区间' if ci is None else f'[{100*ci[0]:+.2f}, {100*ci[1]:+.2f}]')+' |')
    lines+=['','每格仅三个传感器种子，区间仅描述；不将少样本的新难度基准当成一次新的算法优化通过。']
    lines+=['','0.4秒指令变化可能受原有0.5秒伺服反向防抖和加速度约束。若实际转向和联合速度不足，就按工况未达标报告，不修改物理使其过关。各阶段、变向后0–0.5/0.5–1秒、距离分组和零射击见完整难度记录。','',
        '轨迹是世界坐标下预先固定的双方速度路线；不从真值闭环追击，不向算法传入场景标签。现有车辆模型不代表真实舵轮动力学。','',
        '## 视觉证据','', '仅在用户明确要求时生成验收视频，采用最终UI配对：左v3、右R4，展示完整计分/结算、命中靶样、实际速度与角速度、距离及阶段。视频在独立目录导出，不改写本报告。']
    (root/'report.md').write_text('\n'.join(lines)+'\n')
    table='\n'.join(lines)
    (root/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>分档组合运动评估</title><style>body{font:16px/1.6 sans-serif;max-width:1400px;margin:30px auto}pre{white-space:pre-wrap}a{color:#1671b5}</style><h1>分档移动旋转评估</h1><p><a href="report.md">完整Markdown报告</a> · <a href="difficulty-results.json">联合运动与射击数据</a></p><pre>'+html.escape(table)+'</pre>')

def finish(root):
    root=Path(root)
    import shutil
    shutil.copy2(Path(__file__),root/'report-generator-source.py')
    verify(root);s=load(root/'state.json');s.update(state='completed',completed=time.time(),independently_verified=False,no_algorithm_optimization=True)
    s.pop('error',None);write(root/'state.json',s);render(root)
    write(root/'report-seal.json',{p.name:sha(p) for p in root.iterdir() if p.is_file() and p.suffix!='.log' and p.name not in ('report-seal.json','controller.lock')})
    import experiments
    experiments.publish_desktop(manage,root)
    print(dict(completed=True,trials=len(s['runs']),video_queued=False),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();finish(a.output)
