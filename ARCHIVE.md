# RM26 仿真自瞄本地存档

本目录是仅保存在工作站的联调集成 Git 仓库。算法、模拟器、集成部署与评分工具分别保留各自历史。`sp_vision_25/` 和 `vision_simulator_26/` 的源码提交哈希及实验冻结快照校验值记录于 `archive/robustness-v3-20261020/archive-index.json`。

## v3 验证存档

- 228 次独立验证：`tracking_v2` 对照 `tracking_fire_v3`。
- 有效命中率：83.78% → 91.71%；结果以原实验评分口径记录。
- 规则验收未全部通过，人工视觉复核待完成，默认基线未升级。
- 冻结的算法与模拟器源码补丁、配置、源码包和算法二进制位于 `archive/robustness-v3-20261020/frozen/`；验证概要、逐次实验摘要和证据索引位于同目录。
- 原始视频、JPEG、遥测及逐发证据仍保存在 `docker/auto_aim/experiments/robustness-v3-20261020/`，不纳入 Git。索引保留运行清单、证据校验摘要及模型和镜像标识。

## 提交边界

根仓库跟踪 `sim.sh`、`docker/auto_aim/` 的源码与配置、`docker/daedalus/` 的部署说明与脚本及本存档。Sunshine 桌面 HOME、SSH 密钥、Xauthority、日志、运行态配置、缓存、构建产物和原始实验资料由 `.gitignore` 排除。

所有提交留在本地；此存档不配置 GitHub 远程，也不执行 push。
