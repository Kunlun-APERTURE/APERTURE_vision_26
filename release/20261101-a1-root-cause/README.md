# A1 关联拒绝与超时的根因验证

本次确认了指定样本的直接失败机制：旧朝向提示允许从整条位姿代价曲线上选点，使带俯仰约束的位姿深度偏离；关联距离门限拒绝这些观测，后续图像关联也逐渐失效，最终触发可信更新超时。结论限定于该会话首个失效窗口，不宣称已找到所有场景的唯一根因或提升闭环成绩。

源样本为 `20261101T123108_basic_move3_spin_pos_shoot_muzzle_no-drag_25p0_25p0_feedforward_5p0hz_tracking_fire_v3_14a332f5`，seed=2026110103。冻结包为 `benchmark_89840c5470d4`，算法提交 `d5d012d`，原B1/E1环境。

## 复现可信度

从会话第一帧开始送入原JPEG、标定、己方姿态和逻辑时钟，回放到计分1.85秒，共84帧，保留暖机和全部前缀。使用原冻结二进制、镜像和session检测历史重置。检测、跟踪、continuity、filter_state、瞄准与火控等14类字段全部逐帧等于原记录，见 [验证摘要](validation.json)。目标真值只由外部分析器读取。

另使用原二进制的 observation replay 做干预，基线84帧的 tracker、filter_state、selected_plate_id 全部等于原记录。此入口的完整continuity不作字节一致承诺（包含未裁剪的候选列表及不同时间原点），也不用于证明fire_advice一致；火控重现以上述在线协议回放为准。

## 拒绝链路与数值

在 `Solver::solve_with_pitch_constraint` 中，现有B1从 `cost <= best + 0.5` 的全部网格候选选最靠近yaw_hint的点，未限定候选必须是局部极小值。`Target::assess` 对三维残差大于0.8m的观测直接返回 association_distance。

|seq／模型装甲ID|所选解与预测距离 m|图像最低代价解与同一预测距离 m|预测／所选解／最低代价解相对真值误差 m|
|---|---:|---:|---|
|68 / 0|1.195|0.644|0.187 / 1.379 / 0.827|
|69 / 0|1.379|1.570|0.201 / 1.563 / 1.754|
|69 / 3|1.401|0.139|0.327 / 1.104 / 0.332|
|71 / 3|2.222|0.234|0.346 / 2.561 / 0.565|
|72 / 3|3.025|0.726|0.340 / 3.365 / 1.066|

表中基于原状态做候选替换计算，不是用干预后的状态计算。真值使用相邻时间戳中最近的目标装甲记录，再按投影中心匹配，时间差0～3.94ms、投影距离0.68～3.69px；该匹配是离线诊断对应，不能当作算法获得了装甲真值ID。详见 [analysis.json](analysis.json)。

seq 68 的图像关联选择模型ID 0，代价0.098，低于未匹配代价4；没有先在图像关联阶段丢失目标。yaw_hint≈0.251rad，选中yaw≈0.243rad，图像最低代价点≈0.470rad。所选深度约7.679m，而预测约6.486m，真实装甲约6.3m。[候选曲线](seq68-candidate-curve.png)展示了朝向提示将选择拉到代价曲线斜坡上的过程。即使图像最低代价解通过距离门，也仍有约0.827m真值误差，不能直接视为准确位姿。

seq 67 是最后可信更新。seq 68、69、71、72全部候选因距离拒绝；seq 70、73的图像匹配代价均超过4，seq 74没有可用匹配。像素旋转模型在该窗口没有激活，因此没有替代更新。seq 75 距最后更新0.27727724秒，超过0.25秒门限，重置并回到detecting。seq 74时仅0.241880146秒，尚未触发超时。

模型ID 3在seq 69对应最近真值装甲6，seq 71、72则对应最近真值装甲4，说明随状态老化还出现了模型身份对应变化；这与错误朝向提示相互影响，不应把后续全部问题简化为单一深度误差。

## 干预结果与下一步

使用相同观测、相同完整前缀做两个离线干预：完全禁用yaw提示；只开启已有 `perception_v1.minima_only_hint=true`。两者均在seq 68～73保持tracking，seq 74暂失后seq 75恢复tracking，没有原来的超时重置。干预从前缀开始生效，因此只证明该选择策略会影响失效链路，不能声称是只改seq 68一帧的消融。

下一项最小候选建议：原B1配置仅开启 `minima_only_hint`，固定其他配置、原E1和评分器，先回放四个移动旋转重点样本，再跑相同30轮配对。保留0.8m关联门限和火控容差。观察可信更新超时、错误关联、跟踪可用比例、加权得分/秒和命中/秒；逐种子列出退步。当前未修改算法仓库或生产B1配置，没有进行该候选的闭环评分。

## 本机复现

原始和干预结果及日志位于 `exports/a1-root-cause-20261101/`，已失败的首次调用日志也保留（工作目录无可写logs，改用独立输出目录后成功）。`validation.json`记录输入与输出SHA256。

从主仓库根目录运行以下在线回放，输出目录需不存在；该命令仅启动冻结算法容器，不启动模拟器：

```bash
PYTHONPATH=docker/auto_aim python3 - <<'PY'
from pathlib import Path
from sync_first_divergence import run_one
run_one(
    Path('docker/auto_aim/experiments/20261101T123108_basic_move3_spin_pos_shoot_muzzle_no-drag_25p0_25p0_feedforward_5p0hz_tracking_fire_v3_14a332f5').resolve(),
    Path('exports/a1-replay-new').resolve(),
    Path('docker/auto_aim/.runtime/jobs/bundles/benchmark_89840c5470d4').resolve(),
    seconds=1.85, detector_history='session')
PY
```

observation replay使用 `--replay=observations.jsonl --algorithm-profile=tracking_fire_v3`，干预分别增加 `--replay-no-yaw-hint=true` 或只在复制的YAML内添加 `perception_v1.minima_only_hint: true`。数据源由完整session archive生成，不能裁掉前缀只送失败窗口。分析脚本 [analyze.py](analyze.py) 和 [plot.py](plot.py)在统一镜像中运行，假定仓库挂载至 `/project`，只写独立exports目录。
