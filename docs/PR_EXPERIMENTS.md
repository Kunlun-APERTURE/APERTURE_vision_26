# 审查批准后的服务器实验

作者在 PR 模板选择实验项目，取消默认 none；有写权限的非作者 Review → Approve 当前提交后，request workflow 生成选择快照。工作站只接受最新批准、相同 head/base 与选择，并要求 development-tests 成功。选择变化或新提交需要重新批准。当前不自动合并、不按得分自动否决。

基线为批准时目标分支，候选为目标分支与 PR 的临时合并；仅支持算法 submodule 变更及文档等非执行文件。模拟器、评分器或框架变更目前报告 platform change，不执行算法评分；平台专用回归尚待补充。

服务主动轮询 GitHub，不开放公网端口，不注册宿主 self-hosted Actions runner。GitHub App 需 Contents read、Actions/Pull requests/Checks write（Actions write 用于结果 workflow dispatch）。创建并安装 App 后，将 PEM 和配置放在仓库外，由服务账户独占读取。不要将凭据加入 Git。

```bash
PYTHONPATH=docker/auto_aim python3 -m pr_service.service check \
  --config /etc/aperture-experiments/config.json
```

模板配置 `docker/auto_aim/pr_service/config.example.json` 默认 disabled；确认依赖与权限后再启用。systemd 模板同目录，安装时替换用户和路径。当前实现以轮询批准记录和 request workflow artifact 为请求来源，artifact 7天过期后需重新批准。第一版单页最多100条，超出需要分页扩展。

构建由临时容器运行，PR 代码不获得 Docker socket、GitHub 凭据或宿主工作区。服务使用共享资源锁与本地后台调度协调；既有调度服务需重启载入锁支持。PR 任务优先等待已有本地任务，持续本地队列下的公平调度仍需后续完善。Docker 不是运行恶意代码的完整隔离边界，审查批准者需信任待执行代码。

本机每个任务只长期保留最大2MiB文本日志；超过10GiB日志总量停止接收新任务。运行期间 scratch 保存临时 checkout、构建和证据。结束、失败、取消及上传失败均清理，源日志复制到长期日志目录。启动时清理中断任务，不自动重跑。清理按任务标签和专用根目录执行，不调用 Docker system prune。

只向 GitHub 发成绩和截断文本日志；不发图像/遥测/视频。Check 与 PR 评论包含提交和逐场景差值，result workflow 把文本保存为30天 artifacts。GitHub 回传失败后立即删除结果，不等待重传；可依据文本日志手工补报失败状态。

服务必须使用工作站安装的可信框架/场景/评分器。当前还需真实 GitHub App、测试 PR、权限及故障演练完成后才能正式启用，不能以 mock 测试代替线上验收。两名队员的真实 PR 验收尚未进行。

部署状态：功能代码与本地测试已提供，默认未启用。上线前还必须配置 GitHub App、合入默认分支上的三个 workflow，并用真实测试 PR 验证 artifact 请求、批准撤销、结果回传、断网和服务重启清理。当前尚未完成此线上验收。服务读取的批准与 request artifact 必须同时存在，workflow 本身不负责运行候选代码。

## GitHub App 配置步骤

组织 Settings → Developer settings → GitHub Apps 创建专用 App；关闭 webhook（服务主动轮询）。权限：Contents read，Actions/Pull requests/Checks write。仅安装到 APERTURE_vision_26。生成私钥后保存在工作站 `/etc/aperture-experiments/app.pem`，服务账户可读，其他用户不可读；不通过聊天或 Git 传递私钥。

复制 config.example.json 到 `/etc/aperture-experiments/config.json`，填写 App ID、Installation ID、路径，保持 enabled=false。运行 `pr_service.service check` 验证连接。workflow 合入默认分支并完成测试 PR 后再启用 service。

同一批准的重复事件不重跑；失败后新 Review Approve 可以创建新尝试。已成功回传的同版本请求不因另一位审查者批准重复运行。Check 在排队、运行、完成阶段更新同一个 ID。

## 本地真实执行链路验收

`tools/pr_acceptance.py` 直接调用服务的 Executor 和 Lifecycle，仅替换 GitHub 批准/回传，实际拉取源码、分别构建 base/candidate、运行静止及进阶场景并清理。指定真实可获取的提交：

```bash
python3 tools/pr_acceptance.py --repository Kunlun-APERTURE/APERTURE_vision_26 \
  --base <提交SHA> --head <提交SHA> --output exports/pr-acceptance --attempt 1
```

相同 base/head 用于零差异检查；`--upload-failure` 可注入回传故障。新尝试使用不同 attempt。工具产生的本地验收输出属于维护者本地发起实验；服务端远程任务仍仅保留日志。

只读封存目录和 root-owned 证据会阻止普通账户删除。清理先仅对任务树解除只读；权限不足时使用不联网的短生命周期 root 清理容器，只挂载该任务目录，授予 DAC_OVERRIDE/FOWNER，不跟随符号链接，不挂载 Docker socket。正常与上传失败路径已用真实 root-owned 只读夹具验证。

工作站网络：systemd 不自动继承登录终端代理。当前服务通过本机 drop-in 显式设置 HTTP_PROXY/HTTPS_PROXY 为 `http://127.0.0.1:7890`，NO_PROXY 保留 localhost；该值属于机器配置，不作为团队默认网络要求。批准复核默认每30秒一次，已核验的审批 artifact 按 review ID 缓存在服务内存中；PR 状态、提交和批准记录仍定期重新查询。API GET 与 artifact 下载均使用最多三次有限重试，写操作不自动重放。
