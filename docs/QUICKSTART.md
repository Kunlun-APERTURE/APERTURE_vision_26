# 新人快速开始

目标：在自己的 Ubuntu 主机完成一次后台实验，然后提交改进。除首次 clone 与 cd 外，命令均从主仓库根目录执行。

## 主机前提

- Ubuntu 24.04 x86_64，Git、Python 3.12 和 Python venv。
- Docker Engine、Compose 插件；当前用户能访问 Docker。
- 可选 NVIDIA GPU、驱动及 NVIDIA Container Toolkit，用于加速模拟器渲染。无 GPU 使用镜像内的软件渲染，不需要安装 NVIDIA 驱动或 Toolkit。
- 首次建议至少预留20GiB空间，实际需求随实验数量增加。

先确认 `docker version` 能访问 Server、`docker compose version` 可用。仓库不自动安装系统依赖，无需 Sunshine 或显示器。完整空机器安装尚未验收，系统环境有问题时先联系维护者。

WSL 2 配合 Docker Desktop 时，在 **Settings → Resources → WSL Integration** 中启用实际使用的发行版。WSL 后台实验让各容器共享内部网络，仅向宿主机的 `127.0.0.1` 发布 bridge 的 TCP 端口，不依赖 **Enable host networking**；避免 DDS 随机 UDP 端口与 Windows 排除端口范围冲突。WSL 可保持 NAT，不要求切换 `mirrored`。WSL 全面兼容性仍未验收，Ubuntu 24.04 原生主机仍是支持基准。

能运行 `nvidia-smi` 并不等于本次模拟器已使用 NVIDIA Vulkan；应检查模拟器日志中的实际适配器。日志选择 `llvmpipe` 表示软件渲染，此时需要排查容器图形驱动或显式使用 `--renderer cpu`，不能把 CUDA 可见当作 Vulkan 验收通过。

## 获取源码

```bash
git clone --recurse-submodules https://github.com/Kunlun-APERTURE/APERTURE_vision_26.git
cd APERTURE_vision_26
python3 tools/check-workspace.py
```

