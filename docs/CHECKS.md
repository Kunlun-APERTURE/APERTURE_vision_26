# 可用性自检

## 工作区与命令

从完整主仓库根目录执行：

```bash
python3 tools/check-workspace.py
./sim.sh --help
./sim.sh experiment suite --tier advanced --cases variable,turn04 --list
```

检查器核对子仓库是否初始化、提交是否匹配、是否有未提交修改。仅联调文件的桌面同步副本不能通过完整工作区检查，桌面入口见[部署说明](DEPLOYMENT.md)。

## C++构建与回归测试

以下命令需要本机已有`sp_vision_dev:2024.6.0`依赖镜像，在宿主机的完整主仓库根目录执行。源码只读挂载，构建与测试输出写入独立目录，不覆盖已验的`sp_vision_25/build/simulator`。

```bash
mkdir -p docker/auto_aim/.runtime/cpp-checks
docker run --rm --user "$(id -u):$(id -g)" \
  --volume "$PWD:/project:ro" \
  --volume "$PWD/docker/auto_aim/.runtime/cpp-checks:/work" \
  --workdir /work \
  --entrypoint bash sp_vision_dev:2024.6.0 -c '
    set -e
    source /opt/intel/openvino/setupvars.sh >/dev/null
    ln -sfn /project/sp_vision_25/assets /work/assets
    cmake -S /project/sp_vision_25 -B /work/build \
      -DBUILD_SIMULATOR_ADAPTER=ON -DCMAKE_BUILD_TYPE=Release \
      -DOpenCV_DIR=/usr/lib/x86_64-linux-gnu/cmake/opencv4
    cmake --build /work/build --target standard auto_aim_test sim_auto_aim \
      continuity_test robustness_test optimization_test motion_test \
      perception_test maneuver_filter_test -j4
    /work/build/continuity_test /project/tools/test-configs/continuity.yaml
    /work/build/robustness_test /project/tools/test-configs/robustness.yaml
    /work/build/optimization_test /project/tools/test-configs/optimization.yaml
    /work/build/motion_test /project/sp_vision_25/configs/simulator.yaml
    /work/build/perception_test /project/sp_vision_25/configs/simulator.yaml
    /work/build/maneuver_filter_test
    /work/build/sim_auto_aim --help
  '
```

三个专用配置是测试夹具，不是生产配置。不要用默认legacy配置替代continuity/robustness/optimization夹具。motion/perception测试在可写`/work`生成临时配置，模型资源从算法目录读取。

宿主机直接运行容器构建的二进制可能缺少共享库。空机器需先准备依赖镜像，见[部署说明](DEPLOYMENT.md)。

## 后台冒烟

按[快速开始](QUICKSTART.md)提交首轮实验，再从仓库根目录执行 `./sim.sh jobs status JOB_ID` 和 `./sim.sh jobs report JOB_ID`；将 `JOB_ID` 替换为提交返回的实际 ID。

检查任务完成、summary.json未判invalid、session-evidence-audit.json通过、控制与射击记录存在、所有已发射弹丸结算。零射击或低命中可以是有效场景结果；若要验收射击链路，应另外确认实际发生发射。运行耗时、命中表现与运动强度分开判断。

## 框架自动测试

统一测试入口与 Coverage 门槛在 [开发指南](DEVELOPMENT.md#自动测试与覆盖率) 维护。后台首轮验收见 [快速开始](QUICKSTART.md)，历史验收不会自动继承给新产物。
