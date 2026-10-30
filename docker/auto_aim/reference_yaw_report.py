"""Environment-separated reporting and explicitly paired UI review exports."""
import json,math,time
from collections import Counter
from pathlib import Path
import manage
from report import load,write
from decision_benchmark import sha
from decision_benchmark_report import unified_score
from impact_quality import summarize

def packets(path):
    for line in path.open():
        if line.strip():yield json.loads(line)
def diagnose(root,c,r):
    folder=manage.OPS/'experiments'/r['run_id'];out=root/'normalized-scores'/r['run_id'];unified_score(folder,out,c)
    st=load(folder/'run-state.json');start=st['measurement_start_stamp_ns'];end=st['measurement_end_stamp_ns'];duration=(end-start)*1e-9
    shots=list(packets(out/'shots.jsonl'));motion=[s for s in shots if 1<=s['fire_time_s']<duration];startup=[s for s in shots if 0<=s['fire_time_s']<1]
    motion_analysis=None
    if r['scene'].startswith('adv_'):
        from movement_analysis import analyze
        motion_analysis=analyze(folder,root/'motion-analysis'/r['run_id'],shots=shots)
    trace=[p['data'] for p in packets(folder/'telemetry.jsonl') if p['kind']=='gimbal_trace' and start<=p['data']['stamp_ns']<=end]
    errors=[abs(p['error_local_rad'][0]) for p in trace];ordered=sorted(errors)
    results=[p['data']['result'] for p in packets(folder/'session-evidence/events.jsonl') if p['kind']=='result' and start<=p['data']['frame']['stamp_ns']<=end]
    pixel=[p.get('continuity',{}).get('pixel_rotation') for p in results];pixel=[p for p in pixel if p]
    rejects=Counter()
    for p in pixel:
        for u in p.get('updates',[]):
            if not u.get('accepted'):rejects[str(u.get('reason','reason_not_reported'))]+=1
    gates=Counter(k for p in results if not p.get('fire_advice',False) for k,v in (p.get('fire_check') or {}).items() if (k in ('valid','aligned','eligible','pose_valid','target_face_valid','qualification_held') and v is False) or (k=='discontinuity' and v is True))
    times=sorted(s['fire_time_s'] for s in motion);edges=[1,*times,duration];gap=max((b-a for a,b in zip(edges,edges[1:])),default=duration-1)
    def metric(rows,seconds):return dict(shots=len(rows),**summarize(rows),exact_hits=sum(bool(s['useful_hit']) for s in rows),exact_rate=sum(bool(s['useful_hit']) for s in rows)/len(rows) if rows else None,hits_per_s=sum(bool(s['useful_hit']) for s in rows)/seconds)
    return dict(run_id=r['run_id'],environment=r['environment'],scene=r['scene'],variant=r['variant'],sensor_seed=r['sensor_seed'],phase=r['phase'],motion_label='静止对照' if r['scene']=='hf_static_5' else '运动段',motion=metric(motion,duration-1),startup=metric(startup,1),longest_no_fire_s=gap,model_activation_samples=sum(bool(p.get('active')) for p in pixel),yaw_reference_samples=sum(p.get('gimbal_dynamics')=='rm2017-yaw-unitdc' for p in trace),model_limited_samples=sum(bool(p.get('model_limited')) for p in trace),model_rejections=dict(rejects),fire_gate_failures=dict(gates),muzzle_yaw_error_rms_rad=math.sqrt(sum(e*e for e in errors)/len(errors)) if errors else None,muzzle_yaw_error_p95_rad=ordered[min(len(ordered)-1,int(len(ordered)*.95))] if ordered else None,feedforward_nonzero_samples=sum(abs(p.get('feedforward_local_rad_s',[0])[0])>1e-8 for p in trace),trace_samples=len(trace),motion_intensity_achieved=motion_analysis['intensity_achieved'] if motion_analysis else None,motion_phases=motion_analysis['phases'] if motion_analysis else [],motion_failure_reasons=motion_analysis['failure_reasons'] if motion_analysis else [])

