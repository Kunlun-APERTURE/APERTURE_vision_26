# 容器与部署入口

队友先读 [部署前提与新机器限制](../docs/DEPLOYMENT.md)，再读 [联调与实验命令](auto_aim/README.md)。

- `deploy-sunshine.sh`：现有工作站的桌面容器部署脚本。
- `daedalus/`：宿主机独立Docker模拟器与显示接入。
- `auto_aim/`：算法桥接、队列、评分、场景、对照与视频。

脚本目前含固定工作站路径和显示设置；Sunshine持久HOME、SSH凭据、Docker镜像和运行状态不在Git中。不要把该目录视为新机器一键安装包。