以上检出 main 的当前组合，适合日常开发。固定复现 [26-11-1](https://github.com/Kunlun-APERTURE/APERTURE_vision_26/releases/tag/26-11-1) 时，在 clone 命令加入 `--branch 26-11-1`；不要把历史 `26-11-01` 当作单镜像版本。切换主仓库版本后先执行下文子模块更新，再重新拉取该版本清单与构建算法。

主仓库和两个子仓库当前可公开读取，推送权限分别管理。已有克隆缺少子模块时：

```bash
git submodule update --init --recursive
```

## 获取依赖包

所有开发与实验角色共用一个 Ubuntu 24.04 镜像：`ghcr.io/kunlun-aperture/aperture_vision:26-11-1`，包含 OpenVINO 2024.6、C++ 与 Rust 工具链、模拟器和评分依赖。包名必须小写，项目显示名称为 APERTURE_vision。clone 不包含镜像。

公开镜像已通过匿名拉取与身份校验，从根目录执行：

```bash
python3 tools/dependency_registry.py pull
python3 tools/dependency_registry.py check
python3 tools/development.py doctor
docker run --rm aperture_vision:26-11-1 python3 -c 'import openvino, cv2; print(openvino.__version__, cv2.__version__)'
```

pull 按仓库清单固定的 digest 下载，并恢复本地镜像名，不需要 GitHub 登录。doctor 检查已有工具并显示自动选择的渲染模式；缺少 NVIDIA 不会使检查失败。镜像中的 OpenVINO 使用 CPU 推理。

希望使用 NVIDIA 加速时，另行运行 `docker run --rm --gpus all aperture_vision:26-11-1 nvidia-smi` 确认容器驱动。软件渲染无需执行这一步。

无法访问 GHCR 时，从 [26-11-01 Release 附件](https://github.com/Kunlun-APERTURE/APERTURE_vision_26/releases/tag/26-11-01) 下载 `aperture_vision-26-11-1.tar`（约1.48GB）和 `aperture-offline.json`，分别放为主仓库 `exports/aperture/images.tar` 和 `exports/aperture/manifest.json`：

```bash
python3 tools/dependency_bundle.py import exports/aperture
python3 tools/dependency_registry.py check
```

import 校验完整归档 SHA256 和镜像身份。拿不到完整归档和清单时先联系维护者，不假定本机已有依赖。工作站原件为 `/home/rm/rm26/exports/unified-20261101/`，镜像发布与分发维护见 [运维指南](OPERATIONS.md#镜像自动发布)。

使用历史 Release 时按其对应文档操作，旧五镜像离线包继续保留。`exports/` 不提交 Git。

## 构建与首次实验

```bash
python3 tools/development.py build
```

输出位于 `docker/auto_aim/.runtime/benchmark/B1/`。默认 B1 是 R4 配置，E1 是修正原式云台环境；参数见 [实验指南](EXPERIMENTS.md)。

开两个终端，都进入主仓库根目录。终端 A 启动调度器并保持运行（已有后台服务时跳过终端 A）：

```bash
./sim.sh jobs daemon
```

已有服务时不要重复启动，用 `./sim.sh jobs service-status` 检查。长期安装见 [运维指南](OPERATIONS.md#本地实验调度器)。终端 B：

```bash
./sim.sh experiment suite --algorithm B1 --environment E1 --tier basic --cases hf_static_5 --sensor-seeds 1 --output exports/first-static --wait
cat exports/first-static/report.md
```

默认 `--renderer auto`：有可用 NVIDIA 设备时使用它，否则使用 CPU 软件 Vulkan。WSL 会额外检查实际镜像中能否枚举到硬件 NVIDIA Vulkan 适配器；只能看到 `llvmpipe` 等软件适配器时，明确提示原因并回退到软件渲染。可加 `--renderer cpu` 在有 GPU 的机器上明确验证软件渲染。软件渲染较慢，首轮需等待，硬超时为3600秒，报告仍单列40秒成本指标。

WSL 的 CUDA 可用不代表容器里的 Vulkan 可用。对于已安装 Mesa Dozen（`/usr/share/vulkan/icd.d/dzn_icd.json`）的 WSL 主机，可以从固定 SDK 构建独立的本地 GPU 镜像。它通过 WSL D3D12 使用 NVIDIA 渲染，并设置 Bevy 的 `WGPU_SETTINGS_PRIO=webgl2`，避开部分 NVIDIA/Dozen 组合中会崩溃的计算预处理管线。首次编译需要下载 Rust 依赖：

```bash
python3 tools/build_wsl_gpu_runtime.py
RM26_WSL_GPU_IMAGE=aperture_vision:wsl-gpu ./sim.sh experiment suite --algorithm B1 --environment E1 --tier basic --cases hf_static_5 --sensor-seeds 1 --renderer nvidia --output exports/first-static-gpu --wait
python3 tools/build_wsl_gpu_runtime.py --activate-report exports/first-static-gpu
```

激活要求实验完成、证据有效且发生射击；之后新实验的 `auto` 使用该 GPU 镜像，实际镜像和 `mesa-dozen-webgl2` 实现会写入冻结记录。原始 SDK 标签与 digest 保留，升级 SDK 后需要重新构建并验证。恢复旧实验仍使用其冻结的渲染方式，不会切换 GPU；已有输出重跑时使用 `--resume`。

命令返回 job ID，`--wait` 等待完成。目录还有 report.html 和 results.json。执行以下检查，确认完成、证据有效并实际发生射击：

```bash
python3 - <<'PY'
import json
from pathlib import Path
result = json.loads(Path("exports/first-static/results.json").read_text())
assert result["complete"] and result["all_evidence_valid"]
shots = sum(group["shots"] for group in result["groups"])
assert shots > 0
print(f"首轮实验完成，证据有效，共发射 {shots} 发")
PY
```

命中率不保证固定值。

新实验使用新输出目录；恢复和评分解读见 [实验指南](EXPERIMENTS.md)。停止前用 `./sim.sh jobs list` 确认任务完成，再在终端 A 按 Ctrl+C。

如果出现 `Inspect retained attempts` 或任务状态为 `paused`，先用 `./sim.sh jobs status job_实际ID` 和 `./sim.sh jobs logs job_实际ID --tail 100` 查看失败原因。suite `--resume` 只连接已有任务，不解除暂停，也不会采用修改后的源码或配置。保持原冻结输入恢复时，先修复外部故障，再执行 `./sim.sh jobs resume job_实际ID`；源码或配置修复后应改用新的 `--output`，不加 `--resume`，保留旧记录。

## 修改与提交

按 [开发指南](DEVELOPMENT.md) 修改算法或新增场景，再按 [贡献指南](../CONTRIBUTING.md) 上传子仓库提交、更新主仓库引用并申请服务器实验。无需阅读历史实验归档。
