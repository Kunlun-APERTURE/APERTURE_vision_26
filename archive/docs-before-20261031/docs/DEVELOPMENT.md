# 团队开发环境

第一版支持 Ubuntu 24.04、NVIDIA 驱动与 Docker GPU runtime。无 GPU 可运行自动测试。仓库需递归克隆；算法修改在 submodule 自己的分支提交，再更新主仓库指针。

```bash
python3 tools/development.py doctor
python3 tools/development.py build
./sim.sh experiment suite --tier basic --cases hf_static_5 \
  --sensor-seeds 1 --output exports/local-static --wait
./sim.sh experiment suite --tier advanced --cases pursuit \
  --sensor-seeds 1 --output exports/local-pursuit --wait
```

依赖镜像准备方法见 DEPLOYMENT.md。算法环境 Dockerfile 为 `docker/auto_aim/Algorithm.Dockerfile`，其基础镜像为 `sp_vision_dev:2024.6.0`；该基础镜像及模拟器镜像需要团队分发，当前仓库尚不能在全新机器自动取得全部依赖。`doctor` 检查已有依赖，不能替代安装。

开发运行会保存当前源码、配置和模型记录，不要求正式发布认证。默认构建输出位于 `.runtime/benchmark/B1`，修改配置可复制 B1.yaml 并用算法 manifest 指定；实验报告记录实际配置。已有输出目录恢复需使用原参数，新的参数使用新目录。

场景通过 Scenario 子类定义 generate/validate/difficulty；统一注册表在 benchmark/scenarios.py。新增后 CLI、后台和评分从注册表读取。ExampleStatic 展示最小场景；AlgorithmAdapter/ProcessAdapter 负责独立进程和模型资源，公共 image_v1 协议不要求 R4 的内部诊断。

```bash
python3 tools/development.py example
# 按输出命令运行独立 Python 算法和 example_static 场景。
```

示例算法只消费图像、返回无效控制且不开火，用于验证接口，不作为瞄准算法。

```bash
uv run --no-project --with coverage python tools/test-development.py
```

本地和 GitHub 使用同一入口，覆盖率报告位于 `.coverage-reports/html`。统计包含所有 benchmark/pr_service 生产模块，未覆盖的 UI、Docker 和历史迁移路径也保留在总数里。目标为语句80%、分支70%；当前未达标时必须在交付摘要明确说明，不能通过排除模块提升数字。

## 离线依赖交付

维护者在已验工作站运行 `python3 tools/dependency_bundle.py export exports/team-dependencies`，分发目录中的 images.tar 和 manifest.json。它包含构建基础、算法运行、输入链路、评分和修正云台镜像，并记录内容校验。

新 Ubuntu x86_64 工作站先安装 NVIDIA 驱动、Docker Engine 和 NVIDIA Container Toolkit，再执行：

```bash
python3 tools/dependency_bundle.py import /path/to/team-dependencies
python3 tools/development.py doctor
python3 tools/development.py build
```

导入与检查只验证镜像和本机运行条件。全新系统驱动安装仍需要在实际新机器验收；复用现有 Docker daemon 的测试不能被称为全新机器安装通过。
