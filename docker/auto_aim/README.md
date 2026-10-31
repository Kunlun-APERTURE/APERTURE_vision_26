# 联调与实验操作

先读根目录 [README](../../README.md)、[部署前提](../../docs/DEPLOYMENT.md)和[阶段状态](../../docs/STATUS.md)。以下命令从工作站 `/home/rm/rm26` 运行；Sunshine桌面安装的 `rm26-sim` 使用同一参数接口。

## 手动检查

```bash
./sim.sh status
./sim.sh start --scene static --control off --fire off
./sim.sh control on
./sim.sh control off
./sim.sh logs --component algorithm --tail 100
./sim.sh stop
```

`start`不自动构建；配置冲突使用`restart`。停止联调不会关闭Sunshine或VSCode。模型默认legacy；正式对照需显式指定算法配置。advice发射上限5Hz（至少0.2秒），不是无条件固定周期连发。

## 单轮与后台任务

```bash
# 实时UI短测；需要已有桌面与镜像。
./sim.sh experiment run --scene hf_static_5 --terrain flat_range \
  --mode shoot --matrix single --algorithm-profile tracking_fire_v3 \
  --gimbal-profile feedforward --fire-policy advice --fire-rate-hz 5

# 已验输入契约的非实时后台任务。
./sim.sh experiment submit --scene hf_static_8 --terrain flat_range \
  --mode shoot --matrix single --algorithm-profile tracking_fire_v3 \
  --gimbal-profile feedforward --fire-policy advice --fire-rate-hz 5 \
  --input-contract aligned-v1 --detector-history session --sensor-seed 291001 \
  --execution-mode lockstep --timing-profile ui-aligned-v7-clock \
  --parallelism 1 --stage final --evidence full

./sim.sh jobs list
./sim.sh jobs status JOB_ID
./sim.sh jobs report JOB_ID
./sim.sh jobs pause JOB_ID
./sim.sh jobs resume JOB_ID
./sim.sh jobs cancel JOB_ID
```

以上输入契约会核对专用镜像及算法SHA；只能在对应已验产物存在时执行。旧seed示例用于演示参数，正式实验须预先登记新种子，不能依成绩补样。

同条件才能配对比较：固定源码/算法二进制、配置、场景、传感器种子、时序、镜像和评分版本。算法仅读取图像、己方姿态/位置与弹速，目标真值只供离线评分。算法仓库仍无ROS依赖，由独立桥接进程收发消息。

## 操作说明

|主题|入口|
|---|---|
|队列、并行和证据保留|[THROUGHPUT.md](THROUGHPUT.md)|
|同步时序与输入契约|[LOCKSTEP.md](LOCKSTEP.md)|
|基础/进阶移动旋转测试|[MOVEMENT_TIERS.md](MOVEMENT_TIERS.md)|
|左右视频、靶样与参数曲线|[REVIEW_VIDEO.md](REVIEW_VIDEO.md)|
|云台正确公式及历史勘误|[REFERENCE_YAW.md](REFERENCE_YAW.md)|

`*_study.py`、`*_report.py`和诊断脚本保留研究复现能力，不是队友日常首选入口。多数要求本机历史冻结包与原始实验资料；不应在空仓库里依次运行所有研究脚本。以CLI `--help` 查看当前支持参数。
