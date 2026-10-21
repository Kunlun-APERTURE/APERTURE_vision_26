# 算法与模拟器联调

算法保持无 ROS 依赖，继续使用 Sunshine 容器内的 OpenVINO 环境。
独立 Jazzy 桥容器负责 ROS 图像、TF、场景和云台命令；`sim_auto_aim`
复用现有 YOLO、Solver、Tracker、Aimer、Shooter，默认禁火；自动打击需显式启用。

## 当前验收：命中 v3 与 40 秒短测

```bash
./sim.sh build
./sim.sh experiment run --mode shoot --matrix hit-v3 --scene all --repeat 1
```

- 12 个组合：枪口起点 muzzle / 旧起点 gimbal × no-drag / drag × 三个场景；
  默认实际/反馈弹速均为 25 m/s，以单一改动对照起点校正。
- 静止、旋转测量各 6 秒，平移 18 秒；每次再结算在途弹丸 6 秒。
  `timing.json` 记录从场景准备到证据和报告就绪的完整用时，40 秒为有效性上限。
  镜像构建及批次环境初始化单独执行。`--duration` 可指定 1..18 秒。
- Avian 负责弹丸重力和空气阻力积分；集成弹丸作为 Sensor，防止预测碰撞在扫掠前改变轨迹。
  每个 120 Hz 物理步，将 17 mm 球体沿实际前后位置扫掠；移动装甲在相对平移坐标中扫掠。
  使用实际碰撞装甲 ID，第一次实体碰撞结束弹丸；不作前向外推、反向扫掠或最近装甲重分配。
  装甲旋转按 120 Hz 采样，当前 0.5 rad/s、135 mm 装甲的姿态近似误差小于 0.4 mm。
- `contact_point` 在 v3 中为真实有界球体扫掠得到的装甲表面点，平移回实际撞击时刻。
  `/sim/projectiles/trace` 保存物理步位置和速度；评分独立检查接触点距离球心实际路径不超过 11 mm。
  所有有效装甲命中都必须有接触点和轨迹证据，缺失时判实验无效。
- `--aim-origin muzzle` 为新默认：图像时刻 TF 提供真实枪口位置，Aimer 在每次弹道迭代中减去枪口偏移。
  跟踪坐标原点保持稳定；Aimer 通用接口默认偏移为零，现有实机调用不自动改变。
  模拟器发射位置统一取 SHOT_DIRECTION 的 GlobalTransform，与 muzzle_link TF 一致。
- `--aim-origin gimbal` 仅用于旧起点对照，同样使用 v3 命中规则。历史 v1 的 100% 碰撞事件率
  不可与 v3 几何有效命中率直接比较，也不能作为精度已达标的证据。

单组命令：

```bash
./sim.sh experiment run --mode shoot --matrix single --scene line \
  --aim-origin muzzle --physics no-drag --repeat 1
```

旧的 60 秒实验说明与历史矩阵记录保留在下文；新实验默认使用上述短测时长。

## 统一命令

宿主机仓库根目录运行 `bash sim.sh ...`。Sunshine 桌面终端使用
`bash ~/Desktop/项目脚本/sim.sh ...` 或 `~/.local/bin/rm26-sim ...`，参数相同。
桌面仍通过受限本机 SSH 调用宿主机，不挂 Docker socket。

```bash
bash sim.sh build                              # 编译独立仿真镜像和算法
bash sim.sh build --component algorithm        # 只编译算法及原 standard 目标
bash sim.sh build --component simulator        # 只编译 ros2+bench 镜像
bash sim.sh start                              # 首次默认 static / control off / view off
bash sim.sh start --scene line --control on    # 直接进入不开火的闭环
bash sim.sh control on
bash sim.sh control off
bash sim.sh scene static                       # 重置为静止目标
bash sim.sh scene line                         # 0.5 m/s 低速往返侧移
bash sim.sh scene spin                         # 0.5 rad/s 原地自转
bash sim.sh restart --scene static --view on   # 打开算法标注窗口
bash sim.sh status
bash sim.sh logs --component algorithm --tail 100
bash sim.sh logs --component bridge --follow
bash sim.sh logs --component simulator --follow
bash sim.sh stop
bash sim.sh install                            # 更新桌面统一命令
```

