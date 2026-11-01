# 基线与复现参考

日常命令已集中到 [实验指南](EXPERIMENTS.md)，算法接入见 [开发指南](DEVELOPMENT.md)。本页保留高级复现和并发资格参考。

## 可移植实验包

先将 `冻结包路径` 替换为 suite 返回的真实 bundle 路径；输出归档和导入目录使用新名字：

```bash
./sim.sh experiment algorithm export --bundle 冻结包路径 --output exports/experiment.tgz --images
./sim.sh experiment algorithm import --bundle exports/experiment.tgz --output exports/restored --images
./sim.sh experiment suite --bundle exports/restored/payload --algorithm B1 --environment E1 --tier basic --cases hf_static_5 --sensor-seeds 1 --output exports/import-smoke --wait
```

镜像在旁边的 .images.tar，归档校验信息在 .manifest.json。完整复现依赖源码/配置/镜像和机器条件；导入不等于跨硬件一致性认证。该实验包与安装用的依赖包不同。

## 并发资格

已有完整串行参考实验后：

```bash
./sim.sh experiment algorithm qualify --output exports/pursuit-v1
```

每场景第一组种子追加一次串行和两次实际重叠的双路重复。比较规则如下；失败保留，不自动授予双路资格。更改运行快照后需重新检查，不能把历史17项通过直接继承到当前构建。

### 一致性比较的时间边界

`session-relative-v1` 比较从首个暖机输入到物理计分结束的全部 JPEG、输入元数据、算法输出；所有绝对时间戳减去同一会话原点，步序减去会话首步。保持帧间隔、输入/处理/姿态延迟及实际控制不变。计分结束后的媒体尾帧数量单独记录；全部发射弹丸仍必须完成结算并比较碰撞点与事件。原始绝对时间比较、原生飞行时间诊断和首次分歧同时保留。

R4 的 last_good_time 在首次合格观测之前为默认零时刻，last_good_age_s 因此包含绝对 epoch；仅在其值大于10亿秒时标记为未初始化诊断，有限实际年龄仍逐值比较。逐发诊断比较使用记录的碰撞时刻减发射时刻计算飞行时间，原生字段保留在源证据。此规则不允许舍入瞄准角、容差比较图像或忽略控制/命中差异。
