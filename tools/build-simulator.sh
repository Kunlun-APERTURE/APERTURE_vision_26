#!/usr/bin/env bash
# 在统一镜像内从只读源码构建，所有缓存与产物写到 /work。
set -eo pipefail
export CARGO_HOME=/work/cargo
export CARGO_TARGET_DIR=/work/target
export IDL_PACKAGE_FILTER='geometry_msgs;sensor_msgs;std_msgs;visualization_msgs;tf2_msgs;rm_interfaces;rcl_interfaces;builtin_interfaces;action_msgs;unique_identifier_msgs'
export CMAKE_IDL_PACKAGES="$(python3 /project/vision_simulator_26/docker/ros_idl_packages.py)"
cargo build --manifest-path /project/vision_simulator_26/Cargo.toml --release --locked --features 'ros2 bench' --bin daedalus -j4
