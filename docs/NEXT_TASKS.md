# 下一阶段：可靠基线与算法诊断

2026-11-01 工作区同步后的任务规格。原始基线为主仓库 `b83dcb3`、算法 `d5d012d`、模拟器 `778c0c8`，发布镜像身份以 `release/dependencies.json` 为准。执行进展与验收证据见 [能力与限制](STATUS.md)；以下保留实验设计与验收标准。

顺序：工作站同步验收 → 模拟器回归问题归类与修复 → 固定环境下的算法诊断。当前 E1 保留用于复现；模拟器行为修改使用独立候选环境验收，算法收益只在同环境内比较。

执行结果：SIM-01 已通过 [候选回归验收](../release/20261101-simulator-regression/README.md)，ALG-01 的30轮基础实验、2轮重复与逐帧诊断已完成，见 [诊断结论与下一项任务](../release/20261101-algorithm-diagnosis/README.md)。后续先做A1关联拒绝回放，不能将“完成诊断”表述为已提升算法效果。

## SIM-01：消除模拟器十项已知测试失败

历史复现为 283 通过、10 失败、1 忽略，详见 [原始验收摘要](../release/20261101-unified/validation.json)。在统一镜像中沿用 `tools/build-simulator.sh` 的 ROS IDL 环境和独立 Cargo 缓存，从模拟器源码目录执行 `cargo test --locked --features 'ros2 bench' --bin daedalus`；保存工具链版本、源码 SHA、完整命令与日志。源码只读，缓存和编译产物写入 `/work`。先复现原失败集合，再修改。

|问题|已确认的代码证据|修复任务与正确行为|
|六项 `capture::effect::tests` 失败|测试只装载 MinimalPlugins 和成像插件；成像插件在 ros2+bench 下注册 `sensor::poll_control`，该系统强制要求未安装的 ROS 订阅／发布资源|让成像插件在无 ROS 通信资源时仍能独立运行；仅在收发资源均存在时处理传感器控制。保留完整仿真的重置应答，不能靠跳过六项测试验收|
|`shipped_config_publishes_compressed_only`|测试要求发布周期存在，而仓库配置明确 `publish_hz=0`，含义为每次捕获均发布|测试从编译时确定的仓库配置加载，验证 compressed=true、raw=false、publish_hz=0 及无节流周期；保留非零频率的独立语义测试，不为迁就旧断言降低发布频率|
|`debug_sequence_and_indexes_keep_legacy_order`|同一测试先要求 Base 索引为 6，随后又要求为 8；序列长度为 8|删除矛盾预期，测试序列唯一性及各元素索引往返；Base 对应索引 6|
|`robot_configs_preserve_legacy_armor_values`|工程机器人配置为 Two，旧测试要求 Sentry|将工程机器人预期明确为 Two，并核对英雄／步兵配置；用配置语义命名测试，避免继续承诺错误的 legacy 值|
|`sticker_slot_tables_keep_asset_suffixes`|大装甲后缀 `3` 当前映射到 Base，后缀 `B` 也映射到 Base；旧测试期望 `3` 对应 Three。HERO.glb 中存在独立的 `1_B_3` 与 `1_B_B` 网格，没有嵌入图片|核对 GLB 几何图案与选择路径，保存资产证据。若确为数字 3，修复映射为 Three；若资产命名与图案不符，单列资产修复任务，禁止仅更改断言掩盖语义问题|

变更入口集中于模拟器 `src/capture/effect.rs`、`src/capture/sensor.rs`、`src/config.rs` 及装甲／机器人配置测试。传感器缺资源检查需要覆盖：都缺、仅缺发布端、仅缺订阅端、完整收发；完整路径验证 Idle 阶段重置应答、重复 reset_id 幂等、旧帧不能推进新会话，以及非 Idle 不重置。

**验收：** 原十项失败全部消除，无新增失败；现有忽略项记录名称与理由，不能以新增 ignore 达标。运行默认特性及 ros2+bench 两种测试配置；主仓库 Python 检查通过，`tools/validate-ubuntu24-components.sh` 的九个构建目标与六个回归程序通过。

运行代码或资产变化后，构建独立候选模拟器镜像并记录 digest、源码和构建命令，不覆盖已发布日期标签。候选环境须通过 GPU 十个基础场景及 CPU 静止／平移冒烟，核对图像、传感器重置、发射结算和标签。报告平台差异，不将新旧环境成绩解释为算法增益。通过后再更新主仓库 gitlink 和环境清单；先提交子模块，再提交主仓库。公开发布另按发布流程进行。

