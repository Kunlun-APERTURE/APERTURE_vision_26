ARG ALGORITHM_BASE=sp_vision_dev:2024.6.0
FROM ${ALGORITHM_BASE}
USER 1000:1000
ENV OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1
WORKDIR /work
ENTRYPOINT ["/bin/bash", "-c"]
CMD ["source /opt/intel/openvino/setupvars.sh >/dev/null && exec /snapshot/sp_vision_25/build/simulator/sim_auto_aim --help"]
