# 同步时序与输入契约

实时模式随现实时间运行；lockstep按逻辑时间推进物理、图像和命令，计算耗时与仿真时间分开。等待GPU/算法时不应多走物理步，因此可以牺牲实时速度换取稳定的实验输入。

日常示例见 [README](README.md)。当前研究使用 `ui-aligned-v7-clock`；目录中旧v1–v6时序保留给历史研究复现，不表示当前推荐配置。

`--input-contract aligned-v1`必须同时指定`--detector-history session`和uint32 `--sensor-seed`。默认暖机处理30张连续原生采集帧，暖机不计分；物理阶段边界控制测量和开火，完整会话证据强制归档。场景种子与图像噪声种子不同，均应冻结。

运行入口通过`input-contract-runtime.json`核对算法二进制和专用模拟器/评估器镜像。该文件是工作站已验产物描述，不能随意修改摘要来适配新构建。新yaw模型使用独立镜像选择；默认legacy不被替换。

`session-evidence/`保存暖机、测量、结算的输入与结果及摘要；`session-evidence-audit.json`为完整性核对。完整会话证据和可重复性不等于实物拟真，也不自动认证UI命中统计等价。实际显示、传感器时间、返回链路干预和算法改变应分别比较。

本轮yaw部件与后台静止复现结论见 [阶段状态](../../docs/STATUS.md)。通用lockstep尝试预算180秒；本阶段预登记短测另外要求完整尝试≤40秒。

公开CLI当前仅接受 `hf_static_8`、`hf_uniform_3`、`hf_turn90_3`、`hf_spin_neg` 的lockstep提交；`hf_static_5`等研究专用条件不能直接套用该入口。其它基础/进阶项目的研究控制器另有冻结与复现检查，不代表已开放通用CLI支持。
