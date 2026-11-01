# 开发指南

环境、依赖包和第一次构建按 [快速开始](QUICKSTART.md)；日常实验与比较按 [实验指南](EXPERIMENTS.md)。本页只定义开发入口和扩展方法，提交流程见 [贡献指南](../CONTRIBUTING.md)。所有命令从主仓库根目录执行。

## 修改算法

算法核心在 `sp_vision_25/`，主仓库只负责适配、实验和评分。修改 C++ 后重新构建：

```bash
python3 tools/development.py build
```

B1 配置在 `docker/auto_aim/benchmark/configs/B1.yaml`，其补偿参数属于仿真配置，不能直接当作实车标定。构建之后修改配置会触发来源一致性检查，应重新构建或创建独立 manifest。

## 编译模拟器

统一镜像包含 Rust 1.98.1 和 ROS 2 构建依赖。首次编译需下载 Cargo.lock 固定的 crate；缓存与产物写到独立目录：

```bash
mkdir -p exports/simulator-build
docker run --rm --network host --user "$(id -u):$(id -g)" \
  --volume "$PWD:/project:ro" --volume "$PWD/exports/simulator-build:/work" \
  --workdir /work aperture_vision:26-11-1 bash /project/tools/build-simulator.sh
```

产物为 `exports/simulator-build/target/release/daedalus`。源码只读，修改 Rust 后重新执行同一命令。平台源码编译成功后仍需构建并验收新的环境镜像，不能将历史评分环境当作新平台版本。

## 独立算法接入

`AlgorithmAdapter` 提供 build/describe；JSON manifest 由 `ProcessAdapter` 接入独立产物。完整可运行例子：

```bash
python3 tools/development.py example
./sim.sh experiment suite --algorithm exports/idle-example/adapter.json --artifact docker/auto_aim/benchmark/examples/idle_algorithm.py --cases example_static --sensor-seeds 1 --output exports/example-run --wait
```

调度器需运行，启动方法见快速开始。例子独立于 R4，只消费图像并返回无效控制，不开火；验证的是协议和新增场景链路。已有 example-run 时换新目录。

|manifest 字段|说明|
|---|---|
|id / version|算法名称和版本|
|protocol|`rm26-image-v1`|
|capabilities|至少包含 simulation_clock、session_reset、fire_advice|
|image|运行镜像，团队默认使用快速开始中的统一镜像|
|runtime_image|可选，让模拟器、桥接和评分也使用该统一镜像；需具备对应接口和云台来源标记|
|binary_sha256|传入 `--artifact` 文件的 SHA256|
|config / config_sha256|相对 manifest 的配置文件路径及校验值|
|algorithm_profile|独立通用算法用 `image_v1`|
|launch|启动参数数组，支持 `{port}`、`{fire}`、`{aim_origin}`、`{execution_mode}`|
|setup|可选环境初始化命令；不需要 OpenVINO 时设为空字符串|
|resources|可选，相对 manifest 的模型资源目录|

产物在容器内统一挂载到 `/snapshot/sp_vision_25/build/simulator/sim_auto_aim`，配置为 `/snapshot/sp_vision_25/configs/simulator.yaml`，资源在 `/snapshot/sp_vision_25/assets`；`/work` 可写。名称是兼容路径，不要求算法必须用 C++。配置示例由 example 命令生成，不必手写哈希。

协议细节见 [接口参考](INTERFACES.md)。必须响应会话重置，并返回 `detector_history=session`、首次会话结果的 `detector_reset=true`，以满足当前会话审计；内部跟踪／切板等诊断可以缺省。先参照 [最小算法](../docker/auto_aim/benchmark/examples/idle_algorithm.py)，再替换自己的处理逻辑。

## 新增场景

参照 [ExampleStatic](../docker/auto_aim/benchmark/examples/scene.py) 实现 Scenario：generate 返回脚本、时长、语义和所需阶段信息；difficulty 做事后评估。然后在 [注册表](../docker/auto_aim/benchmark/scenarios.py) 注册唯一名称，使用 `--cases 名称` 选择。当前没有自动扫描外部插件机制。

轨迹由场景生成，真实速度与命中由记录评估；目标真值不应回流算法。validate 接口可供扩展，但当前没有通用 CLI 参数注入流程，新增参数需同步设计入口和测试。

## 自动测试与覆盖率

使用 Python 3.12 venv 安装固定测试依赖，不需要 GPU。完整本地闭环也可使用 CPU，选择与耗时见 [实验指南](EXPERIMENTS.md#渲染模式)：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install coverage==7.17.0
.venv/bin/python tools/test-development.py
```

CI 使用同一测试入口。报告在 `.coverage-reports/html/index.html`，门槛为语句≥80%、分支≥70%。统计包含 benchmark 和 pr_service 的生产模块；不能以总体覆盖率替代具体 Docker／GitHub 路径验收。`.venv/` 与覆盖率产物不提交。

C++ 组件的独立测试命令见 [组件检查](CHECKS.md)。增加测试优先覆盖输入协议、场景扩展、零射击、异常、取消、恢复和清理边界。
