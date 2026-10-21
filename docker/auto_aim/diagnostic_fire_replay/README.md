# 换向诊断用离线 Shooter 适配器

该工具直接编译已有 `shooter.cpp` 和 `math_tools.cpp`，从 stdin 读取不含目标真值的
JSONL，只调用 `Shooter::shoot_current` 并输出结果。没有网络、ROS、场景或发射接口。
不会构建或替换 `sim_auto_aim`，不改变核心算法源文件。

在宿主机仓库根目录执行：

```bash
docker cp docker/auto_aim/diagnostic_fire_replay sp_vision_sunshine:/tmp/rm26-diagnostic-fire-replay
docker exec --user 1000 sp_vision_sunshine cmake \
  -S /tmp/rm26-diagnostic-fire-replay \
  -B /workspace/sp_vision_25/build/diagnostic_fire_replay \
  -DSP_ROOT=/workspace/sp_vision_25
docker exec --user 1000 sp_vision_sunshine cmake \
  --build /workspace/sp_vision_25/build/diagnostic_fire_replay -j2
python3 docker/auto_aim/reversal_diagnose.py shadow \
  --root /home/rm/rm26 \
  --output /home/rm/rm26/docker/auto_aim/experiments/reversal-diagnosis-20261020

docker run --rm --user 1000:1000 \
  --env MPLCONFIGDIR=/tmp/rm26-diag-matplotlib --env OPENBLAS_NUM_THREADS=1 \
  --volume /home/rm/rm26:/workspace:ro \
  --volume /home/rm/rm26/docker/auto_aim/experiments/reversal-diagnosis-20261020:/output \
  daedalus:rm26-evaluator python3 /workspace/docker/auto_aim/reversal_diagnose.py analyze \
  --root /workspace --output /output
```

分析固定使用 continuity-v2-20261020 的完整 60 次对照和原验收报告。
现有源码与配置必须仍匹配该研究的冻结摘要，否则 shadow 阶段拒绝执行。
旧证据、报告和算法目录在分析容器中只读挂载，输出单独挂载为可写。
`--no-media` 跳过视频重新裁剪，用于已有片段的分析更新。

时间说明：真实新门控试验使用已记录的准确 pose_age 核对 C++ 输出；其余记录的
调用时刻只可估算，以 receipt 为主、提前 1/2 ms 作敏感性检查。另保留减去整段
pipeline 和再提前 5 ms 的压力情景，可能早于姿态快照，不可视作同等可信的时间估计。
尚未记录的预热历史会影响开头计数；第一次观测到计数清零后检查精确计数一致性。

候选请求按完整录制序列、会话/时间/标定重置以及 0.2 秒限频重建。
实际发射的资格检查与候选请求计数分开报告；历史筛选不能估算改变策略后的命中率。

可选的原始图像关联检查及最后汇总（同一只读仓库/可写输出挂载）：

```bash
docker run --rm --user 1000:1000 --env OPENBLAS_NUM_THREADS=1 \
  --volume /home/rm/rm26:/workspace:ro \
  --volume /home/rm/rm26/docker/auto_aim/experiments/reversal-diagnosis-20261020:/output \
  daedalus:rm26-evaluator python3 /workspace/docker/auto_aim/reversal_image_audit.py \
  --root /workspace --output /output

docker run --rm --user 1000:1000 --env MPLCONFIGDIR=/tmp/rm26-diag-matplotlib \
  --env OPENBLAS_NUM_THREADS=1 --volume /home/rm/rm26:/workspace:ro \
  --volume /home/rm/rm26/docker/auto_aim/experiments/reversal-diagnosis-20261020:/output \
  daedalus:rm26-evaluator python3 /workspace/docker/auto_aim/reversal_diagnose.py report \
  --root /workspace --output /output
```

所有图像统计使用原始 JPEG，真值只用于评分端投影 ROI；颜色重叠只表示相关性。
不重新渲染，不调用修改后的检测器，不用历史筛选样本推算新命中率。
