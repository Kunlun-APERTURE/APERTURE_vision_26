"""Paired batch comparison; repeated runs, not individual shots, are the units."""
from collections import defaultdict
import hashlib
import html
import json
import random
import time
import uuid
from pathlib import Path

from report import load,write


def collect(db,job_id,extensions=()):
    import background_jobs as jobs
    job=jobs.get_job(db,job_id)
    if json.loads(job['spec']).get('_protocol')!=2:
        raise ValueError('Compare requires new pipeline tasks; legacy results must be explicitly rescored separately')
    groups=defaultdict(dict);excluded=[]
    attempts=[a for jid in (job_id,*extensions) for a in jobs.status(db,jid)['attempts']]
    for attempt in attempts:
        if not attempt['run_id']:continue
        folder=jobs.OPS/'experiments'/attempt['run_id'];s=load(folder/'summary.json',{});m=load(folder/'manifest.json',{})
        if not s or s['verdict']=='invalid' or any(not r['passed'] and r['category'] in ('validity','performance') for r in s['rules']):
            excluded.append(folder.name);continue
        shot=m.get('shooting') or {}
        if m.get('scoring_version')!=2 or m.get('execution',{}).get('profile')!='backend-240-v1':
            raise ValueError('Comparison requires unified scoring v2 and backend-240-v1')
        condition=dict(scene=m['scene'],terrain=m['terrain'],duration=m['duration_s'],
            execution=m['execution'],physics=shot.get('physics'),bullet_speed=shot.get('bullet_speed'),
            reported_bullet_speed=shot.get('reported_bullet_speed'),origin=shot.get('aim_origin'),
            rate=shot.get('fire_rate_limit_hz'),policy=shot.get('fire_policy'),gimbal=shot.get('gimbal_profile'),
            hit_method=shot.get('hit_method'),projectile_visible=m.get('projectile_visible'),
            simulator=m['containers']['simulator_image_id'],
            simulator_config=m['configuration_sha256']['simulator.toml'],
            parallelism=m['background']['effective_parallelism'],scorer=s['shot_scorer_sha256'])
        key=json.dumps(condition,sort_keys=True)
        pair=(m['random_seed'],m['repeat_id'])
        if pair in groups[key]:raise ValueError('Duplicate successful pair identity; do not select favourable reruns')
        v=s['metrics'];n=v.get('shots_fired',0)
        if n and v.get('weighted_hit_score_sum') is None:
            excluded.append(folder.name);continue
        groups[key][pair]=dict(run_id=folder.name,shots=n,hits=v.get('target_armor_hits',0),
            weight=v.get('weighted_hit_score_sum') or 0,duration=m['duration_s'])
    return groups,excluded


def extension_jobs(db,baseline,candidate):
    results={'baseline':[],'candidate':[]};active=[]
    for job in db.execute('SELECT id,state,spec FROM jobs'):
        spec=json.loads(job['spec'])
        if spec.get('_comparison')!=[baseline,candidate]:continue
        results[spec['_side']].append(job['id'])
        if job['state'] not in ('completed','cancelled'):active.append(job['id'])
    return results,active


def extend(db,args,results,left,right):
    import background_jobs as jobs
    _,active=extension_jobs(db,args.baseline,args.candidate)
    if active:return dict(active_jobs=active,queued_jobs=[],note='Existing supplemental tasks must finish or be resumed first')
    rowsets={'baseline':[],'candidate':[]}
    for result in results:
        if result['conclusion']!='insufficient_samples' or result['additional_pairs']<=0:continue
        key=json.dumps(result['condition'],sort_keys=True)
        pairs=set(left[key])|set(right[key])
        if not pairs:continue
        # A new repeat is never an alternative attempt for a previous score.
        seed=max(pairs)[0];next_repeat=max(p[1] for p in pairs if p[0]==seed)+1
        count=min(result['additional_pairs'],10-len(pairs))
        for side,source in (('baseline',left[key]),('candidate',right[key])):
            run_id=next(iter(source.values()))['run_id']
            item=db.execute('SELECT i.settings FROM items i JOIN attempts a ON a.item_id=i.id WHERE a.run_id=?',(run_id,)).fetchone()
            if not item:raise ValueError('Missing frozen settings for supplemental experiment')
            settings=json.loads(item['settings'])
            for n in range(count):rowsets[side].append(dict(settings,seed=seed,repeat_id=next_repeat+n))
    if not rowsets['baseline']:return dict(queued_jobs=[],note='No additional paired repeats are available within the 10-pair limit')
    queued=[]
    with jobs.transaction(db):
        for side,parent_id in (('baseline',args.baseline),('candidate',args.candidate)):
            parent=jobs.get_job(db,parent_id);name='job_extend_'+uuid.uuid4().hex[:12];now=time.time()
            spec=dict(version=1,_protocol=2,_comparison=[args.baseline,args.candidate],_side=side,
                _extension_of=parent_id,repeat=1)
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',
                (name,'queued',now,now,json.dumps(spec),parent['bundle'],parent['parallelism'],0,None))
            db.executemany('INSERT INTO items(job_id,ordinal,state,settings) VALUES(?,?,?,?)',
                [(name,i,'pending',json.dumps(row)) for i,row in enumerate(rowsets[side])])
            jobs.event(db,name,'Supplemental paired repeats from original frozen payload; all previous scores retained')
            queued.append(name)
    return dict(queued_jobs=queued,items_per_side=len(rowsets['baseline']))


