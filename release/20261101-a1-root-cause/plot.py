import json
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
p=Path('/project/exports/a1-root-cause-20261101')
a=json.loads((p/'analysis.json').read_text());r=next(r for r in a['rows'] if r['seq']==68)
b=next(json.loads(l) for l in (p/'observations-baseline.jsonl').read_text().splitlines() if json.loads(l)['seq']==68)
pose=b['continuity']['diagnosis_poses'][0];v=np.array(pose['candidates']);yaw=np.degrees(v[:,0]);dist=np.linalg.norm(v[:,3:6]-r['predicted_xyz'],axis=1)
fig,axes=plt.subplots(2,1,figsize=(9,7),sharex=True)
axes[0].plot(yaw,v[:,1],label='Image candidate cost');axes[0].set_ylabel('Candidate cost');axes[0].set_ylim(0,3)
axes[1].plot(yaw,dist,label='Distance to predicted plate');axes[1].axhline(.8,color='black',ls='--',label='0.8 m gate');axes[1].set_ylabel('Position residual (m)');axes[1].set_xlabel('Candidate inward yaw (degrees)')
for ax in axes:
 ax.axvline(np.degrees(r['hint']),color='gray',ls=':',label='Previous yaw hint')
 ax.axvline(np.degrees(r['chosen_yaw']),color='red',label='Selected candidate')
 ax.axvline(np.degrees(r['image_minimum_yaw']),color='green',label='Image minimum')
 ax.grid(alpha=.2);ax.set_xlim(-30,65);ax.legend(fontsize=8)
fig.suptitle('A1: seq 68, same observation and prior state\nSelected residual 1.195 m; image-minimum residual 0.644 m')
fig.tight_layout();fig.savefig(p/'seq68-candidate-curve.png',dpi=160)
