# Daedalus：宿主机 Docker 部署

模拟器在宿主机 Docker 中独立运行，使用 RTX 5070 Ti 和 ROS 2 Jazzy。
窗口显示到现有 `sp_vision_sunshine` 容器的 GNOME `:20` 桌面，可通过 Moonlight 操作。
本部署没有嵌套 Docker，也不要求宿主机安装 Rust 或 ROS 2。

## 使用

在宿主机终端执行：

```bash
bash /home/rm/rm26/docker/daedalus/manage.sh build
bash /home/rm/rm26/docker/daedalus/manage.sh start
bash /home/rm/rm26/docker/daedalus/manage.sh status
bash /home/rm/rm26/docker/daedalus/manage.sh check
bash /home/rm/rm26/docker/daedalus/manage.sh logs --tail 100
bash /home/rm/rm26/docker/daedalus/manage.sh stop
```

`start` 要求 Sunshine 桌面已启动；如果桌面已停止，先运行
`sudo systemctl start rm26-sunshine`。重启 Sunshine 后，再执行一次模拟器 `start`，
刷新显示认证并重建模拟器容器。`start` 可重复执行，会重新启动模拟器。
`start` 会把实体屏幕切到 Sunshine 的 VT8，运行时应保持这个终端活动。
如果切到宿主机其他 TTY，NVIDIA Vulkan 呈现可能卡住或导致模拟器退出；
再次执行 `start` 可切回桌面并重启模拟器。SSH 管理不需要切换 TTY。
Sunshine 桌面已设置开机自启；模拟器仍手动启动，没有配置开机自启或自动重启。
停止模拟器不会停止 Sunshine。

首次构建需要下载系统包和 Rust 依赖，使用现有代理 `http://127.0.0.1:7890`。
可通过 `RM26_BUILD_PROXY` 指定其他代理；设为空字符串可直接联网。
构建使用 Docker 自带 BuildKit 缓存。镜像为 `daedalus:rm26-ros2`，容器为
`vision_simulator_26`。`RM26_GPU_UUID` 可指定显卡，默认使用第一块 NVIDIA GPU。

## Sunshine 桌面入口

桌面提供四个可双击图标，对应的 `.bash` 文件保存在“项目脚本”文件夹：

- **启动模拟器**：刷新显示认证并启动模拟器，再次点击会重启。
- **停止模拟器**：停止模拟器，桌面和 Moonlight 继续运行。
- **编译模拟器**：构建 ROS 2 镜像，完成后再用启动入口应用新镜像。
- **模拟器状态与日志**：查看容器状态和最近 100 行日志。

每个入口会打开中文终端，操作完成或失败后按回车关闭；关闭日志终端不会停止模拟器。
编译入口禁止同时运行多个编译任务。桌面入口和连接设置随持久 HOME 保留，重启后仍可使用。

桌面通过本机 SSH 调用宿主机管理脚本，密钥只允许模拟器的固定操作，不提供任意命令或转发。
安装或修复桌面连接时，在宿主机以 `rm` 用户执行，无需 sudo：

```bash
bash /home/rm/rm26/docker/daedalus/install-desktop.sh
```

安装脚本保留其他 SSH 公钥，不重建桌面容器，不启动模拟器。

## 配置与接口

宿主机 `/home/rm/rm26/vision_simulator_26/config.toml` 只读挂载到容器
`/app/config.toml`，模拟器支持的热重载行为沿用项目实现。
工作目录为 `/app`，资产来自镜像。修改 Rust 源码或资产后重新 `build`、`start`。
模拟器 F2 截图保存在容器 `/app/screenshot-N.png`，可用 `docker cp` 取出；
重新创建容器会删除未取出的截图。

构建启用 `ros2` 和默认 `talos` 特性，不启用 `bench`、`ffmpeg`。
运行时 ROS 2 采集启用后，项目默认跳过 Talos 插件。
保持项目原有采集参数：1440×1080、JPEG、关闭阴影，不人为限帧。

- ROS_DOMAIN_ID：`0`
- RMW_IMPLEMENTATION：`rmw_fastrtps_cpp`
- 图像：`/image_raw/compressed`
- 内参：`/camera_info`
- 坐标变换：`/tf`
- 云台输入：`/rm_gimbal/cmd`，类型 `rm_interfaces/msg/GimbalCmd`

自瞄默认关闭，按 F5 切换。当前部署未连接 `sp_vision_25` 自瞄算法。
`check` 会检查动态库和 ROS 接口、确认 NVIDIA Vulkan 适配器，
并通过单独的订阅容器，在默认 20 秒内实际接收图像、相机内参和 TF；
可传入 `--duration 60`。

## 显示与日志

两个容器使用宿主网络，通过 Xorg 的 abstract Unix socket 连接，不挂载整个
Sunshine 容器文件系统。启动时只提取显示 `:20` 的认证记录，转换为 FamilyWild，
保存在权限为 0700 的 `.runtime/` 目录，认证文件权限为 0600，并只读挂载。
认证内容不会输出到日志，不使用 `xhost +`。

`logs/` 保存构建、自检、ROS 接收和运行日志。容器原始日志按 20 MB × 3 轮转。
模拟器进程使用镜像默认 root 用户；不授予 privileged 权限或 Docker socket。
未连接模拟器音频输出，启动时出现 `No audio device found` 不影响渲染和 ROS 发布。

## 本机验收记录（2026-10-19）

- 镜像约 2.45 GB，真实模拟器 ELF 48,005,312 字节；动态库和 ROS 自检通过。
- 日志确认 NVIDIA GeForce RTX 5070 Ti / Vulkan / 驱动 595.99.02。
- 已用键鼠事件验证 F2 截图、F3 视角切换、鼠标及 W 输入，画面证据在
  `logs/simulator-initial.png`、`logs/simulator-camera-switch.png`、`logs/simulator-input.png`。
- 停止后重新启动，通过独立 ROS 订阅容器连续接收 60 秒：内参 60 条、TF 34,749 条、
  1440×1080 JPEG 图像 9,580 帧，约 159.67 Hz。该速率来自本次场景，不代表其他场景或算法闭环速率。
- 验收结束时模拟器仍在运行，自动重启次数为 0，活动终端为 tty8。
- 原始结果保存在 `logs/restart-check.log` 和 `logs/validation.json`。

排障发现：停留在其他 TTY 时，X11 认证仍可成功，但 Vulkan 呈现会卡住并报
`DeviceLost`。已通过切回 VT8、重新启动解决，仿真源码和宿主驱动均未修改。