`start` 不自动构建。未运行时默认静止、控制关闭、预览关闭、枪口起点 muzzle；运行中省略参数沿用
当前配置，使重复启动保持幂等。`restart` 的省略参数沿用上次配置。
相同运行配置重复启动不创建额外进程；参数冲突时用 `restart` 或 `scene/control`。
普通模拟器正在运行时，必须在命令中加 `start --replace` 才切换到联调模式。
模拟器沿用同一个容器名与 Compose 项目，两种模式不能同时运行。

`scene` 会关闭算法控制，加载并启动场景，等待物理落地，将云台朝向地图 +X 水平
方向以消除模型默认约 25° 的仰角，清空算法状态，再恢复先前的
控制开关。三个场景均为 60 秒，己方使用静止脚本，未列出的其他敌车隐藏。
场景结束后目标停下；再次执行 `scene` 可从初始状态重跑。
己方与目标放在模拟器已定义的平坦走廊，目标初始距离约 4 米。

关闭预览只关闭算法窗口；模拟器仍依赖已有 GNOME `:20` 和活动 VT8。
联调模式固定己方第一人称与 ROS 控制订阅，F3/O/F5 不会覆盖脚本设定；
算法是否接管由 `control on/off` 决定，普通模式的键盘功能保持。
停止联调不会关闭 Sunshine、VSCode 或其他桌面程序。所有联调组件均不自动重启、
不随宿主机开机启动。原 `docker/daedalus/manage.sh` 保留普通模拟器功能；
切回时先 `bash sim.sh stop`，再用原入口启动。

## 数据与算法边界

- 原模型与实机配置保留，仿真参数在 `configs/simulator.yaml`。
- 可选 CMake 开关 `BUILD_SIMULATOR_ADAPTER` 默认关闭；独立构建目录为
  `sp_vision_25/build/simulator`，不改现有 `build` 的 CMake 缓存。
- Solver 增加通用外参接口；legacy 保留原计算路径，实验性 v2 可在近似等价的姿态候选间保持连续性。相机内参与畸变改变时重建
  跟踪管线；动态外参逐帧更新，不因浮点噪声反复丢失跟踪。
- 图像和 TF 使用同一渲染帧时间；从 `map -> gimbal_link` 获取姿态，
  不将 `/gimbal_pose` 的单位姿态误认为世界姿态。
- 算法状态以图像时刻的云台位置为原点，方向与 map 对齐；己方平移时，速度表示
  目标相对于移动原点的速度。模拟器发射继承己方车速，目标真值只供独立评分端使用。
- 桥传递图像与己方姿态，不向算法传递目标真值。
- 仿真配置单独复制至 `.runtime/config.toml`，不覆盖模拟器原配置。
  JPEG 发布和算法处理上限均为 30 Hz；弹速从运行配置读取。
- 原枪口 yaw/pitch 修正置零；弹道预测保留。因此瞄准点不要求始终位于图像中心。
- 图像、标定、姿态未准备好时不输出有效控制。250 ms 无新命令、断线、无解、
  控制关闭或时间跳变会解除跟随；时间跳变后需显式重新开启控制。
- 默认禁火；显式 `--fire auto` 或射击实验才启用算法开火，具体见下方自动打击章节。

## 本机传输接口 v1

桥仅监听 `127.0.0.1:18726`，只允许一个算法连接。每个消息为两个网络字节序
`uint32`（JSON 长度、二进制长度），随后是 UTF-8 JSON 与可选 JPEG；
JSON 上限 64 KiB、JPEG 上限 16 MiB，所有 JSON 包含 `version: 1`。

算法发送 `type: next`，桥回复 `frame` 或 `wait`。`frame` 包含
`session/seq/stamp_ns/calibration/q_gimbal2world/bullet_speed` 和 JPEG。
四元数按 `[x,y,z,w]`，矩阵按行展开，距离为米，时间为 Unix 纳秒。
算法为每个 `frame` 返回一个 `type: command`，回传原会话、序号、时间，
并提供 `valid/yaw/pitch/distance` 和可选 `fire_advice`（缺省 false）；yaw/pitch 是原 Aimer 的弧度约定。
桥转换为模拟器角度约定：`yaw = degrees(yaw)`，`pitch = -degrees(pitch)`。
每次只请求最新帧，不排队保存历史 JPEG；场景重置后旧会话命令被丢弃。

管理连接发送 `type: admin`，`op` 为 `status/control/scene`，独立于算法连接。
这些消息不执行 shell；桌面 SSH 接口也只接收经 argparse 校验的参数列表。

## 日志与验收指标

### 每次实验的独立验收

