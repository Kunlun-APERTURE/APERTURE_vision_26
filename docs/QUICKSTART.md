# 新人快速开始

目标：在自己的 Ubuntu 主机完成一次后台实验，然后提交改进。除首次 clone 与 cd 外，命令均从主仓库根目录执行。

## 主机前提

- Ubuntu 24.04 x86_64，Git、Python 3.12 和 Python venv。
- NVIDIA GPU 与驱动，`nvidia-smi` 可用。
- Docker Engine、Compose 插件、NVIDIA Container Toolkit；当前用户能访问 Docker。
- 首次建议至少预留20GiB空间，实际需求随实验数量增加。

先确认 `docker version` 能访问 Server、`docker compose version` 可用。仓库不自动安装系统依赖，无需 Sunshine 或显示器。完整空机器安装尚未验收，系统环境有问题时先联系维护者。

## 获取源码

```bash
git clone --recurse-submodules https://github.com/Kunlun-APERTURE/APERTURE_vision_26.git
cd APERTURE_vision_26
python3 tools/check-workspace.py
```

主仓库和两个子仓库当前可公开读取，推送权限分别管理。已有克隆缺少子模块时：

```bash
git submodule update --init --recursive
```

## 获取依赖包

所有开发与实验角色共用一个 Ubuntu 24.04 镜像：`ghcr.io/kunlun-aperture/aperture_vision:26-11-1`，包含 OpenVINO 2024.6、C++ 与 Rust 工具链、模拟器和评分依赖。包名必须小写，项目显示名称为 APERTURE_vision。clone 不包含镜像。

GHCR 首次公开下载仍待验收，当前从 [26-11-01 Release 附件](https://github.com/Kunlun-APERTURE/APERTURE_vision_26/releases/tag/26-11-01) 下载以下两个文件：

|文件|用途|
|---|---|
|`aperture_vision-26-11-1.tar`|统一镜像离线归档，约1.48GB|
|`aperture-offline.json`|完整归档 SHA256 和镜像身份|

将文件分别放为主仓库 `exports/aperture/images.tar` 和 `exports/aperture/manifest.json`，然后从根目录执行：

```bash
python3 tools/dependency_bundle.py import exports/aperture
python3 tools/dependency_registry.py check
python3 tools/development.py doctor
docker run --rm --gpus all aperture_vision:26-11-1 nvidia-smi
```

import 校验归档并加载镜像；check 核对当前源码所需的固定身份。GHCR 公开验收通过后，同一目录的依赖也可用 `python3 tools/dependency_registry.py pull` 获取，该命令按 digest 拉取并恢复本地镜像名；目前不把它作为已可用来源。拿不到完整归档和清单时先联系维护者，不假定本机已有依赖。工作站原件为 `/home/rm/rm26/exports/unified-20261101/`，镜像分发维护见 [运维指南](OPERATIONS.md#镜像自动发布)。

使用历史 Release 时按其对应文档操作，旧五镜像离线包继续保留。`exports/` 不提交 Git。

## 构建与首次实验

```bash
python3 tools/development.py build
```

输出位于 `docker/auto_aim/.runtime/benchmark/B1/`。默认 B1 是 R4 配置，E1 是修正原式云台环境；参数见 [实验指南](EXPERIMENTS.md)。

开两个终端，都进入主仓库根目录。终端 A 启动调度器并保持运行：

```bash
./sim.sh jobs daemon
```

已有服务时不要重复启动，用 `./sim.sh jobs service-status` 检查。长期安装见 [运维指南](OPERATIONS.md#本地实验调度器)。终端 B：

```bash
./sim.sh experiment suite --algorithm B1 --environment E1 --tier basic --cases hf_static_5 --sensor-seeds 1 --output exports/first-static --wait
cat exports/first-static/report.md
```

命令返回 job ID，`--wait` 等待完成。目录还有 report.html 和 results.json。确认 `complete`、`all_evidence_valid` 为 true；验收射击链路还需发射数大于零。命中率不保证固定值。

新实验使用新输出目录；恢复和评分解读见 [实验指南](EXPERIMENTS.md)。停止前用 `./sim.sh jobs list` 确认任务完成，再在终端 A 按 Ctrl+C。

## 修改与提交

按 [开发指南](DEVELOPMENT.md) 修改算法或新增场景，再按 [贡献指南](../CONTRIBUTING.md) 上传子仓库提交、更新主仓库引用并申请服务器实验。无需阅读历史实验归档。
