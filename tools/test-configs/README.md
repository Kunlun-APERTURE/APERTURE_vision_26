# C++回归测试配置

三份配置来自工作站既有回归夹具，现纳入交接以便新clone运行测试。它们不是生产/实车配置，不替换 `sp_vision_25/configs/simulator.yaml`。

- continuity.yaml：tracking_fire_v2与开火启用。
- robustness.yaml：tracking_fire_v3，门限时间检查显式使用0.04s。
- optimization.yaml：tracking_fire_v4m，启用该测试所需的机动与预测资格路径。

`continuity_test`需要启用连续跟踪；传入默认legacy配置可能触发未初始化测试状态的段错误。现有测试程序没有完整的错误配置保护，不要将默认生产配置作为通用测试夹具。程序需在OpenVINO依赖环境、算法目录为当前工作目录时执行。
