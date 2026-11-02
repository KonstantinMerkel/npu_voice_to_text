#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Konstantin Merkel
"""Create, update or remove npu-voice-to-text's GNOME custom shortcut (used by install.sh).

  gnome-shortcut.py set ACCELERATOR COMMAND
      e.g.  set F8 true   /   set F8 "~/.local/bin/npu-voice-to-text toggle"
  gnome-shortcut.py remove

There is at most one npu-voice-to-text shortcut. An existing shortcut counts as ours
if it is named or runs npu-voice-to-text (or npu-dictate, the project's former
name), or is a do-nothing `true` on the same key (what the README used to tell
people to create by hand) - it is updated in place instead of adding a duplicate. A
different shortcut on the same key is left alone and reported (exit code 2).
"""

import ast
import subprocess
import sys

NAME = "npu-voice-to-text"
OUR_COMMANDS = ("npu-voice-to-text", "npu-dictate")   # incl. the former project name
SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"


def gs_get(schema: str, key: str):
    out = subprocess.run(["gsettings", "get", schema, key], capture_output=True, text=True,
                         check=True).stdout.strip()
    return ast.literal_eval(out.split(" ", 1)[1] if out.startswith("@") else out)


def gs_set(schema: str, key: str, value):
    subprocess.run(["gsettings", "set", schema, key, gvariant(value)], check=True)


def gvariant(value) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(gvariant(v) for v in value) + "]"
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def entry(path: str) -> str:
    return f"{SCHEMA}.custom-keybinding:{path}"


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("set", "remove") or \
            (sys.argv[1] == "set" and len(sys.argv) != 4):
        sys.exit(__doc__)
    accel, command = (sys.argv[2], sys.argv[3]) if sys.argv[1] == "set" else (None, None)

    paths = list(gs_get(SCHEMA, "custom-keybindings"))
    ours, conflicts = [], []
    for p in paths:
        name, cmd, binding = (gs_get(entry(p), k) for k in ("name", "command", "binding"))
        if (name in OUR_COMMANDS or any(c in cmd for c in OUR_COMMANDS)
                or (cmd.strip() == "true" and binding == accel)):
            ours.append(p)
        elif accel and binding == accel:
            conflicts.append(f"'{name}' ({cmd})")

    if conflicts:
        print(f"{accel} is already used by {', '.join(conflicts)} - not changing it",
              file=sys.stderr)
        return 2

    keep = None
    if accel:
        keep = ours[0] if ours else next(f"{BASE}custom{i}/" for i in range(len(paths) + 1)
                                         if f"{BASE}custom{i}/" not in paths)
        gs_set(entry(keep), "name", NAME)
        gs_set(entry(keep), "command", command)
        gs_set(entry(keep), "binding", accel)
    for p in ours:
        if p != keep:
            for k in ("name", "command", "binding"):
                subprocess.run(["gsettings", "reset", entry(p), k], check=False)
    new_paths = [p for p in paths if p == keep or p not in ours] + \
        ([keep] if keep and keep not in paths else [])
    if new_paths != paths:
        gs_set(SCHEMA, "custom-keybindings", new_paths)
    print(f"GNOME shortcut: {accel} -> {command}" if accel else "GNOME shortcut removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
