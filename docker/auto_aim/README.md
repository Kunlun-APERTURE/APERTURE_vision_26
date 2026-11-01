# 联调工具入口

新人从 [快速开始](../../docs/QUICKSTART.md) 完成依赖导入与首次实验。日常后台任务、场景、评分和视频统一见 [实验指南](../../docs/EXPERIMENTS.md)；调度器与可选桌面配置见 [运维指南](../../docs/OPERATIONS.md)。以下命令从主仓库根目录执行，仅用于已有桌面环境的手动联调。

## 手动检查

```bash
./sim.sh status
./sim.sh start --scene static --control off --fire off
./sim.sh control on
./sim.sh control off
./sim.sh logs --component algorithm --tail 100
./sim.sh stop
```

`start`不自动构建；配置冲突使用`restart`。停止联调不会关闭Sunshine或VSCode。模型默认legacy；正式对照需显式指定算法配置。advice发射上限5Hz（至少0.2秒），不是无条件固定周期连发。


研究脚本保留复现能力，依赖当时冻结包和原始资料，不是首次运行步骤。历史入口与结论见 [归档索引](../../ARCHIVE.md)。