def totals(rows):
    shots=sum(r['shots'] for r in rows);duration=sum(r['duration'] for r in rows)
    hits=sum(r['hits'] for r in rows)
    return dict(shots=shots,hit_rate=hits/shots if shots else 0,
        weighted_hit_rate=sum(r['weight'] for r in rows)/shots if shots else 0,hits_per_s=hits/duration if duration else 0)


def paired_result(left,right):
    pairs=sorted(set(left)&set(right));a=[left[k] for k in pairs];b=[right[k] for k in pairs]
    base,candidate=totals(a),totals(b)
    result=dict(pairs=len(pairs),baseline=base,candidate=candidate,paired_run_ids=[[left[k]['run_id'],right[k]['run_id']] for k in pairs],
        unpaired_baseline=[left[k]['run_id'] for k in sorted(set(left)-set(right))],
        unpaired_candidate=[right[k]['run_id'] for k in sorted(set(right)-set(left))],
        conclusion='insufficient_samples',additional_pairs=max(0,5-len(pairs)),maximum_pairs=10)
    if len(pairs)<5 or base['shots']<30 or candidate['shots']<30:
        if len(pairs)>=5 and base['shots']>=30 and candidate['shots']==0:result['conclusion']='regressed_no_fire'
        result['additional_pairs']=min(max(0,10-len(pairs)),max(1,result['additional_pairs']))
        return result
    rng=random.Random(20261022);samples=defaultdict(list)
    for _ in range(5000):
        indices=[rng.randrange(len(pairs)) for _ in pairs]
        x,y=totals([a[i] for i in indices]),totals([b[i] for i in indices])
        samples['hit_rate_delta'].append(y['hit_rate']-x['hit_rate'])
        samples['weighted_hit_rate_delta'].append(y['weighted_hit_rate']-x['weighted_hit_rate'])
        samples['hits_per_s_margin'].append(y['hits_per_s']-.95*x['hits_per_s'])
    intervals={k:[sorted(v)[124],sorted(v)[4874]] for k,v in samples.items()}
    thresholds=dict(hit_rate_delta=-.05,weighted_hit_rate_delta=-.05,hits_per_s_margin=0)
    result.update(intervals_95=intervals,thresholds=thresholds,bootstrap=dict(unit='paired repeat',resamples=5000,seed=20261022))
    if all(intervals[k][0]>=limit for k,limit in thresholds.items()):result['conclusion']='passed'
    elif any(intervals[k][1]<limit for k,limit in thresholds.items()):result['conclusion']='regressed'
    else:result['conclusion']='insufficient_samples'
    result['additional_pairs']=min(1,max(0,10-len(pairs))) if result['conclusion']=='insufficient_samples' else 0
    return result


def compare(args):
    import background_jobs as jobs
    with jobs.connect() as db:
        extensions,active=extension_jobs(db,args.baseline,args.candidate)
        a,excluded_a=collect(db,args.baseline,extensions['baseline']);b,excluded_b=collect(db,args.candidate,extensions['candidate'])
        if not a or not b:raise ValueError('Both tasks need completed, healthy, scored experiments')
        if set(a)!=set(b):raise ValueError('Scene/execution/scorer conditions differ; comparison refused')
        results=[dict(condition=json.loads(k),**paired_result(a[k],b[k])) for k in sorted(a)]
        report=dict(version=1,baseline=args.baseline,candidate=args.candidate,conditions=results,
            supplemental_tasks=extensions,active_supplemental_tasks=active,
            frozen_payloads={side:dict(bundle=jobs.get_job(db,jid)['bundle'],
                metadata_sha256=hashlib.sha256((Path(jobs.get_job(db,jid)['bundle'])/'bundle.json').read_bytes()).hexdigest())
                for side,jid in (('baseline',args.baseline),('candidate',args.candidate))},
            excluded=dict(baseline=excluded_a,candidate=excluded_b),
            conclusion='regressed' if any(r['conclusion'].startswith('regressed') for r in results) else
                'passed' if all(r['conclusion']=='passed' for r in results) else 'insufficient_samples',
            visual_status='not_reviewed',note='Statistics do not grant visual approval; retain all measured low scores.')
        if args.extend:report['extension']=extend(db,args,results,a,b)
        key=hashlib.sha256((args.baseline+':'+args.candidate).encode()).hexdigest()[:12]
        folder=jobs.OPS/'experiments'/('compare_'+key);folder.mkdir(exist_ok=True)
        write(folder/'comparison.json',report)
        (folder/'report.html').write_text('<!doctype html><meta charset="utf-8"><h1>配对实验比较</h1><pre>'+html.escape(json.dumps(report,ensure_ascii=False,indent=2))+'</pre>')
        import experiments
        experiments.publish_desktop(jobs.manage,folder)
        print(json.dumps(dict(report=str(folder/'report.html'),**report),ensure_ascii=False,indent=2))
        return report
