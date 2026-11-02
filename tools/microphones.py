#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Konstantin Merkel
"""List microphones (PipeWire/PulseAudio sources) for install.sh.

Prints one tab-separated line per microphone:
  node-name, description, bus (pci/usb/bluetooth/...), is-default (0/1), plugged-in (0/1)
plugged-in is 0 when PipeWire's jack detection says nothing is connected to the
active port (an empty headset jack records silence). Monitors are left out.
"""

import json
import subprocess
import sys


def run(*cmd: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def main() -> int:
    try:
        sources = json.loads(run("pactl", "-f", "json", "list", "sources"))
        default = run("pactl", "get-default-source").strip()
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return 1
    for s in sources:
        props = s.get("properties", {})
        if s["name"].endswith(".monitor") or props.get("device.class") == "monitor":
            continue
        bus = props.get("device.bus") or props.get("device.api") or "?"
        active = [p for p in s.get("ports", []) if p.get("name") == s.get("active_port")]
        plugged = not (active and active[0].get("availability") == "not available")
        print(s["name"], s.get("description", s["name"]), bus, int(s["name"] == default),
              int(plugged), sep="\t")
    return 0


if __name__ == "__main__":
    sys.exit(main())
