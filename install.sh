#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Konstantin Merkel
# Install or update npu-voice-to-text for the current user. Safe to re-run; re-run it to
# change the dictation key or the mode. Installed without root:
#   ~/.local/bin/npu-voice-to-text                     the program
#   ~/.local/share/npu-voice-to-text/{venv,lib,models} Python env, Intel NPU libs, Whisper models
#   ~/.config/systemd/user/npu-voice-to-text.service   the daemon
# Only for hold-to-talk mode (asks first, uses sudo):
#   /usr/local/bin/npu-voice-to-text-keywatch, /etc/systemd/system/npu-voice-to-text-keywatch@.service,
#   /etc/npu-voice-to-text/keywatch-<uid>.env
set -euo pipefail

# Intel NPU userspace driver + compiler. Must match the OpenVINO version in
# requirements.txt (see https://github.com/intel/linux-npu-driver/releases).
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
KEYWATCH_UNIT=/etc/systemd/system/npu-voice-to-text-keywatch@.service
KEYWATCH_ENV=/etc/npu-voice-to-text/keywatch-$(id -u).env

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
for cmd in python3 curl ar tar ydotool wl-copy wl-paste gsettings; do
    command -v "$cmd" >/dev/null || missing+=("$cmd")
done
has_lib libze_loader.so.1    || missing+=("Level Zero loader (Fedora: oneapi-level-zero, Ubuntu: libze1)")
has_lib libtbb.so.12         || missing+=("oneTBB (Fedora: tbb, Ubuntu: libtbb12)")
has_lib libportaudio.so.2    || missing+=("PortAudio (Fedora: portaudio, Ubuntu: libportaudio2)")
has_lib libxkbcommon.so.0    || missing+=("libxkbcommon")
(( ${#missing[@]} == 0 )) || die "missing: ${missing[*]}"
ok "NPU, tools and libraries found"
if pgrep -x ydotoold >/dev/null; then ok "ydotoold is running"
else warn "ydotoold is not running - nothing can be typed until it is (see README)"; fi

# --------------------------------------------------------------------------- #
step "Python environment"
python3 -m venv "$DATA/venv"
"$DATA/venv/bin/pip" install --quiet --upgrade pip
"$DATA/venv/bin/pip" install --quiet -r "$SRC/requirements.txt"
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
install -Dm755 "$SRC/npu-voice-to-text.py" "$BIN/npu-voice-to-text"
sed -i "1s|.*|#!$DATA/venv/bin/python -I|" "$BIN/npu-voice-to-text"     # run inside the venv
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
current_mic=$(sed -n 's/^--input-device[ =]//p' "$OPTIONS" 2>/dev/null | head -1)
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
step "Service"
install -Dm644 "$SRC/systemd/npu-voice-to-text.service" "$UNITS/npu-voice-to-text.service"
systemctl --user daemon-reload
systemctl --user enable --quiet npu-voice-to-text
systemctl --user restart npu-voice-to-text
restarts=$(systemctl --user show -p NRestarts --value npu-voice-to-text)
printf '    starting (the first start compiles the model for the NPU, up to a few minutes) '
for (( i = 0; ; i++ )); do
    "$BIN/npu-voice-to-text" status -q && break
    systemctl --user is-failed -q npu-voice-to-text &&
        { echo; die "the service failed - see: journalctl --user -u npu-voice-to-text"; }
    # Restart=on-failure every 5 s never hits systemd's start limit, so a crash loop
    # never shows as "failed" - look at the restart counter instead
    (( $(systemctl --user show -p NRestarts --value npu-voice-to-text) == restarts )) ||
        { echo; die "the service keeps crashing - see: journalctl --user -u npu-voice-to-text"; }
    (( i < 900 )) || { echo; die "not ready after 15 min - see: journalctl --user -u npu-voice-to-text"; }
    (( i % 5 )) || printf .
    sleep 1
done
echo; ok "npu-voice-to-text is running"

# --------------------------------------------------------------------------- #
step "Dictation key and mode"
declare -A KEYCODE KEYACCEL
while read -r name code accel; do KEYCODE[$name]=$code; KEYACCEL[$name]=$accel; done \
    < <(python3 -I "$SRC/npu-voice-to-text-keywatch.py" --list-keys)
cat <<'EOF'
    hold-to-talk  hold the key while speaking, text appears on release.
                  Needs a small root service that reads the keyboards and forwards
                  only this key's press/release (sudo; see README, Security).
    toggle        press the key to start, press it again to stop.
                  A plain GNOME shortcut, no root needed.
EOF
[[ "$(ask "Use hold-to-talk? [Y/n]")" == [nN]* ]] && mode=toggle || mode=hold

current=$(sed -n 's/^KEY=//p' "$KEYWATCH_ENV" 2>/dev/null || true)
echo "    keys: F1-F12, PAUSE, SCROLLLOCK, INSERT, MENU$([[ $mode == hold ]] && echo ", RIGHTCTRL, RIGHTMETA")"
echo "          (F13-F24 also work, if your keyboard or remapping tool has them)"
while :; do
    key=$(ask "Dictation key [${current:-F8}]:")
    key=${key:-${current:-F8}}; key=${key^^}
    if [[ -z "${KEYCODE[$key]:-}" ]]; then warn "unknown key '$key'"
    elif [[ $mode == toggle && "${KEYACCEL[$key]}" == - ]]; then warn "$key alone can't be a GNOME shortcut - pick another key"
    else break; fi
done
accel=${KEYACCEL[$key]}

if [[ $mode == hold ]]; then
    echo "    installing the key watcher (sudo):"
    sudo install -Dm755 -o root -g root "$SRC/npu-voice-to-text-keywatch.py" /usr/local/bin/npu-voice-to-text-keywatch
    sudo install -Dm644 -o root -g root "$SRC/systemd/npu-voice-to-text-keywatch@.service" "$KEYWATCH_UNIT"
    echo "KEY=$key" | sudo install -Dm644 -o root -g root /dev/stdin "$KEYWATCH_ENV"
    sudo systemctl daemon-reload
    sudo systemctl enable --quiet "npu-voice-to-text-keywatch@$(id -u)"
    sudo systemctl restart "npu-voice-to-text-keywatch@$(id -u)"
    ok "key watcher running for $key"
    # the watcher doesn't grab the key: let GNOME swallow it so apps don't receive it
    if [[ $accel != - ]]; then
        python3 -I "$SRC/tools/gnome-shortcut.py" set "$accel" true \
            && ok "GNOME swallows $key (shortcut 'npu-voice-to-text' -> true)" \
            || warn "apps will also receive $key - free it up in GNOME Settings -> Keyboard"
    else
        python3 -I "$SRC/tools/gnome-shortcut.py" remove >/dev/null || true
    fi
    howto="Hold $key, speak, release - the text is typed about 2 s later."
else
    if [[ -e "$KEYWATCH_UNIT" ]]; then
        echo "    removing the key watcher from a previous hold-to-talk install (sudo):"
        sudo systemctl disable --now --quiet "npu-voice-to-text-keywatch@$(id -u)" || true
        sudo rm -f "$KEYWATCH_ENV"
        ok "key watcher removed"
    fi
    python3 -I "$SRC/tools/gnome-shortcut.py" set "$accel" "$BIN/npu-voice-to-text toggle" \
        && ok "GNOME shortcut $key -> npu-voice-to-text toggle" \
        || die "could not create the shortcut - free up $key in GNOME Settings -> Keyboard and re-run"
    howto="Press $key, speak, press $key again - the text is typed about 2 s later."
fi

# --------------------------------------------------------------------------- #
step "Done"
cat <<EOF
    $howto
    Logs:            journalctl --user -u npu-voice-to-text -f
    Options:         $OPTIONS (see npu-voice-to-text serve --help), then: systemctl --user restart npu-voice-to-text
    Key, mode, mic:  re-run ./install.sh
EOF
