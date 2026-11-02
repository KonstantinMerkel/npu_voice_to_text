#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Konstantin Merkel
# Remove npu-voice-to-text. Models, Python env and NPU libs are only removed with --all.
set -uo pipefail

SRC=$(cd "$(dirname "$0")" && pwd)
DATA=${XDG_DATA_HOME:-$HOME/.local/share}/npu-voice-to-text
CACHE=${XDG_CACHE_HOME:-$HOME/.cache}/npu-voice-to-text
UNITS=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
CONFIG=${XDG_CONFIG_HOME:-$HOME/.config}/npu-voice-to-text

systemctl --user disable --now --quiet npu-voice-to-text 2>/dev/null
rm -f "$UNITS/npu-voice-to-text.service" "$HOME/.local/bin/npu-voice-to-text"
systemctl --user daemon-reload
python3 -I "$SRC/tools/gnome-shortcut.py" remove

if [[ -e /etc/systemd/system/npu-voice-to-text-keywatch@.service ]]; then
    echo "removing the key watcher (sudo)"
    sudo systemctl disable --now --quiet "npu-voice-to-text-keywatch@$(id -u)"
    sudo rm -f "/etc/npu-voice-to-text/keywatch-$(id -u).env"
    sudo rmdir --ignore-fail-on-non-empty /etc/npu-voice-to-text 2>/dev/null
    # shared by all users - only remove it if nobody else uses it
    if ! ls /etc/npu-voice-to-text/keywatch-*.env >/dev/null 2>&1; then
        sudo rm -f /etc/systemd/system/npu-voice-to-text-keywatch@.service /usr/local/bin/npu-voice-to-text-keywatch
    fi
    sudo systemctl daemon-reload
fi

if [[ "${1:-}" == "--all" ]]; then
    rm -rf "$DATA" "$CACHE" "$CONFIG"
    echo "removed $DATA, $CACHE and $CONFIG"
else
    echo "kept $DATA (venv, NPU libs, models), $CACHE and $CONFIG - run with --all to remove them"
fi
