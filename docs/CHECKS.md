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

先按 [快速开始](QUICKSTART.md#获取依赖包) 获取统一镜像，然后在仓库根目录执行：

```bash
mkdir -p docker/auto_aim/.runtime/cpp-checks
docker run --rm --network none --user "$(id -u):$(id -g)" \
  --volume "$PWD:/project:ro" \
  --volume "$PWD/docker/auto_aim/.runtime/cpp-checks:/work" \
  --workdir /work aperture_vision:26-11-1 \
  bash /project/tools/validate-ubuntu24-components.sh
```

[组件脚本](../tools/validate-ubuntu24-components.sh) 是构建与回归程序列表的唯一执行入口，加载 ROS/OpenVINO 后构建9个目标并运行6个回归程序。源码只读，结果写入上述独立目录。专用 YAML 是测试夹具，不替代生产配置；不要直接在宿主机运行容器构建的二进制。

模拟器源码编译见 [开发指南](DEVELOPMENT.md#编译模拟器)，其已知单元测试失败见 [能力与限制](STATUS.md)。完整环境镜像重建与发布分别见运维和发布指南。

## 后台冒烟

按[快速开始](QUICKSTART.md)提交首轮实验，再从仓库根目录执行 `./sim.sh jobs status JOB_ID` 和 `./sim.sh jobs report JOB_ID`；将 `JOB_ID` 替换为提交返回的实际 ID。

检查任务完成、summary.json未判invalid、session-evidence-audit.json通过、控制与射击记录存在、所有已发射弹丸结算。零射击或低命中可以是有效场景结果；若要验收射击链路，应另外确认实际发生发射。运行耗时、命中表现与运动强度分开判断。

## 框架自动测试

统一测试入口与 Coverage 门槛在 [开发指南](DEVELOPMENT.md#自动测试与覆盖率) 维护。后台首轮验收见 [快速开始](QUICKSTART.md)，历史验收不会自动继承给新产物。
