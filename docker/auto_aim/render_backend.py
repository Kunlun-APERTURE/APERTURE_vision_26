"""Freeze renderer choice; CPU mode never probes or reserves NVIDIA devices."""
import os
import subprocess
import sys
import uuid

BACKENDS = ('auto', 'cpu', 'nvidia')
SOFTWARE_ICD = '/usr/share/vulkan/icd.d/lvp_icd.json'


def is_wsl():
    return 'microsoft' in os.uname().release.lower()


VULKAN_PROBE = '''import ctypes as c, struct, sys
lib = c.CDLL('libvulkan.so.1')
class Info(c.Structure):
    _fields_ = [('sType',c.c_uint32),('pNext',c.c_void_p),('flags',c.c_uint32),
        ('app',c.c_void_p),('layers',c.c_uint32),('layer_names',c.c_void_p),
        ('extensions',c.c_uint32),('extension_names',c.c_void_p)]
info = Info(1,None,0,None,0,None,0,None)
instance = c.c_void_p()
lib.vkCreateInstance.argtypes = [c.POINTER(Info),c.c_void_p,c.POINTER(c.c_void_p)]
lib.vkCreateInstance.restype = c.c_int32
if lib.vkCreateInstance(c.byref(info),None,c.byref(instance)) != 0:
    sys.exit(1)
lib.vkEnumeratePhysicalDevices.argtypes = [c.c_void_p,c.POINTER(c.c_uint32),c.POINTER(c.c_void_p)]
lib.vkGetPhysicalDeviceProperties.argtypes = [c.c_void_p,c.c_void_p]
count = c.c_uint32()
lib.vkEnumeratePhysicalDevices(instance,c.byref(count),None)
devices = (c.c_void_p * count.value)()
lib.vkEnumeratePhysicalDevices(instance,c.byref(count),devices)
hardware = False
for device in devices:
    props = c.create_string_buffer(4096)
    lib.vkGetPhysicalDeviceProperties(device,props)
    api,driver,vendor,identifier,kind = struct.unpack_from('IIIII',props.raw)
    hardware |= vendor == 0x10de and kind in (1,2,3)
lib.vkDestroyInstance.argtypes = [c.c_void_p,c.c_void_p]
lib.vkDestroyInstance(instance,None)
sys.exit(0 if hardware else 1)
'''


def nvidia_vulkan_available(image_id, gpu_uuid):
    """Check the actual image's Vulkan adapters; NVML/CUDA visibility is insufficient."""
    name = 'rm26-vulkan-probe-'+uuid.uuid4().hex[:12]
    try:
        provider = ['--volume','/usr/lib/wsl:/usr/lib/wsl:ro'] if is_wsl() else []
        result = subprocess.run(['docker','run','--rm','--name',name,'--network','none',*provider,
            '--gpus','device='+gpu_uuid,'--env','NVIDIA_DRIVER_CAPABILITIES=graphics,utility,compute',
            '--entrypoint','python3',image_id,'-c',VULKAN_PROBE],
            capture_output=True,text=True,timeout=20)
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        try:
            subprocess.run(['docker','rm','--force',name],capture_output=True,timeout=5)
        except (OSError,subprocess.TimeoutExpired):
            pass
        return False
    except OSError:
        return False


def nvidia_devices():
    try:
        result = subprocess.run(['nvidia-smi', '--query-gpu=uuid', '--format=csv,noheader'],
                                capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return result.stdout.strip().splitlines() if result.returncode == 0 else []


def select(backend='auto', image_id=None):
    if backend not in BACKENDS:
        raise ValueError('Unknown renderer: ' + str(backend))
    if backend == 'cpu':
        return dict(backend='cpu', gpu_uuid=None, implementation='mesa-lavapipe')
    devices = nvidia_devices()
    requested = os.environ.get('RM26_BENCHMARK_GPU')
    if requested and requested not in devices:
        raise ValueError('Requested GPU unavailable')
    if devices:
        if is_wsl() and backend == 'nvidia' and not image_id:
            raise ValueError(
                'WSL NVIDIA rendering requires a resolved simulator image for the Vulkan probe')
        if is_wsl() and image_id and not nvidia_vulkan_available(
                image_id, requested or devices[0]):
            reason = 'The WSL container has no usable hardware NVIDIA Vulkan adapter'
            if backend == 'nvidia':
                raise ValueError(reason + '; use --renderer cpu')
            print('sim.sh: ' + reason + '; auto is using CPU software rendering', file=sys.stderr)
            return dict(backend='cpu', gpu_uuid=None, implementation='mesa-lavapipe')
        return dict(backend='nvidia', gpu_uuid=requested or devices[0], implementation='nvidia-vulkan')
    if backend == 'nvidia':
        raise ValueError('NVIDIA renderer requested but no usable NVIDIA device is available; use --renderer cpu')
    return dict(backend='cpu', gpu_uuid=None, implementation='mesa-lavapipe')


def frozen(metadata):
    # Old payloads were NVIDIA-only. Never silently change a recorded renderer.
    return metadata.get('renderer', dict(backend='nvidia', gpu_uuid=metadata.get('gpu_uuid'), implementation='nvidia-vulkan'))


def wall_budget(renderer, lockstep):
    return 3600 if lockstep and renderer.get('backend')=='cpu' else 180 if lockstep else 40
