# 可用性自检

## 仓库与命令

```bash
python3 tools/check-workspace.py
./sim.sh --help
./sim.sh experiment suite --tier advanced --cases variable,turn04 --list
```

完整工作区应通过版本检查。当前Sunshine里 `~/workspaces/rm26-sim` 仍是仅联调文件的同步副本，不能把它当成完整递归clone；桌面使用原 `~/workspaces/RM26-三仓库.code-workspace`，其两个源码目录指向 `/workspace/`真实挂载。副本中的根 `RM26.code-workspace` 适用于完整clone/未来完整挂载，而非当前仅联调副本。

## C++源码构建与测试

在已配置OpenVINO/C++依赖的环境中，从算法目录构建到**独立目录**，不覆盖已验 `build/simulator`。以下源码/夹具路径以完整主仓库ROOT为例：

```bash
# 先在依赖环境中设置ROOT与独立BUILD目录。
cmake -S "$ROOT/sp_vision_25" -B "$BUILD" -DBUILD_SIMULATOR_ADAPTER=ON \
  -DCMAKE_BUILD_TYPE=Release -DOpenCV_DIR=/usr/lib/x86_64-linux-gnu/cmake/opencv4
cmake --build "$BUILD" --target sim_auto_aim continuity_test robustness_test \
  optimization_test motion_test perception_test maneuver_filter_test -j4
cd "$ROOT/sp_vision_25"
"$BUILD/continuity_test" "$ROOT/tools/test-configs/continuity.yaml"
"$BUILD/robustness_test" "$ROOT/tools/test-configs/robustness.yaml"
"$BUILD/optimization_test" "$ROOT/tools/test-configs/optimization.yaml"
"$BUILD/motion_test" configs/simulator.yaml
"$BUILD/perception_test" configs/simulator.yaml
"$BUILD/maneuver_filter_test"
"$BUILD/sim_auto_aim" --help
```

测试执行环境需要可写 `/work`（motion/perception夹具在此生成临时配置），算法资源仍从当前目录读取。本轮使用本机已有 `sp_vision_dev:2024.6.0` Docker依赖环境；宿主机直接运行容器构建的二进制可能缺共享库。空机器的依赖镜像制作仍按部署文档准备，不由submodule自动完成。

## 后台实际运行

使用联调README中受支持的 `hf_static_8` lockstep示例。`completed`后还要核对该轮 `summary.json`有效性和`session-evidence-audit.json`完整性。本轮已用已验算法二进制完成一次3秒计分的短测：完整18.70秒，15发15中。它只验证运行链路，不替代多场景效果/拟真验收；新编译候选并未替换生产二进制。

## 结果与边界

2026-10-31：独立C++构建9个目标成功（包括原standard和auto_aim_test入口），6个测试程序及适配器help通过；旧continuity程序使用正确夹具也通过。之前段错误与给测试程序传默认legacy配置有关，不应作为当前源码构建失败的证据。测试程序对错误配置仍缺少保护。

离线恢复、submodule引用、宿主机CLI、桌面SSH命令入口和后台冻结/评分可用。GitHub远端递归clone现已通过；全新机器依赖部署及正在运行桌面的完整主仓库挂载尚未验证/应用，仍不能称为开箱即用安装包。
