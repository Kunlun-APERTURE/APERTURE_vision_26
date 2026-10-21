#!/usr/bin/env bash
set -euo pipefail

task_dir=/home/rm/rm26/docker/sunshine
task_repo=/home/rm/rm26/sp_vision_25
task_simulator_repo=/home/rm/rm26/vision_simulator_26
task_image=sp_vision_sunshine:2026.914.233613
task_name=sp_vision_sunshine
task_proxy=${https_proxy:-${HTTPS_PROXY:-http://127.0.0.1:7890}}

docker build --network host --progress plain \
  --build-arg "HTTP_PROXY=$task_proxy" --build-arg "HTTPS_PROXY=$task_proxy" \
  --tag "$task_image" "$task_dir"

nvidia-smi >/dev/null
task_gpu=$(nvidia-smi --query-gpu=uuid --format=csv,noheader | head -n 1)
task_input_gid=$(getent group input | cut -d: -f3)
task_driver=/usr/lib/x86_64-linux-gnu/nvidia/xorg/nvidia_drv.so
task_glx=/usr/lib/x86_64-linux-gnu/nvidia/xorg/libglxserver_nvidia.so
mkdir -p "$task_dir/config" "$task_dir/home"
chmod 0700 "$task_dir/config"
if [[ ! -f $task_dir/config/sunshine.conf ]]; then
  cp "$task_dir/sunshine.conf" "$task_dir/config/sunshine.conf"
fi
if [[ ! -f $task_dir/config/apps.json ]]; then
  cp "$task_dir/apps.json" "$task_dir/config/apps.json"
fi

# Generate a distinct Sunshine password. Never reuse the sudo password.
if [[ ! -f $task_dir/config/sunshine_state.json ]]; then
  if [[ ! -f $task_dir/credentials.json ]]; then
    python3 - "$task_dir/credentials.json" <<'PY'
import json, os, secrets, sys
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as output:
    json.dump({'username': 'rm', 'password': secrets.token_urlsafe(24)}, output)
PY
  fi
  docker run --rm --user 1000:1000 --entrypoint python3 \
    --mount "type=bind,src=$task_dir/config,dst=/config" \
    --mount "type=bind,src=$task_dir/credentials.json,dst=/run/credentials.json,readonly" \
    "$task_image" -c \
    'import json, subprocess; c = json.load(open("/run/credentials.json")); subprocess.run(["sunshine", "/config/sunshine.conf", "--creds", c["username"], c["password"]], check=True)' \
    > "$task_dir/credentials-setup.log" 2>&1
fi

if [[ ! -d $task_simulator_repo/.git ]]; then
  printf 'Simulator repository not found: %s\n' "$task_simulator_repo" >&2
  exit 1
fi

if docker container inspect "$task_name" >/dev/null 2>&1; then
  if [[ $(docker inspect --format '{{index .Config.Labels "rm26.workspace"}}' "$task_name") != "$task_repo" ]]; then
    printf 'Container name is already in use by another environment.\n' >&2
    exit 1
  fi
  if [[ $(docker inspect --format '{{index .Config.Labels "rm26.display"}}' "$task_name") != physical-gnome ]]; then
    printf 'Existing container uses an earlier display setup. Preserve config/home, remove the stopped container, then deploy again.\n' >&2
    exit 1
  fi
  printf 'Container already exists. Start it using: sudo systemctl start rm26-sunshine\n'
  exit 0
fi

docker create --init --name "$task_name" --restart unless-stopped --stop-timeout 20 \
  --label "rm26.workspace=$task_repo" \
  --label rm26.display=physical-gnome \
  --gpus "device=$task_gpu" \
  --env NVIDIA_DRIVER_CAPABILITIES=compute,utility,video,graphics,display \
  --env DISPLAY=:20 --env HOME=/home/openvino \
  --env "RM26_INPUT_GID=$task_input_gid" \
  --network host --shm-size 1g \
  --cap-add SYS_NICE --cap-add SYS_TTY_CONFIG \
  --device /dev/uinput --device /dev/tty0 --device /dev/tty8 \
  --device-cgroup-rule 'c 13:* rmw' \
  --group-add "$task_input_gid" \
  --mount type=bind,src=/dev/input,dst=/dev/input \
  --mount type=bind,src=/run/udev,dst=/run/udev,readonly \
  --mount "type=bind,src=$task_driver,dst=/usr/lib/xorg/modules/drivers/nvidia_drv.so,readonly" \
  --mount "type=bind,src=$task_glx,dst=/usr/lib/xorg/modules/extensions/libglxserver_nvidia.so,readonly" \
  --mount "type=bind,src=$task_dir/config,dst=/config" \
  --mount "type=bind,src=$task_dir/home,dst=/home/openvino" \
  --mount "type=bind,src=$task_repo,dst=/workspace/sp_vision_25" \
  --mount "type=bind,src=$task_simulator_repo,dst=/workspace/vision_simulator_26" \
  "$task_image"

printf '\nDesktop container prepared and left stopped. Credentials: %s/credentials.json\n' "$task_dir"
printf 'Start through the host display service: sudo systemctl start rm26-sunshine\n'
