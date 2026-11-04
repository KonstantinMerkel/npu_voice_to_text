#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Konstantin Merkel
# Install or update npu-voice-to-text for the current user. Safe to re-run; re-run it to
# change the microphone or the dictation key. Everything goes into your home
# directory, no root needed:
#   ~/.local/bin/npu-voice-to-text                     launcher for the program in the venv
#   ~/.local/share/npu-voice-to-text/{venv,lib,models} Python env, Intel NPU libs, Whisper models
#   ~/.local/share/applications/<APP_ID>.desktop       the app's identity for the portals
#   ~/.config/systemd/user/npu-voice-to-text.service   the daemon
set -euo pipefail

# Intel NPU userspace driver + compiler. Must match the OpenVINO version in
# pyproject.toml (see https://github.com/intel/linux-npu-driver/releases).
NPU_DRIVER_VERSION=1.38.0
NPU_DRIVER_URL=https://github.com/intel/linux-npu-driver/releases/download/v1.38.0/linux-npu-driver-v1.38.0.20260910-34487311128-ubuntu2604.tar.gz
NPU_DRIVER_SHA256=83685e6d92f4db1ca65e442846d487c7feb1335634b33994292ceb38f933bcc7

MODEL=OpenVINO/whisper-large-v3-turbo-int8-ov
DETECT_MODEL=OpenVINO/whisper-small-int8-ov

SRC=$(cd "$(dirname "$0")" && pwd)
DATA=${XDG_DATA_HOME:-$HOME/.local/share}/npu-voice-to-text
BIN=$HOME/.local/bin
UNITS=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
OPTIONS=${XDG_CONFIG_HOME:-$HOME/.config}/npu-voice-to-text/options
TOKEN=${XDG_STATE_HOME:-$HOME/.local/state}/npu-voice-to-text/keyboard-access.token
APP_ID=io.github.KonstantinMerkel.NpuVoiceToText
DESKTOP=${XDG_DATA_HOME:-$HOME/.local/share}/applications/$APP_ID.desktop

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
ask()  { local reply; read -rp "    $1 " reply; printf '%s' "$reply"; }   # ask "question"
LDCACHE=$(ldconfig -p)           # read once; `ldconfig -p | grep -q` breaks under pipefail
has_lib() { [[ "$LDCACHE" == *"$1"* ]]; }

# --------------------------------------------------------------------------- #
step "Checking requirements"
[[ -e /dev/accel/accel0 ]] || die "no NPU found (/dev/accel/accel0). Needs an Intel Core Ultra CPU and the intel_vpu kernel driver."
[[ -r /dev/accel/accel0 && -w /dev/accel/accel0 ]] || die "/dev/accel/accel0 is not read/writable for you (normally group 'render')."
missing=()
for cmd in python3 curl ar tar gsettings gdbus gnome-shell; do
    command -v "$cmd" >/dev/null || missing+=("$cmd")