## ALG-01：固定 E1 的多种子诊断

使用当前 B1/R4 产物、配置与 E1 镜像；显式指定 NVIDIA，默认串行。开始前记录二进制／配置／镜像哈希，确认没有任务争用。固定轨迹种子 `2026102207`，传感器种子 `2026110101,2026110102,2026110103`；传感器种子不代表路线变化。

在调度器正常运行后，从主仓库根目录执行以下命令；输出目录必须不存在，重做使用新名称：

```bash
./sim.sh experiment suite --algorithm B1 --environment E1 --tier basic \
  --trajectory-seed 2026102207 --sensor-seeds 2026110101,2026110102,2026110103 \
  --renderer nvidia --parallelism 1 --output exports/algorithm-diagnosis-basic --wait

# 两个独立的同种子重复，分别与上面的原轮次配对检查重复性。
./sim.sh experiment suite --algorithm B1 --environment E1 --tier basic \
  --cases hf_static_5,basic_move3_spin_pos --trajectory-seed 2026102207 \
  --sensor-seeds 2026110101 --renderer nvidia --parallelism 1 \
  --output exports/algorithm-diagnosis-repeat --wait
```

共 30 个基础轮次和 2 个重复轮次。所有失败和低分都保留；无效实验优先定位输入、时间或资源问题，恢复时保留原 contract，不能只保留重跑后的好结果。

**分析与输出：**

1. 每场景／种子汇总发射数、精确命中率、中心加权得分／秒、发射／秒、命中／秒、实际速度／角速度／距离与运动阶段。汇总分子分母，同时保留逐种子值；零发射命中率保持 N/A。
2. 沿图像检测 → 位姿有效性 → 跟踪／切板 → 瞄准 → fire_advice → 桥接授权 → 实际发射 → 接触结算，生成分阶段漏斗。逐项标明分母，不能将建议帧数等同于发射数。
3. 当前 B1 启动参数是 `tracking_fire_v3`；`blocked_reason` 仅在启用 v4 prediction gate 时输出。优先读取现有 `fire_check` 的 valid、pose_valid、aligned、stable_time_s、eligible、discontinuity 等字段离线归因，缺失记为 unknown；不因缺少 blocked_reason 切换算法 profile。复合不满足条件分别计数，并单列首次阻断分类，避免把顺序归因当因果结论。
4. 重点分析 `basic_move3_spin_pos/neg` 和 `basic_move4_spin_pos/neg`，以静止、平移和原地旋转作为参照。每个重点场景选加权得分／秒最低的有效轮次（同分取较小 seed）做逐帧复核；所有异常样本另列。目标真值只用于离线误差分析。
5. 同种子重复先比较 JPEG 哈希、归一化输入／结果及发射事件。使用现有 `benchmark.diagnostics` 定位首个差异，注明全局时间原点归一化；出现影响结论的分歧时先调查渲染、时序和会话状态。单次重复只作诊断，不宣称统计稳定性。
6. 输出 `report.md`、可追溯的逐轮结果表、异常帧索引和重点片段。新分析输出放在 `exports/`，不修改已封存证据。视频对照仅使用相同场景和种子的合法配对；本阶段重点片段是诊断素材，不冒充算法前后效果对照。

任务完成标准是给出有证据支持的瓶颈排序，而非预设命中率增长。每个建议修复必须包含 run_id／帧序号、可检验假设、目标模块、最小改动范围、固定配对实验和验收指标。若现有数据无法归因，交付明确的观测缺口；只添加向后兼容的可选诊断字段，不改变控制或评分协议。

离线表格入口为 `python3 tools/analyze_algorithm.py --suite exports/algorithm-diagnosis-basic --repeat exports/algorithm-diagnosis-repeat --output exports/algorithm-diagnosis-analysis`。两套实验必须引用同一个冻结包；重复实验增加 `--bundle` 指向基础实验 `contract.json` 的 bundle 路径，保证镜像、配置、算法和平台完全一致。输出目录必须不存在，工具只读取原始实验记录。

## 后续算法修改的入口条件

完成 ALG-01 后，每次只选择一个已定位问题实施。候选和基线使用同一模拟器、渲染后端、场景和种子，重新构建并冻结产物；同时报告命中质量和吞吐及所有基础场景退步。收益门槛根据本轮种子间与重复轮次波动确定，修改前写入实验规格。

CPU 优化、全新运动路线、实车标定和物理真实性升级均另立任务。模拟器修复与算法候选分开验收；当前接口 `rm26-image-v1`、评分口径和 E1 历史记录继续保留。
