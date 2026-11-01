import json
from pathlib import Path
import sys
import numpy as np
import openvino as ov

core = ov.Core()
report = dict(openvino=ov.__version__, devices=core.available_devices, models=[])
outputs = {}
for model_name in ('tiny_resnet.onnx', 'yolov5.xml'):
    model = core.read_model('/project/sp_vision_25/assets/' + model_name)
    if model.input(0).partial_shape.is_dynamic:
        model.reshape({0: [1, 1, 32, 32] if model_name == 'tiny_resnet.onnx' else [1, 3, 640, 640]})
    compiled = core.compile_model(model, 'CPU')
    port = compiled.input(0)
    shape = list(port.shape)
    dtype = np.dtype(port.element_type.to_dtype())
    rng = np.random.default_rng(20261101)
    data = rng.random(shape).astype(dtype) if np.issubdtype(dtype, np.floating) else rng.integers(0, 256, shape, dtype=dtype)
    result = compiled([data])
    rows = []
    for index, value in enumerate(result.values()):
        array = np.asarray(value)
        if not np.isfinite(array).all():
            raise RuntimeError('推理结果含非有限数值')
        outputs[model_name + ':' + str(index)] = array
        rows.append(dict(shape=list(array.shape), dtype=str(array.dtype), minimum=float(array.min()), maximum=float(array.max())))
    report['models'].append(dict(model=model_name, input_shape=shape, input_dtype=str(dtype), outputs=rows))
np.savez_compressed('/work/' + sys.argv[1] + '-inference.npz', **outputs)
Path('/work/' + sys.argv[1] + '-inference.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
