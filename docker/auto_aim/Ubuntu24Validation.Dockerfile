ARG OPENVINO_SOURCE=sp_vision_dev:2024.6.0
ARG VALIDATION_BASE=daedalus:rm26-reference-yaw-corrected-20261029
FROM ${OPENVINO_SOURCE} AS toolkit
FROM ${VALIDATION_BASE}
USER root
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    git g++ make cmake pkg-config libopencv-dev libopencv-dnn-dev \
    libfmt-dev libspdlog-dev libyaml-cpp-dev libeigen3-dev libusb-1.0-0-dev \
    libceres-dev can-utils nlohmann-json3-dev libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=toolkit /opt/intel/openvino_2024.6.0.0 /opt/intel/openvino_2024.6.0.0
RUN ln -s /opt/intel/openvino_2024.6.0.0 /opt/intel/openvino && \
    ln -s /opt/intel/openvino_2024.6.0.0 /opt/intel/openvino_2024
ENV OpenVINO_DIR=/opt/intel/openvino/runtime/cmake \
    OpenCV_DIR=/usr/lib/x86_64-linux-gnu/cmake/opencv4 \
    OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1
WORKDIR /work
ENTRYPOINT ["/bin/bash","-c"]
CMD ["source /opt/intel/openvino/setupvars.sh >/dev/null && exec bash"]
