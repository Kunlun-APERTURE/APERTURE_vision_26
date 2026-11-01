"""从版本清单选择统一环境；历史清单继续走原冻结镜像路径。"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def unified_image():
    path = ROOT / 'release/dependencies.json'
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if data.get('version') != 3:
        return None
    images = data.get('images', [])
    if len(images) != 1:
        raise ValueError('统一环境必须只包含一个镜像')
    return images[0]['local']


def build_image():
    return unified_image() or 'sp_vision_dev:2024.6.0'
