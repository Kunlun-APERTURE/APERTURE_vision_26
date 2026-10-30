# 后台队列与并行

```bash
./sim.sh experiment submit --spec docker/auto_aim/background-matrix.example.json --parallelism 1
./sim.sh jobs list
./sim.sh jobs status JOB_ID
./sim.sh jobs report JOB_ID
./sim.sh jobs pause JOB_ID
./sim.sh jobs resume JOB_ID
./sim.sh jobs cancel JOB_ID
```

每个任务冻结源码、二进制、模型、配置和镜像digest。已提交任务不跟随工作树更新；改代码后需新建任务。自动容量没有匹配记录时使用单路；显式多路仍受资源和容量限制。当前纠正版仅检查过静止单/双路复现，不代表所有条件已认证。不要通过提高路数代替同条件复现。

`completed`表示基础结果与证据可读，离线媒体可能尚未完成。基础耗时与实例预置/媒体耗时分别记录；非实时lockstep通用代码允许180秒，本阶段短测另外检查完整尝试≤40秒，不能把180秒称为默认40秒保证。

重启后中断尝试保留，任务暂停；查看失败证据后用resume恢复。低命中、无射击和实际难度未达标也属于实验结果。环境异常按任务策略暂停/有限重试，不能反复重跑挑高分。

`--stage final --evidence full`保留完整图像；默认筛选模式可能仅保留稀疏图像与关键射击帧，已删除图像无法恢复。最终团队验收应预先请求full。完整遥测、数值、逐发成绩和保留策略记录在每轮目录。

并行实验结束并释放GPU后再渲染视频。历史冻结包、队列数据库和镜像都不随Git分发。
