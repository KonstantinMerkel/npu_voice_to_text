#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Konstantin Merkel
"""GNOME's record of npu-voice-to-text's dictation key (used by install.sh / uninstall.sh).

  gnome-keys.py get            print the bound key, e.g. F8 (exit code 1 if none)
  gnome-keys.py reset          forget the binding: the next start asks in GNOME's dialog

The key is bound through the GlobalShortcuts portal. GNOME stores the binding in
dconf: /org/gnome/settings-daemon/global-shortcuts/<APP_ID>/shortcuts, and the app
in the `applications` list next to it.
"""

import sys

from gi.repository import Gio     # PyGObject: part of every GNOME installation

APP_ID = "io.github.KonstantinMerkel.NpuVoiceToText"     # = portal.APP_ID
GLOBAL = "org.gnome.settings-daemon.global-shortcuts"
APP_PATH = f"/org/gnome/settings-daemon/global-shortcuts/{APP_ID}/"


def bound_key() -> str | None:
    shortcuts = Gio.Settings.new_with_path(f"{GLOBAL}.application", APP_PATH)["shortcuts"]
    for sid, props in shortcuts:
        if sid == "dictate" and props.get("shortcuts"):
            return props["shortcuts"][0]
    return None


def reset():
    Gio.Settings.new_with_path(f"{GLOBAL}.application", APP_PATH).reset("shortcuts")
    settings = Gio.Settings.new(GLOBAL)
    apps = settings["applications"]
    if APP_ID in apps:
        settings["applications"] = [a for a in apps if a != APP_ID]
    Gio.Settings.sync()


def main() -> int:
    cmd = sys.argv[1:]
    if cmd == ["get"]:
        key = bound_key()
        if key:
            print(key)
        return 0 if key else 1
    if cmd == ["reset"]:
        reset()
        return 0
    sys.exit(__doc__)


if __name__ == "__main__":
    sys.exit(main())
