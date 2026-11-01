# 工作站运维

面向维护者。队员第一次运行请用 [快速开始](QUICKSTART.md)，PR 作者的选择与批准流程只在 [贡献指南](../CONTRIBUTING.md) 说明。下面仓库命令均从主仓库根目录执行，系统路径是当前工作站实例，不应原样作为其他机器默认值。

## 依赖包分发

实际获取位置统一在 [快速开始](QUICKSTART.md#获取依赖包) 维护。当前只导出清单中的一个统一镜像，使用新目录：

```bash
python3 tools/dependency_bundle.py export exports/aperture-new
python3 tools/dependency_bundle.py check exports/aperture-new
```

导出的 `images.tar` 和 `manifest.json` 必须一起交付。历史离线包的 import/check 仍兼容原清单，不修改历史镜像或实验记录。

## 镜像自动发布

Release 日期、镜像清单、发版步骤和失败恢复统一见 [版本与镜像发布](RELEASES.md)。日常只使用“发布统一镜像”工作流；首次迁移记录见归档。

## 维护构建

本节面向重建统一环境的维护者。新人从快速开始拉取最终镜像，无需取得下列旧构建材料。当前 Dockerfile 从修正云台模拟器继承平台程序，并加入算法和开发依赖：

```bash
docker build --network host --progress plain \
  --file docker/auto_aim/Unified.Dockerfile --tag aperture_vision:26-11-1 .
```

本命令重建当前环境，不代表发布了新版本。维护构建需已有 `sp_vision_dev:2024.6.0`、`daedalus:rm26-reference-yaw-corrected-20261029` 和 `rust:1.98-slim-bookworm`；前两者在 [历史 26-11-01 Release](https://github.com/Kunlun-APERTURE/APERTURE_vision_26/releases/tag/26-11-01) 的五镜像离线附件，Rust 来自 Docker Hub 官方镜像。历史导入方法见该 Release，仅作为重建来源，不进入新人路径。

平台源码改变时必须先更新并验收模拟器基础产物；仅修改 Rust 源码或运行算法构建命令，不会替换镜像里的模拟器程序。候选环境仍需通过 [组件检查](CHECKS.md)、同镜像闭环与公开分发校验，不能直接覆盖已发布日期标签。当前验证结果保存于 release/20261101-unified/。

## 本地实验调度器

普通开发者可前台运行 daemon，见快速开始。长期工作站安装宿主服务：

```bash
./sim.sh jobs install
./sim.sh jobs service-status
./sim.sh jobs list
```

install 会安装并启动 systemd 服务，要求已有 Docker 和对应镜像。CPU 任务仅检查主存和磁盘余量，NVIDIA 任务还检查显存；NVIDIA 查询缺失不会阻塞 CPU 队列。不要在任务执行中重启服务；重启中断任务会留档并暂停，由维护者检查后恢复。

同一主机只运行一个实验调度器，不在多个仓库克隆中同时启动 daemon。当前 worker 容器名和端口按 slot 编号分配，多套调度器会相互影响；切换验证目录前先等任务结束并停止上一套调度器。

## GitHub App 与 PR 服务

App 归组织管理，只授权主仓库；关闭 webhook，使用主动轮询。Repository permissions：Contents read；Actions、Checks、Pull requests write；Metadata read 自动添加。私钥保存在仓库外，目录700、文件600，服务账户可读，不向聊天或日志输出。

配置模板见 [config.example.json](../docker/auto_aim/pr_service/config.example.json)，systemd 模板见 [服务文件](../docker/auto_aim/pr_service/aperture-pr-experiments.service)。新部署先保持 enabled=false，填写 App ID、Installation ID、私钥、scratch/logs 路径，并替换 unit 中的用户名与仓库路径。

当前工作站使用 `/home/rm/.config/aperture-experiments/config.json`，连接自检：

```bash
PYTHONPATH=docker/auto_aim python3 -m pr_service.service check --config /home/rm/.config/aperture-experiments/config.json
systemctl status aperture-pr-experiments.service --no-pager
journalctl -u aperture-pr-experiments.service -n 50 --no-pager
```

`check` 只验证认证及仓库读取，不替代权限、workflow 和完整实验验收。“实验申请”只保存批准请求，工作站异步执行后通过“实验结果”回传文本；触发条件和用途见 [工作流职责](RELEASES.md#工作流职责)。批准请求 artifact 保留7天，结果 artifact 保留30天。当前已完成一次真实批准到成绩回传与清理的烟测，不代表所有故障场景均已线上认证。

## 网络与调度

systemd 不继承终端代理。当前主机用 drop-in 配置 HTTP/HTTPS 代理 `http://127.0.0.1:7890`，NO_PROXY 包含 localhost、127.0.0.1、::1；其他机器按实际网络配置。代理不是所有部署必需项。

API GET 和 artifact 下载最多三次重试，写请求不自动重放。批准复核每30秒一次；审批 artifact 按 review ID 缓存在进程内存。遇到 TLS EOF 时先核对服务进程代理与路由，再看 Check 和日志，勿把历史红色 Check 当成新任务仍在执行。

服务与本地调度共享资源锁，PR 默认串行。公平调度、分页扩展以及平台变更专用回归仍待完善；当前查询单页最多100条。

## 远程清理与日志

远程任务结束或失败后删除临时源码、构建、原始图像、遥测、结果、容器、任务标签与临时缓存，不发布到 Sunshine。只长期保留文本日志：每任务最多2MiB，总量超过10GiB停止接收新任务，由维护者处理。当前 scratch/logs 位于 `/home/rm/.local/state/aperture-pr/`。

上传失败也立即清理，不等待结果重传。启动时清理中断残留，不自动重跑实验。只读目录先解除任务树只读；root-owned 证据通过只挂载该目录的受限清理容器删除，不跟随符号链接，不执行全局 prune。

审批、重跑和结果解读见贡献指南。服务故障要区分请求未批准、CI失败、源码构建失败、实验无效、回传失败与清理失败；每项保留明确日志，不能用低分触发自动补样。

## 本地执行链路验收

该命令由维护者本地发起，用真实源码和 GPU，只替换 GitHub 批准/回传。先将两个 SHA 占位值替换为可获取的提交：

```bash
python3 tools/pr_acceptance.py --repository Kunlun-APERTURE/APERTURE_vision_26 --base 基线SHA --head 候选SHA --output exports/pr-acceptance-new --attempt 1
```

相同提交验证零差异；`--upload-failure` 注入上传失败。新的测试使用新 output/attempt。该本地验收的汇总由维护者保留，不改变远程只留日志的规则。

## 可选桌面联调

现有工作站 Sunshine 容器为 `sp_vision_sunshine`，显示会话使用 `:20`／VT8。桌面挂载和同步副本不等于新队员完整递归克隆，不在两份源码同时修改。UI 启停及底层工具见 [联调参考](../docker/auto_aim/README.md)。后台实验无需这套桌面配置。
