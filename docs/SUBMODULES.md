# 三仓库协作

主仓库固定算法与模拟器的提交组合；两个submodule各自保留远端、分支、历史、构建方式和许可证。

## 获取与更新

```bash
git clone --recurse-submodules https://github.com/angjustinl/APERTURE_vision_26.git
cd APERTURE_vision_26
python3 tools/check-workspace.py
```

已有完整克隆先提交或自行保存本地修改，再更新：

```bash
git pull --ff-only
git submodule update --init --recursive
python3 tools/check-workspace.py
```

主仓库记录的gitlink是源码版本依据；`.gitmodules`提供下载URL。`update --remote`会选择另一提交，不用于固定版本复现。初始化后的detached HEAD是正常状态，修改前需在子仓库创建开发分支。

## 独立开发与上传

以算法为例：

```bash
git -C sp_vision_25 switch -c feature/my-change
# 在算法仓库完成修改、测试并提交。
git -C sp_vision_25 push -u origin feature/my-change

# 验证组合后，在主仓库记录新的子提交。
git add sp_vision_25
git commit -m 'chore(deps): select validated algorithm revision'
git push --recurse-submodules=check origin HEAD
```

模拟器使用相同流程，路径换为`vision_simulator_26`。先上传子仓库提交，再上传主仓库引用；`check`检查引用是否已在子仓库远端可获取，不代替功能测试。队友需要三个仓库的读取权限，当前主仓库为私有。

统一阶段标签便于定位组合；已有标签保留原义，新阶段使用新标签。算法与模拟器不必同步发版，组合提交仍由主仓库确定。

## 工作区与容器

标准新克隆的子目录`.git`通常是文件，实际元数据位于主仓库`.git/modules/`。容器只挂载子目录时可能无法访问这些元数据。新部署定义提供完整项目挂载`/workspace/rm26`及旧路径兼容入口；完整工作区可打开根目录`RM26.code-workspace`。

现有工作站保留子目录的独立`.git/`目录，以兼容已运行容器。不要在未调整挂载时执行`absorbgitdirs`。运行中的Sunshine使用两个真实源码挂载，联调目录为单独同步副本；具体位置见[部署说明](DEPLOYMENT.md)，不要在两份主仓库同时开发。

## 源码与运行产物

GitHub递归克隆已验证。Git引用只锁定源码，Docker镜像、原始实验、视频与已验二进制需另行准备。新构建不自动继承旧成绩。

离线交接需要主仓库及两个子仓库各自的bundle；主仓库bundle不包含子仓库对象。历史patch是审计备份，正常获取源码使用submodule。
