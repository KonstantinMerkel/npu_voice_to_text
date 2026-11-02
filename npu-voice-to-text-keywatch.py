#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Konstantin Merkel
"""npu-voice-to-text-keywatch - forwards press/release of ONE key to the npu-voice-to-text daemon.

GNOME shortcuts only fire on key press, never on release, so hold-to-talk needs
to read the keyboards directly (/dev/input/event*). That requires root or the
`input` group; adding your user to `input` would let every program you run read
all your keystrokes. Instead this small, standard-library-only script runs as a
root system service (npu-voice-to-text-keywatch@<uid>.service) and passes on nothing
but "press" / "release" of the configured key to the user's daemon socket.

It does not grab the key, so the focused app still receives it. For keys that
produce input (F8 in a terminal prints ^[[19~) bind the key to a do-nothing GNOME
shortcut (command `true`) so GNOME swallows it; a lone RIGHTCTRL needs nothing.
"""

import argparse
import glob
import os
import selectors
import socket
import struct
import sys
import time

# name -> (evdev code, GNOME accelerator or None). The accelerator is what
# install.sh binds to swallow the key (or to `npu-voice-to-text toggle`); modifiers
# alone produce no input in apps and can't be GNOME shortcuts, hence None.
KEYS = ({f"F{i}": (58 + i, f"F{i}") for i in range(1, 11)}
        | {"F11": (87, "F11"), "F12": (88, "F12")}
        | {f"F{i}": (170 + i, f"F{i}") for i in range(13, 25)}
        | {"PAUSE": (119, "Pause"), "SCROLLLOCK": (70, "Scroll_Lock"),
           "INSERT": (110, "Insert"), "MENU": (127, "Menu"),
           "RIGHTCTRL": (97, None), "RIGHTMETA": (126, None)})
EV_KEY = 1
INPUT_EVENT = struct.Struct("llHHi")                    # struct input_event (64-bit)
RESCAN_SECONDS = 3                                      # pick up hot-plugged keyboards


def send(sock_path: str, cmd: str) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(3)
            s.connect(sock_path)
            s.sendall(cmd.encode() + b"\n")
            s.recv(256)
        return True
    except OSError:
        return False


def keyboards() -> dict[str, str]:
    """{/dev/input/eventN: symlink name} for every keyboard udev knows about."""
    links = glob.glob("/dev/input/by-path/*-event-kbd") + glob.glob("/dev/input/by-id/*-event-kbd")
    return {os.path.realpath(link): os.path.basename(link) for link in links}


def watch(code: int, sock_path: str):
    sel = selectors.DefaultSelector()
    open_devs: dict[str, int] = {}
    warned: set[str] = set()
    next_scan = 0.0
    while True:
        if time.monotonic() >= next_scan:
            next_scan = time.monotonic() + RESCAN_SECONDS
            for dev, name in keyboards().items():
                if dev in open_devs:
                    continue
                try:
                    fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
                except OSError as e:
                    if dev not in warned:
                        warned.add(dev)
                        print(f"cannot open {dev}: {e}", file=sys.stderr, flush=True)
                    continue
                open_devs[dev] = fd
                sel.register(fd, selectors.EVENT_READ, dev)
                print(f"listening on {dev} ({name})", flush=True)

        for sk, _ in sel.select(timeout=1):
            try:
                data = os.read(sk.fd, INPUT_EVENT.size * 64)
            except BlockingIOError:
                continue
            except OSError:
                data = b""
            if not data:                                # keyboard unplugged
                sel.unregister(sk.fd)
                os.close(sk.fd)
                open_devs.pop(sk.data, None)
                warned.discard(sk.data)
                print(f"lost {sk.data}", flush=True)
                continue
            for off in range(0, len(data) - INPUT_EVENT.size + 1, INPUT_EVENT.size):
                _, _, ev_type, ev_code, value = INPUT_EVENT.unpack_from(data, off)
                if ev_type == EV_KEY and ev_code == code and value in (0, 1):  # 2 = autorepeat
                    cmd = "press" if value else "release"
                    if not send(sock_path, cmd):
                        print(f"{cmd}: npu-voice-to-text not reachable at {sock_path}",
                              file=sys.stderr, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--key", default="F8",
                    help=f"{', '.join(KEYS)} or a numeric evdev code (default: F8)")
    ap.add_argument("--socket", help="daemon socket, e.g. /run/user/1000/npu-voice-to-text.sock")
    ap.add_argument("--list-keys", action="store_true",
                    help="print 'NAME CODE GNOME-ACCELERATOR' for every key name and exit")
    args = ap.parse_args()

    if args.list_keys:
        for name, (code, accel) in KEYS.items():
            print(name, code, accel or "-")
        return 0
    if not args.socket:
        ap.error("--socket is required")
    key = args.key.upper()
    code = int(key) if key.isdigit() else KEYS.get(key, (None,))[0]
    if code is None:
        ap.error(f"unknown key {args.key!r}")
    print(f"watching {key} (evdev code {code}) -> {args.socket}", flush=True)
    watch(code, args.socket)
    return 0


if __name__ == "__main__":
    sys.exit(main())
