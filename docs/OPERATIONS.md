# 工作站运维

面向维护者。队员第一次运行请用 [快速开始](QUICKSTART.md)，PR 作者的选择与批准流程只在 [贡献指南](../CONTRIBUTING.md) 说明。下面仓库命令均从主仓库根目录执行，系统路径是当前工作站实例，不应原样作为其他机器默认值。

## 依赖包分发

实际取得位置由 [快速开始](QUICKSTART.md#获取依赖包) 统一维护。包有 images.tar 与 manifest.json，分发前核验：

```bash
python3 tools/dependency_bundle.py check exports/team-dependencies-20261031
```

重新导出需新目录，避免覆盖已有归档：

```bash
python3 tools/dependency_bundle.py export exports/team-dependencies-new
```

当前镜像包括构建基础、算法运行、输入链路、评分和修正云台镜像。归档及 manifest 必须一起分发，镜像 digest 不是下载地址。新分发位置确定后只修改快速开始中的来源表。导出/同机导入已验，完整新系统安装未验。

## 镜像自动发布

镜像目录由 [依赖清单](../release/dependencies.json) 统一记录。本地名称兼容现有 CLI，reference 固定 GHCR digest，image_id 固定 OCI 索引，manifest_id 和 config_id 兼容不同 Docker 版本报告的本地身份。更新依赖时先完成构建与实验验收，再更新清单；源码或文档发版可以复用同一组镜像。

[发布工作流](../.github/workflows/dependency-release.yml) 在 Release 发布时触发，镜像标签与 Release 日期一致，例如 `26-11-01`。工作流检出该标签，校验全部来源，复制到本版本标签，匿名下载验证全部镜像层后，才向 Release 附加 `dependencies.json`。不重新编译算法，也不替代 GPU 实验验收。

工作流只申请镜像写权限和 Release 附件写权限，使用临时 GITHUB_TOKEN；队员拉取公开镜像无需登录。同一日期标签已指向其他镜像时直接失败，不覆盖；相同内容允许重试。已有 Release 清单内容不同时也拒绝覆盖。

首次迁移使用 [迁移工作流](../.github/workflows/dependency-bootstrap.yml)：从 `26-11-01` 的离线附件分片重组归档，核对分片与完整 SHA256，再推送已验收镜像。迁移清单在 [dependencies-bootstrap.json](../release/dependencies-bootstrap.json)。OCI 索引和 digest 全程保留，后续发版直接从 GHCR 复制。

GHCR 包首次创建默认私有。维护者需在组织 Packages 页面将五个包设置为 Public，确认关联本仓库，并允许本仓库的 Actions 写入。公开下载未验证通过前，快速开始继续使用离线包，不把 GHCR 视作可用来源。

失败先查看“发布依赖镜像”日志。修复网络或包访问设置后，从 Actions 页面手动运行，输入同一 Release 日期即可；无需移动 Git 标签或重新发布 Release。首次发布前的旧标签缺少发布工具，应通过迁移工作流处理，不使用常规重试入口。

本地已安装 Skopeo 的维护者可验证匿名下载：

```bash
python3 tools/dependency_registry.py verify-public --download
```

新人只需要 Docker，在线拉取入口为 `python3 tools/dependency_registry.py pull`；仅在公开下载验收完成并更新快速开始后作为主流程使用。离线导入入口继续保留。

## 本地实验调度器

普通开发者可前台运行 daemon，见快速开始。长期工作站安装宿主服务：

```bash
./sim.sh jobs install
./sim.sh jobs service-status
./sim.sh jobs list
```

install 会安装并启动 systemd 服务，要求已有 Docker 和对应镜像。不要在任务执行中重启服务；重启中断任务会留档并暂停，由维护者检查后恢复。

## GitHub App 与 PR 服务

App 归组织管理，只授权主仓库；关闭 webhook，使用主动轮询。Repository permissions：Contents read；Actions、Checks、Pull requests write；Metadata read 自动添加。私钥保存在仓库外，目录700、文件600，服务账户可读，不向聊天或日志输出。

配置模板见 [config.example.json](../docker/auto_aim/pr_service/config.example.json)，systemd 模板见 [服务文件](../docker/auto_aim/pr_service/aperture-pr-experiments.service)。新部署先保持 enabled=false，填写 App ID、Installation ID、私钥、scratch/logs 路径，并替换 unit 中的用户名与仓库路径。

当前工作站使用 `/home/rm/.config/aperture-experiments/config.json`，连接自检：

```bash
PYTHONPATH=docker/auto_aim python3 -m pr_service.service check --config /home/rm/.config/aperture-experiments/config.json
systemctl status aperture-pr-experiments.service --no-pager
journalctl -u aperture-pr-experiments.service -n 50 --no-pager
```

`check` 只验证认证及仓库读取，不替代权限、workflow 和完整实验验收。main 必须包含 “开发检查”“实验申请”“实验结果”三个工作流；批准请求 artifact 保留7天，结果 artifact 保留30天。当前已完成一次真实批准到成绩回传与清理的烟测，不代表所有故障场景均已线上认证。

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