done
has_lib libze_loader.so.1    || missing+=("Level Zero loader (Fedora: oneapi-level-zero, Ubuntu: libze1)")
has_lib libtbb.so.12         || missing+=("oneTBB (Fedora: tbb, Ubuntu: libtbb12)")
has_lib libportaudio.so.2    || missing+=("PortAudio (Fedora: portaudio, Ubuntu: libportaudio2)")
has_lib libxkbcommon.so.0    || missing+=("libxkbcommon")
python3 -I -c 'from gi.repository import Gio' 2>/dev/null || missing+=("PyGObject (Fedora: python3-gobject, Ubuntu: python3-gi)")
(( ${#missing[@]} == 0 )) || die "missing: ${missing[*]}"
# GNOME 48 brought the global shortcuts portal (the dictation key)
gnome=$(gnome-shell --version | grep -o '[0-9]\+' | head -1 || true)
(( ${gnome:-0} >= 48 )) || die "needs GNOME 48 or newer (found: $(gnome-shell --version))"
ok "NPU, GNOME $gnome, tools and libraries found"

# --------------------------------------------------------------------------- #
step "Python environment"
python3 -m venv "$DATA/venv"
"$DATA/venv/bin/pip" install --quiet --upgrade pip
# the npu_voice_to_text package and its dependencies (pyproject.toml). pip reinstalls
# a local directory on every run, so re-running install.sh updates the program.
# Built from a fresh copy: setuptools builds inside the source tree and reuses an
# old build/ there, so modules deleted or renamed since would still be installed.
# This also leaves the checkout untouched (it may be read-only).
build=$(mktemp -d)
trap 'rm -rf "$build"' EXIT
cp -r "$SRC/pyproject.toml" "$SRC/README.md" "$SRC/src" "$build/"
"$DATA/venv/bin/pip" install --quiet "$build"
rm -rf "$build"
ok "$DATA/venv (openvino-genai $("$DATA/venv/bin/python" -I -c 'import openvino_genai as g; print(g.__version__.split("-")[0])'))"

# --------------------------------------------------------------------------- #
step "Intel NPU driver + compiler"
if [[ "$(cat "$DATA/lib/VERSION" 2>/dev/null)" == "$NPU_DRIVER_VERSION" ]]; then
    ok "v$NPU_DRIVER_VERSION already installed"
else
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    curl -fL --progress-bar -o "$tmp/driver.tar.gz" "$NPU_DRIVER_URL"
    echo "$NPU_DRIVER_SHA256  $tmp/driver.tar.gz" | sha256sum -c --quiet - || die "checksum mismatch"
    tar -C "$tmp" -xzf "$tmp/driver.tar.gz"
    rm -rf "$DATA/lib" && mkdir -p "$DATA/lib"
    # only the userspace driver and the compiler; firmware comes from the distro
    for pkg in intel-level-zero-npu intel-driver-compiler-npu; do
        deb=$(ls "$tmp"/${pkg}_*.deb)
        mkdir "$tmp/$pkg" && (cd "$tmp/$pkg" && ar x "$deb" && tar xf data.tar.*)
        find "$tmp/$pkg" -name '*.so*' -exec cp -a {} "$DATA/lib/" \;
    done
    echo "$NPU_DRIVER_VERSION" > "$DATA/lib/VERSION"
    ok "v$NPU_DRIVER_VERSION installed to $DATA/lib"
fi
devices=$(LD_LIBRARY_PATH="$DATA/lib" "$DATA/venv/bin/python" -I -c \
    "import openvino as ov; print(' '.join(ov.Core().available_devices))")
[[ " $devices " == *" NPU "* ]] || die "OpenVINO does not see the NPU (devices: $devices) - see README, Troubleshooting"
ok "OpenVINO sees the NPU (devices: $devices)"

# --------------------------------------------------------------------------- #
step "Program and models"
# a launcher instead of pip's entry-point script: -I keeps PYTHONPATH and the
# current directory out of the program's imports
# (%q quotes the path for bash; rm first so an old symlink isn't written through)
mkdir -p "$BIN"
rm -f "$BIN/npu-voice-to-text"
printf '#!/bin/bash\nexec %q -I -m npu_voice_to_text "$@"\n' "$DATA/venv/bin/python" \
    > "$BIN/npu-voice-to-text"
chmod 755 "$BIN/npu-voice-to-text"
ok "$BIN/npu-voice-to-text"
for model in "$MODEL" "$DETECT_MODEL"; do
    if [[ -e "$DATA/models/${model##*/}/openvino_encoder_model.xml" ]]; then
        ok "${model##*/} already downloaded"
    else
        "$BIN/npu-voice-to-text" download --model "$model"
        ok "${model##*/} downloaded"
    fi
done

# --------------------------------------------------------------------------- #
step "Microphone"
# created only once a choice is made, so an install aborted at the prompt still
# counts as a first install next time
fresh=$([[ -e "$OPTIONS" ]] && echo 0 || echo 1)
create_options() {
    [[ -e "$OPTIONS" ]] && return
    mkdir -p "${OPTIONS%/*}"
    cat > "$OPTIONS" <<'EOF'
# Options for `npu-voice-to-text serve`, one per line - see `npu-voice-to-text serve --help`.
# After editing: systemctl --user restart npu-voice-to-text
EOF
}
current_mic=$(sed -n 's/^--input-device[ =]//p' "$OPTIONS" 2>/dev/null | head -1 || true)
mapfile -t MICS < <(python3 -I "$SRC/tools/microphones.py" 2>/dev/null || true)
if (( ${#MICS[@]} == 0 )); then
    create_options
    warn "could not list microphones (is pactl installed?) - dictation uses the system default"
else
    cat <<'EOF'
    Dictation records from the preferred microphone while it is connected, and from
    the system default (your desktop's sound settings) otherwise. Bluetooth headsets
    switch into a low-quality headset mode when their mic opens; that takes ~1 s, so
    the start of every dictation is lost. Prefer a wired or built-in mic.
EOF
    printf '    0) None - always the system default\n'
    names=(); descs=(); current=""; default=""; first=""
    for i in "${!MICS[@]}"; do
        IFS=$'\t' read -r name desc bus is_default plugged <<<"${MICS[$i]}"
        names+=("$name"); descs+=("$desc")
        notes=""
        [[ $is_default == 1 ]]        && notes+=", system default"
        [[ $name == "$current_mic" ]] && notes+=", current"
        [[ $bus == bluetooth ]]       && notes+=", Bluetooth: not recommended"
        [[ $plugged == 0 ]]           && notes+=", nothing plugged in"
        printf '    %d) %s%s\n' $((i + 1)) "$desc" "${notes:+  [${notes#, }]}"
        usable=$([[ $bus != bluetooth && $plugged == 1 ]] && echo 1 || echo 0)
        [[ $name == "$current_mic" ]] && current=$((i + 1))
        [[ $usable == 1 && $is_default == 1 ]] && default=$((i + 1))
        [[ $usable == 1 && -z $first ]] && first=$((i + 1))
    done
    if [[ -n $current_mic && -z $current ]]; then     # preferred mic, unplugged right now
        names+=("$current_mic"); descs+=("$current_mic")
        current=${#names[@]}
        printf '    %d) %s  [current, not connected]\n' "$current" "$current_mic"
    fi
    # suggestion: the current choice; None if that was chosen before; on a first
    # install a wired/built-in mic (the system default if it is one), so dictation
    # doesn't follow the default to Bluetooth earbuds connected later
    if [[ -n $current ]]; then suggested=$current
    elif (( ! fresh )); then suggested=0
    else suggested=${default:-${first:-0}}; fi
    while :; do
        pick=$(ask "Preferred microphone [$suggested]:")
        pick=${pick:-$suggested}
        # 10#: a leading zero would make bash read the number as octal
        [[ $pick =~ ^[0-9]+$ ]] && pick=$((10#$pick)) && (( pick <= ${#names[@]} )) && break
        warn "enter a number between 0 and ${#names[@]}"
    done
    create_options
    sed -i '/^--input-device/d' "$OPTIONS"
    if (( pick == 0 )); then
        ok "no preferred microphone - always the system default"
    else
        echo "--input-device ${names[$((pick - 1))]}" >> "$OPTIONS"
        ok "preferred: ${descs[$((pick - 1))]}, otherwise the system default (saved in $OPTIONS)"
    fi
fi

# --------------------------------------------------------------------------- #
step "Dictation key"
cat <<'EOF'
    Hold the key while speaking, the text appears on release.
    Or tap it, speak hands-free, and tap again to finish.
EOF
# The key is bound through GNOME's global shortcuts portal: GNOME asks for it in a
# dialog on the first start and remembers it. To change it, the binding is reset.
key=$(python3 -I "$SRC/tools/gnome-keys.py" get 2>/dev/null || true)
if [[ -n $key ]]; then
    if [[ "$(ask "Dictation key: $key. Change it? [y/N]")" == [yY]* ]]; then
        python3 -I "$SRC/tools/gnome-keys.py" reset
        key=""
        ok "GNOME will ask for the new key in a moment"
    else
        ok "keeping $key"
    fi
fi

# --------------------------------------------------------------------------- #
step "Service"
# before the start: the portals need it. Exec gets the full path - GLib ignores a
# desktop file whose program isn't on PATH, and ~/.local/bin may not be on it yet
mkdir -p "${DESKTOP%/*}"
sed "s|^Exec=npu-voice-to-text |Exec=\"$BIN/npu-voice-to-text\" |" \
    "$SRC/data/$APP_ID.desktop" > "$DESKTOP"
install -Dm644 "$SRC/systemd/npu-voice-to-text.service" "$UNITS/npu-voice-to-text.service"
systemctl --user daemon-reload
systemctl --user enable --quiet npu-voice-to-text
if [[ ! -s $TOKEN ]]; then
    cat <<'EOF'
    GNOME dialog: "Remote Desktop" - please allow it.
      npu-voice-to-text types the dictated text into other windows, so it asks for
      keyboard access. Only the keyboard: no mouse, no screen sharing.
EOF
fi
if [[ -z $key ]]; then
    cat <<'EOF'
    GNOME dialog: "Add Keyboard Shortcuts" - confirm F8 or press the key you want.
      Avoid keys apps need; on laptops, F-keys may need Fn or FnLock.
EOF
fi
[[ -s $TOKEN && -n $key ]] || echo "    GNOME remembers both answers; ./uninstall.sh revokes them."
systemctl --user restart npu-voice-to-text
restarts=$(systemctl --user show -p NRestarts --value npu-voice-to-text)
printf '    starting (the first start compiles the model for the NPU, up to a few minutes) '
for (( i = 0; ; i++ )); do
    "$BIN/npu-voice-to-text" status -q && break
    if systemctl --user is-failed -q npu-voice-to-text; then
        echo
        # exit status 3: keyboard access declined (or the dialog was left unanswered)
        [[ $(systemctl --user show -p ExecMainStatus --value npu-voice-to-text) == 3 ]] &&
            die "no keyboard access - re-run ./install.sh and allow it in the GNOME dialog"
        die "the service failed - see: journalctl --user -u npu-voice-to-text"
    fi
    # Restart=on-failure every 5 s never hits systemd's start limit, so a crash loop
    # never shows as "failed" - look at the restart counter instead
    (( $(systemctl --user show -p NRestarts --value npu-voice-to-text) == restarts )) ||
        { echo; die "the service keeps crashing - see: journalctl --user -u npu-voice-to-text"; }
    (( i < 900 )) || { echo; die "not ready after 15 min - see: journalctl --user -u npu-voice-to-text"; }
    (( i % 5 )) || printf .
    sleep 1
done
echo; ok "npu-voice-to-text is running"

# the key dialog may still be open: wait for GNOME to record the binding
printf '    waiting for the dictation key '
for (( i = 0; ; i++ )); do
    key=$(python3 -I "$SRC/tools/gnome-keys.py" get 2>/dev/null || true)
    [[ -n $key ]] && break
    [[ $("$BIN/npu-voice-to-text" status 2>/dev/null) == *"no dictation key"* ]] &&
        { echo; die "no dictation key - re-run ./install.sh and confirm a key in the GNOME dialog"; }
    (( i < 600 )) || { echo; die "no key confirmed after 10 min - re-run ./install.sh"; }
    (( i % 5 )) || printf .
    sleep 1
done
echo; ok "dictation key: $key"
howto="Hold $key while speaking, or tap it, speak and tap again - the text is typed about 2 s later."

# --------------------------------------------------------------------------- #
step "Done"
cat <<EOF
    $howto
    Logs:            journalctl --user -u npu-voice-to-text -f
    Options:         $OPTIONS (see npu-voice-to-text serve --help), then: systemctl --user restart npu-voice-to-text
    Key, mic:        re-run ./install.sh
EOF
