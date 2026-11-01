#!/usr/bin/env python3
"""Ubuntu development entry points, runnable from any directory."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
root=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('action',choices=('doctor','build','test','example'));a=p.parse_args()
if a.action=='test':subprocess.run([sys.executable,str(root/'tools/test-development.py')],check=True)
elif a.action=='build':subprocess.run([str(root/'sim.sh'),'experiment','algorithm','build'],cwd=root,check=True)
elif a.action=='example':
 sys.path.insert(0,str(root/'docker/auto_aim'))
 from benchmark.core import sha
 from dependency_image import unified_image
 script=root/'docker/auto_aim/benchmark/examples/idle_algorithm.py'
 descriptor=dict(id='idle-example',version=1,protocol='rm26-image-v1',algorithm_profile='image_v1',setup='',image='daedalus:rm26-algorithm',config='B1.yaml',config_sha256=sha(root/'docker/auto_aim/benchmark/configs/B1.yaml'),binary_sha256=sha(script),capabilities=['simulation_clock','session_reset','fire_advice'],launch=['python3','/snapshot/sp_vision_25/build/simulator/sim_auto_aim','--port={port}'])
 if unified_image():descriptor.update(image=unified_image(),runtime_image=unified_image())
 output=root/'exports/idle-example';output.mkdir(parents=True,exist_ok=True)
 import shutil
 shutil.copy2(root/'docker/auto_aim/benchmark/configs/B1.yaml',output/'B1.yaml');(output/'adapter.json').write_text(json.dumps(descriptor,indent=2))
 print('sim.sh experiment suite --algorithm exports/idle-example/adapter.json --artifact docker/auto_aim/benchmark/examples/idle_algorithm.py --cases example_static --sensor-seeds 1 --output exports/example-run --wait')
else:
 for cmd in (['git','submodule','status'],['docker','version','--format','{{.Server.Version}}']):
  r=subprocess.run(cmd,cwd=root,check=False)
  if r.returncode:raise SystemExit(r.returncode)
 sys.path.insert(0,str(root/'docker/auto_aim'))
 from render_backend import select
 print('默认渲染环境：'+json.dumps(select(),ensure_ascii=False))
