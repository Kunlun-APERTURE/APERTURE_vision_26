#!/usr/bin/env python3
"""Refresh display :20 authentication without exposing the cookie in logs."""

import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile


def main():
    source = "sp_vision_sunshine"
    running = subprocess.check_output(
        ["docker", "inspect", "--format", "{{.State.Running}}", source], text=True
    ).strip()
    if running != "true":
        raise RuntimeError(
            "Sunshine desktop is stopped. Start it with: "
            "sudo systemctl start rm26-sunshine"
        )
    # This Xorg owns physical VT8. An authenticated X connection can still work
    # while VT8 is inactive, but NVIDIA Vulkan presentation stalls and loses the
    # device. Activate it through the desktop's existing SYS_TTY_CONFIG grant.
    active = Path("/sys/class/tty/tty0/active").read_text().strip()
    if active != "tty8":
        print(f"Activating Sunshine VT8 (was {active}).", flush=True)
        subprocess.run(
            [
                "docker", "exec", source, "python3", "-c",
                "import fcntl, os; fd = os.open('/dev/tty0', os.O_RDWR); "
                "fcntl.ioctl(fd, 0x5606, 8); fcntl.ioctl(fd, 0x5607, 8); os.close(fd)",
            ],
            check=True,
        )
        if Path("/sys/class/tty/tty0/active").read_text().strip() != "tty8":
            raise RuntimeError("Sunshine VT8 could not be activated")
    blob = subprocess.check_output(
        ["docker", "exec", source, "cat", "/run/user/1000/Xauthority"]
    )
    pos = 0
    record = None
    cookie = None
    while pos < len(blob):
        start = pos
        pos += 2  # family
        fields = []
        for _ in range(4):
            size = struct.unpack_from(">H", blob, pos)[0]
            pos += 2
            fields.append(blob[pos : pos + size])
            pos += size
        _, number, name, data = fields
        if number == b"20" and name == b"MIT-MAGIC-COOKIE-1":
            # FamilyWild permits a client with a different container hostname.
            record = b"\xff\xff" + blob[start + 2 : pos]
            cookie = data
            break
    if record is None:
        raise RuntimeError("Sunshine has no authentication record for display :20")

    name = b"MIT-MAGIC-COOKIE-1"
    pad = lambda value: value + b"\0" * (-len(value) % 4)
    request = struct.pack("<BBHHHHH", ord("l"), 0, 11, 0, len(name), len(cookie), 0)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect("\0/tmp/.X11-unix/X20")
        connection.sendall(request + pad(name) + pad(cookie))
        response = connection.recv(4096)
        if not response or response[0] != 1:
            raise RuntimeError("X11 authentication failed for Sunshine display :20")

    directory = Path(__file__).resolve().parent / ".runtime"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=".Xauthority-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(record)
        os.replace(temporary, directory / "Xauthority")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print("Sunshine :20 connection authenticated; refreshed private Xauthority file.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, struct.error, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Display setup failed: {error}", file=sys.stderr)
        sys.exit(1)
