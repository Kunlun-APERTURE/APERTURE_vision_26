# 团队基线与实验框架

B1 是 R4 算法配置；E1 是修正原式云台 `rm2017-yaw-corrected-raw`、现有外位置控制/前馈及固定图像时序的实验环境。算法身份与模拟环境分别记录。旧 v3/R4 成绩属于原环境，不继承到 E1。

## 构建与运行

在完整递归克隆的主仓库执行。需准备部署文档中的 Docker 依赖镜像和 NVIDIA 环境；后台无需显示设备或 Sunshine。构建使用独立目录，不覆盖桌面二进制。

```bash
./sim.sh experiment algorithm build --algorithm B1
./sim.sh experiment suite --tier advanced --list
./sim.sh experiment suite --algorithm B1 --environment E1 \
  --tier advanced --cases variable,turn04,pursuit \
  --sensor-seeds 8101,8102,8103 --output exports/my-evaluation --wait
```

suite 提交后返回 job ID；持久调度服务需已运行，可用 `./sim.sh jobs service-status` 查看。`jobs install` 安装系统服务，`jobs daemon` 可在独立终端运行。使用 `jobs status ID`、`jobs pause ID`、`jobs resume ID` 管理任务。suite `--resume` 要求相同算法、环境、场景、种子和重复次数，继续原冻结包；参数改变必须使用新输出目录。

基础默认十项，进阶十项；`--cases` 按需选择，进阶 `--pressure` 增加 variable/pursuit 正反转压力项。轨迹种子为 `--trajectory-seed`，噪声种子为 `--sensor-seeds`；现有进阶轨迹确定，换噪声种子不改变路线。`--repeats` 表示同条件重复，不能作为独立样本数。

默认串行；未通过相同产物和场景的并发资格不得使用双路。单轮完整执行成本目标为40秒，包括准备、采集、结算和基础证据归档，不包含排队、首次编译和离线视频。成本与证据有效性分别展示。

```bash
./sim.sh experiment algorithm report --output exports/my-evaluation
./sim.sh experiment algorithm compare --output exports/before --candidate exports/after
```

JSON/Markdown/HTML 报告保留全部尝试、零开火、分阶段成绩和实际难度。比较要求相同环境、场景、种子与评分口径。低分不触发补样或重跑。

## 队友接入

场景实现 `Scenario.generate(seed)`，返回脚本及阶段/难度定义；实际难度通过 `difficulty` 后验计算，不回流控制。算法实现 `AlgorithmAdapter.build/describe`，或提供独立构建产物与 JSON manifest，由 `ProcessAdapter` 接入。新增算法不需要修改场景或评分器。

manifest 最少提供 `id`、`version`、`image`、`binary_sha256`、`config`（相对 manifest 的配置路径）、`config_sha256`、`protocol`、`capabilities` 和 `launch` argv。协议名为 `rm26-image-v1`，必须声明 simulation_clock、session_reset、fire_advice。launch 支持 `{port}`、`{fire}`、`{aim_origin}`、`{execution_mode}`，二进制与配置分别位于 `/snapshot/sp_vision_25/build/simulator/sim_auto_aim`、`/snapshot/sp_vision_25/configs/simulator.yaml`。可选 `resources` 指向相对 manifest 的模型资源目录，冻结到 `/snapshot/sp_vision_25/assets`；省略时沿用仓库资源。工作目录 `/work` 可写，冻结快照只读。默认初始化 OpenVINO；独立容器可以在 manifest 设置 `setup` 为空字符串或自己的环境初始化命令。

```bash
./sim.sh experiment suite --algorithm path/to/adapter.json \
  --artifact path/to/program --environment E1 --tier basic \
  --output exports/teammate
```

算法使用独立进程。输入为带 session/seq/stamp_ns 的 JPEG、标定、己方姿态/位置和弹速；时间单位 ns，角度 rad，姿态采用现有 map/gimbal/camera 约定。输出回传同一 session/seq/stamp_ns，提供 yaw/pitch/distance、valid、fire_advice。时钟、暖机和会话重置必须遵守图像协议；输出非法或过期时禁止开火。输入不含目标真值。R4 诊断字段可选；完整接口见 INTERFACES.md 及现有 sim_auto_aim 入口。

## 可移植产物

```bash
./sim.sh experiment algorithm export --bundle <冻结包> --output exports/B1.tgz --images
./sim.sh experiment algorithm import --bundle exports/B1.tgz --output exports/restored --images
./sim.sh experiment suite --bundle exports/restored/payload --algorithm B1 \
  --environment E1 --tier advanced --cases variable --output exports/import-smoke
```

导入包包含配置、模型、源码快照和运行代码，镜像通过旁边的 `.images.tar` 分发。校验完整文件清单后运行；机器的 GPU、输出与桌面路径需要当前机器配置，跨 GPU/驱动的图像一致性需重新验收。包内历史来源说明只作审计，运行不读取原历史目录。

E1 的枪口旋转切向速度、旋转连续判碰和实车传感时延仍有已知局限。B1/E1 是团队仿真实验参考，尚未证明实车拟真或实时截止时间达标。

## 一致性与视觉验收

```bash
./sim.sh experiment algorithm qualify --output exports/my-evaluation
./sim.sh experiment algorithm video --reference <旧运行目录> \
  --candidate <新运行目录> --output exports/visual-review
```

qualify 以每场景第一组噪声种子的正式串行结果为参照，追加一次串行重复及两次实际重叠双路重复；比较完整会话输入、算法输出、逐发记录及40秒成本。同一镜像、代码、模型、配置、场景及轨迹种子/硬件身份下合格后，suite 可显式使用 `--parallelism 2`。更换任一身份需重验，不自动推广四路。

video 直接接受两轮不可变证据，校验条件后生成并提交现有视频队列；用 `python3 docker/auto_aim/review_video.py status` 查看队列。渲染在采集空闲时进行，导出成功仍需团队审查。

无 Docker 的框架自检：`PYTHONPATH=docker/auto_aim python3 -m benchmark.checks`。

可选 UI 特征检查通过 `experiment algorithm ui-check --bundle <B1冻结包> --output <新目录>` 执行，覆盖静止、旋转、变速移动旋转和追击。此入口会启动桌面模拟器，使用既有 VT8 会话；它只验证特征场景，不能宣布 UI/后台统计等价。

### 一致性比较的时间边界

`session-relative-v1` 比较从首个暖机输入到物理计分结束的全部 JPEG、输入元数据、算法输出；所有绝对时间戳减去同一会话原点，步序减去会话首步。保持帧间隔、输入/处理/姿态延迟及实际控制不变。计分结束后的媒体尾帧数量单独记录；全部发射弹丸仍必须完成结算并比较碰撞点与事件。原始绝对时间比较、原生飞行时间诊断和首次分歧同时保留。

R4 的 last_good_time 在首次合格观测之前为默认零时刻，last_good_age_s 因此包含绝对 epoch；仅在其值大于10亿秒时标记为未初始化诊断，有限实际年龄仍逐值比较。逐发诊断比较使用记录的碰撞时刻减发射时刻计算飞行时间，原生字段保留在源证据。此规则不允许舍入瞄准角、容差比较图像或忽略控制/命中差异。
