# syntax=docker/dockerfile:1.7
ARG PLATFORM_BASE=daedalus:rm26-reference-yaw-corrected-20261029
ARG OPENVINO_SOURCE=sp_vision_dev:2024.6.0
ARG RUST_SOURCE=rust:1.98-slim-bookworm
FROM ${OPENVINO_SOURCE} AS openvino
FROM ${RUST_SOURCE} AS rust
FROM ${PLATFORM_BASE}
USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive
# Ubuntu 24.04 系统依赖：算法构建、模拟器构建、评分与视频共用。
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates build-essential cmake pkg-config clang libclang-dev llvm libssl-dev \
    libopencv-dev libopencv-dnn-dev libfmt-dev libspdlog-dev libyaml-cpp-dev \
    libeigen3-dev libusb-1.0-0-dev libceres-dev can-utils nlohmann-json3-dev \
    libx11-dev libxcursor-dev libxi-dev libxrandr-dev libxinerama-dev \
    libxkbcommon-dev libxkbcommon-x11-dev libwayland-dev libasound2-dev \
    libudev-dev libvulkan-dev libegl1-mesa-dev mesa-vulkan-drivers \
    python3-opencv python3-matplotlib python3-venv ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=openvino /opt/intel/openvino_2024.6.0.0 /opt/intel/openvino_2024.6.0.0
RUN ln -s /opt/intel/openvino_2024.6.0.0 /opt/intel/openvino && \
    ln -s /opt/intel/openvino_2024.6.0.0 /opt/intel/openvino_2024
COPY --from=rust /usr/local/rustup /usr/local/rustup
COPY --from=rust /usr/local/cargo /usr/local/cargo
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/tmp/aperture-cargo \
    PATH=/usr/local/cargo/bin:$PATH \
    OpenVINO_DIR=/opt/intel/openvino/runtime/cmake \
    OpenCV_DIR=/usr/lib/x86_64-linux-gnu/cmake/opencv4 \
    OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1 \
    BASH_ENV=/usr/local/lib/aperture/environment.sh
COPY docker/auto_aim/aperture-environment.sh /usr/local/lib/aperture/environment.sh
COPY docker/auto_aim/aperture-entrypoint.sh /usr/local/bin/aperture
LABEL org.opencontainers.image.title="APERTURE_vision" \
    org.opencontainers.image.source="https://github.com/Kunlun-APERTURE/APERTURE_vision_26" \
    aperture.unified="1" \
    aperture.openvino.version="2024.6.0"
RUN chmod 755 /usr/local/bin/aperture && \
    mkdir -p /work && \
    /usr/local/bin/aperture bash -c 'rustc --version; cargo --version; python3 -c "import openvino,cv2,matplotlib; print(openvino.__version__)"'
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/aperture"]
CMD ["bash"]
