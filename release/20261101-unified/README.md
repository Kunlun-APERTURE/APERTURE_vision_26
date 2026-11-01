# APERTURE_vision 统一镜像验收

候选名为 aperture_vision:26-11-1，目标公开地址为 ghcr.io/kunlun-aperture/aperture_vision:26-11-1。仓库路径按 Docker 要求小写，显示名保留 APERTURE_vision。镜像身份、源码来源与结果见 [validation.json](validation.json)，逐场成绩见 [report.md](report.md)。

本轮兼容验收通过：9 个 C++ 目标构建成功，6 个回归程序通过；Rust 1.98.1 在镜像内从只读源码完成 ros2 bench 模拟器的完整 release 构建；10 个基础场景完成 181 次发射和 161 次命中，全部证据审计有效。模拟器、桥接、算法和评分使用同一镜像 ID。

Rust 全部单元测试为 283 通过、10 失败、1 忽略。同一测试产物在原模拟器环境中重现完全相同的失败，没有新增失败。既有问题涉及捕获传感器资源、默认配置和装甲标签断言；本次镜像迁移未修改子仓库源码，不将结果表述为全部单测通过。对照记录见 [rust-regression-comparison.json](rust-regression-comparison.json)。

首轮闭环因默认工作目录不同而失败，原始尝试保留。修正为模拟器显式使用 /app 后，新冻结任务的 10 场全部完成。入口通过函数作用域加载 ROS/OpenVINO，隔离上游脚本对位置参数的修改，保证传入命令和参数不被丢弃。

镜像展开约6.42GB，离线归档约1.48GB。包含 Ubuntu 24.04、OpenVINO 2024.6、C++/Rust 工具链、ROS 2 Jazzy、模拟器、Python 评分及视频依赖。算法源码和构建产物运行时挂载，修改算法不需要重建环境镜像。

## 构建与检查

所有命令从仓库根目录执行。维护者构建前需已有旧 OpenVINO、修正模拟器和 Rust 基础镜像，来源见 [运维指南](../../docs/OPERATIONS.md#镜像自动发布)。

```bash
docker build --network host --progress plain \
  --file docker/auto_aim/Unified.Dockerfile --tag aperture_vision:26-11-1 .
mkdir -p exports/unified-check
docker run --rm --network none --user "$(id -u):$(id -g)" \
  --volume "$PWD:/project:ro" --volume "$PWD/exports/unified-check:/work" \
  --workdir /work aperture_vision:26-11-1 \
  bash /project/tools/validate-ubuntu24-components.sh
mkdir -p exports/unified-rust
docker run --rm --network host --user "$(id -u):$(id -g)" \
  --volume "$PWD:/project:ro" --volume "$PWD/exports/unified-rust:/work" \
  --workdir /work aperture_vision:26-11-1 bash /project/tools/build-simulator.sh
```

本地算法构建与闭环命令统一见 [开发指南](../../docs/DEVELOPMENT.md) 和 [实验指南](../../docs/EXPERIMENTS.md)，当前 B1 从依赖清单选择统一镜像。10 场验收使用 basic 全档和固定 sensor seed 2026110101。

本轮仅含一个传感器种子，尚未认证进阶/压力全档、跨硬件一致性或空机器安装。GHCR 首次发布、全部层匿名下载及本机 Docker 匿名拉取已验证通过，Release 已附加在线依赖清单。旧五包继续用于历史版本复现。
