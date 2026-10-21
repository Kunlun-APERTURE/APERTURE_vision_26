#!/usr/bin/env python3
"""Receive real ROS messages; topic discovery alone is insufficient."""

import argparse
import json
import time

import rclpy
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import CameraInfo, CompressedImage
from tf2_msgs.msg import TFMessage


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=20.0)
    args = parser.parse_args()
    rclpy.init()
    node = rclpy.create_node("rm26_daedalus_probe")
    counts = {"/camera_info": 0, "/tf": 0, "/image_raw/compressed": 0}
    image_bytes = 0
    image_format = ""
    camera_size = []
    started = time.monotonic()

    def camera(message):
        nonlocal camera_size
        if message.width > 0 and message.height > 0:
            counts["/camera_info"] += 1
            camera_size = [message.width, message.height]

    def transforms(message):
        if message.transforms:
            counts["/tf"] += 1

    def image(message):
        nonlocal image_bytes, image_format
        if message.data:
            counts["/image_raw/compressed"] += 1
            image_bytes += len(message.data)
            image_format = message.format

    subscriptions = [
        node.create_subscription(CameraInfo, "/camera_info", camera, qos_profile_sensor_data),
        node.create_subscription(TFMessage, "/tf", transforms, qos_profile_sensor_data),
        node.create_subscription(
            CompressedImage, "/image_raw/compressed", image, qos_profile_sensor_data
        ),
    ]
    try:
        while time.monotonic() - started < args.duration:
            rclpy.spin_once(node, timeout_sec=0.2)
        elapsed = time.monotonic() - started
        nodes = [f"{namespace.rstrip('/')}/{name}" for name, namespace in node.get_node_names_and_namespaces()]
        result = {
            "duration_seconds": round(elapsed, 2),
            "nodes": nodes,
            "messages": counts,
            "camera_size": camera_size,
            "image_bytes": image_bytes,
            "image_format": image_format,
            "image_hz": round(counts["/image_raw/compressed"] / elapsed, 2),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if all(counts.values()) and "/robomaster/simulator" in nodes else 1
    finally:
        # Keep subscriptions alive until the receive loop has finished.
        del subscriptions
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
