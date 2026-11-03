#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Konstantin Merkel
# Remove npu-voice-to-text and revoke its keyboard access. Models, Python env and NPU
# libs are only removed with --all.
set -uo pipefail

SRC=$(cd "$(dirname "$0")" && pwd)
DATA=${XDG_DATA_HOME:-$HOME/.local/share}/npu-voice-to-text
CACHE=${XDG_CACHE_HOME:-$HOME/.cache}/npu-voice-to-text
UNITS=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
CONFIG=${XDG_CONFIG_HOME:-$HOME/.config}/npu-voice-to-text
STATE=${XDG_STATE_HOME:-$HOME/.local/state}/npu-voice-to-text
TOKEN=$STATE/remote-desktop-token

systemctl --user disable --now --quiet npu-voice-to-text 2>/dev/null
rm -f "$UNITS/npu-voice-to-text.service" "$HOME/.local/bin/npu-voice-to-text"
systemctl --user daemon-reload
python3 -I "$SRC/tools/gnome-shortcut.py" remove

bash "$SRC/tools/remove-keywatch.sh"

# revoke the keyboard access granted in GNOME's dialog, so a reinstall asks again
if [[ -s $TOKEN ]]; then
    if gdbus call --session --dest org.freedesktop.impl.portal.PermissionStore \
            --object-path /org/freedesktop/impl/portal/PermissionStore \
            --method org.freedesktop.impl.portal.PermissionStore.Delete \
            remote-desktop "$(cat "$TOKEN")" >/dev/null; then
        echo "revoked the keyboard access (GNOME remote desktop permission)"
    else
        echo "could not revoke the keyboard access - it may be gone already"
    fi
fi
rm -rf "$STATE"

if [[ "${1:-}" == "--all" ]]; then
    rm -rf "$DATA" "$CACHE" "$CONFIG"
    echo "removed $DATA, $CACHE and $CONFIG"
else
    echo "kept $DATA (venv, NPU libs, models), $CACHE and $CONFIG - run with --all to remove them"
fi
