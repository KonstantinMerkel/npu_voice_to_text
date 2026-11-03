# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Konstantin Merkel
"""Socket client for the daemon. Standard library only: bound to a shortcut, it must
start instantly."""

import socket
import sys
from pathlib import Path

from . import paths


def request(cmd: str, timeout: float = 5, path: Path | None = None) -> str | None:
    """Send one command to the daemon and return its reply (None if not running)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(path or paths.SOCKET_PATH))
            s.sendall(cmd.encode() + b"\n")
            # empty: the daemon closed the connection without answering (it died)
            return s.recv(4096).decode().strip() or None
    except OSError:                     # not running, stale socket, timeout, …
        return None


def daemon_running(path: Path | None = None) -> bool:
    """Whether a daemon listens on the socket - even one too busy to answer right now."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(str(path or paths.SOCKET_PATH))
            return True
    except OSError:                     # no socket, or a stale one nobody listens on
        return False


def run_client(cmd: str, quiet: bool = False) -> int:
    """quiet: no output, only the exit code (for scripts)."""
    reply = request(cmd)
    if reply is None:
        if not quiet:
            print("npu-voice-to-text is not running (systemctl --user start npu-voice-to-text)",
                  file=sys.stderr)
        return 1
    if not quiet:
        print(reply)
    return 1 if reply.startswith("error") else 0
