#!/usr/bin/env bash
# Entry point for the desktop's restricted, loopback-only SSH key.
set -euo pipefail
task_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

# Never evaluate SSH_ORIGINAL_COMMAND or forward arbitrary arguments.
case "${SSH_ORIGINAL_COMMAND:-}" in
  sim-v1\ *)
    # Encoded argv is parsed as data by argparse. Never eval the SSH command.
    exec python3 "$task_dir/../auto_aim/manage.py" --desktop-payload "${SSH_ORIGINAL_COMMAND#sim-v1 }"
    ;;
  start|stop|status|check)
    exec bash "$task_dir/manage.sh" "$SSH_ORIGINAL_COMMAND"
    ;;
  build)
    mkdir -p "$task_dir/logs"
    exec flock --nonblock --conflict-exit-code 75 "$task_dir/logs/desktop-build.lock" \
      bash "$task_dir/manage.sh" build
    ;;
  logs)
    exec bash "$task_dir/manage.sh" logs --tail 100
    ;;
  status-logs)
    bash "$task_dir/manage.sh" status
    exec bash "$task_dir/manage.sh" logs --tail 100
    ;;
  *)
    printf 'This desktop key only accepts simulator start, stop, build, status, logs, check, or status-logs.\n' >&2
    exit 64
    ;;
esac
