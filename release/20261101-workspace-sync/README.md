# 工作区与运行服务同步验收

2026-11-01 将工作区从 `feat/cpu-rendering` 的 `45d12df` 切换并快进至 main 的 `b83dcb3`。算法与模拟器 gitlink 均匹配，统一镜像保持原 digest。两个服务在任务空闲时依次停止，重载 systemd 后使用当前源码启动；PR 服务在冒烟结束后恢复，认证读取检查通过。

`rm26-experiments.service` 与 `aperture-pr-experiments.service` 均为 active，PID 已更新，NeedDaemonReload=no；调度器心跳包含新版本的 nvidia_healthy。四个历史暂停任务保持暂停，无运行任务被中断。现场信息见 [同步前记录](before.json)，结果见 [验收数据](validation.json)。这是本次检查时的状态，不代替实时服务监测。

## 本次实际闭环

在统一镜像中重新构建 B1，固定 E1、轨迹种子 2026102207、传感器种子 2026110103，串行运行：

|渲染|场景|命中／发射|完整耗时|硬预算|40 秒成本|
|---|---|---:|---:|---:|---|
|NVIDIA|静止 hf_static_5|28/28|17.71 秒|180 秒|通过|
|NVIDIA|平移 hf_uniform_3|15/16|17.40 秒|180 秒|通过|
|CPU|静止 hf_static_5|28/28|526.20 秒|900 秒|未通过|
|CPU|平移 hf_uniform_3|17/18|459.64 秒|900 秒|未通过|

四轮 complete、all_evidence_valid 均为 true，会话审计通过，已发射弹丸均有终结记录。CPU 的模拟器、桥接、算法、评分四类容器检查均使用 runc 且无 GPU 设备请求；三个常驻容器内 /dev/nvidia* 和 /dev/dri 均不可见。渲染日志确认 [llvmpipe / Cpu / Vulkan](cpu-renderer.log)。设备采样取自 CPU 静止轮次。

CPU 和 NVIDIA 是分别验收的运行路径，不作跨后端算法收益比较；CPU 超出 40 秒成本目标。此次使用有 NVIDIA 硬件的工作站进行 CPU 容器隔离验收，不属于空机器安装认证。

完整本地构建日志、任务 contract、报告和一次性验收脚本位于 `exports/workspace-sync-20261101T105006Z/`；逐轮原始记录仍在 `docker/auto_aim/experiments/`，run_id 与证据索引哈希保存在 validation.json。

## 下一阶段交付

[任务规格](../../docs/NEXT_TASKS.md)已列出十项 Rust 失败的原因与修复验收要求，以及固定 E1 的 30 轮基础诊断加 2 轮重复检查。[场景选择](diagnostic-plan-selection.json)已通过 CLI 只读核对。本轮未修改算法／模拟器代码，尚未执行这 32 轮诊断，也未触发新 PR 实验或发布镜像。
