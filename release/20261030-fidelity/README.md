# 阶段精简证据

只保存来源/数值/运行检查摘要和必要图片；完整报告、六轮原始输入、遥测、视频、镜像仍在工作站，不随Git下载。

- `validation.json`：各层验收状态与未通过范围。
- `component-summary.json`：源图摘要、数值误差及入口检查。
- `integration-checks.json`：被测源码、二进制、镜像与测试统计。
- `position-step-metrics.json`：位置环部件超调，非实物测量。
- `formula.png`：官方原公式归档；来源URL保存在component摘要中。
- `corrected-response.png`：公式/模型线一致性与生产部件响应。
- `evidence-index.json`：本机完整材料位置与报告/视频摘要；摘要不代表已分发文件。

完整模型来源与限制见../../docker/auto_aim/REFERENCE_YAW.md。视频团队审查待完成，拟真与实车效果未标定。
