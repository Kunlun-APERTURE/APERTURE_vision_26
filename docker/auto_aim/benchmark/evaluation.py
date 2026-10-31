"""Common scoring, explicit comparison eligibility and behavior fingerprints."""
import html
import json
from pathlib import Path
from .core import identity,sha
from .artifacts import verify
from .scenarios import get
from report import load,write


def behavior(folder):
    """Whole-session fingerprint, excluding only documented administrative clocks/IDs."""
    from .diagnostics import events
    return identity(list(events(folder)))


class Evaluator:
    def evaluate(self,folder,output):
        from shots import score
        folder=Path(folder);output=Path(output)
        if output.resolve().is_relative_to(folder.resolve()):raise ValueError('Derived scoring must be outside sealed evidence')
        output.mkdir(parents=True,exist_ok=True)
        bundle=load(folder/'configuration/bundle.json',{})
        for name in ('shots.py','impact_quality.py'):
            expected=bundle.get('files',{}).get('docker/auto_aim/'+name)
            if expected and sha(Path(__file__).parents[1]/name)!=expected:raise ValueError('Use the frozen scoring implementation: '+name)
        for name,digest in load(folder/'evidence-sha256.json',{}).items():
            if sha(folder/name)!=digest:raise ValueError('Source evidence changed: '+name)
        manifest=load(folder/'manifest.json');state=load(folder/'run-state.json');summary=load(folder/'summary.json')
        manifest=dict(manifest,scoring_version=2)
        metrics,errors=score(folder,manifest,state,output)
        duration=manifest['duration_s']
        metrics.update(hits_per_s=metrics['target_armor_hits']/duration,weighted_points_per_s=metrics['weighted_hit_score_sum']/duration if metrics['weighted_hit_score_sum'] is not None else None)
        shots=[json.loads(x) for x in (output/'shots.jsonl').read_text().splitlines()]
        result=dict(run_id=folder.name,scene=manifest['scene'],sensor_seed=manifest.get('sensor_seed'),metrics=metrics,
            evidence_valid=summary['verdict']!='invalid' and not errors and load(folder/'session-evidence-audit.json',{}).get('passed',False),
            errors=errors,within_cost_budget=load(folder/'timing.json',{}).get('wall_seconds',float('inf'))<=40,
            difficulty=get(manifest['scene']).difficulty(folder,output/'motion',shots),
            behavior_sha256=behavior(folder),source_manifest_sha256=sha(folder/'manifest.json'))
        write(output/'result.json',result)
        return result


