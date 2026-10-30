"""Conditional realtime confirmation; never imported before backend acceptance."""
import argparse
import fcntl
from pathlib import Path
import random
import subprocess

import experiments
import manage
from perception_study import SCENES,effect,quantile,trial_metrics
from decision_benchmark_report import aggregate
from report import load,write
from sync_contract_study import settings
from sync_history_study import matched_ui


def run_ui(root,state,c,selected):
    if not load(root/'confirm-result.json')['passed'] or not load(root/'repeatability.json')['passed']:
        raise ValueError('UI acquisition requires both independent backend benefit and repeatability')
    if state.get('current_ui_case'):
        raise ValueError('Interrupted UI attempt retained; inspect it before resuming')
    state['ui_scheduled']=True;write(root/'state.json',state)
    subprocess.run(['python3',str(manage.ROOT/'docker/daedalus/display_auth.py')],check=True)
    with (manage.RUNTIME/'operation.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            for i,seed in enumerate(c['confirm_seeds']):
                for scene in SCENES if i%2==0 else SCENES[::-1]:
                    for variant in (['baseline',selected] if i%2==0 else [selected,'baseline']):
                        if any(r['phase']=='ui' and (r['variant'],r['scene'],r['sensor_seed'])==(variant,scene,seed) for r in state['runs']):continue
                        with matched_ui(Path(state['bundles'][variant])):
                            try:
                                manage.stop()
                                manage.start(argparse.Namespace(**dict(settings('static',seed),control='off',view='off',replace=False,fire='auto')))
                                current=dict(phase='ui',variant=variant,scene=scene,sensor_seed=seed,repeat_id=i,engine='ui')
                                state['current_ui_case']=current;write(root/'state.json',state)
                                args=argparse.Namespace(**dict(settings(scene,seed),repeat_id=i,label='perception-ui-'+variant))
                                folder=experiments.run_one(manage,scene,args)
                                entry=dict(current,run_id=folder.name);state['runs'].append(entry)
                                state.pop('current_ui_case',None);write(root/'state.json',state)
                                if trial_metrics(root,entry,c)['wall_s']>40:raise ValueError('UI trial exceeded budget')
                            finally:manage.stop()
        finally:manage.save_profile(c['original_profile'])
    rows=[trial_metrics(root,r,c) for r in state['runs'] if r['phase']=='ui']
    result=effect(rows,selected,c,True)
    backend=load(root/'confirm-result.json')
    result['backend_weighted_gain']=backend['groups']['all']['candidate']['weighted_hit_rate']-backend['groups']['all']['baseline']['weighted_hit_rate']
    result['ui_weighted_gain']=result['groups']['all']['candidate']['weighted_hit_rate']-result['groups']['all']['baseline']['weighted_hit_rate']
    result['effect_difference']=result['ui_weighted_gain']-result['backend_weighted_gain']
    backend_rows=[trial_metrics(root,r,c) for r in state['runs'] if r['phase']=='confirm']
    rng=random.Random(c['bootstrap']['random_seed']);samples=[]
    for _ in range(c['bootstrap']['samples']):
        seeds=rng.choices(c['confirm_seeds'],k=len(c['confirm_seeds']));gains=[]
        for data in (rows,backend_rows):
            values={v:aggregate([r for seed in seeds for r in data if r['sensor_seed']==seed and r['variant']==v])
                    for v in ('baseline',selected)}
            gains.append(values[selected]['weighted_hit_rate']-values['baseline']['weighted_hit_rate'])
        samples.append(gains[0]-gains[1])
    result['effect_difference_95']=[quantile(samples,.025),quantile(samples,.975)]
    write(root/'ui-result.json',result)
