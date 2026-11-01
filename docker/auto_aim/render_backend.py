"""Freeze renderer choice; CPU mode never probes or reserves NVIDIA devices."""
import os
import subprocess

BACKENDS = ('auto', 'cpu', 'nvidia')
SOFTWARE_ICD = '/usr/share/vulkan/icd.d/lvp_icd.json'


def nvidia_devices():
    try:
        result = subprocess.run(['nvidia-smi', '--query-gpu=uuid', '--format=csv,noheader'],
                                capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return result.stdout.strip().splitlines() if result.returncode == 0 else []


def select(backend='auto'):
    if backend not in BACKENDS:
        raise ValueError('Unknown renderer: ' + str(backend))
    if backend == 'cpu':
        return dict(backend='cpu', gpu_uuid=None, implementation='mesa-lavapipe')
    devices = nvidia_devices()
    requested = os.environ.get('RM26_BENCHMARK_GPU')
    if requested and requested not in devices:
        raise ValueError('Requested GPU unavailable')
    if devices:
        return dict(backend='nvidia', gpu_uuid=requested or devices[0], implementation='nvidia-vulkan')
    if backend == 'nvidia':
        raise ValueError('NVIDIA renderer requested but no usable NVIDIA device is available; use --renderer cpu')
    return dict(backend='cpu', gpu_uuid=None, implementation='mesa-lavapipe')


def frozen(metadata):
    # Old payloads were NVIDIA-only. Never silently change a recorded renderer.
    return metadata.get('renderer', dict(backend='nvidia', gpu_uuid=metadata.get('gpu_uuid'), implementation='nvidia-vulkan'))


def wall_budget(renderer, lockstep):
    return 900 if lockstep and renderer.get('backend')=='cpu' else 180 if lockstep else 40
