set -eo pipefail
source /opt/ros/jazzy/setup.bash >/dev/null
source /opt/intel/openvino/setupvars.sh >/dev/null
ln -sfn /project/sp_vision_25/assets /work/assets
cat /etc/os-release
cmake --version | head -1
g++ --version | head -1
pkg-config --modversion opencv4
cmake -S /project/sp_vision_25 -B /work/build \
  -DBUILD_SIMULATOR_ADAPTER=ON -DCMAKE_BUILD_TYPE=Release \
  -DOpenCV_DIR=/usr/lib/x86_64-linux-gnu/cmake/opencv4
cmake --build /work/build --target standard auto_aim_test sim_auto_aim \
  continuity_test robustness_test optimization_test motion_test \
  perception_test maneuver_filter_test -j4
for spec in \
  'continuity_test /project/tools/test-configs/continuity.yaml' \
  'robustness_test /project/tools/test-configs/robustness.yaml' \
  'optimization_test /project/tools/test-configs/optimization.yaml' \
  'motion_test /project/sp_vision_25/configs/simulator.yaml' \
  'perception_test /project/sp_vision_25/configs/simulator.yaml' \
  'maneuver_filter_test'; do
  read -r program argument <<< "$spec"
  echo "RUN $program"
  if [ -n "$argument" ]; then /work/build/"$program" "$argument"; else /work/build/"$program"; fi
done
/work/build/sim_auto_aim --help
ldd /work/build/sim_auto_aim
