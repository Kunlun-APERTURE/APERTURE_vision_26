"""Bounded continuity pilot. Never grants capacity or UI equivalence."""
import argparse
import fcntl
import html
import json
import os
from pathlib import Path
import statistics
import time
import uuid

import background_jobs as jobs
import experiments
import manage
from report import load,write
from sync_diagnosis import freeze_candidate
from sync_study import DEFAULTS,background,original_ui

SCENES=('hf_uniform_3','hf_turn90_3')


def ui(path,state):
    if len(state['reference'])==6:return
    with original_ui(state),(manage.RUNTIME/'operation.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            manage.stop()
            manage.start(argparse.Namespace(**dict(DEFAULTS,scene='static',control='off',view='off',replace=False,fire='auto')))
            for repeat in range(3):
                for scene in (SCENES if repeat%2==0 else tuple(reversed(SCENES))):
                    if any(r['scene']==scene and r['repeat_id']==repeat for r in state['reference']):continue
                    opts=argparse.Namespace(**DEFAULTS,scene=scene,seed=2026102207+repeat%3,
                        repeat_id=repeat,label='continuity-instrumented-ui',session_evidence=True)
                    folder=experiments.run_one(manage,scene,opts)
                    state['reference'].append(dict(run_id=folder.name,scene=scene,repeat_id=repeat,seed=opts.seed))
                    write(path,state)
                    if load(folder/'summary.json')['verdict']=='invalid':raise RuntimeError('Invalid UI evidence: '+folder.name)
        finally:
            manage.stop()
            profile=manage.profile();profile.update(scene='static',terrain='arena',control='off',fire='off',view='off',
                algorithm_profile='legacy',gimbal_profile='legacy',max_fps=None,record_timing='off')
            manage.save_profile(profile)


def rows(state):
    for entry in state['reference']:yield 'instrumented_ui',entry
    for mode,value in state['modes'].items():
        for entry in value['runs']:yield mode,entry


def summarize(root):
    state=load(root/'continuity-state.json');trials=[]
    for mode,entry in rows(state):
        folder=manage.OPS/'experiments'/entry['run_id'];s=load(folder/'run-state.json');summary=load(folder/'summary.json')
        start=s['measurement_start_stamp_ns'];audit=load(folder/'session-evidence-audit.json')
        packets=[json.loads(line) for line in (folder/'session-evidence/events.jsonl').open()]
        pulses=[p['data'] for p in packets if p['kind']=='result' and p['data'].get('fire_forwarded')]
        measured=[p for p in pulses if p['receipt_ns']>=start]
        boundary=next((p['data'] for p in packets if p['kind']=='measurement_pipeline'),None)
        first=measured[0] if measured else None
        trials.append(dict(entry,mode=mode,verdict=summary['verdict'],metrics=summary['metrics'],
            evidence_passed=audit['passed'],warmup_inputs=audit.get('warmup_inputs'),
            first_pulse_delay_ms=(first['receipt_ns']-start)*1e-6 if first else None,
            first_pulse_capture_relative_ms=(first['frame']['stamp_ns']-start)*1e-6 if first else None,
            pipeline_preserved=(boundary['before']==boundary['after']) if boundary else None,
            boundary=boundary))
    aggregates=[]
    for mode in sorted(set(t['mode'] for t in trials)):
        for scene in SCENES:
            values=[t for t in trials if t['mode']==mode and t['scene']==scene]
            if not values:continue
            shots=sum(t['metrics']['shots_fired'] for t in values)
            aggregates.append(dict(mode=mode,scene=scene,runs=len(values),shots=shots,
                exact=sum(t['metrics']['target_armor_hits'] for t in values)/shots if shots else None,
                weighted=sum(t['metrics']['weighted_hit_score_sum'] or 0 for t in values)/shots if shots else None,
                correct_tracking=statistics.mean(t['metrics']['correct_tracking_fraction'] for t in values),
                first_pulse_ms=statistics.mean(t['first_pulse_delay_ms'] for t in values if t['first_pulse_delay_ms'] is not None),
                warmup_inputs=[t['warmup_inputs'] for t in values]))
    result=dict(state=state['state'],trials=trials,aggregates=aggregates,certified=False,
        limitation='Three paired runs diagnose direction only; no statistical equivalence or capacity registration.')
    write(root/'result.json',result)
    lines=['# 暖机与测量连续性修复','',
        '本批为预先固定的三对匀速/转向诊断，不授予 UI 等效或并发资格。旧 UI 使用冻结的模拟器和算法，增加被动全会话归档，单独标记为 instrumented_ui。','',
        '| 模式 | 场景 | 轮数 | 发数 | 精确命中 | 加权命中 | 正确跟踪 | 首个开火脉冲 ms |',
        '|---|---|---:|---:|---:|---:|---:|---:|']
    for r in aggregates:
        lines.append(f"| {r['mode']} | {r['scene']} | {r['runs']} | {r['shots']} | {r['exact']:.2%} | {r['weighted']:.2%} | {r['correct_tracking']:.2%} | {r['first_pulse_ms']:.2f} |")
    lines+=['','## 证据与逐轮结果','',
        '暖机原始 JPEG、发送头、算法结果、开火放行与阶段边界完整归档；暖机不计分。首脉冲时间不同于弹丸实际发射时间。']
    for t in trials:
        link=os.path.relpath(manage.OPS/'experiments'/t['run_id']/'report.html',root)
        lines.append(f"- {t['mode']} / {t['scene']} / {t['repeat_id']}：[报告]({link})，完整历史审计 {t['evidence_passed']}，暖机 {t['warmup_inputs']} 帧，管线保留 {t['pipeline_preserved']}。")
    lines+=['','[结构化结果](result.json) · [冻结与执行计划](continuity-state.json)']
    text='\n'.join(lines)+'\n';(root/'report.md').write_text(text)
    (root/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>连续性诊断</title>'
        '<style>body{font:15px system-ui;max-width:1200px;margin:2em auto}pre{white-space:pre-wrap}</style>'
        '<h1>暖机与测量连续性修复</h1><pre>'+html.escape(text)+'</pre><p><a href="result.json">完整结果</a></p>')
    experiments.publish_desktop(manage,root)
    return result


def run(root):
    root.mkdir(parents=True,exist_ok=True)
    with (root/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        path=root/'continuity-state.json';state=load(path)
        if not state:
            with jobs.connect() as db:
                if db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running','pausing')").fetchone()[0]:
                    raise RuntimeError('Drain existing jobs first')
            state=dict(name=root.name,state='freezing',created=time.time(),reference=[],modes={},
                plan=dict(control='ui-fixed-v4 single lane, one run per scene; passive instrumentation only',
                    reference='Frozen original UI with complete session archive; three runs per scene',
                    candidate='ui-fixed-v5 single lane, three runs per scene; continuity only',
                    scenes=list(SCENES),formal_qualification=False,capacity_registration=False))
            write(path,state)
            state['bundle']=str(freeze_candidate('sync_continuity_'+uuid.uuid4().hex[:12],
                'daedalus:rm26-sync-diag-v3','daedalus:rm26-evaluator-sync-diag-v3'))
            write(path,state)
        state['state']='running';write(path,state)
        try:
            background(path,state,'ui-fixed-v4',1,1,scenes=SCENES)
            ui(path,state)
            background(path,state,'ui-fixed-v5',1,3,scenes=SCENES)
            state.update(state='completed',ended=time.time());write(path,state)
            summarize(root)
        except BaseException as error:
            state.update(state='failed',error=type(error).__name__+': '+str(error));write(path,state);raise


def receipt(root,parent):
    """Six separately frozen trials; reuse references without pooling versions."""
    root.mkdir(parents=True,exist_ok=True)
    with (root/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        path=root/'continuity-state.json';state=load(path)
        if not state:
            reference=load(parent/'continuity-state.json')
            if reference['state']!='completed':raise ValueError('Complete the parent pilot first')
            state=dict(name=root.name,state='freezing',created=time.time(),reference=[],modes={},
                parent_reference=str(parent),original_ui=reference['original_ui'],
                plan=dict(candidate='ui-fixed-v6, three single-lane runs per scene',scenes=list(SCENES),
                    intervention='Park only after the final warmup logical receipt and command ACK, while continuing camera sampling.',
                    formal_qualification=False,capacity_registration=False))
            write(path,state)
            state['bundle']=str(freeze_candidate('sync_receipt_'+uuid.uuid4().hex[:12],
                'daedalus:rm26-sync-diag-v3','daedalus:rm26-evaluator-sync-diag-v3'))
            write(path,state)
        if state['parent_reference']!=str(parent):raise ValueError('Resume with the same reference')
        state['state']='running';write(path,state)
        try:
            background(path,state,'ui-fixed-v6',1,3,scenes=SCENES)
            state.update(state='completed',ended=time.time());write(path,state)
            summarize(root)
        except BaseException as error:
            state.update(state='failed',error=type(error).__name__+': '+str(error));write(path,state);raise


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['run','receipt','report'])
    parser.add_argument('--output',required=True,type=Path);parser.add_argument('--parent',type=Path);args=parser.parse_args()
    if args.action=='receipt':
        if args.parent is None:parser.error('Receipt trials require --parent')
        receipt(args.output.resolve(),args.parent.resolve())
    else:(run if args.action=='run' else summarize)(args.output.resolve())
