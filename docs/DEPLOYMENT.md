# 部署与工作站前提

## 使用现有工作站

当前已验环境：Ubuntu宿主机、NVIDIA GPU与容器工具链、Docker Compose/Buildx；Sunshine容器 `sp_vision_sunshine` 提供GNOME显示和OpenVINO开发环境。模拟器运行在宿主机独立Docker容器，桥接器/评估器另行运行，不使用嵌套Docker。

现有工作站路径为 `/home/rm/rm26`，Sunshine源码挂载为 `/workspace/sp_vision_25`、`/workspace/vision_simulator_26`。UI显示为 `:20`，使用工作站时保持VT8；切换TTY可能影响NVIDIA呈现。后台运行可以慢于实时，但本部署仍使用既有GPU/显示设施，并不是无GPU的纯数值模拟。

```bash
cd /home/rm/rm26
./sim.sh status
./sim.sh jobs service-status
./sim.sh jobs list
```

Sunshine由宿主机服务管理；UI模拟器/桥接手动启停。实验调度器为宿主机服务，重启后中断尝试留档、队列暂停，核对证据后用 `jobs resume JOB_ID` 恢复。停止模拟器不会停止桌面。不要在有实验运行时重建镜像或替换算法二进制。

## 新机器部署边界

本阶段交付是工作站集成源码，**尚非一键跨机器安装包**。新机器需要先准备：

- Docker、NVIDIA驱动与容器GPU支持，以及显示会话；
- 算法OpenVINO/C++依赖环境（参考算法仓库Dockerfile和readme）；
- ROS 2 Jazzy + Bevy模拟器镜像（`ros2 bench`特性）；
- Sunshine桌面或适配现有显示方案，以及对应挂载、用户、服务和桌面命令。

[现有显示部署脚本](../docker/deploy-sunshine.sh)和[独立模拟器说明](../docker/daedalus/README.md)面向上述固定工作站；运行前检查路径、显卡、代理与挂载。Sunshine持久HOME、密钥和宿主机服务状态不随Git分发。

`./sim.sh build`要求桌面开发环境已存在。`./sim.sh build --component background`也要求本地 `sp_vision_dev:2024.6.0`。构建默认代理 `http://127.0.0.1:7890`；根据网络设置 `RM26_BUILD_PROXY`，直连可设为空。

## 已验镜像与重新构建

版本清单记录本机已验镜像ID及算法二进制SHA。`input-contract-runtime.json`是已验输入契约描述，运行时会检查镜像及算法摘要；新机器自行构建的产物不保证具有相同SHA，不能直接把摘要改成新值宣称验收通过。

正确yaw模型镜像通过10月29日的增量构建验证，依赖旧冻结builder及Cargo缓存。源码完整保存在模拟器仓库，但本轮未在空机器重新构建这些镜像。交给队友直接复现时，需要另行提供镜像包/镜像仓库，以及选择性原始实验资料；本次Git推送不包含这些大文件。

重新构建可以改变已有二进制；当前已验v3二进制可能与工作树实验候选源码不同。先冻结现有任务和记录，再建立新候选、构建与验收，不能继承旧二进制的成绩。

## Submodule部署兼容

新的桌面部署定义增加完整项目挂载 `/workspace/rm26`，并保留旧算法/模拟器挂载和对应Git元数据入口。启动脚本从自身位置推导主仓库路径，显示/用户/宿主机服务仍使用既有工作站约定。本次没有重建正在运行的Sunshine；它继续使用原有挂载。现有布局与标准新克隆均经过独立辅助容器的Git可见性检查。后续重建桌面时改用根工作区文件，见 [协作说明](SUBMODULES.md)。