```bash
bash sim.sh build                         # 包括独立验收镜像
bash sim.sh experiment run                # 三个场景各三次，默认 6 / 18 / 6 秒测量
bash sim.sh experiment run --scene line --repeat 1 --label change-01
bash sim.sh experiment list
bash sim.sh experiment report RUN_ID
bash sim.sh experiment review RUN_ID --decision pass --note '已检查全片：目标身份、投影、失锁重捕、瞄准行为和画面连续性'
bash sim.sh experiment review RUN_ID --decision fail --note '填写具体异常及视频时刻'
bash sim.sh experiment baseline RUN_ID    # 显式选择已复核的同场景效果基线
```

`experiment run` 需要 Sunshine 桌面运行，会重启联调组件以确保镜像和消息接口一致，
不会重启 Sunshine。普通控制入口保持可用。正式实验期间管理操作串行锁定；
`status`、日志可正常查看。每次结束都会关闭控制；环境异常时保留无效报告并停止批次。
默认跟随实验不发射；`--mode shoot` 使用独立射击规则。

场景先加载为 Idle，落地、归位、输入与诊断准备好，再启动独立录像进程。
录像订阅准备完成后调用新的 `/sim/scenario/begin`，保留准备好的物理姿态，仅启动
场景时钟和运动，正式测量 manifest 中指定的时长。旧 `/sim/scenario/start` 的完整重置语义保留。

每次数据保存在 `docker/auto_aim/experiments/<时间_场景_随机ID>/`，不覆盖历史：

- `manifest.json`、`configuration/`：Git 版本、未提交修改摘要、源码和模型 SHA256、
  实际镜像 ID、场景及配置副本、当次固定的验收基线。
- `input.avi`、`annotated.avi`、`annotated.mp4`：完整处理帧的输入浏览录像与同步标注录像。
  视频按 30 FPS 播放；真实时间以 `frames.jsonl` 的采集时间和视频帧号为准。
- `input.mjpg`：逐帧保留算法输入的原始 JPEG 字节，`frames.jsonl` 保存字节偏移与长度，
  可精确重新解码；`input.avi` 仅供浏览，会重新压缩。`evidence-sha256.json` 固定主要证据摘要。
- `telemetry.jsonl`：独立真值、场景状态、实际 ROS 命令和算法观测；`frames.jsonl`：逐帧评分。
- `metrics.png`、`summary.json`、`report.html`、`report.md`：曲线、规则判定和可直接打开的报告。
- `keyframes/`、各组件日志、`reviews/`：抽样与异常关键帧、诊断和每次人工复核记录。

完成的证据还会通过硬链接显示在 Sunshine 持久 HOME 的
`~/.local/share/rm26-sim/experiments/RUN_ID/`，视频不占用双份磁盘。
可以在桌面文件管理器打开 `report.html`；`experiment report RUN_ID` 会刷新桌面副本。
桌面“仿真实验报告”文件夹也指向此位置。批次生成 HTML 汇总（中位数与范围），
无效实验不计入算法成绩统计。
验收进程以宿主用户身份写文件，仅其 ROS 订阅使用 UDP，避免与 root 发布者共享内存的
跨用户权限问题；图像仍通过本机只读观察流传送。

验收容器独立读取 `/sim/gt/robots`、`/sim/gt/armors` 和实际命令，桥只向它单向输出
图像与算法诊断；验收进程没有 ROS 发布器，真值不进入算法。算法增加只读诊断，
原有求解、跟踪和瞄准逻辑不变。

**规则分四类：**

| 类别 | 判定 |
| --- | --- |
| 实验有效性 | 场景完成；环境无退出/重启；真值覆盖 ≥99%；起止覆盖完整指定时长（边界容差 0.25 秒）；最大采样间断 ≤0.25 秒；录像观察流零丢包；己方漂移 ≤5 cm；出生成功；目标实际发生规定运动 |
| 基础性能 | 处理 ≥20 Hz；帧龄 P95 ≤150 ms；静态命令执行角误差 P95 ≤2° |
| 不开火（track 模式） | 实际 fire 事件为零、所有 ROS fire_advice 为 false |
| 效果 | 正确跟踪覆盖、首次锁定、最长失锁、重捕耗时、位置与速度误差；选定基线后才能自动判断是否回退 |

