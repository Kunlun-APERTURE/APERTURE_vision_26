#!/usr/bin/env bash
# 在统一镜像内运行默认及 ROS/bench 单测；源码只读，缓存写到 /work。
set -euo pipefail
export CARGO_HOME=/work/cargo
export CARGO_TARGET_DIR=/work/target
export IDL_PACKAGE_FILTER='geometry_msgs;sensor_msgs;std_msgs;visualization_msgs;tf2_msgs;rm_interfaces;rcl_interfaces;builtin_interfaces;action_msgs;unique_identifier_msgs'
export CMAKE_IDL_PACKAGES="$(python3 /project/vision_simulator_26/docker/ros_idl_packages.py)"
cd /project/vision_simulator_26
rustc --version
cargo test --locked --release --bin daedalus -j4
cargo test --locked --release --features 'ros2 bench' --bin daedalus -j4
