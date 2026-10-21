# 仿真自瞄鲁棒性 v3 实施与独立验收

独立矩阵完成：228/228；规则验收：未全部通过；人工视觉：待确认；基线未升级。

完成原图诊断/回放、轨迹内弱候选、颜色灯条矫正与多尺度检测、联合位姿估计、径向过程噪声及时间制开火恢复；新增双方随机运动、距离变化、旋转平移叠加实验。

| 算法 | 实验 | 有效命中/发射 | 有效命中/秒 |
|---|---:|---:|---:|
| tracking_v2 | 114 | 5942/7092 | 3.512 |
| tracking_fire_v3 | 114 | 6486/7072 | 3.833 |

上表是所有不同场景的描述性汇总，具体是否通过以每个场景/弹道的配对门槛为准。

全体有效开发与验证记录最长完整用时 35.678 秒，最短实际发射间隔 0.200553 秒；原始证据哈希、JPEG索引与视频解码帧数检查通过。

## 未通过的独立验收条件

- distance_3/no-drag：useful_hits_per_s_improved
- distance_5/no-drag：useful_hits_per_s_improved
- line/drag：steady_useful_hits_nonregression
- line/no-drag：steady_useful_hits_nonregression
- random_both_05/drag：first_hit_censor_nonregression
- range_in_out/no-drag：first_hit_nonregression
- spin/no-drag：useful_hits_per_s_improved
- spin_move_05_neg/drag：first_hit_censor_nonregression
- spin_move_05_neg/no-drag：useful_hits_per_s_improved, first_hit_censor_nonregression
- spin_move_05_pos/drag：useful_hits_per_s_improved, first_hit_nonregression
- spin_move_05_pos/no-drag：first_hit_censor_nonregression
- spin_move_15_neg/drag：useful_hits_per_s_improved, first_hit_censor_nonregression, first_hit_nonregression
- spin_move_15_neg/no-drag：useful_hits_per_s_improved, first_hit_censor_nonregression
- spin_move_15_pos/drag：useful_hits_per_s_improved, first_hit_censor_nonregression
- spin_move_15_pos/no-drag：useful_hits_per_s_improved, first_hit_censor_nonregression
- spin_move_ego_05/drag：useful_hits_per_s_improved, first_hit_censor_nonregression, first_hit_nonregression
- spin_move_ego_05/no-drag：useful_hits_per_s_improved, first_hit_censor_nonregression, first_hit_nonregression
- spin_move_ego_15/drag：useful_hits_per_s_improved, first_hit_censor_nonregression
- spin_move_ego_15/no-drag：useful_hits_per_s_improved, first_hit_censor_nonregression, first_hit_nonregression

## 解释限制

- 新场景要求有效命中/秒严格提高；已接近5Hz上限的静止条件可能因平手不通过该要求，报告没有放宽规则。
- 高强度组是2m/s指令、0.2–0.4s换向；受现有底盘控制限制，实际速度显著更低，不能宣称实际2m/s能力已通过。
- 8m的小像素目标仍可能有深度与朝向歧义；保留误差、跟踪和命中三类结果。
- 非旋转按正面装甲，旋转按目标任意有效装甲评价；随机运动首次命中在下一次指令变化处截断，同时报告截断比例。
- 开发记录及单次显示开关对照不能替代独立验收；早期被建筑遮挡的距离路线明确标记为场景设计问题。

[交互总报告](report.html) · [失败片段与相同种子对照](failure-clips.html) · [逐条件结果](result-summary.json) · [证据核对](integrity.json)
