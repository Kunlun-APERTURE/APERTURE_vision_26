# 部署与工作站

## 先选择使用方式

|方式|准备与入口|
|---|---|
|使用现有工作站|保留已有镜像、桌面与运行产物，在宿主机`/home/rm/rm26`执行`sim.sh`|
|新机器获取源码|先配置GitHub读取权限，再递归克隆主仓库；submodule不自动安装依赖|
|独立使用算法/模拟器|在对应子仓库按其构建入口准备环境；组合运行仍需主仓库的桥接与配套接口|

当前主仓库为`Kunlun-APERTURE/APERTURE_vision_26`组织私有仓库，队友须有组织/仓库访问权限并在自己设备上配置GitHub认证。Git提交作者名称与登录认证是两回事。

## 已有工作站

当前部署采用Ubuntu宿主机、NVIDIA GPU与容器工具链、Docker Compose/Buildx。Sunshine容器`sp_vision_sunshine`提供GNOME显示和OpenVINO开发环境；模拟器、ROS桥接与评估器在宿主机Docker中运行，不使用嵌套Docker。

```bash
cd /home/rm/rm26
./sim.sh status
./sim.sh jobs service-status
./sim.sh jobs list
```

UI显示使用`:20`和VT8，切换TTY可能影响Vulkan呈现。后台可以慢于实时，但现有部署仍使用GPU/显示设施。

|组件|重启行为|
|---|---|
|Sunshine|由宿主机`rm26-sunshine`服务和既有容器重启策略管理|
|手动联调模拟器/桥接|通过`sim.sh`启停，不作为自动恢复任务|
|后台调度器|宿主机服务；重启后中断尝试留档、队列暂停，检查后用`jobs resume JOB_ID`恢复|

停止联调不会关闭桌面。实验运行期间不要重建镜像或替换其算法二进制；已冻结任务继续使用自己的产物。

## 桌面工作区

现有容器的真实源码挂载是`/workspace/sp_vision_25`和`/workspace/vision_simulator_26`；`~/workspaces/rm26-sim`是主仓库的独立同步副本。当前桌面打开`~/workspaces/RM26-三仓库.code-workspace`，不要将联调副本误当成完整递归clone，或在宿主机与副本同时修改主仓库。

新部署定义支持完整工作区`/workspace/rm26`及旧路径/Git元数据兼容入口，已在辅助容器核对Git可见性。它尚未应用到正在运行的Sunshine；需要安排容器重建后，才使用完整挂载中的根`RM26.code-workspace`。

## 新机器前提

新机器需要另行准备Docker、NVIDIA驱动与容器GPU支持、显示会话、算法OpenVINO/C++依赖环境，以及ROS 2 Jazzy/Bevy模拟器镜像。集成模拟器须启用`ros2 bench`特性。

本仓库当前没有经过空机器验证的一键安装流程。Sunshine持久HOME、密钥、宿主机服务状态和`docker/sunshine`本地构建上下文不随Git提供。[桌面部署脚本](../../../docker/deploy-sunshine.sh)和[普通模拟器入口](../../../docker/daedalus/README.md)供维护已有工作站时使用，运行前核对路径、显卡、代理与挂载。

`./sim.sh build`要求已有桌面开发环境；`./sim.sh build --component background`也依赖本地`sp_vision_dev:2024.6.0`。构建默认代理为`http://127.0.0.1:7890`，可设置`RM26_BUILD_PROXY`，直连时设为空。详细算法组件检查见[自检说明](../../../docs/CHECKS.md)。

## 镜像与验收身份

`input-contract-runtime.json`核对已验算法二进制和专用模拟器/评估器镜像摘要。新构建产物通常不会具有相同摘要；不能直接改验收描述来继承旧资格。应建立新候选、记录产物并重新验收。

参考yaw模型镜像通过增量构建验证，依赖既有冻结builder与Cargo缓存；尚未在空机器完成全链路重建。需要队友直接复现实验时，应另行分发镜像及所需原始证据。源码SHA或镜像digest只能标识内容，不是下载地址。
