# 首次镜像迁移归档

单一 APERTURE_vision 镜像已完成发布和匿名下载验收，日常入口是 [版本与镜像发布](../../docs/RELEASES.md)。本目录退出 Actions 列表，只保留一次性迁移的审计材料。

- dependency-bootstrap.yml：原“统一镜像首次发布”工作流。它从旧 Release 附件导入已验收归档，不是今后每次发版的步骤。
- unified-bootstrap.json：首批归档实际来源、大小和 SHA256。归档仍保留在原 Release，既有引用不移动。
- releases-before-cleanup.json：本轮整理前两份 GitHub Release 说明的快照；更新说明只校正导航与状态，保留成绩和历史版本标签。

五镜像迁移及原始清单保留在 release/20261101-ghcr/ 和 release/dependencies-bootstrap.json；当前构建和拉取仅以 release/dependencies.json 的单镜像清单为准。不要把归档工作流复制回日常 Actions 后直接运行。