def validate(root,c,s):
    from collections import Counter
    from decision_benchmark import verify_bundle
    assert len(s['runs'])==132
    assert Counter('probe' if r['phase'].startswith('probe') else r['phase'] for r in s['runs'])=={'compatibility':4,'probe':48,'audit':72,'ui':8}
    assert sha(root/'contract.json')==s['contract_sha256']
    for p,h in c['source_hashes'].items():assert sha(manage.ROOT/p)==h,p
    for p,h in c['historical_hashes'].items():assert sha(Path(p))==h,p
    for p,h in c['source_archives'].items():assert sha(root/p)==h,p
    assert all(r['complete_wall_s']<=40 for r in s['runs'])
    for r in s['runs']:
        meta=verify_bundle(Path(c['bundles'][r['variant']]));folder=manage.OPS/'experiments'/r['run_id'];m=load(folder/'manifest.json')
        assert m['algorithm_binary_sha256']==meta['files']['sp_vision_25/build/simulator/sim_auto_aim']
        assert m['configuration_sha256']['algorithm.yaml']==meta['files']['sp_vision_25/configs/simulator.yaml']
        assert m['containers']['simulator_image_id']==meta['images']['simulator']
        assert m['gimbal_dynamics']==c['environments'][r['environment']][0]
        if r['engine']=='backend':
            assert m['execution']['timing_profile']==c['environments'][r['environment']][1]
            timing_path=Path(c['bundles'][r['variant']])/'docker/auto_aim/timing_profiles'/(m['execution']['timing_profile']+'.json')
            assert m['execution']['timing_sha256']==sha(timing_path)
            timing=load(timing_path);observed=0
            for p in packets(folder/'session-evidence/events.jsonl'):
                if p['kind']!='result':continue
                h=p['data']['frame'];observed+=1
                assert h['logical_receipt_ns']-h['logical_now_ns']==timing.get('return_delay_ns',0)
                assert h['logical_now_ns']-h['sent_ns']==timing['processing_delay_ns']
                assert p['data']['receipt_ns']==h['logical_receipt_ns']
            assert observed>0
        assert m['sensor_seed']==r['sensor_seed']
        assert load(folder/'summary.json')['verdict']!='invalid' and load(folder/'session-evidence-audit.json')['passed']
        for name,h in r['source_hashes'].items():assert sha(folder/name)==h
    for env in c['environments']:
        for scene in c['scenes']:
            rr=[r for r in s['runs'] if r['phase']=='audit' and r['environment']==env and r['scene']==scene]
            assert {(r['variant'],r['sensor_seed']) for r in rr}=={(v,seed) for v in c['variants'] for seed in c['sensor_seeds']}
    return dict(trials=132,counts=dict(Counter(r['phase'] for r in s['runs'])),passed=True)

