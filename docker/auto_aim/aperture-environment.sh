# 函数作用域隔离上游 setupvars.sh 对位置参数的修改。
aperture_load_environment() {
  if [ -z "${APERTURE_ENV_READY:-}" ]; then
    export APERTURE_ENV_READY=1
    case $- in *u*) aperture_restore_nounset=1; set +u ;; *) aperture_restore_nounset=0 ;; esac
    source /opt/ros/jazzy/setup.bash
    source /ros2_ws/install/setup.bash
    source /opt/intel/openvino/setupvars.sh >/dev/null
    if [ "$aperture_restore_nounset" = 1 ]; then set -u; fi
    unset aperture_restore_nounset
  fi
}
aperture_load_environment
unset -f aperture_load_environment