正确跟踪要求状态严格为 `tracking`、类别正确、关联到目标机器人，且估计中心平面
误差 ≤0.5 m；`detecting` 和 `temp_lost` 单列。覆盖率分母仅包含可评估的可见目标帧。
速度和位置按图像采集时刻对齐，最多等待 150 ms 获取真值前后样本，不能用到达时刻
或最新姿态替代。目标中心高度不是装甲板中心高度，中心位置指标只比较水平面，
装甲板测距误差另行统计。

可见性结合正面朝向、完整装甲板投影位于图像内和物理中心射线检查，射线忽略己方
与目标自己的碰撞包围体。它是可解释的近似，不是逐像素渲染分割；遮挡、模型自身遮挡
与边缘情况须通过视频复核。图中青色为真值投影、绿色为检测、紫色为预测装甲、黄色为
弹道瞄准点。实际瞄准点包含弹道提前量，不要求停在图像中心。

自动规则失败分别输出 `invalid`（环境/证据无效）或 `failed`（性能/效果失败）。
初次未人工复核为 `pending_visual_review`；视觉通过但未选效果基线为
`pending_effect_baseline`；只有全部门槛、基线比较及视觉复核通过才输出 `passed`。
人工复核不能覆盖环境无效或规则失败。

基线必须由用户显式选择，且该次实验有效、基础性能通过、视觉复核通过、关键真值指标
完整。初版回退容差：正确跟踪覆盖率最多下降 5 个百分点；位置/速度误差门槛为基线
的 1.2 倍加 0.02 单位；锁定/失锁耗时门槛为基线的 1.2 倍加 0.2 秒。
基线是项目内相对回归标准，不代表实机精度承诺。建议先看每场景三次的差异再选基线。
当次基线随 manifest 固定，后续修改不会改变旧报告。场景文件不一致时不直接比较。

`review --decision pass` 表示操作者已按目标身份、检测与真值投影、失锁重捕、瞄准
行为、视频连续性五项检查全片；必须填写具体观察备注。不同意效果时使用 `fail` 并记
下时间点。初版不自动假定视觉已复核，也不自动选择基线。

`status` 包含连接、控制是否请求/生效、场景、帧龄、检测/跟踪/控制帧数、
处理速率、处理耗时与角误差 P95。样本窗口最多 2048 个，处理速率从本场景
启动后累计；P95 未单独剔除刚接管时的收敛过程。
桥状态每秒写入 `.runtime/bridge-status.json`，Docker 日志按 20 MB × 3 轮转。
`.runtime/latest.jpg` 和 `latest-frame.json` 每 5 秒覆盖保存一帧实际输入及配套标定。
每个场景结束时保存 `.runtime/rm26_<场景>-completed.json`，冻结运动阶段的指标，
避免场景结束后目标静止的数据混入比较。相同场景再次完成时覆盖旧快照。
算法日志位于 `sp_vision_25/build/sim/algorithm.log`，启动时超过 20 MB 则轮换。

验收建议依次运行静止、平移、自转各 60 秒，记录处理频率、有效跟踪比例、帧龄与
误差。静态稳定后命令到云台角误差 P95 目标 ≤2°；运动场景目标处理 ≥20 Hz、
帧龄 P95 ≤150 ms。还需验证断线、进程退出、场景重置和重复启停时旧控制会停止。
这组指标描述完整算法闭环，不把 ROS 图像发布速率当作算法处理速率。
模型无法稳定识别时保留日志单独分析，本轮不换权重、不使用真值替代检测。

## 本机运行记录（2026-10-19）

`sim_auto_aim`、原 `standard` 与启用 `ros2 bench` 的镜像均已编译。
三个 60 秒场景均曾完成实际闭环，模型权重与算法计算逻辑保持。

| 场景 | 处理频率 | 帧龄 P95 | yaw / pitch 误差 P95 |
| --- | --- | --- | --- |
| 静止 | 28.62 Hz | 60.14 ms | 0.30° / 0.22° |
| 平移 | 28.50 Hz | 60.58 ms | 2.06° / 0.32° |
| 自转 | 28.55 Hz | 59.79 ms | 1.94° / 0.26° |

静态取首次场景完成后的快照；平移和自转取场景满 60 秒时冻结的指标。
这些角误差是命令到云台执行方向的误差，不是目标位置真值误差或命中率。
平移与自转有效跟踪帧占比分别约 90% 和 95%，仍有短暂丢失与重新锁定，
本轮未调整模型、跟踪器或控制器参数。
原始数据在 `logs/run-summary.json` 和各场景的 `*-status.json`。

