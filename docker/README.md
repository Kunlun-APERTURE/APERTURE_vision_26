# 容器与部署入口

队员先按 [快速开始](../docs/QUICKSTART.md) 获取依赖并完成首轮实验。日常流程见 [实验指南](../docs/EXPERIMENTS.md)，工作站部署见 [运维指南](../docs/OPERATIONS.md)。

- `deploy-sunshine.sh`：现有工作站的桌面容器部署脚本。
- `daedalus/`：宿主机独立Docker模拟器与显示接入。
- `auto_aim/`：算法桥接、队列、评分、场景、对照与视频。

脚本目前含固定工作站路径和显示设置；Sunshine持久HOME、SSH凭据、Docker镜像和运行状态不在Git中。不要把该目录视为新机器一键安装包。
