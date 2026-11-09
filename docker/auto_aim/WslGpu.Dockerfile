# Local overlay: preserve the registry-pinned SDK and its algorithm/ROS interfaces.
ARG BASE_TAG
FROM ${BASE_TAG}
ARG BASE_IMAGE
ARG SOURCE_SHA256
LABEL aperture.base_image="${BASE_IMAGE}" \
    aperture.wsl_gpu="mesa-dozen-webgl2" \
    aperture.simulator.source_sha256="${SOURCE_SHA256}"
COPY out/daedalus /app/daedalus
COPY out/lib/ /app/lib/
COPY drivers/ /opt/aperture-wsl-vulkan/
ENV WGPU_SETTINGS_PRIO=webgl2 \
    WGPU_ALLOW_UNDERLYING_NONCOMPLIANT_ADAPTER=1 \
    WGPU_ADAPTER_NAME=NVIDIA \
    VK_DRIVER_FILES=/opt/aperture-wsl-vulkan/dzn_icd.json \
    VK_ICD_FILENAMES=/opt/aperture-wsl-vulkan/dzn_icd.json \
    LD_LIBRARY_PATH=/opt/aperture-wsl-vulkan:/usr/lib/wsl/lib:/app/lib:/ros2_ws/install/lib:/opt/ros/jazzy/lib
