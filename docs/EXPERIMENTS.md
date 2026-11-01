# 实验指南

首次运行先完成 [快速开始](QUICKSTART.md)。所有命令从主仓库根目录执行。

## 默认环境

B1 使用 R4 配置；E1 使用 `rm2017-yaw-corrected-raw` yaw 响应和现有外位置控制／前馈。默认平坦靶场、枪口起点、25m/s 无阻力弹道、advice 开火且最多0.2秒一发、30帧暖机和固定逻辑时序。后台可以慢于实时，分数不证明算法满足实车计算期限。

legacy 用于历史环境对照。改变物理环境后重新建立参考成绩。

## 选择项目

以下命令只列出选择，不启动实验：

```bash
./sim.sh experiment suite --tier basic --list
./sim.sh experiment suite --tier advanced --list
./sim.sh experiment suite --tier advanced --cases variable,turn04 --list
./sim.sh experiment suite --tier advanced --cases variable,pursuit --pressure --list
```

|档次／项目|用途|
|---|---|
|basic|静止、平移、变向、正反转、自车横移及3/4m/s移动旋转，共10项|
|advanced：variable|变速移动叠加旋转|
|advanced：turn08 / turn04|每0.8秒／0.4秒改变方向|
|advanced：spin_startstop|移动中旋转启停|
|advanced：pursuit|动态追击，方向与距离变化|
|pressure|只增加 variable/pursuit 的高角速度条件|

基础 `--cases` 填具体场景名，例如 `hf_static_5,hf_uniform_3`；进阶填项目名，正反转自动成对选择。

```bash
./sim.sh experiment suite --tier advanced --cases pursuit --sensor-seeds 8101,8102,8103 --output exports/pursuit-v1 --wait
```

调试可先用单种子。`--trajectory-seed` 和 `--sensor-seeds` 分别记录轨迹与传感器种子；当前进阶路线确定，换噪声种子不改变路线。`--repeats` 是重复，不算新独立样本。默认串行；双路须验证对应产物及场景。

## 状态与恢复

下面 `job_实际ID` 是占位值，先替换为提交返回的 job ID：

```bash
./sim.sh jobs status job_实际ID
./sim.sh jobs logs job_实际ID --tail 100
./sim.sh jobs pause job_实际ID
./sim.sh jobs resume job_实际ID
```

重启后先查日志再恢复。`jobs resume` 恢复队列；suite `--resume` 用原参数连接原输出目录。恢复上面的追击实验：

```bash
./sim.sh experiment suite --tier advanced --cases pursuit --sensor-seeds 8101,8102,8103 --output exports/pursuit-v1 --resume --wait
```

改算法或参数后使用新输出目录。低分和失败保留，不通过重跑筛选好结果。

## 分数含义

|指标|含义|
|---|---|
|精确命中率|有效目标装甲命中数／总发射数|
|中心加权命中率|有效命中中心权重之和／总发射数|
|命中/秒|有效命中数／计分时长|
|加权得分/秒|中心权重总和／计分时长|
|实际难度|速度、角速度、距离与变向响应是否达到要求|

中心权重为 `1 − min(1, max(|x|/半宽, |y|/半高))`，x/y 是装甲局部落点。中心为1、边缘为0，不是欧氏距离百分比。未命中计零；零发射时命中率为 N/A、命中/秒为0。

同时看命中率和吞吐，避免少开火抬高命中率。基础、进阶、压力分别解释；运动阶段和难度详情在 `results.json` 的 `runs[].difficulty`。指令4m/s不等于实际4m/s。

`Execution: valid` 或 `all_evidence_valid=true` 表示证据有效，不表示算法改善。部分效果规则失败仍可以是有效实验。40秒是单轮成本目标，`within_cost_budget` 单列；lockstep 硬超时更长。

## 两版对照

分别以相同环境、场景和种子运行，输出到 `exports/before`、`exports/after`，然后执行：

```bash
./sim.sh experiment algorithm report --output exports/before
./sim.sh experiment algorithm report --output exports/after
./sim.sh experiment algorithm compare --output exports/before --candidate exports/after
```

compare 检查配对并返回双方数据，当前不自动判定统计显著性。构建与配置修改见 [开发指南](DEVELOPMENT.md)。逐轮数据位于 `docker/auto_aim/experiments/`，汇总 results.json 提供 run_id。

## 本地视觉对照

以真实逐轮目录替换下列 `旧run_id`、`新run_id`，要求相同场景和种子，输出目录尚不存在：

```bash
./sim.sh experiment algorithm video --reference docker/auto_aim/experiments/旧run_id --candidate docker/auto_aim/experiments/新run_id --output exports/visual-review
python3 docker/auto_aim/review_video.py status
```

视频在采集空闲时渲染，包含左右画面、靶样和同步曲线；导出不等于人工审查通过。远程 PR 只回传得分和文本日志，需要视觉排查时在本地复现。

## 结果的适用范围

枪口旋转切向速度、旋转判碰近似和云台串联响应仍需核对；底盘侧滑、曝光和时延缺少当前实车标定。跨 GPU/驱动复现及 UI／后台全面等价尚未认证。分数用于同环境算法比较。

故障先查 job 日志和 summary.json；服务器问题按 [运维指南](OPERATIONS.md) 处理。
