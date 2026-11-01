# Ubuntu 24.04 与 OpenVINO 2024.6 兼容验证

本次在已有 Ubuntu 24.04 模拟器镜像上增加算法开发依赖，仅复制原 OpenVINO 2024.6 SDK 目录，未复制 Ubuntu 22.04 的系统库。算法源码和 B1 配置保持不变，使用 GCC 13、OpenCV 4.6、Python 3.12 重新构建并测试。完整身份与结果见 [validation.json](validation.json)。

构建的 9 个目标全部成功，6 个回归程序通过。固定输入的 tiny_resnet 和 YOLOv5 CPU 推理与旧环境逐值相同。静止、平移和旋转三组同种子闭环均完成实际射击和证据审计；旧环境与候选环境的命中数、发射数和加权命中率逐项一致。

|场景|旧环境命中/发射|Ubuntu 24.04 命中/发射|加权命中率（双方相同）|
|---|---:|---:|---:|
|hf_static_5|29/29|29/29|93.1706%|
|hf_uniform_3|15/17|15/17|44.8630%|
|hf_spin_pos|20/21|20/21|70.9079%|

这里只验证算法运行环境迁移。模拟器、桥接和评分仍使用原验收镜像；完整单镜像部署、全场景回归及跨硬件一致性尚未验收。推理测试使用 CPU，NVIDIA GPU 用于现有模拟器渲染。

## 复现构建与组件测试

以下命令从仓库根目录执行，需要已取得旧环境依赖镜像。首次构建需网络访问 Ubuntu 软件源。使用新输出目录，避免复用已有构建与实验输出：

```bash
RM26_VALIDATION_DIR=exports/ubuntu24-validation-new
mkdir -p "$RM26_VALIDATION_DIR"
docker build --network host --progress plain \
  --file docker/auto_aim/Ubuntu24Validation.Dockerfile \
  --tag rm26-validation:ubuntu24-openvino2024.6 docker/auto_aim
docker run --rm --network none --user "$(id -u):$(id -g)" \
  --volume "$PWD:/project:ro" --volume "$PWD/$RM26_VALIDATION_DIR:/work" \
  --workdir /work --entrypoint bash rm26-validation:ubuntu24-openvino2024.6 \
  /project/tools/validate-ubuntu24-components.sh
```

组件脚本加载 ROS 与 OpenVINO 环境。ROS 库存在但未加载环境时，CMake 的可选探测会因找不到 ament_package 而失败；上游 setupvars.sh 也不能在开启 nounset 的 shell 中直接加载。脚本使用显式错误检查，不依赖这两个隐含前提。

## 复现模型推理

沿用上文变量。两个模型均使用固定输入，先执行原环境，再执行候选环境：

```bash
for RM26_INFERENCE_CASE in ubuntu22 ubuntu24; do
  if [ "$RM26_INFERENCE_CASE" = ubuntu22 ]; then
    RM26_INFERENCE_IMAGE=sp_vision_dev:2024.6.0
  else
    RM26_INFERENCE_IMAGE=rm26-validation:ubuntu24-openvino2024.6
  fi
  docker run --rm --network none --user "$(id -u):$(id -g)" \
    --volume "$PWD:/project:ro" --volume "$PWD/$RM26_VALIDATION_DIR:/work" \
    --workdir /work --entrypoint bash "$RM26_INFERENCE_IMAGE" -c \
    'source /opt/intel/openvino/setupvars.sh >/dev/null && python3 /project/release/20261101-ubuntu24/inference.py "$1"' \
    bash "$RM26_INFERENCE_CASE"
done
docker run --rm --network none --user "$(id -u):$(id -g)" \
  --volume "$PWD:/project:ro" --volume "$PWD/$RM26_VALIDATION_DIR:/work" \
  --entrypoint bash rm26-validation:ubuntu24-openvino2024.6 -c \
  'source /opt/intel/openvino/setupvars.sh >/dev/null && python3 /project/release/20261101-ubuntu24/compare-inference.py'
```

## 复现配对闭环

需要运行中的本地调度器，启动流程见 [快速开始](../../docs/QUICKSTART.md#构建与首次实验)。先构建旧环境 B1，然后生成候选 manifest，在相同环境、场景和种子下配对运行：

```bash
python3 tools/development.py build
python3 release/20261101-ubuntu24/prepare-adapter.py "$RM26_VALIDATION_DIR"
./sim.sh experiment suite --algorithm B1 --environment E1 --tier basic \
  --cases hf_static_5,hf_uniform_3,hf_spin_pos --sensor-seeds 2026110101 \
  --output "$RM26_VALIDATION_DIR/reference" --wait
./sim.sh experiment suite --algorithm "$RM26_VALIDATION_DIR/adapter.json" \
  --artifact "$RM26_VALIDATION_DIR/build/sim_auto_aim" \
  --environment E1 --tier basic --cases hf_static_5,hf_uniform_3,hf_spin_pos \
  --sensor-seeds 2026110101 --output "$RM26_VALIDATION_DIR/candidate" --wait
./sim.sh experiment algorithm compare \
  --output "$RM26_VALIDATION_DIR/reference" --candidate "$RM26_VALIDATION_DIR/candidate"
```

本轮完整日志、构建产物和推理数组保留在工作站 `exports/ubuntu24-openvino-20261101/`，逐轮记录保留在本机实验目录。本目录只保存精简结果、证据审计及复现脚本。
