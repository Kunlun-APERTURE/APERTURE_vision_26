# Yaw参考模型与勘误

官方[原帖](https://bbs.robomaster.com/article/3559?source=5)的公式为：

`G(s)=(-12.65s+1084)/(s²+34.16s+975)`

|模型ID|含义|
|---|---|
|`legacy`|默认原控制输出直接积分|
|`rm2017-yaw-unitdc`|历史误录分母43.16/3975，只作历史复现|
|`rm2017-yaw-corrected-raw`|正确原式，DC=1084/975≈1.111795|
|`rm2017-yaw-corrected-unitdc`|正确原式乘975/1084；单位DC为额外假设|

`--gimbal-dynamics`与位置控制器`--gimbal-profile`独立。新ID由入口选择`daedalus:rm26-reference-yaw-corrected-20261029`镜像；混合模型矩阵冻结支持全部ID的新镜像。固定历史highspeed-v1矩阵不接受非legacy，应选择具体场景与matrix single。

来源是已带速度PID的目标yaw角速度→实际陀螺角速度闭环响应，不是电机力矩模型或当前机器人的标定。原帖全部数据拟合88.58%不代表留出集或本模拟器拟真度。本模拟器外位置PID/前馈、限速和pitch旧路径另属于假设。

正确公式−3dB带宽约6.4187Hz，与原帖自洽。此前“原文与公式冲突”的结论撤回；误录模型下132轮不能当成官方模型拟真证据。旧模型与封存资料不静默重写。

部件检查入口：`python3 docker/auto_aim/reference_yaw_correction_checks.py`。源图、核心验收数字和运行digest见 [阶段证据](../../release/20261030-fidelity/README.md)。完整检查读取本机旧归档来复核历史输出，克隆仓库后需另行取得这些资料。

已通过的是来源/数值一致性与特定静止/平移条件的后台运行复现，实车拟真未验证。现有位置PID串联后仍有较大超调，枪口旋转速度和旋转装甲连续判碰待修；见 [阶段状态](../../docs/STATUS.md)。
