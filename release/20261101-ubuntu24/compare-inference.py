import json
import numpy as np
from pathlib import Path

left=np.load('/work/ubuntu22-inference.npz')
right=np.load('/work/ubuntu24-inference.npz')
rows=[]
for name in left.files:
    a,b=left[name],right[name]
    if a.shape!=b.shape:
        raise RuntimeError('模型输出形状不同: '+name)
    row=dict(output=name,shape=list(a.shape),exact=bool(np.array_equal(a,b)),
             max_absolute_error=float(np.max(np.abs(a-b))),
             close=bool(np.allclose(a,b,rtol=1e-5,atol=1e-6)))
    rows.append(row)
    if not row['close']:
        raise RuntimeError('模型数值误差超出预定阈值: '+name)
Path('/work/inference-comparison.json').write_text(json.dumps(rows,indent=2)+'\n')
print(json.dumps(rows,indent=2))
