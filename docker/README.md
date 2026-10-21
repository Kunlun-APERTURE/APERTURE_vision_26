# 本机 Docker 开发环境

目标：Ubuntu 24.04 / AMD Ryzen 7 9700X 宿主机，使用项目原有的
Ubuntu 22.04 / OpenVINO 2024.6 Dockerfile 创建开发环境。

部署已完成：Docker Engine 29.9.1，开发镜像已构建，基础容器 `sp_vision_dev` 保留为停止状态。
环境核对：Ubuntu 22.04.5、GCC 11.4、CMake 3.22.1、OpenCV 4.5.4、
OpenVINO 2024.6、Eigen 3.4、Ceres 2.0。
OpenVINO 能识别 `CPU` 设备及 AMD Ryzen 7 9700X 型号。
依赖核对记录见 `environment-info.txt`。
`auto_aim_test` 已编译并使用 CPU 跑通 620 帧，结果位于项目
`build/demo/annotated.avi`，日志位于本目录 `demo.log`。

## 安装 Docker Engine

在本机终端执行（需要输入 sudo 密码）：

```bash
sudo bash /home/rm/rm26/docker/setup-host.sh
```

脚本使用 Docker 官方 apt 源，安装 Engine、Buildx 和 Compose，启用服务，
将 `rm` 加入 `docker` 组，并配置 Docker 服务使用现有代理
`http://127.0.0.1:7890`。docker 组提供 Docker 管理权限，等同于宿主机 root 权限。
代理配置位于 `/etc/systemd/system/docker.service.d/rm26-proxy.conf`。

安装脚本会检查 Ubuntu 版本和冲突软件包；不会自动卸载已有容器软件。
当前登录会话不会立即获得新的组权限，以下命令通过 `sg docker` 使用新组。
重新登录后通常可直接使用 docker。

## 构建镜像并创建容器

```bash
sg docker -c 'bash /home/rm/rm26/docker/deploy.sh'
```

镜像：`sp_vision_dev:2024.6.0`；容器：`sp_vision_dev`。
构建时从 `/home/rm/rm26/sp_vision_25/Dockerfile` 生成本地
`Dockerfile.generated`，仅将 Ubuntu 官方 apt 下载地址替换为宿主机已经使用的
阿里云 Ubuntu 镜像源；项目 Dockerfile 保持原样。
构建过程使用宿主机网络访问已有代理。容器也使用宿主机网络，便于后续
通过本机 UDP 9870 端口向 PlotJuggler 发送数据。
代理必须保持运行，Docker 才能通过该配置拉取新镜像。

仓库挂载到容器内 `/workspace/sp_vision_25`，容器使用宿主机用户 UID/GID，
生成文件归宿主机用户所有。容器 HOME 为 `/tmp/sp-vision-home`。
容器不配置开机自动启动；启动后持续运行，直到手动停止。

## 进入和停止

```bash
sg docker -c 'bash /home/rm/rm26/docker/enter.sh'
sg docker -c 'docker stop sp_vision_dev'
sg docker -c 'docker start sp_vision_dev'
```

进入脚本会加载 OpenVINO 的环境。项目的相对资源路径要求从容器内仓库根目录运行。

## 编译与离线运行

进入容器后执行：

```bash
cmake -S . -B build -DOpenCV_DIR=/usr/lib/x86_64-linux-gnu/cmake/opencv4
cmake --build build --target auto_aim_test -j8
./build/auto_aim_test --headless=true --end-index=619 --output=build/demo/annotated.avi
```

OpenVINO 的 `setupvars.sh` 会将 `OpenCV_DIR` 指向镜像自带的精简 OpenCV 4.8，
其中缺少项目所需的 DNN 模块。进入脚本会重新设置这个变量，编译命令也显式
选择已安装的完整 OpenCV 4.5.4，避免缓存指向错误路径。

当前素材可解码 687 帧，但姿态文件只有 620 行，因此运行范围明确限定为 0 到 619。
视频头的帧数元数据为 0，不能用它来判断素材实际帧数。
导出结果为 720×540、30 FPS 的 MJPEG AVI，已确认 620 帧均可解码。
黄色标记为筛选后的观测装甲板，绿色为估计的装甲板位置，红色为有效瞄准位置。
`--headless=true` 关闭图形窗口；去掉该参数可在配置好的图形会话中运行。

## Ubuntu GNOME 桌面

已增加 Sunshine GPU 桌面容器 `sp_vision_sunshine`，详情见
[Sunshine 操作说明](sunshine/README.md)。此容器包含开发环境、Ubuntu 默认 GNOME 桌面与 Sunshine。
实体显示器及键鼠交给容器，远程 Moonlight 共享同一会话，使用 NVIDIA NVENC 编码。
Sunshine 桌面已设置随宿主机开机启动；模拟器和原始开发容器继续手动启动。
原始 `sp_vision_dev` 容器仍保留，可按需启动；两个容器共用源码目录和编译产物。
相机和云台设备尚未接入。本目录位于项目 Git 仓库之外。

## Daedalus 模拟器

`vision_simulator_26` 使用宿主机上的独立 NVIDIA Docker 容器，包含 ROS 2 Jazzy，
窗口显示到上述 Sunshine 桌面。构建、启动、检查和停止命令见
[Daedalus 部署说明](daedalus/README.md)。运行时须保持 Sunshine 的 VT8 活动。

参考：[Docker Ubuntu 安装](https://docs.docker.com/engine/install/ubuntu/)、
[Docker 服务代理](https://docs.docker.com/engine/daemon/proxy/)。
