#!/usr/bin/env bash
# One argument-based interface on both the host and the Sunshine desktop.
set -euo pipefail
task_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
if [[ -f "$task_root/docker/auto_aim/manage.py" ]] && command -v docker >/dev/null; then
  exec python3 "$task_root/docker/auto_aim/manage.py" "$@"
fi
task_key="$HOME/.ssh/rm26_simulator_ed25519"
task_payload=$(python3 - "$@" <<'PY'
import base64, json, sys
print(base64.urlsafe_b64encode(json.dumps(sys.argv[1:]).encode()).decode())
PY
)
exec ssh -T -o BatchMode=yes -o ConnectTimeout=5 -o IdentitiesOnly=yes \
  -o HostKeyAlias=rm26-simulator-host \
  -o "UserKnownHostsFile=$HOME/.ssh/rm26_simulator_known_hosts" \
  -i "$task_key" rm@127.0.0.1 "sim-v1 $task_payload"