桌面 Bash 的受限 SSH 状态查询也已实际使用。收尾时还遇到一次 Sunshine/Xorg
会话重启，模拟器因 X 连接关闭而退出，桥进入未生效状态；需用 `restart`
刷新显示认证恢复。未运行自动化测试套件、原视频回归或主动故障注入。
# 模拟器内自动打击

历史首轮矩阵为 30 次、每次 60 秒。下方命令再次执行时采用新的短测时长，并使用 v3 命中判定：

```bash
./sim.sh build
./sim.sh experiment run --mode shoot --scene all --matrix ballistic-v1 --repeat 1
```

矩阵覆盖 static / line / spin；no-drag / drag（均无风）；实际/反馈弹速
25/25、20/20、28/28、25/23、25/27 m/s。反馈值模拟算法收到的测速数据，
实际值决定模拟器初速度。算法仍使用无阻力弹道解算并补偿枪口位置，因此 drag 组是模型差异对照。
所有参数和模型版本随每次实验冻结，不能把这些参数视为已实测标定。

只运行一个组合：

```bash
./sim.sh experiment run --mode shoot --matrix single --scene static --repeat 1 \
  --physics drag --bullet-speed 25 --reported-bullet-speed 23
```

交互式自动射击（沿用普通 Bash 入口）：

```bash
./sim.sh restart --scene static --control on --fire auto \
  --physics no-drag --bullet-speed 25 --reported-bullet-speed 25
./sim.sh control off
```

开火建议来自算法原有 Shooter；桥接层和模拟器均限制为最多 2 发/秒。
`control off` 同时撤销射击授权；之后需要重新 `scene static` 或开始下一次实验才重新授权。
默认 `--fire off`，准备期间、场景结束和指令失效时禁火；键盘/手柄不能混入集成实验。
自动批次完成后关闭控制及射击授权，保留 Sunshine 桌面。

每次报告包含 `shots.csv` / `shots.jsonl`，按 `shot_seq` 配对实际发射与碰撞事件；
目标装甲命中率以实际出膛数为分母。其他机器人、环境命中和无命中事件分别列出。
接触点未知时保持空值；零发射显示“无射击样本”，不能作为命中基线。
录像尾部包括在途弹丸结算，但跟随指标只统计 60 秒测量区间。
射击基线按场景、物理条件和实际/反馈弹速分别保存，仍需明确的人工复核。


中断后可以复用相同条件下已完成的有效组合（批次参数填写不含 `.json` 的文件名）：

```bash
./sim.sh experiment run --mode shoot --scene all --matrix ballistic-v1 --repeat 1 \
  --resume batch_20261020T033657_675572
```

续跑核对算法二进制、模型、算法配置、场景、模拟器镜像和有效物理参数；
允许单独修复录像/报告代码，具体代码摘要仍按每次实验保存。
无效组合会保留并重跑，完整性门槛不会因续跑放宽。
`report-generation.json` 标明派生报告的生成时间和评分代码，与采集时版本分别记录。
环境接触可能先于装甲命中（反弹），最终按装甲命中优先归类，同时保留环境接触标记。

2026-10-20 首轮已有 30 个有效组合，汇总见
`experiments/ballistic-v1-initial.md` 和 `experiments/batch_20261020T033657_675572.html`。
本轮实际出膛 2834 发，目标装甲命中 2834 发；全部等待人工视觉复核，未自动选定正式基线。

