"""Optional live R4 feature checks using an explicitly supplied payload."""
import argparse
import contextlib
import fcntl
import os
from pathlib import Path
import shutil
import time
from types import SimpleNamespace
import subprocess
import manage
import experiments
from report import load,write
from .artifacts import verify
from .core import EnvironmentSpec

@contextlib.contextmanager
def matched_ui(bundle):
    """Use the SAME candidate simulator/model/binary as the backend diagnostic."""
    meta=verify(bundle)
    relative='.local/share/rm26-sim/detector-history-reference/'+bundle.name+'/sp_vision_25'
    reference=manage.DESKTOP/relative
    reference.mkdir(parents=True,exist_ok=True)
    def copy_payload(source,target):
        Path(target).unlink(missing_ok=True)
        return shutil.copy2(source,target)
    for name in ('assets','configs'):shutil.copytree(bundle/'sp_vision_25'/name,reference/name,dirs_exist_ok=True,copy_function=copy_payload)
    binary=reference/'build/simulator/sim_auto_aim';binary.parent.mkdir(parents=True,exist_ok=True)
    if binary.exists():binary.unlink()
    shutil.copy2(bundle/'sp_vision_25/build/simulator/sim_auto_aim',binary)
    names=('RM26_SIMULATOR_IMAGE','RM26_BRIDGE_IMAGE','RM26_ALGORITHM_ROOT')
    previous={name:os.environ.get(name) for name in names}
    os.environ.update(RM26_SIMULATOR_IMAGE=meta['images']['simulator'],RM26_BRIDGE_IMAGE=meta['images']['simulator'],
        RM26_ALGORITHM_ROOT='/home/openvino/'+relative)
    original_compose=manage.compose;original_inspect=manage.inspect
    overlay=manage.RUNTIME/'history-evaluator-overlay.json'
    write(overlay,dict(services=dict(evaluator=dict(image=meta['images']['evaluator']))))
    manage.compose=lambda *args,**kwargs:original_compose('--file',str(overlay),*args,**kwargs)
    manage.inspect=lambda name:original_inspect(meta['images']['evaluator'] if name=='daedalus:rm26-evaluator' else name)
    def snapshot(folder,scene,args):
        proxy=SimpleNamespace(ROOT=bundle,OPS=bundle/'docker/auto_aim',RUNTIME=manage.RUNTIME,
            inspect=manage.inspect,profile=manage.profile,run=manage.run,SIMULATOR=manage.SIMULATOR,
            BRIDGE=manage.BRIDGE,FROZEN_REPOSITORIES=meta['repositories'])
        result=experiments.snapshot(proxy,folder,scene,args)
        runtime=reference/'build/sim/calibration.yaml'
        if runtime.exists():shutil.copy2(runtime,folder/'configuration/runtime-calibration.yaml')
        shutil.copy2(bundle/'bundle.json',folder/'configuration/bundle.json')
        result['reference_kind']='matched_candidate_ui_session_reset'
        result['configuration_sha256']={str(p.relative_to(folder/'configuration')):experiments.digest(p)
            for p in (folder/'configuration').rglob('*') if p.is_file()}
        write(folder/'manifest.json',result);return result
    manage.snapshot=snapshot
    try:yield
    finally:
        delattr(manage,'snapshot');manage.compose=original_compose;manage.inspect=original_inspect
        for name,value in previous.items():
            if value is None:os.environ.pop(name,None)
            else:os.environ[name]=value



def check(bundle,output):
    bundle=Path(bundle).resolve();output=Path(output);output.mkdir(parents=True,exist_ok=True)
    if load(bundle/'bundle.json').get('algorithm',{}).get('id')!='B1':raise ValueError('Live feature checker currently supports B1 adapter')
    subprocess.run(['python3',str(manage.ROOT/'docker/daedalus/display_auth.py')],check=True)
    original=manage.profile();rows=[]
    with (manage.RUNTIME/'operation.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            with matched_ui(bundle):
                for scene in ('hf_static_5','hf_spin_pos','adv_variable_pos','adv_pursuit_pos'):
                    from background_jobs import validate
                    settings=validate(dict(EnvironmentSpec().settings(scene,8601,2026102207),algorithm_profile='tracking_fire_v3'))
                    settings.update(execution_mode='realtime',timing_profile=None,record_timing='on')
                    started=time.monotonic()
                    try:
                        manage.stop()
                        manage.start(argparse.Namespace(**dict(settings,control='off',view='off',replace=False,fire='auto')))
                        folder=experiments.run_one(manage,scene,argparse.Namespace(**dict(settings,repeat_id=0)))
                        rows.append(dict(scene=scene,run_id=folder.name,verdict=load(folder/'summary.json')['verdict'],complete_wall_s=time.monotonic()-started))
                        write(output/'ui-check.json',dict(rows=rows,ui_equivalence_certified=False))
                    finally:manage.stop()
        finally:manage.save_profile(original)
    return dict(rows=rows,ui_equivalence_certified=False)
