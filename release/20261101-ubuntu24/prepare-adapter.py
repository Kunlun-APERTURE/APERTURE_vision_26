"""从仓库根目录执行，冻结独立候选的算法配置和构建身份。"""
import json
from pathlib import Path
import shutil
import subprocess
import sys

root = Path.cwd()
sys.path.insert(0, str(root / 'docker/auto_aim'))
from benchmark.algorithms import R4Adapter

output = Path(sys.argv[1]).resolve()
artifact = output / 'build/sim_auto_aim'
descriptor = R4Adapter().describe(root, artifact)
image = 'rm26-validation:ubuntu24-openvino2024.6'
descriptor.update(id='B1-ubuntu24-validation', image=image, config='B1.yaml',
                  build_image=json.loads(subprocess.check_output(['docker', 'image', 'inspect', image]))[0]['Id'])
shutil.copy2(root / 'docker/auto_aim/benchmark/configs/B1.yaml', output / 'B1.yaml')
for name in ('adapter.json', 'build-provenance.json'):
    (output / name).write_text(json.dumps(descriptor, indent=2) + '\n')
print(output / 'adapter.json')
