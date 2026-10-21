#!/usr/bin/env bash
# Run on the host as rm. All desktop files and connection settings persist.
set -euo pipefail
task_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
task_desktop_home=/home/rm/rm26/docker/sunshine/home
task_ssh_dir="$task_desktop_home/.ssh"
task_key="$task_ssh_dir/rm26_simulator_ed25519"

if [[ $(id -un) != rm ]]; then
  printf 'Run this script as the host user rm, without sudo.\n' >&2
  exit 1
fi
install -d -m 0700 "$task_ssh_dir" "$HOME/.ssh"
printf '*\n' > "$task_ssh_dir/.gitignore"
if [[ ! -f $task_key ]]; then
  ssh-keygen -q -t ed25519 -N '' -C rm26-daedalus-desktop -f "$task_key"
fi
chmod 0600 "$task_key"
# Derive the public half again so a missing/stale .pub file is repaired.
ssh-keygen -y -f "$task_key" > "$task_key.pub"
chmod 0644 "$task_key.pub"

python3 - "$task_key.pub" "$HOME/.ssh/authorized_keys" "$task_dir/desktop-command.sh" <<'PY'
import os
from pathlib import Path
import sys

public = Path(sys.argv[1]).read_text().strip()
authorized = Path(sys.argv[2])
dispatcher = sys.argv[3]
entry = (
    'restrict,from="127.0.0.1,::1",command="/bin/bash ' + dispatcher + '" '
    + public + ' rm26-daedalus-desktop'
)
lines = authorized.read_text().splitlines() if authorized.exists() else []
blob = public.split()[1]
# Replace only entries for this key; preserve every unrelated authorized key.
lines = [line for line in lines if blob not in line.split()]
lines.append(entry)
fd = os.open(authorized, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w') as output:
    output.write('\n'.join(lines) + '\n')
authorized.chmod(0o600)
PY

python3 - "$task_ssh_dir/rm26_simulator_known_hosts" <<'PY'
from pathlib import Path
import os, sys
public = Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text().split()
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w') as output:
    output.write('rm26-simulator-host ' + ' '.join(public[:2]) + '\n')
os.chmod(sys.argv[1], 0o600)
PY

chmod 0755 "$task_desktop_home"/Desktop/RM26-Simulator-*.desktop \
  "$task_desktop_home/Desktop/项目脚本/.simulator-tools.bash" \
  "$task_desktop_home/Desktop/项目脚本/启动模拟器.bash" \
  "$task_desktop_home/Desktop/项目脚本/停止模拟器.bash" \
  "$task_desktop_home/Desktop/项目脚本/编译模拟器.bash" \
  "$task_desktop_home/Desktop/项目脚本/模拟器状态与日志.bash"

if [[ $(docker inspect --format '{{.State.Running}}' sp_vision_sunshine) == true ]]; then
  task_bus=$(docker exec -i -u 1000:1000 sp_vision_sunshine python3 - <<'PY'
from pathlib import Path
for process in Path('/proc').iterdir():
    if not process.name.isdigit():
        continue
    try:
        command = (process / 'cmdline').read_bytes().split(b'\0')[0]
        if command != b'/usr/libexec/gnome-session-binary':
            continue
        for item in (process / 'environ').read_bytes().split(b'\0'):
            if item.startswith(b'DBUS_SESSION_BUS_ADDRESS='):
                print(item.split(b'=', 1)[1].decode())
                raise SystemExit(0)
    except OSError:
        pass
raise SystemExit('GNOME session bus not found')
PY
  )
  for task_launcher in "$task_desktop_home"/Desktop/RM26-Simulator-*.desktop; do
    docker exec -u 1000:1000 \
      --env "DBUS_SESSION_BUS_ADDRESS=$task_bus" \
      sp_vision_sunshine gio set \
      "/home/openvino/Desktop/$(basename "$task_launcher")" metadata::trusted true
  done
fi
printf 'Installed simulator desktop shortcuts and restricted local SSH connection.\n'
