# 无 GPU 后台流程验证

本轮验证单一 aperture_vision:26-11-1 镜像的 CPU 软件渲染路径，不改变镜像、模拟器分辨率、物理步长或逻辑输入时序。实现入口是 suite 的 --renderer auto|cpu|nvidia，操作说明统一见 [实验指南](../../docs/EXPERIMENTS.md#渲染模式)。

测试使用独立源码检出和调度器，算法在当前统一镜像内全新构建。主机物理上有 NVIDIA GPU，但模拟器、桥接、算法和评分全部明确使用 runc，不申请或映射 GPU；容器中 /dev/nvidia* 和 /dev/dri 均不可见。调度器与提交命令使用的 nvidia-smi 返回不可用，验证默认 auto 的回退。模拟器日志必须明确显示 Vulkan、llvmpipe 和 device_type: Cpu。

## 复现

先按 [快速开始](../../docs/QUICKSTART.md) 取得当前源码、依赖并构建算法，启动一套调度器。不要在同一主机的不同克隆中同时启动 daemon，worker 名和端口当前共享。

```bash
./sim.sh experiment suite --algorithm B1 --environment E1 --tier basic \
  --cases hf_static_5,hf_uniform_3 --sensor-seeds 2026110103 \
  --renderer cpu --output exports/cpu-verification-new --wait
```

物理无 NVIDIA 的主机可省略 --renderer，auto 会自动选 CPU。显式 cpu 在有 GPU 主机上同样不会查询或申请 NVIDIA。恢复使用相同参数和 --resume，记录的渲染后端不会静默切换。

CPU lockstep 硬超时为900秒；40秒成本继续独立报告，软件模式通常超出该成本。报告必须同时核对 complete、all_evidence_valid、实际发射数和资源证据，不能只因进程启动成功就宣称闭环可用。

## 已确认的静止结果

静止场景完成28次发射、28次命中，217帧会话输入与结果一一审计通过，最终封存判定 passed。完整耗时533.49秒，在900秒硬预算内，但超出40秒成本目标。精简证据见 [static-result.json](static-result.json)。平移场景的完整任务仍在验收，最终汇总以 validation.json 为准。

## 尝试与限制

前期保留了旧二进制缺库、旧180秒超时以及两套调度器容器名冲突的失败记录。最终结果使用单一调度器、新编译算法和900秒CPU预算，失败不删除、不计为有效样本。

本轮不认证 CPU 与 GPU 图像逐像素相等，也不进行跨后端算法收益比较。CPU 结果使用独立基线与并行资格标识，compare 会拒绝跨后端配对。模拟器桌面联调不属于本次后台无 GPU 验收范围。
