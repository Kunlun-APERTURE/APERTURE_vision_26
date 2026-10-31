# Daedalus普通模拟器入口

本目录管理现有工作站上的独立模拟器容器，用于手动浏览场景和ROS接口。视觉算法闭环、后台任务与评分请使用[主实验入口](../auto_aim/README.md)。

## 使用

从主仓库根目录执行：

```bash
bash docker/daedalus/manage.sh --help
bash docker/daedalus/manage.sh build
bash docker/daedalus/manage.sh start
bash docker/daedalus/manage.sh status
bash docker/daedalus/manage.sh check --duration 20
bash docker/daedalus/manage.sh logs --tail 100
bash docker/daedalus/manage.sh stop
```

这是固定工作站的维护入口：脚本仍使用`/home/rm/rm26`、Sunshine显示`:20`和已有镜像/显卡约定。`check`还包含当前工作站的RTX 5070 Ti检查；新机器需要适配，不是通用硬件探测安装器。

普通模式和联调模式共享`vision_simulator_26`容器名。切换前先结束后台任务，再停止当前模式；例如从联调切入普通模式时，先在主仓库运行`./sim.sh stop`。不要用两个入口交替管理正在执行的同一轮实验。

## 显示与容器

模拟器独立运行在宿主机Docker，窗口呈现到Sunshine桌面。启动前应已有桌面服务；`start`刷新显示认证、切到VT8并重建普通模拟器容器。切换到其它TTY可能导致Vulkan呈现停滞；SSH管理不需要切换TTY。

普通模拟器镜像为`daedalus:rm26-ros2`，容器手动启停。停止模拟器不停止Sunshine。重建前应取出容器内需要保留的截图等文件。

构建代理可用`RM26_BUILD_PROXY`覆盖，设为空可直连；`RM26_GPU_UUID`可指定显卡。首次构建需要系统包和Rust依赖。源码、资源或镜像改变后须重新构建并启动，不能继承旧镜像验收结论。

## 配置与接口

普通入口挂载模拟器原`config.toml`；联调入口另生成自己的运行配置，两者应分开理解。普通构建启用ROS 2及默认特性，集成实验构建另需`bench`。

主要接口包括压缩图像、相机内参、TF和`/rm_gimbal/cmd`控制消息；实际话题/采集频率以所选配置与运行状态为准，不能用历史空场景发布速率代替算法闭环性能。

`check`核对动态库、GPU与ROS采集；日志写入本目录被忽略的`logs/`。没有已运行容器时，应先启动再检查采集。显示认证仅从本机会话提取到私有`.runtime/`，不使用`xhost +`。

桌面固定操作入口可通过`install-desktop.sh`维护，要求既有本机SSH/桌面条件。完整工作站前提和重启关系见[部署说明](../../docs/DEPLOYMENT.md)。
