# B1 / E1 多种子算法诊断

固定原始 B1/E1 冻结包和 NVIDIA 后端。阶段计数是观察归类，不代表已证明的因果关系。

|场景|轮次|命中/发射|精确命中率|发射/秒|命中/秒|加权得分/秒|
|---|---:|---:|---:|---:|---:|---:|
|basic_move3_spin_neg|3|17/22|77.3%|0.815|0.630|0.462|
|basic_move3_spin_pos|3|17/23|73.9%|0.852|0.630|0.514|
|basic_move4_spin_neg|3|15/18|83.3%|0.667|0.556|0.422|
|basic_move4_spin_pos|3|16/19|84.2%|0.704|0.593|0.497|
|ego_right_centered|3|115/116|99.1%|4.833|4.792|4.270|
|hf_spin_neg|3|61/67|91.0%|3.722|3.389|2.569|
|hf_spin_pos|3|64/68|94.1%|3.778|3.556|2.665|
|hf_static_5|3|84/84|100.0%|4.667|4.667|4.367|
|hf_turn90_3|3|40/55|72.7%|3.333|2.424|1.140|
|hf_uniform_3|3|43/50|86.0%|3.333|2.867|1.577|

## 计分阶段的观察

各列按计分帧独立计数，并非单调递减漏斗；角度对准等 fire_check 条件可以重叠。

|场景|计分帧|无检测|其中存在可见装甲|未跟踪阶段|未对准阶段|建议帧|
|---|---:|---:|---:|---:|---:|---:|
|basic_move3_spin_neg|780|43|42|397|232|93|
|basic_move3_spin_pos|780|14|13|405|253|105|
|basic_move4_spin_neg|780|421|73|176|95|88|
|basic_move4_spin_pos|780|34|33|383|262|95|
|ego_right_centered|693|42|40|10|13|624|
|hf_spin_neg|519|14|12|33|202|260|
|hf_spin_pos|519|6|4|30|191|291|
|hf_static_5|519|38|31|39|0|434|
|hf_turn90_3|477|34|34|37|204|199|
|hf_uniform_3|432|31|31|44|188|167|

## 同种子重复

- hf_static_5 / 2026110101：JPEG 序列相同=True，归一化事件相同=True，归一化逐发记录相同=True；首个事件差异=None。
- basic_move3_spin_pos / 2026110101：JPEG 序列相同=True，归一化事件相同=True，归一化逐发记录相同=True；首个事件差异=None。

## 逐帧复核样本

每个移动旋转场景按加权得分/秒最低选择，同分取较小 seed。

- basic_move3_spin_neg / 2026110102：`20261101T123206_basic_move3_spin_neg_shoot_muzzle_no-drag_25p0_25p0_feedforward_5p0hz_tracking_fire_v3_b5571216`。
- basic_move3_spin_pos / 2026110103：`20261101T123108_basic_move3_spin_pos_shoot_muzzle_no-drag_25p0_25p0_feedforward_5p0hz_tracking_fire_v3_14a332f5`。
- basic_move4_spin_neg / 2026110102：`20261101T123439_basic_move4_spin_neg_shoot_muzzle_no-drag_25p0_25p0_feedforward_5p0hz_tracking_fire_v3_8dbd8858`。
- basic_move4_spin_pos / 2026110103：`20261101T123348_basic_move4_spin_pos_shoot_muzzle_no-drag_25p0_25p0_feedforward_5p0hz_tracking_fire_v3_3587701a`。

逐轮数值见 trials.csv，逐帧记录与重复对照细节见 diagnosis.json 和各 *.frames.jsonl。
零发射保持 N/A；传感器种子只改变噪声，不代表新路线。单次重复不能证明统计稳定性。
实车时延与标定仍未认证；仿真分数不能代替实车性能。
