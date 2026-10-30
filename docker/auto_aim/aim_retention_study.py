"""Physical-plate retention ablation with native/BS sensing at measured delay."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import shutil
import time

import background_jobs as jobs
import manage
from decision_benchmark import sha,verify_bundle
from decision_benchmark_report import aggregate,behavior
from maneuver_fire_study import compare,regression_gate
from motion_study import submit_regression
from perception_repair import parallel_probe,batch,collect
from perception_study import protected
from report import load,write
from sync_first_divergence import run_one
from sync_first_divergence_analysis import primary
from maneuver_study import CORE_FIELDS

ROOT=manage.OPS/'experiments/aim-retention-20261027'
PARENT=manage.OPS/'experiments/timing-factor-20261026'
VARIANTS=('baseline','BS4','BSL4','N4','NL4')
CANDIDATES=('BSL4','N4','NL4')
OPTIONS=dict(baseline=dict(bs=False,delay=False,retain=False),BS4=dict(bs=True,delay=True,retain=False),
 BSL4=dict(bs=True,delay=True,retain=True),N4=dict(bs=False,delay=True,retain=False),NL4=dict(bs=False,delay=True,retain=True))
FILES=('tasks/auto_aim/aim_retention.hpp','tasks/auto_aim/motion_model.hpp','tasks/auto_aim/target.hpp',
 'tasks/auto_aim/aimer.hpp','tasks/auto_aim/aimer.cpp','tests/maneuver_filter_test.cpp')


def initialize(root):
    root.mkdir(parents=True,exist_ok=True)
    if (root/'contract.json').exists():
        c=load(root/'contract.json');s=load(root/'state.json')
        assert sha(root/'contract.json')==s['contract_sha256'] and protected()==c['protected']
        for b in c['bundles'].values():verify_bundle(Path(b))
        return c
    pc=load(PARENT/'contract.json');ps=load(PARENT/'state.json');assert ps['state']=='completed' and protected()==pc['protected']
    binary=root/'build-source/sp_vision_25/build/motion/sim_auto_aim'
    for name in ('maneuver','motion','perception'):assert 'PASSED' in (root/(name+'-checks.log')).read_text()
    seeds={3791,*range(3801,3806),*range(3901,3904),*range(4001,4011)}
    with jobs.connect() as db:used={json.loads(x[0]).get('sensor_seed') for x in db.execute('SELECT settings FROM items')}
    assert not seeds & used
    sources=[]
    for e in pc['replay_sources']:
        refs={v:next(x for x in ps['replays'] if x['source_run']==e['run_id'] and x['variant']==v) for v in ('baseline','BS4')}
        sources.append(dict(e,retention_references=refs))
    history={}
    for name,h in load(PARENT/'report-seal.json').items():assert sha(PARENT/name)==h;history[str(PARENT/name)]=h
    history[str(PARENT/'report-seal.json')]=sha(PARENT/'report-seal.json')
    c=dict(version=1,created=time.time(),parent=str(PARENT),protected=pc['protected'],variants=VARIANTS,options=OPTIONS,
      bundles={},sources=sources,maximum_replays=10,candidate_binary_sha256=sha(binary),
      source_sha256={p:sha(root/'build-source/sp_vision_25'/p) for p in FILES},
      runtime_files=pc['runtime_files'],backend_timing_profile=pc['backend_timing_profile'],short_delay_s=pc['short_delay_s'],
      probe_seed=3791,screen_seeds=list(range(3801,3806)),regression_seeds=list(range(3901,3904)),confirm_seeds=list(range(4001,4011)),
      regression_scenes=pc['regression_scenes'],effect_gate=pc['effect_gate'],bootstrap=pc['bootstrap'],
      maximum_trials=dict(probe=20,screen=50,regression=60,confirm=60,repeat=6,ui=0),
      objective_gain=dict(main_weighted_gain_pp=5,paired_holdout_lower_above_zero=True,scene_and_regression_no_material_loss=True),
      design='Isolate retention versus sensing: native v3/BS x measured short delay, retention on/off; unchanged v3 reference. '
        'Retention keeps a freshly measured physical plate inside the existing 60-degree facing envelope. No smoothing, new fire tolerance or scene/GT input.',
      selection='Candidate must pass existing per-scene exact/weighted/hits/s/latency gates, overall +5pp weighted vs v3, '
        'noninferiority vs BS4 at -2pp, and all four regression scene gates. Select highest main weighted score; '
        'then holdout vs v3 and BS4, requiring >0 paired lower bound vs v3 and unchanged noninferiority constraints vs BS4.',
      historical_files=history,automatic_promotion=False,capacity_registration=False,maximum_complete_trial_s=40)
    for v,opt in OPTIONS.items():
        base=Path(pc['bundles']['BS4' if opt['bs'] else 'baseline'])
        dst=jobs.STATE/'bundles'/('retention_'+v+'_'+sha(binary)[:12]);assert not dst.exists()
        shutil.copytree(base,dst,copy_function=os.link)
        for f in FILES:
            out=dst/'sp_vision_25'/f;out.parent.mkdir(parents=True,exist_ok=True);out.unlink(missing_ok=True)
            shutil.copy2(root/'build-source/sp_vision_25'/f,out)
        out=dst/'sp_vision_25/build/simulator/sim_auto_aim';out.unlink();shutil.copy2(binary,out)
        cfg=dst/'sp_vision_25/configs/simulator.yaml';text=cfg.read_text();cfg.unlink()
        if opt['delay'] and not opt['bs']:text+='\nmotion_v5:\n  calibrated_delay: true\n  launch_delay_s: '+repr(c['short_delay_s'])+'\n'
        if opt['retain']:text+='\naim_retention:\n  enabled: true\n'
        cfg.write_text(text)
        meta=load(base/'bundle.json');meta.update(bundle_id=dst.name,created=time.time(),parent_bundle=str(base),retention_options=opt)
        for f in (*FILES,'build/simulator/sim_auto_aim','configs/simulator.yaml'):meta['files']['sp_vision_25/'+f]=sha(dst/'sp_vision_25'/f)
        (dst/'bundle.json').unlink();write(dst/'bundle.json',meta);verify_bundle(dst);c['bundles'][v]=str(dst)
    write(root/'contract.json',c);write(root/'state.json',dict(state='frozen',contract_sha256=sha(root/'contract.json'),replays=[],runs=[],jobs=[],screen_variants=list(VARIANTS),parallelism=1))
    return c


def replays(root,c,s):
    def one(e,v):
        source=manage.OPS/'experiments'/e['run_id'];out=root/'replays'/source.name/v
        result=load(out/'result.json') if (out/'result.json').exists() else run_one(source,out,Path(c['bundles'][v]),seconds=e['duration_s'],detector_history='session',diagnosis=True)
        ref=e['retention_references']['BS4' if OPTIONS[v]['bs'] else 'baseline'];original=primary(Path(ref['output']));current=primary(out)
        assert result['complete'] and len(original)==len(current) and result['primary_input_sha256']==ref['input_sha256']
        assert sha(Path(ref['output'])/'events.jsonl')==ref['output_sha256']
        fields=CORE_FIELDS if v in ('baseline','BS4') else ('observations','filter_state','tracker','plate_velocities')
        different={k:0 for k in fields};active=0
        for a,b in zip(original,current):
            assert a['frame']==b['frame'] and a['jpeg_sha256']==b['jpeg_sha256']
            for k in fields:different[k]+=a['result'].get(k)!=b['result'].get(k)
            d=(b['result'].get('diagnosis') or {}).get('aimer') or {}
            active+=bool(d.get('aim_retention'))
        assert not any(different.values()),(v,different)
        if OPTIONS[v]['retain']:assert active>30
        return dict(variant=v,source_run=source.name,output=str(out),frames=len(current),differences=different,
          activation_frames=active,input_sha256=result['primary_input_sha256'],output_sha256=sha(out/'events.jsonl'))
    pending=[(e,v) for e in c['sources'] for v in VARIANTS if not any(x['source_run']==e['run_id'] and x['variant']==v for x in s['replays'])]
    with ThreadPoolExecutor(max_workers=2) as pool:
        for r in pool.map(lambda p:one(*p),pending):
            s['replays'].append(r);write(root/'state.json',s);print(json.dumps(dict(stage='replay',done=len(s['replays']),variant=r['variant'])),flush=True)


def evaluate(rows,v,c,reference='baseline',independent=False):
    r=compare(rows,v,c,reference,independent and reference=='baseline')
    if reference=='baseline':
        a,b=r['groups']['all']['baseline'],r['groups']['all']['candidate']
        r['checks']['all_weighted']=b['weighted_hit_rate'] is not None and a['weighted_hit_rate'] is not None and b['weighted_hit_rate']>=a['weighted_hit_rate']+.05
    else:
        a,b=r['groups']['all']['baseline'],r['groups']['all']['candidate']
        r['checks']['all_weighted']=b['weighted_hit_rate'] is not None and a['weighted_hit_rate'] is not None and b['weighted_hit_rate']>=a['weighted_hit_rate']-.02
    r['passed']=all(r['checks'].values());return r


def run(root):
    c=initialize(root)
    with (root/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);s=load(root/'state.json')
        if s['state']=='completed':return
        try:
            s['state']='running';s.pop('error',None);write(root/'state.json',s);replays(root,c,s)
            if not (root/'parallel-result.json').exists():parallel_probe(root,c,s)
            batch(root,c,s,'screen',list(VARIANTS),c['screen_seeds'])
            entries=[submit_regression(root,c,s,v,sc,seed,seed-c['regression_seeds'][0]) for sc in c['regression_scenes'] for seed in c['regression_seeds'] for v in VARIANTS]
            collect(root,c,s,entries)
            rows=[r for r in s['runs'] if r['phase']=='screen'];reg=[r for r in s['runs'] if r['phase']=='regression']
            effects=[evaluate(rows,v,c,ref) for v in CANDIDATES for ref in ('baseline','BS4')]
            regressions=[regression_gate(reg,v) for v in CANDIDATES]
            write(root/'screen-result.json',effects);write(root/'regression-result.json',regressions)
            eligible=[v for v in CANDIDATES if all(r['passed'] for r in effects if r['variant']==v) and next(r['passed'] for r in regressions if r['variant']==v)]
            selected=max(eligible,key=lambda v:aggregate([r for r in rows if r['variant']==v])['weighted_hit_rate']) if eligible else None
            s['selected']=selected;write(root/'state.json',s)
            if selected:
                batch(root,c,s,'confirm',['baseline','BS4',selected],c['confirm_seeds'])
                confirmation=[evaluate([r for r in s['runs'] if r['phase']=='confirm'],selected,c,ref,True) for ref in ('baseline','BS4')]
                write(root/'confirm-result.json',confirmation);s['backend_confirmed']=all(r['passed'] for r in confirmation)
                if s['backend_confirmed']:batch(root,c,s,'repeat',['baseline','BS4',selected],[c['confirm_seeds'][0]])
            s.update(state='acquisition_complete',ended=time.time());write(root/'state.json',s)
            from aim_retention_report import finish
            finish(root)
        except BaseException as error:
            s.update(state='failed',error=repr(error));write(root/'state.json',s);raise


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('freeze','run'));parser.add_argument('--output',type=Path,default=ROOT)
    args=parser.parse_args();root=args.output.resolve()
    if args.action=='freeze':initialize(root)
    else:run(root)
