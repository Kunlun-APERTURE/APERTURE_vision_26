#!/usr/bin/env python3
"""Build a local WSL graphics overlay; leave the registry-pinned image intact."""
import json
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT/'docker/auto_aim'
WORK = OPS/'.runtime/wsl-gpu-build'
TAG = 'aperture_vision:wsl-gpu'
LIBRARY_DIRS = ('/usr/lib', '/usr/lib/x86_64-linux-gnu', '/lib/x86_64-linux-gnu')
sys.path.insert(0,str(OPS))


def host_library(name):
    for directory in LIBRARY_DIRS:
        path = Path(directory)/name
        if path.exists():
            return path
    raise SystemExit('Missing host library for the WSL GPU overlay: ' + name)


def require_wgpu_env_support():
    source = ROOT/'vision_simulator_26/src/main.rs'
    if not source.exists() or 'settings_priority_from_env' not in source.read_text():
        raise SystemExit(
            'vision_simulator_26 is missing the WGPU_SETTINGS_PRIO compatibility patch; '
            'merge the simulator change and update the submodule before building the GPU overlay')


def main():
    from dependency_image import unified_image
    from experiments import repository
    import manage
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--activate-report',type=Path,
        help='Activate an already validated GPU runtime using this suite output')
    args=parser.parse_args()
    base=manage.inspect(unified_image())
    if not base or base['Config'].get('Labels',{}).get('aperture.unified')!='1':
        raise SystemExit('Pull the pinned unified SDK first')
    if args.activate_report:
        from benchmark.artifacts import verify
        from wsl_gpu import STATE,IMPLEMENTATION,compatible_runtime
        output=args.activate_report
        result=json.loads((output/'results.json').read_text())
        contract=json.loads((output/'contract.json').read_text())
        metadata=verify(Path(contract['bundle']),contract.get('payload_identity'))
        if (not result.get('complete') or not result.get('all_evidence_valid')
                or metadata.get('renderer',{}).get('implementation')!=IMPLEMENTATION
                or not any(group.get('shots',0)>0 for group in result.get('groups',[]))):
            raise SystemExit('Activation requires a completed, valid NVIDIA suite with shots')
        image=metadata['images']['simulator']
        os.environ['RM26_WSL_GPU_IMAGE']=image
        compatible_runtime(base,'nvidia',manage.inspect)
        from render_backend import select
        if select('auto',image)['backend']!='nvidia':
            raise SystemExit('Hardware Vulkan probe failed; GPU runtime was not activated')
        STATE.write_text(json.dumps(dict(base_image=base['Id'],image=image,
            validated_report=str(output.resolve())),indent=2)+'\n')
        print('Activated '+image)
        return
    require_wgpu_env_support()
    if not Path('/usr/lib/wsl/lib/libd3d12.so').exists():
        raise SystemExit('WSL D3D12 provider unavailable')
    for folder in ('cargo','target','out','home','drivers','provenance'):
        (WORK/folder).mkdir(parents=True,exist_ok=True)
    # Keep every downloaded dependency inside the workspace, including cache misses.
    registry=Path.home()/'.cargo/registry'
    for folder in ('cache','index'):
        destination=WORK/'cargo/registry'/folder
        if not destination.exists() and (registry/folder).exists():
            shutil.copytree(registry/folder,destination)
    build=WORK/'build.sh'
    build.write_text('''#!/bin/bash
set -eo pipefail
export CARGO_HOME=/cargo CARGO_TARGET_DIR=/target CARGO_BUILD_JOBS=4
export IDL_PACKAGE_FILTER='geometry_msgs;sensor_msgs;std_msgs;visualization_msgs;tf2_msgs;rm_interfaces;rcl_interfaces;builtin_interfaces;action_msgs;unique_identifier_msgs'
export CMAKE_IDL_PACKAGES="$(python3 /source/docker/ros_idl_packages.py)"
cd /source
cargo build --release --locked --features bench --bin daedalus
mkdir -p /out/lib
cp /target/release/daedalus /out/daedalus
cp /target/release/deps/libbevy_dylib-*.so /out/lib/
cp "$(rustc --print sysroot)"/lib/rustlib/x86_64-unknown-linux-gnu/lib/libstd-*.so /out/lib/
test "$(stat -c %s /out/daedalus)" -gt 5000000
LD_LIBRARY_PATH="/out/lib:${LD_LIBRARY_PATH:-}" ldd /out/daedalus > /out/ldd.txt
if grep -q 'not found' /out/ldd.txt; then cat /out/ldd.txt; exit 1; fi
''')
    command=['docker','run','--rm','--user',f'{os.getuid()}:{os.getgid()}',
        '--env','HOME=/tmp/aperture-build-home']
    for source,target,readonly in (
            (ROOT/'vision_simulator_26','/source',True),
            (WORK/'cargo','/cargo',False),(WORK/'target','/target',False),
            (WORK/'out','/out',False),(WORK/'home','/tmp/aperture-build-home',False),
            (build,'/build-gpu.sh',True)):
        command+=['--volume',str(source)+':'+target+(':ro' if readonly else '')]
    subprocess.run(command+[base['Id'],'bash','/build-gpu.sh'],check=True)
    # Mesa's Dozen driver must use matching graphics helper libraries, while
    # libc, libstdc++, ROS and OpenVINO continue to come from the SDK.
    libraries=['libvulkan_dzn.so','libdisplay-info.so.3','libSPIRV-Tools.so',
        'libwayland-client.so.0','libxcb.so.1','libxcb-keysyms.so.1',
        'libxcb-dri3.so.0','libxcb-present.so.0','libxcb-randr.so.0',
        'libxcb-shm.so.0','libxcb-sync.so.1','libxcb-xfixes.so.0']
    for name in libraries:
        target=WORK/'drivers'/name
        if target.exists():target.chmod(0o644)
        shutil.copyfile(host_library(name),target)
    icd_path=Path('/usr/share/vulkan/icd.d/dzn_icd.json')
    if not icd_path.exists():
        raise SystemExit('Mesa Dozen ICD not found: ' + str(icd_path))
    icd=json.loads(icd_path.read_text())
    icd['ICD']['library_path']='/opt/aperture-wsl-vulkan/libvulkan_dzn.so'
    (WORK/'drivers/dzn_icd.json').write_text(json.dumps(icd,indent=2)+'\n')
    source=repository(manage,'vision_simulator_26',WORK/'provenance')
    (WORK/'.dockerignore').write_text('*\n!out/\n!out/**\n!drivers/\n!drivers/**\n')
    subprocess.run(['docker','build','--file',str(OPS/'WslGpu.Dockerfile'),
        '--build-arg','BASE_TAG='+unified_image(),
        '--build-arg','BASE_IMAGE='+base['Id'],
        '--build-arg','SOURCE_SHA256='+source['source_files_sha256'],
        '--tag',TAG,str(WORK)],check=True)
    print('Built '+TAG+'; validate with RM26_WSL_GPU_IMAGE='+TAG+' and --renderer nvidia')


if __name__=='__main__':
    main()
