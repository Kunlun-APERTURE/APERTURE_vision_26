# 模拟器回归修复与候选验收

原模拟器 `778c0c8` 的 ROS/bench 单测失败已复现：283 通过、10 失败、1 忽略。修复后默认特性为 171 通过、0 失败、1 忽略；ROS/bench 为 295 通过、0 失败、1 忽略。保留的忽略项 `systems::gimbal_pid::tests::corrected_angle_loop_csv` 是手动输出 CSV 的组件诊断，未新增忽略项。

## 修复内容

- 成像插件允许独立加载：ROS 传感器控制收发资源缺任一个时不消费重置请求，完整收发路径继续支持 Idle 重置和重复请求应答。
- 新增收发资源缺失、运行阶段禁止重置、重复 reset_id 幂等、旧渲染帧隔离等回归。
- 仓库配置测试从编译时固定的 config.toml 加载，正确验证 publish_hz=0、压缩发布启用、raw 发布关闭；不受运行目录影响。
- 修复 Base 索引自相矛盾的旧断言，验证调试序列唯一性与索引往返；工程机器人装甲预期更新为 Two。
- 大装甲后缀 `3` 从 Base 修复为 Three。[GLB 几何投影](hero-label-geometry.png)确认 `1_B_3` 是数字 3，`1_B_B` 是独立基地图案；不是单纯修改测试来匹配错误映射。

源码补丁见 [source.patch](source.patch)，构建、二进制和候选镜像身份见 [build-provenance.json](build-provenance.json)。完整原始测试日志在本机 `exports/simulator-regression-20261101/`。独立回归入口为 `tools/test-simulator.sh`，命令见 [组件检查](../../docs/CHECKS.md)。

## 验收边界

候选标签为 `aperture_vision:simulator-regression-20261101`，基于现有统一镜像替换本次编译的模拟器与对应动态库。原发布镜像、Release 标签和 `release/dependencies.json` 保留，候选镜像没有公开发布。

C++ 九个目标构建与六个组件回归通过；Python 70 项测试和 benchmark 9 项检查通过，覆盖率语句 83.85%、分支 74.04%。候选 GPU 十个基础场景均完成且证据有效，命中／发射与原单种子验收逐场一致。这不等于逐帧图像或实车性能认证。

CPU 两项候选闭环均通过：静止 28/28，541.30 秒；平移 17/18，457.08 秒。会话审计与所有发射弹丸结算通过，均在 900 秒硬预算内，超出 40 秒成本目标。四类容器使用 runc 且无 GPU 请求，渲染日志确认 Lavapipe CPU Vulkan。完整汇总见 [validation.json](validation.json)。

算法诊断始终复用原始冻结 B1/E1 包，不将平台修复的差异计为算法收益。
