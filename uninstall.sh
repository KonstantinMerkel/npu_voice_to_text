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

bash "$SRC/tools/remove-keywatch.sh"

if [[ "${1:-}" == "--all" ]]; then
    rm -rf "$DATA" "$CACHE" "$CONFIG"
    echo "removed $DATA, $CACHE and $CONFIG"
else
    echo "kept $DATA (venv, NPU libs, models), $CACHE and $CONFIG - run with --all to remove them"
fi
