#!/usr/bin/env python3
"""Export standalone scientific figures from paired robustness evidence."""
import argparse,json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np


def main():
    p=argparse.ArgumentParser();p.add_argument('--study',type=Path,required=True);a=p.parse_args();data=json.loads((a.study/'result-summary.json').read_text())
    validation=[r for r in data['comparisons'] if 'validation' in r['batch'] and r['profile']=='tracking_fire_v3' and r['baseline']]
    if not validation:validation=[r for r in data['comparisons'] if r['profile']=='tracking_fire_v3' and r['baseline'] and r['control_source']=='algorithm']
    fig,axes=plt.subplots(3,1,figsize=(15,max(9,len(validation)*.23)),constrained_layout=True)
    x=np.arange(len(validation));labels=[r['scene']+'/'+r['physics'] for r in validation]
    for ax,key,title in [(axes[0],'useful_hits_per_s','Useful hits per second (full measurement interval)'),(axes[1],'coverage','Strict tracking coverage'),(axes[2],'first_hit_censor_fraction','First-hit events censored at next change')]:
        ax.bar(x-.18,[r['baseline'][key] for r in validation],.36,label='v2');ax.bar(x+.18,[r['candidate'][key] for r in validation],.36,label='v3')
        ax.set_title(title);ax.grid(axis='y',alpha=.2);ax.set_xticks(x,labels,rotation=75,ha='right',fontsize=7);ax.legend()
    for suffix in ('png','svg'):fig.savefig(a.study/('robustness-comparison.'+suffix),dpi=180)
    plt.close(fig)
    for scene in ('random_both_05','random_both_10','random_both_20','distance_3','distance_5','distance_8'):
        # Keep each frozen batch and physics separate; only independent validation is used if present.
        selected=[r for r in data['runs'] if r['scene']==scene and r['control_source']=='algorithm' and ('validation' in r['study_batch'] if any('validation' in z['study_batch'] for z in data['runs']) else True)]
        if not selected:continue
        fig,ax=plt.subplots(figsize=(8,4),constrained_layout=True)
        groups={}
        for r in selected:groups.setdefault((r['study_batch'],r['profile'],r['physics']),[]).append(r)
        for label,rows in groups.items():
            ax.scatter([r.get('target_speed_p95_mps') or 0 for r in rows],[r['useful_hits_per_s'] for r in rows],label='/'.join(label),alpha=.8)
        ax.set_xlabel('Actual target speed P95 (m/s)');ax.set_ylabel('Useful hits / s');ax.set_title(scene);ax.grid(alpha=.2);ax.legend(fontsize=6)
        fig.savefig(a.study/(scene+'-actual-speed.png'),dpi=160);plt.close(fig)
    print(json.dumps({'comparisons':len(validation),'figures':[p.name for p in a.study.glob('robustness-comparison.*')]}))

if __name__=='__main__':main()