物理接口依据：[Avian 0.7 CCD](https://docs.rs/avian3d/0.7.0/avian3d/dynamics/ccd/index.html)。

诊断版 v2 的真实流形过滤仍存在高速薄装甲漏判，保留其原始记录供审计，不作为精度基线。

## 2026-10-20 修复后的短时对照

最终批次 `batch_20261020T053348_637769` 有 12 个有效组合，完整用时 18.91–32.10 秒。
汇总：`experiments/hit-v3-origin-compare.html`，CSV 与 Markdown 同名。
修正起点后的静止、旋转两种阻力条件均全部命中；平移任意目标装甲为无阻力 17/28、阻力 18/28。
平移正面装甲另计为 7/28、6/28，说明正面打击仍需改进。
当前沿用原模拟器碰撞层，车身粗包围体不阻挡己方弹丸；因此正面未命中后可能再击中侧面或背面装甲。
这个口径在汇总中单列，不能把任意装甲命中率理解为正面命中率。
本轮未调整云台 PID、跟踪参数或模型，未自动选择正式基线；最终恢复 muzzle 起点并关闭控制、开火。

## 平移射击离线诊断

`diagnostics.py` 读取已完成批次，不发布指令、不修改原始证据或验收结论。
在具有 numpy、matplotlib、OpenCV、ffmpeg 的 evaluator 环境中运行：

```bash
python3 /opt/rm26/diagnostics.py \
  --batch /experiments/<无阻力批次>.json /experiments/<有阻力批次>.json \
  --output /experiments/<诊断目录>
```

每个条件重复三次，静止/旋转测量 6 秒、平移 18 秒，单次完整实验仍以 40 秒为上限。
报告包含逐帧/逐发 CSV、平移与旋转预测分解、左右方向与换向分组、曲线以及带原始时间戳的视频片段。
预测通过同一装甲板的运动模型拟合重建，不能用最近的当前装甲板直接相减。
前平面穿越误差只统计确实穿过的已观测球心轨迹；命中时提前终止的弹丸使用真实表面接触误差另计。
指令发布时间不等于模拟器内部接收时间，ROS 轨迹时间戳也有渲染帧分辨率；
本轮诊断不能独立证明 PID、调度或传输延迟的各自因果贡献。
无效实验保留原始记录并按相同条件补采，不降低有效性门槛。

本轮完整报告：`experiments/lead-diagnostic-20261020/report.html`。
18 个有效实验，另保留 1 个因图像间隔超限而补采的无效记录；
单次采集、结算及原始报告完成用时为 18.86–32.18 秒，批次离线诊断另行生成。
`execution-audit.json` 保存有效性检查、原始证据哈希核对及运行恢复情况。
已有诊断结果可追加 `--render-only` 快速重建汇总，此模式核对源文件指纹，不重新生成曲线或视频。

## 云台时序与前馈对照

`start/restart` 和 `experiment run` 新增 `--gimbal-profile`：

| 配置 | 控制时序 | 角速度前馈 |
|---|---|---|
| `legacy`（默认） | 原渲染循环、原 ROS 出膛顺序 | 关闭 |
| `timing` | 120 Hz 固定控制、即时计算层级位姿、传播后出膛 | 关闭 |
| `feedforward` | 原控制时序 | 开启 |
| `combined` | 固定控制及传播后出膛 | 开启 |

```bash
./sim.sh experiment run --mode shoot --matrix gimbal-ab --scene line --repeat 3
./sim.sh restart --gimbal-profile combined --control off --fire off
```

`gimbal-ab` 为四种配置 × 两种阻力条件；每次平移测量 18 秒，单次完整运行仍限时 40 秒。
PID 增益、算法二进制/模型/YAML、开火建议与 2 Hz 上限均保持不变。
前馈仅从带时间戳的下发角度差分得到，不读取目标位置或速度真值。
增益 1、低通时间常数 60 ms、角速度限幅 0.6 rad/s；指令间隔超过 150 ms、
时间戳不递增或角度跳变超过 0.06 rad 时清零；指令失效/超时仍取消挂起射击。
前馈通过目标旋转的局部差分转换到执行轴，叠加后仍受原各轴速度限制。

`/sim/gimbal/trace` 为独立评分器提供控制步长、已接收指令时间戳和反馈/前馈分量，
不转发给算法。固定控制用 [Bevy TransformHelper](https://docs.rs/bevy_transform/0.19.0/bevy_transform/helper/struct.TransformHelper.html)
计算当前层级位姿，避免多次物理步继续使用上次渲染的缓存位姿。
时序配置整体改变频率、位姿读取和 ROS 出膛顺序，本轮对照不能继续拆分三者贡献。

`gimbal_compare.py` 在 evaluator 环境离线处理完整的三次重复矩阵和静止/旋转回归，
输出逐发诊断、已接收指令误差、各次范围、单项/组合差异及视频证据；不修改原始验收结论。
代码默认保留 `legacy`，候选配置仍需人工视觉复核。

2026-10-20 本轮报告：`experiments/gimbal-ab-20261020/report.html`。
正式平移对照 24 次、静止/旋转回归 12 次，另有关闭射击回归及未纳入正式对照的首轮组合平移试验。
仅前馈平移正面命中为无阻力 57/81、有阻力 54/78；原控制为 20/84、17/82。
角误差绝对值中位数从约 1.13–1.17° 降至 0.19–0.21°，发射频率中位数约 1.44 Hz。
仅前馈旋转回归为 19/21，原控制及组合分别为 21/21；每种物理条件只有一轮回归，
未据此宣称泛化验收通过或自动切换默认配置。对应两发未命中有单独的视频片段。


## 己方移动与持续连发稳定性

```bash
# 四场景：反向几何静止对照、原目标平移、己方沿地图 +Y/−Y 平移。
./sim.sh experiment run --mode shoot --matrix single --scene stability-all --repeat 3 --gimbal-profile feedforward --fire-policy continuous --physics no-drag
# 相同矩阵重复有阻力条件。
./sim.sh experiment run --mode shoot --matrix single --scene stability-all --repeat 3 --gimbal-profile feedforward --fire-policy continuous --physics drag
```

`--fire-policy advice` 为默认原建议开火模式；`continuous` 为显式选择的仿真诊断模式。
后者由独立 bench 系统约每 500 ms 调用原弹丸发射函数，绕过算法开火建议，丢跟踪时也继续射击。
角度仍由原视觉算法驱动；无效/过期指令继续撤销云台跟随，连发样本中保留此段结果。
连发需同时满足 `--fire auto`、控制开启、算法连接、场景 Running 与新鲜桥接器授权心跳；
关闭控制/射击、断开连接、场景结束，或授权心跳超时 250 ms 后停止。
发射、弹道、球体扫掠及装甲归属判定不变；连发在当前位姿传播后出膛，报告明确记录新镜像。
录像标注 `CONTINUOUS 2Hz`，并记录全部授权心跳。单次实验新增样本数与最大间隔验收规则。

`move_ref` 是交换双方位置后、面向地图 −X 的 6 秒静止对照。
`ego_left` / `ego_right` 名称分别表示地图 +Y / −Y；两者反向穿过同一条已验证走廊。
测量 8 秒：0–2 秒静止，2–6 秒名义 0.5 m/s 平移，6–8 秒静止；稳定窗口 3–5.5 秒。
目标位置固定、距离随路线约 4–4.4 m；此结果不代表己方与目标同时移动的验收。
原 static/line/spin 场景文件保持完整。

己方移动时，跟踪器坐标原点随云台平移，速度评分使用目标速度减己方速度。
弹丸继承己方底盘速度，因此初速验收在评分器中从世界初速度减去独立记录的己方速度，
按枪口相对初速校验 25 m/s；原场景仍用原初速规则。真值只进入离线评分器。

`moving_fire.py` 输出逐秒命中图、逐发 H/M 序列、最长连续未命中、实际间隔、双轴角误差、
未命中弹丸穿越正面装甲平面的偏差和原始录像入口。离线门限筛选只描述保留的实际弹丸，
不作为修改开火策略后的闭环命中率。


## Advice 5 Hz 命中稳定性（当前设置）

```bash
./sim.sh experiment run --mode shoot --matrix single --scene advice-motion --repeat 3 --gimbal-profile feedforward --fire-policy advice --fire-rate-hz 5 --physics no-drag
./sim.sh experiment run --mode shoot --matrix single --scene advice-motion --repeat 3 --gimbal-profile feedforward --fire-policy advice --fire-rate-hz 5 --physics drag
```

当前默认 `fire-policy=advice`、发射上限 5 Hz（至少间隔 0.2 秒），不独立强制连发。
建议持续为真且指令有效时继续请求发射；advice 为 false 时不新增请求，已有请求仍保留原最长 250 ms 的执行语义。
桥接器限制请求间隔；模拟器通过 `DAEDALUS_INTEGRATION_FIRE_RATE_HZ` 独立限制实际出膛间隔。
`--fire-rate-hz` 范围 (0,5]；旧记录缺字段仍按原 2 Hz 解析，基线键包括非原频率，避免混用。
机械冷却现在按真实经过的帧时间持续计时，避免每次 advice 脉冲重新附加等待；弹丸物理和命中扫掠保持原样。
advice 间断、跟踪失效和处理节拍均可能使实际发射频率低于上限，不能将此模式称为严格固定周期强制连发。

`advice-motion` 包含 `move_ref`、原 `line`、`ego_sweep`、`ego_sweep_reverse`。
己方往返场景持续 18 秒，0–2 秒静止、2–6 / 6–10 / 10–14 秒按名义 0.5 m/s 往返、14–18 秒静止。
稳定移动窗口为 3–5.5 / 7–9.5 / 11–13.5 秒；换向过渡另列，单次完整流程仍限制 40 秒。
反向场景使用已知初始己方朝向 −156°，减少画面边缘起始识别对移动评估的干扰，不向算法提供目标真值。

`advice_stability.py` 离线读取两种阻力各三次的完整矩阵，输出逐发序列、双轴执行误差、
打击点变化残差、径向/横向分量、突变与命中关联、逐轮曲线及突变附近未命中录像。
相邻有效指令间隔限定 15–100 ms，用前一帧变化率外推下一帧；超过 100 ms 的间隔单列重捕获。
主突变阈值为 0.5° 或世界 XY 5 cm，发射前关联窗口 300 ms；另完整报告 0.5/1/2° 的角突变敏感性。
这些统计描述关联，不证明因果，也没有追加开火稳定门限。

记录器修复了预热帧等待不可恢复的旧真值导致队列积压的问题：预热仍记录日志并排除，
只有测量帧等待图像时间真值配对；零丢帧、20 Hz 下限及原真值/帧间隔门槛保持原样。

## 实验性连续跟踪配置

`--algorithm-profile` 支持以下三个配置，默认仍为 `legacy`：

| 配置 | 跟踪与预测 | 开火判断 |
|---|---|---|
| `legacy` | 原有路径 | 原有 advice |
| `tracking_v2` | 先验创新门控、短时漏检保留、装甲一对一关联、分装甲 CV 平移预测 | 原有 advice |
| `tracking_fire_v2` | 同 `tracking_v2` | 当前指令的 yaw/pitch 对齐与独立资格状态 |

新跟踪把可见装甲的位置/速度与车体法向解耦，距离、横向和高度使用不同的过程噪声；
装甲选择带面积迟滞，弹道迭代固定本帧选中的物理装甲。预测点不在不同装甲之间插值。
单装甲下车体几何参数保持初值，整车中心和速度为近似值；高速旋转及多相机入口尚未完成验收。
`continuity_v2` 位于算法配置文件，核心接口不依赖 ROS，也不读取目标真值。

新开火配置要求当前目标 yaw/pitch 均在 0.5° 内、己方姿态新鲜度不超过 50 ms、
最近观测可信。轨迹重建、装甲切换或指令残差超过 1° 后，至少经过三个稳定帧才重新具备资格。
桥每帧单独发布 `/sim/aim/fire_eligibility`，模拟器在实际发射前再次检查，
新资格为 false 或 50 ms 过期会取消待发请求。频率仍由 advice 限制为最多 5 Hz，
实际发射间隔至少 0.2 秒。单独的资格状态不会把限频产生的 fire=false 误当作取消。

单次诊断（不会自动记为人工验收通过）：

```bash
./sim.sh experiment run --scene line --mode shoot --matrix single --repeat 1 \
  --algorithm-profile tracking_v2 --gimbal-profile feedforward \
  --aim-origin muzzle --physics no-drag --fire-rate-hz 5
```

离线回放只将历史检测角点、相机标定、己方姿态和弹速交给 C++；目标真值仅留在 Python 评分端。
`continuity_replay.py` 用旧实验第 1/2 次选择参数，第 3 次留作检查，保存每个候选的原始输出、
输入/算法摘要和失败项。回放是估计器检查，不能当作新的闭环命中率。

`continuity_study.py` 交替运行五个场景、两种弹道、三次重复，每阶段 60 次，
固定算法二进制、配置和模拟器镜像；支持相同冻结条件下的 `--resume`，保留无效尝试。
跟踪选择未通过时只能用 `--diagnostic` 收集对照证据，不能通过阶段验收。
`continuity_compare.py` 校验证据摘要、完整矩阵、平滑度、跟踪覆盖、速度恢复和正面有效命中频率。
跟踪阶段验收通过前，脚本不会运行正式开火阶段。
恢复时间的 P90 使用经验分布最近秩 `ceil(0.9 × N)`；未恢复事件按 2 秒截尾计入，
小样本不会以较快的事件代替尾部。回放缓存同时校验观测、二进制、配置和评分源码摘要。

```bash
python3 docker/auto_aim/continuity_study.py --phase tracking --output STUDY_DIR \
  --selection STUDY_DIR/replay-frozen/selection.json --diagnostic
python3 docker/auto_aim/continuity_compare.py --batch STUDY_DIR/tracking_BATCH.json \
  --output STUDY_DIR/tracking-acceptance.json
```

阶段结果不会修改旧基线，也不会填写人工视觉通过；每次仍保留完整录像与 40 秒有效性规则。
