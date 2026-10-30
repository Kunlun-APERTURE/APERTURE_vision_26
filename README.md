# RM26 仿真联调与实验平台

本仓库负责部署、图像/控制桥接、后台批量实验、评分与视觉验收；算法与模拟器保留独立仓库。当前阶段是**拟真核算与纠错**，尚未完成实车标定。

## 队友从哪里开始

1. 阅读 [部署前提](docs/DEPLOYMENT.md)，确认使用现有工作站还是部署新机器。
2. 阅读 [运行与后台实验](docker/auto_aim/README.md)。先检查状态，再做一轮短测。
3. 阅读 [阶段结论与已知问题](docs/STATUS.md)；查看 [版本对应表](release/20261030-fidelity.json)。
4. 需要移动旋转场景时看 [测试分档](docker/auto_aim/MOVEMENT_TIERS.md)；团队复核看 [视频流程](docker/auto_aim/REVIEW_VIDEO.md)。

## 三个仓库

|目录|仓库|职责|
|---|---|---|
|本目录|rm26 集成仓库|部署、桥接、实验、评分、版本清单|
|`sp_vision_25/`|https://github.com/angjustinl/sp_vision_25|视觉估计、预测与开火建议；不依赖模拟器真值|
|`vision_simulator_26/`|https://github.com/ANGJustinl/vision_simulator_26|场景、渲染、车辆/弹丸与命中事件|

算法与模拟器目录由根仓库忽略，**不是 submodule**。分别克隆到对应目录，再按版本清单 checkout；只克隆集成仓库不会自动取得它们。三个仓库使用同名标签 `stage-20261030-fidelity`，清单同时记录不可变提交SHA。

## 分享范围

Git保存源码、必要使用文档、精简验收摘要与版本/证据摘要。实验原图、遥测、视频、Docker镜像、模型缓存、SSH密钥与工作站HOME不在本次新增提交范围内。旧Git历史保留已有的v3小型冻结归档，未重写历史。

原始实验仍留在工作站 `docker/auto_aim/experiments/`，队友克隆后不会自动获得。镜像digest只能标识已有镜像，无法替代可下载镜像或完整构建环境；新机器部署限制见部署文档。

开发过程的逐轮调参笔记未纳入分享，复现实验所需的控制器与分析源码仍保留。历史v3归档见 [ARCHIVE.md](ARCHIVE.md)，不可与当前评分或拟真结论直接混用。
