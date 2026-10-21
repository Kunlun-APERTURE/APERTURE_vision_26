ARG EVALUATOR_BASE=daedalus:rm26-integration
FROM daedalus:rm26-integration AS integration
FROM ${EVALUATOR_BASE}
USER root
RUN if python3 -c 'import cv2, matplotlib' && command -v ffmpeg; then exit 0; fi; \
    apt-get update && apt-get install -y --no-install-recommends \
    python3-opencv python3-matplotlib ffmpeg \
    && rm -rf /var/lib/apt/lists/*
# Reuse installed recording tools, but always take interfaces and runtime from
# the current integration image. Rust-only rebuilds need no package downloads.
COPY --from=integration /ros2_ws/install/ /ros2_ws/install/
COPY --from=integration /app/daedalus /app/daedalus
COPY --from=integration /app/lib/ /app/lib/
ARG INTEGRATION_IMAGE_ID
LABEL rm26.integration_image_id=${INTEGRATION_IMAGE_ID}
WORKDIR /opt/rm26
