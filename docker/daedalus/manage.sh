#!/usr/bin/env bash
set -euo pipefail

task_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
task_container=vision_simulator_26
task_repo=/home/rm/rm26/vision_simulator_26
task_image=daedalus:rm26-ros2
mkdir -p "$task_dir/logs"
chmod 0700 "$task_dir/logs"
task_action=${1:-help}
if [[ $# -gt 0 ]]; then shift; fi

compose() {
  docker compose --file "$task_dir/compose.yml" "$@"
}

assert_owned() {
  if docker container inspect "$task_container" >/dev/null 2>&1; then
    local workspace
    workspace=$(docker inspect --format '{{index .Config.Labels "rm26.workspace"}}' "$task_container")
    if [[ $workspace != "$task_repo" ]]; then
      printf 'Container name %s belongs to a different environment.\n' "$task_container" >&2
      exit 1
    fi
  fi
}

if [[ $task_action != help && $task_action != --help && $task_action != -h ]]; then
  docker info >/dev/null
  export RM26_GPU_UUID=${RM26_GPU_UUID:-$(nvidia-smi --query-gpu=uuid --format=csv,noheader | head -n 1)}
fi

case "$task_action" in
  build)
    export RM26_BUILD_PROXY=${RM26_BUILD_PROXY-${https_proxy:-${HTTPS_PROXY:-http://127.0.0.1:7890}}}
    # Compose 5 uses Bake but does not expose its --allow switch. Generate its
    # exact build definition, then explicitly allow the requested host network.
    (umask 077; compose build --print > "$task_dir/logs/build-bake.json")
    docker buildx bake --file "$task_dir/logs/build-bake.json" \
      --allow=network.host --progress plain --load daedalus 2>&1 | tee "$task_dir/logs/build.log"
    ;;
  start)
    assert_owned
    docker image inspect "$task_image" >/dev/null
    python3 "$task_dir/display_auth.py" 2>&1 | tee -a "$task_dir/logs/manage.log"
    compose up --detach --no-build --force-recreate daedalus 2>&1 | tee -a "$task_dir/logs/manage.log"
    printf 'Daedalus starting on Sunshine desktop :20. Use manage.sh logs or check.\n'
    ;;
  stop)
    assert_owned
    compose stop daedalus
    ;;
  status)
    assert_owned
    compose ps --all
    ;;
  logs)
    assert_owned
    docker logs --timestamps "$@" "$task_container" 2>&1 | tee "$task_dir/logs/runtime.log"
    ;;
  check)
    assert_owned
    docker run --rm "$task_image" sanity 2>&1 | tee "$task_dir/logs/sanity.log"
    if [[ $(docker inspect --format '{{.State.Running}}' "$task_container") != true ]]; then
      printf 'Simulator is not running; start it before checking ROS publication.\n' >&2
      exit 1
    fi
    if [[ $(cat /sys/class/tty/tty0/active) != tty8 ]]; then
      printf 'Sunshine VT8 is inactive; run manage.sh start to reactivate the desktop.\n' >&2
      exit 1
    fi
    docker logs "$task_container" > "$task_dir/logs/runtime.log" 2>&1
    if ! python3 - "$task_dir/logs/runtime.log" <<'PY'
from pathlib import Path
import sys
text = Path(sys.argv[1]).read_text(errors="replace")
if not any("AdapterInfo" in line and "RTX 5070 Ti" in line and "Vulkan" in line for line in text.splitlines()):
    raise SystemExit("NVIDIA Vulkan adapter not confirmed in simulator log")
print("Confirmed RTX 5070 Ti Vulkan adapter.")
PY
    then
      exit 1
    fi
    # A separate subscriber also verifies communication across containers.
    docker run --rm --network host --ipc host \
      --env ROS_DOMAIN_ID=0 --env RMW_IMPLEMENTATION=rmw_fastrtps_cpp \
      --mount "type=bind,src=$task_dir/probe_ros.py,dst=/probe_ros.py,readonly" \
      "$task_image" python3 /probe_ros.py "$@" 2>&1 | tee "$task_dir/logs/ros-check.log"
    if [[ $(docker inspect --format '{{.State.Running}}' "$task_container") != true || $(cat /sys/class/tty/tty0/active) != tty8 ]]; then
      printf 'Simulator stopped or Sunshine VT8 became inactive during the check.\n' >&2
      exit 1
    fi
    docker logs "$task_container" > "$task_dir/logs/runtime.log" 2>&1
    printf 'Receive check passed; simulator is still running and Sunshine VT8 is active.\n'
    ;;
  help|--help|-h)
    printf 'Usage: bash %s {build|start|stop|status|logs|check}\n' "${BASH_SOURCE[0]}"
    printf '  logs [--follow|--tail N]     capture/show container logs\n'
    printf '  check [--duration SECONDS]  check payload, GPU and received ROS messages\n'
    ;;
  *)
    printf 'Unknown action: %s\n' "$task_action" >&2
    exit 2
    ;;
esac
