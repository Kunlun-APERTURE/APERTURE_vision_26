"""Adapters describe independent processes; no simulator or target truth API."""
import json
import subprocess
from pathlib import Path
from .core import AlgorithmAdapter, sha, identity

CONFIG=Path(__file__).parent/'configs/B1.yaml'


class R4Adapter(AlgorithmAdapter):
    id='B1'

    def build(self,root,output):
        output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
        command='''set -e
source /opt/intel/openvino/setupvars.sh >/dev/null
cmake -S /source -B /work/build -DBUILD_SIMULATOR_ADAPTER=ON -DCMAKE_BUILD_TYPE=Release -DOpenCV_DIR=/usr/lib/x86_64-linux-gnu/cmake/opencv4
cmake --build /work/build --target sim_auto_aim -j4
'''
        import os
        subprocess.run(['docker','run','--rm','--user',f'{os.getuid()}:{os.getgid()}',
            '-v',str(root/'sp_vision_25')+':/source:ro','-v',str(output)+':/work',
            '--entrypoint','bash','sp_vision_dev:2024.6.0','-c',command],check=True)
        artifact=output/'build/sim_auto_aim'
        descriptor=self.describe(root,artifact)
        (output/'build-provenance.json').write_text(json.dumps(descriptor,indent=2))
        return artifact

    def describe(self,root,artifact):
        artifact=Path(artifact).resolve()
        if not artifact.is_file():raise ValueError('Build B1 first: experiment algorithm build --algorithm B1')
        head=subprocess.check_output(['git','-C',str(root/'sp_vision_25'),'rev-parse','HEAD'],text=True).strip()
        # Bind executable to the full source tree and dependency image, not only HEAD.
        paths=subprocess.check_output(['git','-C',str(root/'sp_vision_25'),'ls-files','-z']).decode().split('\0')
        files={p:sha(root/'sp_vision_25'/p) for p in paths if p and (root/'sp_vision_25'/p).is_file() and not p.startswith('assets/')}
        image=json.loads(subprocess.check_output(['docker','image','inspect','sp_vision_dev:2024.6.0'],text=True))[0]['Id']
        return dict(id=self.id,version=1,adapter='r4',source_commit=head,source_tree_sha256=identity(files),build_image=image,binary_sha256=sha(artifact),
            config_sha256=sha(CONFIG),protocol='rm26-image-v1',algorithm_profile='tracking_fire_v3',
            capabilities=['simulation_clock','session_reset','fire_advice','r4_diagnostics'],
            launch=['/snapshot/sp_vision_25/build/simulator/sim_auto_aim',
                '--config=/snapshot/sp_vision_25/configs/simulator.yaml','--runtime=/work/calibration.yaml',
                '--view=false','--port={port}','--fire={fire}','--aim-origin={aim_origin}',
                '--algorithm-profile=tracking_fire_v3','--execution-mode={execution_mode}','--detector-history=session'])


class ProcessAdapter(AlgorithmAdapter):
    """Team adapter: explicit artifact, image and argv; no shell interpolation."""
    def __init__(self,manifest):
        self.manifest=Path(manifest).resolve();self.data=json.loads(self.manifest.read_text())
        if not isinstance(self.data.get('id'),str) or not self.data['id']:raise ValueError('Algorithm id required')
        if self.data.get('protocol')!='rm26-image-v1':raise ValueError('Unsupported algorithm protocol')
        if not isinstance(self.data.get('launch'),list) or not self.data['launch'] or not all(isinstance(v,str) for v in self.data['launch']):raise ValueError('launch argv required')
        if not {'simulation_clock','session_reset','fire_advice'}<=set(self.data.get('capabilities',[])):
            raise ValueError('Synchronous benchmark capabilities required')

    def build(self,root,output):
        raise ValueError('Build independent algorithm externally and provide its artifact manifest')

    def describe(self,root,artifact):
        data=dict(self.data)
        if sha(artifact)!=data['binary_sha256']:raise ValueError('Algorithm checksum mismatch')
        return data


def get(name):
    if name=='B1':return R4Adapter()
    return ProcessAdapter(name)
