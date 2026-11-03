#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Konstantin Merkel
# Remove the current user's hold-to-talk key watcher (uses sudo). Used by install.sh
# (switching to toggle mode) and uninstall.sh. The unit file and the script are
# shared by all users, so they are only removed when no other user's watcher is
# configured. Does nothing if there is nothing to remove.
set -uo pipefail

uid=$(id -u)
env_file=/etc/npu-voice-to-text/keywatch-$uid.env
unit=/etc/systemd/system/npu-voice-to-text-keywatch@.service
others=$(ls /etc/npu-voice-to-text/keywatch-*.env 2>/dev/null | grep -vxF "$env_file")

[[ -e $env_file || ( -e $unit && -z $others ) ]] || exit 0
echo "    removing the key watcher (sudo)"
sudo systemctl disable --now --quiet "npu-voice-to-text-keywatch@$uid" 2>/dev/null
sudo rm -f "$env_file"
if [[ -z $others ]]; then
    sudo rm -f "$unit" /usr/local/bin/npu-voice-to-text-keywatch
    sudo rmdir /etc/npu-voice-to-text 2>/dev/null
fi
sudo systemctl daemon-reload
exit 0