def finish(root,c,s):
    validation=validate(root,c,s)
    rows=[diagnose(root,c,r) for r in s['runs'] if r['phase'] in ('audit','ui')];write(root/'diagnostics.json',rows)
    lines=['# 公开参考 yaw 与时序敏感性诊断','', '仅模拟诊断；冻结 v3/R4 算法与 5 Hz advice。原始模型为目标角速度到实际角速度的闭环响应，射击采用 G/G(0)，仅作用于 yaw。默认仍为 legacy，不自动推广。','', '来源：[2017 官方系统辨识教程](https://bbs.robomaster.com/article/3559?source=5)。原始 DC=1084/3975。原式与正文 6.4 Hz 描述不直接一致，保留原系数；6.4 Hz 和 IHiter 80–100 ms 仅作比较。额外 20 ms 只增加 return_delay_ns，是敏感性假设。','', '运动场景的 t≥1 s 与启动 t<1 s 分列，hf_static_5 全程为静止对照；探针不混入正式评分，低分、零射击和未达物理难度均保留。','', '|环境|场景|算法|种子|t≥1s命中/射击|精确率|加权率|命中/s|最长停火s|启动命中/射击|枪口误差RMS rad|','|---|---|---|---|---|---|---|---|---|---|---|']
    for r in rows:
        if r['phase']!='audit':continue
        m=r['motion'];a=r['startup'];lines.append(f"|{r['environment']}|{r['scene']}|{r['variant']}|{r['sensor_seed']}|{m['exact_hits']}/{m['shots']}|{m['exact_rate']}|{m['weighted_hit_rate']}|{m['hits_per_s']:.3f}|{r['longest_no_fire_s']:.3f}|{a['exact_hits']}/{a['shots']}|{r['muzzle_yaw_error_rms_rad']}|")
    aggregates=[]
    for env in c['environments']:
        for scene in c['scenes']:
            for variant in c['variants']:
                rr=[r for r in rows if r['phase']=='audit' and r['environment']==env and r['scene']==scene and r['variant']==variant]
                shots=sum(r['motion']['shots'] for r in rr);hits=sum(r['motion']['exact_hits'] for r in rr);weight=sum(r['motion']['weighted_hit_score_sum'] or 0 for r in rr)
                aggregates.append(dict(environment=env,scene=scene,variant=variant,seeds=len(rr),shots=shots,hits=hits,exact_rate=hits/shots if shots else None,weighted_rate=weight/shots if shots else None,hits_per_s=sum(r['motion']['hits_per_s'] for r in rr)/len(rr),longest_no_fire_s=max(r['longest_no_fire_s'] for r in rr),joint_motion_achieved=[r['motion_intensity_achieved'] for r in rr],model_activation_samples=sum(r['model_activation_samples'] for r in rr),model_rejections=sum(sum(r['model_rejections'].values()) for r in rr)))
    write(root/'environment-aggregate.json',aggregates)
    paired=[];sensitivity=[]
    for env in c['environments']:
        for scene in c['scenes']:
            a,b=[next(r for r in aggregates if r['environment']==env and r['scene']==scene and r['variant']==v) for v in c['variants']]
            paired.append(dict(environment=env,scene=scene,delta_R4_minus_v3={k:b[k]-a[k] if a[k] is not None and b[k] is not None else None for k in ('exact_rate','weighted_rate','hits_per_s','longest_no_fire_s')}))
    for variant in c['variants']:
        for scene in c['scenes']:
            base=next(r for r in aggregates if r['environment']=='legacy' and r['scene']==scene and r['variant']==variant)
            for env in ('refnominal','refdelay20'):
                r=next(r for r in aggregates if r['environment']==env and r['scene']==scene and r['variant']==variant)
                sensitivity.append(dict(variant=variant,scene=scene,environment=env,delta_from_legacy={k:r[k]-base[k] if r[k] is not None and base[k] is not None else None for k in ('exact_rate','weighted_rate','hits_per_s','longest_no_fire_s')}))
    write(root/'within-environment-paired.json',paired);write(root/'cross-environment-sensitivity.json',sensitivity)
    lines+=['','环境内三种子聚合及 R4−v3 配对见 environment-aggregate.json 与 within-environment-paired.json；跨环境敏感性见 cross-environment-sensitivity.json。模型激活、拒绝、各开火门、前馈、联合运动强度和枪口误差保存在 diagnostics.json。新环境不要求提高命中。','','UI 单种子实录与后台三种子统计分开；额外20ms环境不做UI认证。','', '|UI环境|场景|算法|命中/射击|完整用时s|','|---|---|---|---|---|']
    for r in s['runs']:
        if r['phase']=='ui':lines.append(f"|{r['environment']}|{r['scene']}|{r['variant']}|{r['hits']}/{r['shots']}|{r['complete_wall_s']:.3f}|")
    (root/'report.md').write_text('\n'.join(lines)+'\n');s.update(state='completed',completed=time.time(),independently_verified=False,ui_equivalence_certified=False);write(root/'state.json',s)
    write(root/'validation.json',validation);write(root/'report-seal.json',{p.name:sha(p) for p in root.iterdir() if p.is_file() and p.name not in ('report-seal.json','controller.lock') and p.suffix!='.log'})
    # Each environment gets an independent study and video to make cross-env
    # scene/seed collision impossible in the established exporter.
    import review_video,experiments
    for env in ('legacy','refnominal'):
        sub=root/('video-study-'+env);sub.mkdir(exist_ok=True);write(sub/'contract.json',dict(c,environment=env,parent=None));write(sub/'state.json',dict(s,runs=[r for r in s['runs'] if r['environment']==env]))
        (sub/'report.md').write_text('# '+env+' 环境内 v3/R4 诊断\n\n'+c['assumptions'][0]+'\n\n'+'\n'.join(l for l in lines if l.startswith('|'+env+'|') or l.startswith('|环境') or l.startswith('|---')))
        write(sub/'validation.json',dict(validation,environment=env))
        write(sub/'report-seal.json',{p.name:sha(p) for p in sub.iterdir() if p.is_file() and p.name!='report-seal.json'})
        out=review_video.prepare(sub,candidate='R4',output=root.parent/('reference-yaw-'+env+'-visual-review'),statistics=sub)
        spec=load(out/'export.json');spec['reference_yaw_environment']=env;spec['reference_yaw_assumptions']='legacy 原时序' if env=='legacy' else '2017 yaw 单位DC假设 / 原时序';write(out/'export.json',spec);review_video.enqueue(out)
    experiments.publish_desktop(manage,root)
