"""Explicit experiment input contract; no target truth or aiming policy."""
import json
import contextlib
import os

ALIGNED='aligned-v1'


def aligned(settings):return settings.get('input_contract','legacy')==ALIGNED


def validate(settings):
    if not aligned(settings):
        if settings.get('sensor_seed') is not None:raise ValueError('sensor-seed requires aligned-v1')
        return
    if settings.get('detector_history')!='session':raise ValueError('aligned-v1 requires detector-history=session')
    if settings.get('session_evidence')=='off':raise ValueError('aligned-v1 requires complete session evidence')
    seed=settings.get('sensor_seed')
    if type(seed) is not int or not 0<=seed<=4294967295:raise ValueError('aligned-v1 requires an explicit uint32 sensor-seed')
    count=settings.get('warmup_frames',30)
    if type(count) is not int or not 3<=count<=120:raise ValueError('warmup-frames must be 3..120')


def boundary(value):
    if not isinstance(value,dict) or value.get('version')!=1:raise ValueError('Missing physical scenario boundary')
    if not isinstance(value.get('id'),str) or type(value.get('generation')) is not int:raise ValueError('Invalid scenario identity')
    if value.get('phase') not in (0,1,2) or type(value.get('stamp_ns')) is not int:raise ValueError('Invalid physical phase')
    start,end=value.get('measurement_start_stamp_ns'),value.get('measurement_end_stamp_ns')
    if value['phase']>=1 and (type(start) is not int or start>value['stamp_ns']):raise ValueError('Invalid physical start')
    if value['phase']==2 and (type(end) is not int or not start<=end<=value['stamp_ns']):raise ValueError('Invalid physical end')
    return value


def accept_boundary(previous,new):
    boundary(new)
    if previous and previous['id']==new['id'] and new['generation']<previous['generation']:return False
    if previous and (previous['id'],previous['generation'])==(new['id'],new['generation']):
        if new['stamp_ns']<previous['stamp_ns'] or new['phase']<previous['phase']:return False
        if previous.get('measurement_start_stamp_ns') is not None and previous['measurement_start_stamp_ns']!=new.get('measurement_start_stamp_ns'):
            raise ValueError('Physical start changed within a generation')
    return True


@contextlib.contextmanager
def runtime(settings,manager):
    """Select a validated candidate explicitly, without retagging defaults."""
    if not aligned(settings):
        yield;return
    path=manager.OPS/'input-contract-runtime.json'
    if not path.exists():raise RuntimeError('aligned-v1 runtime has not completed acceptance')
    release=json.loads(path.read_text())
    if release.get('contract')!=ALIGNED or not release.get('mechanisms_verified'):
        raise RuntimeError('Invalid aligned-v1 runtime descriptor')
    import hashlib
    binary=manager.ROOT/'sp_vision_25/build/simulator/sim_auto_aim'
    if hashlib.sha256(binary.read_bytes()).hexdigest()!=release['algorithm_sha256']:
        raise RuntimeError('Algorithm binary differs from aligned-v1 runtime; freeze a new accepted candidate')
    for name in ('simulator','evaluator'):
        info=manager.inspect(release[name]['tag'])
        if not info or info['Id']!=release[name]['image_id']:raise RuntimeError('Aligned runtime image changed: '+name)
    old_image,old_inspect=manager.IMAGE,manager.inspect
    names=('RM26_SIMULATOR_IMAGE','RM26_BRIDGE_IMAGE','RM26_INTEGRATION_IMAGE_ID')
    previous={name:os.environ.get(name) for name in names}
    manager.IMAGE=release['simulator']['tag']
    manager.inspect=lambda name:old_inspect(release['evaluator']['tag'] if name=='daedalus:rm26-evaluator' else name)
    from gimbal_dynamics import simulator_image
    selected=simulator_image(settings.get('gimbal_dynamics','legacy'),release['simulator']['tag'])
    os.environ.update(RM26_SIMULATOR_IMAGE=selected,RM26_BRIDGE_IMAGE=release['simulator']['tag'],
        RM26_INTEGRATION_IMAGE_ID=release['simulator']['image_id'])
    original_compose=manager.compose
    overlay=manager.RUNTIME/'input-contract-overlay.json'
    overlay.write_text(json.dumps(dict(services=dict(evaluator=dict(image=release['evaluator']['tag'])))))
    manager.compose=lambda *args,**kwargs:original_compose('--file',str(overlay),*args,**kwargs)
    try:yield
    finally:
        manager.IMAGE,manager.inspect,manager.compose=old_image,old_inspect,original_compose
        for name,value in previous.items():
            if value is None:os.environ.pop(name,None)
            else:os.environ[name]=value
