# 三仓库协作

## 获取与更新

```bash
git clone --recurse-submodules <主仓库URL> rm26
cd rm26
python3 tools/check-workspace.py
# 拉取主仓库后，取其指定版本；有本地修改时先提交或自行保存。
git submodule update --init --recursive
```

主仓库的gitlink是源码版本的唯一依据；`.gitmodules`只提供仓库URL。不要用`update --remote`替代固定版本复现。submodule常处于detached HEAD，这是固定提交的正常状态，修改代码前先创建自己的分支。

```bash
git -C sp_vision_25 switch -c feature/my-change
# 在对应子仓库修改、测试、提交并push。
git -C sp_vision_25 push -u origin feature/my-change
# 集成方核验后，在主仓库更新引用。
git add sp_vision_25
git commit -m 'chore(deps): select validated algorithm revision'
git push --recurse-submodules=check origin HEAD
```

`check`只检查远端可达性，不代替测试。先push子仓库的分支/标签，再push主仓库；队友必须有三个仓库的读取权限。统一阶段标签是便于导航的约定，实际依赖仍由主仓库记录的SHA确定。算法与模拟器不需要每次一起发版。

## 原有工作站与标准新克隆

当前主机保留子目录原有独立`.git/`目录（Git支持的旧式submodule布局），以保持已有容器单独挂载的兼容性；不要直接在运行中的工作站执行`absorbgitdirs`。子仓库历史、分支、远端和源码未变。

新递归克隆通常使用子目录`.git`文件，元数据在主仓库`.git/modules/`。新桌面部署脚本将完整工作区挂到`/workspace/rm26`，保留既有两条源码路径，并映射它们相对引用所需的元数据；VSCode可打开根目录`RM26.code-workspace`。

已运行Sunshine的挂载不自动改变；当前桌面继续使用原有两个真实源码挂载和同步后的联调副本。桌面副本不是宿主机主仓库的实时映射，不能在两份主仓库同时开发。未来安排桌面容器重建后，统一使用`/workspace/rm26`，不再依赖副本。本轮不重启正在使用的桌面。

## 分发与验收

Git引用只锁定源码，不分发镜像、数据、模型缓存或当前二进制。原验收清单继续记录这些摘要；重新构建不自动继承历史成绩。旧patch保留为历史审计，不再作为正常源码交付入口。

远端尚未具备本阶段提交时，递归克隆将失败；本地验收通过受控URL覆盖，从两个本机仓库初始化，不改`.gitmodules`里的公开URL。正式上传后还需从远端新克隆一次。本机离线bundle可以保存提交，但主仓库bundle本身不包含子仓库Git对象。