def report(root):
    import background_jobs as jobs
    import manage
    root=Path(root);contract=load(root/'contract.json');state=load(root/'state.json')
    if identity(contract)!=state['contract_sha256']:raise ValueError('Frozen contract changed')
    verify(contract['bundle'],contract.get('payload_identity'))
    rows=[]
    with jobs.connect() as db:
        attempts=[dict(r) for r in db.execute('SELECT a.*,i.ordinal FROM attempts a JOIN items i ON a.item_id=i.id WHERE i.job_id=? ORDER BY i.ordinal,a.started',(state['job_id'],))]
    for attempt in attempts:
        if attempt.get('run_id'):
            try:row=Evaluator().evaluate(manage.OPS/'experiments'/attempt['run_id'],root/'scores'/attempt['run_id'])
            except (OSError,ValueError,KeyError,TypeError) as error:
                manifest=load(manage.OPS/'experiments'/attempt['run_id']/'manifest.json',{})
                row=dict(run_id=attempt['run_id'],scene=manifest.get('scene','unknown'),sensor_seed=manifest.get('sensor_seed'),
                    evidence_valid=False,within_cost_budget=False,errors=[str(error)],difficulty={},
                    metrics=dict(shots_fired=0,target_armor_hits=0,weighted_hit_rate=None,weighted_hit_score_sum=None,hits_per_s=0))
            rows.append(dict(row,attempt=attempt))
    completed={r['attempt']['ordinal'] for r in rows if r['evidence_valid']}
    groups=[]
    for scene in contract['spec']['scenes']:
        rr=[r for r in rows if r['scene']==scene and r['evidence_valid']]
        n=sum(r['metrics']['shots_fired'] for r in rr);hits=sum(r['metrics']['target_armor_hits'] for r in rr)
        weight=sum(r['metrics']['weighted_hit_score_sum'] or 0 for r in rr)
        duration=sum(load(__import__('manage').OPS/'experiments'/r['run_id']/'manifest.json')['duration_s'] for r in rr)
        groups.append(dict(scene=scene,trials=len(rr),shots=n,hits=hits,exact=hits/n if n else None,
            weighted=weight/n if n else None,hits_per_s=hits/duration if duration else None,
            weighted_points_per_s=weight/duration if duration else None))
    result=dict(groups=groups,version=1,contract_sha256=state['contract_sha256'],runs=rows,attempts=attempts,
        complete=len(completed)==state['items'],all_evidence_valid=bool(rows) and all(r['evidence_valid'] for r in rows) and all(a['state']=='finished' and a['verdict']!='invalid' for a in attempts),
        within_cost_budget=bool(rows) and all(r['within_cost_budget'] for r in rows),ui_equivalence_certified=False)
    write(root/'results.json',result)
    lines=['# B1 实验参考成绩','','环境：'+contract['spec']['environment'], '',
        '|场景|噪声种子|命中/发射|加权命中|命中/秒|证据有效|40秒成本|','|---|---:|---:|---:|---:|---|---|']
    for r in rows:
        m=r['metrics'];w=m['weighted_hit_rate'];lines.append(f"|{r['scene']}|{r['sensor_seed']}|{m['target_armor_hits']}/{m['shots_fired']}|{w if w is not None else 'N/A'}|{m['hits_per_s']:.3f}|{r['evidence_valid']}|{r['within_cost_budget']}|")
    lines+=['','实际难度与分阶段射击见 results.json 的 runs[].difficulty；零射击保持 N/A，所有失败尝试保留。',
        '','|场景|有效轮数|命中/发射|精确命中|加权命中|命中/秒|加权得分/秒|','|---|---:|---:|---:|---:|---:|---:|']
    pct=lambda v:'N/A' if v is None else f'{100*v:.2f}%'
    for g in groups:
        lines.append(f"|{g['scene']}|{g['trials']}|{g['hits']}/{g['shots']}|{pct(g['exact'])}|{pct(g['weighted'])}|{g['hits_per_s']}|{g['weighted_points_per_s']}|")
    write(root/'state.json',dict(state,state='completed' if result['complete'] and result['all_evidence_valid'] else 'incomplete'))
    text='\n'.join(lines)+'\n';(root/'report.md').write_text(text)
    (root/'report.html').write_text('<meta charset="utf-8"><pre>'+html.escape(text)+'</pre>')
    return result


def compare(left,right):
    a,b=[load(Path(p)/'contract.json') for p in (left,right)]
    for key in ('environment','plans','scoring_version','impact_quality_version'):
        if a[key]!=b[key]:raise ValueError('Comparison conditions differ: '+key)
    for key in ('scenes','sensor_seeds','trajectory_seed','repeats'):
        if a['spec'][key]!=b['spec'][key]:raise ValueError('Pairing differs: '+key)
    ma,mb=[verify(c['bundle']) for c in (a,b)]
    for role in ('simulator','evaluator'):
        if ma['images'][role]!=mb['images'][role]:raise ValueError('Runtime image differs: '+role)
    common=('vision_simulator_26/config.toml','docker/auto_aim/shots.py','docker/auto_aim/impact_quality.py','docker/auto_aim/bridge.py','docker/auto_aim/sync_bridge.py')
    for name in common:
        if ma['files'].get(name)!=mb['files'].get(name):raise ValueError('Environment implementation differs: '+name)
    aa,bb=[load(Path(p)/'results.json') for p in (left,right)]
    if not all(r['complete'] and r['all_evidence_valid'] for r in (aa,bb)):raise ValueError('Complete valid experiments required')
    return dict(eligible=True,left=aa,right=bb)
