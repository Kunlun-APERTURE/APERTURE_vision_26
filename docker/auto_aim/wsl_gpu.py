"""Select a separately built WSL graphics runtime without changing the pinned SDK."""
import json
import os
from pathlib import Path

STATE = Path(__file__).resolve().parent/'.runtime/wsl-gpu.json'
IMPLEMENTATION = 'mesa-dozen-webgl2'


def compatible_runtime(base, renderer, inspect):
    from render_backend import is_wsl
    if renderer == 'cpu' or not is_wsl() or not base:
        return None
    if base['Config'].get('Labels', {}).get('aperture.unified') != '1':
        return None
    requested = os.environ.get('RM26_WSL_GPU_IMAGE')
    if not requested and STATE.exists():
        record = json.loads(STATE.read_text())
        if record.get('base_image') == base['Id']:
            requested = record.get('image')
    if not requested:
        return None
    candidate = inspect(requested)
    labels = candidate['Config'].get('Labels', {}) if candidate else {}
    if (labels.get('aperture.wsl_gpu') != IMPLEMENTATION
            or labels.get('aperture.base_image') != base['Id']
            or labels.get('aperture.unified') != '1'):
        raise ValueError('WSL GPU runtime is missing or does not match the pinned SDK; rebuild it')
    return candidate
