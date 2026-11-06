#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Konstantin Merkel
# Install or update npu-voice-to-text for the current user. Safe to re-run. Asks for
# the settings: Recommended, or Custom - microphone, dictation key, typing speed,
# languages, sounds.
# Everything goes into your home directory, no root needed:
#   ~/.local/bin/npu-voice-to-text                     launcher for the program in the venv
#   ~/.local/share/npu-voice-to-text/{venv,lib,models} Python env, Intel NPU libs, Whisper models
#   ~/.local/share/applications/<APP_ID>.desktop       the app's identity for the portals
#   ~/.config/systemd/user/npu-voice-to-text.service   the daemon
set -euo pipefail

case "${1:-}" in
    "")         ;;
    -h|--help)  sed -n '4,6p' "$0" | cut -c3-; exit 0 ;;
    *)          echo "unknown option: $1 (see ./install.sh --help)" >&2; exit 2 ;;
esac

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
# Load the library the way the daemon does (ctypes, 64-bit python3, no
# LD_LIBRARY_PATH, which the service doesn't inherit). Unlike searching
# `ldconfig -p`, this needs no sbin on PATH or ld.so.cache, and skips 32-bit copies.
has_lib() { env -u LD_LIBRARY_PATH python3 -I -c 'import ctypes, sys; ctypes.CDLL(sys.argv[1])' "$1" 2>/dev/null; }

# --------------------------------------------------------------------------- #
step "Checking requirements"
[[ -e /dev/accel/accel0 ]] || die "no NPU found (/dev/accel/accel0). Needs an Intel Core Ultra CPU and the intel_vpu kernel driver."
[[ -r /dev/accel/accel0 && -w /dev/accel/accel0 ]] || die "/dev/accel/accel0 is not read/writable for you (normally group 'render')."
missing=()
for cmd in python3 curl ar tar gsettings gdbus gnome-shell; do
    command -v "$cmd" >/dev/null || missing+=("$cmd")
done
if command -v python3 >/dev/null; then       # the library checks need it
    has_lib libze_loader.so.1    || missing+=("Level Zero loader (Fedora: oneapi-level-zero, Ubuntu: libze1)")
    has_lib libtbb.so.12         || missing+=("oneTBB (Fedora: tbb, Ubuntu: libtbb12)")
    has_lib libportaudio.so.2    || missing+=("PortAudio (Fedora: portaudio, Ubuntu: libportaudio2)")
    has_lib libxkbcommon.so.0    || missing+=("libxkbcommon")
    python3 -I -c 'from gi.repository import Gio' 2>/dev/null || missing+=("PyGObject (Fedora: python3-gobject, Ubuntu: python3-gi)")
fi
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
    # the marker is written when a download finished (models.py COMPLETE_MARKER);
    # without it, `download` finishes an interrupted one
    if [[ -e "$DATA/models/${model##*/}/.download-complete" ]]; then
        ok "${model##*/} already downloaded"
    else
        "$BIN/npu-voice-to-text" download --model "$model"
        ok "${model##*/} downloaded"
    fi
done

# --------------------------------------------------------------------------- #
# Settings: "Recommended" (no questions) or "Custom" (one question each, with the
# current choices as defaults). Stored as `serve` options in $OPTIONS; options at
# their default are left out, lines the installer doesn't manage are kept.
step "Settings"
create_options() {
    [[ -e "$OPTIONS" ]] && return
    mkdir -p "${OPTIONS%/*}"
    cat > "$OPTIONS" <<'EOF'
# Options for `npu-voice-to-text serve`, one per line - see `npu-voice-to-text serve --help`.
# After editing: systemctl --user restart npu-voice-to-text
EOF
}
opt_get() { sed -n "s/^$1[ =]//p" "$OPTIONS" 2>/dev/null | head -1 || true; }   # opt_get --name
opt_has() { [[ -e "$OPTIONS" ]] && grep -q -- "^$1\( \|=\|$\)" "$OPTIONS"; }    # opt_has --flag
# opt_set --name [value]: replace the option's line; without a value, remove it.
# A value of "" writes a bare flag.
opt_set() {
    create_options
    sed -i -- "/^$1\( \|=\|$\)/d" "$OPTIONS"
    if (( $# > 1 )); then echo "$1${2:+ $2}" >> "$OPTIONS"; fi
}
title() { printf '\n    \033[1m%s\033[0m\n' "$1"; }
pick() {     # pick suggested max: a number from 0 to max
    local reply
    while :; do
        reply=$(ask "Choose [$1]:")
        reply=${reply:-$1}
        # 10#: a leading zero would make bash read the number as octal
        [[ $reply =~ ^[0-9]+$ ]] && (( 10#$reply <= $2 )) && { echo $((10#$reply)); return; }
        warn "enter a number between 0 and $2" >&2
    done
}
yes_no() {   # yes_no "question" y|n: true for yes
    local reply
    reply=$(ask "$1 [$([[ $2 == y ]] && echo Y/n || echo y/N)]")
    [[ ${reply:-$2} == [yY]* ]]
}

custom=0
if [[ -t 0 ]]; then            # without a terminal (piped, scripted): Recommended
    cat <<'EOF'
    1) Recommended   a wired or built-in mic, normal typing speed, German and English
    2) Custom        choose the mic, dictation key, typing speed, languages, sounds
EOF
    while :; do
        reply=$(ask "Choose [1]:")
        case ${reply:-1} in 1) break ;; 2) custom=1; break ;; esac
        warn "enter 1 or 2"
    done
fi

# ---- microphone. Recommended: a wired or built-in mic (the system default if it
# is one), so dictation doesn't follow the default to Bluetooth earbuds
current_mic=$(opt_get --input-device)
mapfile -t MICS < <("$DATA/venv/bin/python" -I "$SRC/tools/microphones.py" 2>/dev/null || true)
names=(); descs=(); lines=(); current=""; default=""; first=""
for i in "${!MICS[@]}"; do
    IFS=$'\t' read -r name desc bus is_default plugged <<<"${MICS[$i]}"
    names+=("$name"); descs+=("$desc")
    notes=""
    [[ $is_default == 1 ]]        && notes+=", system default"
    [[ $name == "$current_mic" ]] && notes+=", current"
    [[ $bus == bluetooth ]]       && notes+=", Bluetooth: not recommended"
    [[ $plugged == 0 ]]           && notes+=", nothing plugged in"
    lines+=("$(printf '    %d) %s%s' $((i + 1)) "$desc" "${notes:+  [${notes#, }]}")")
    usable=$([[ $bus != bluetooth && $plugged == 1 ]] && echo 1 || echo 0)
    [[ $name == "$current_mic" ]] && current=$((i + 1))
    [[ $usable == 1 && $is_default == 1 ]] && default=$((i + 1))
    [[ $usable == 1 && -z $first ]] && first=$((i + 1))
done
if [[ -n $current_mic && -z $current ]]; then         # preferred mic, unplugged right now
    names+=("$current_mic"); descs+=("$current_mic")
    current=${#names[@]}
    lines+=("$(printf '    %d) %s  [current, not connected]' "$current" "$current_mic")")
fi
mic=${default:-${first:-0}}
mic_text="the system default"
if (( ${#MICS[@]} == 0 )); then
    # pactl missing, or only monitors (the preferred USB mic unplugged): keep it
    if [[ -n $current ]]; then
        warn "could not list microphones (is pactl installed?) - keeping $current_mic"
        mic=$current
    else
        warn "could not list microphones (is pactl installed?) - dictation uses the system default"
        mic=0
    fi
elif (( custom )); then
    title "Microphone"
    cat <<'EOF'
    Used while connected, otherwise the system default. Avoid Bluetooth headsets:
    their mic needs ~1 s to start, which cuts off the start of each dictation.
    0) None - always the system default
EOF
    printf '%s\n' "${lines[@]}"
    [[ -n $current ]] && mic=$current
    [[ -z $current && -e $OPTIONS ]] && mic=0           # "None" was chosen before
    mic=$(pick "$mic" "${#names[@]}")
fi
if (( mic == 0 )); then
    opt_set --input-device
else
    opt_set --input-device "${names[$((mic - 1))]}"
    mic_text="${descs[$((mic - 1))]}, otherwise the system default"
fi

# ---- dictation key: bound through GNOME's global shortcuts portal. GNOME asks for
# it in a dialog on the first start and remembers it; to change it, the binding is
# reset, and GNOME asks again.
key=$(python3 -I "$SRC/tools/gnome-keys.py" get 2>/dev/null || true)
if [[ -n $key ]] && (( custom )); then
    title "Dictation key"
    if yes_no "$key. Change it?" n; then
        python3 -I "$SRC/tools/gnome-keys.py" reset
        key=""
    fi
fi

# ---- typing speed, languages, sounds, space
delay=4; languages=de,en; fallback=en; sounds=n; space=y
if (( custom )); then
    delay=$(opt_get --key-delay); delay=${delay:-4}
    languages=$(opt_get --languages); languages=${languages:-de,en}
    fallback=$(opt_get --fallback-language); fallback=${fallback:-en}
    sounds=$(opt_has --sounds && echo y || echo n)
    space=$(opt_has --no-trailing-space && echo n || echo y)

    title "Typing speed"
    echo "    Some apps lose characters when they are typed too fast."
    speeds=(0 2 4 10)
    printf '    1) instant   no pause between keys, some apps may drop characters\n'
    printf '    2) fast      ~0.5 s for 50 words\n    3) normal    ~1 s\n    4) careful   ~2.5 s\n'
    speed=0
    for i in "${!speeds[@]}"; do [[ ${speeds[$i]} == "$delay" ]] && speed=$((i + 1)); done
    (( speed )) || printf '    0) keep the current %s ms per key\n' "$delay"
    speed=$(pick "$speed" ${#speeds[@]})
    (( speed )) && delay=${speeds[$((speed - 1))]}

    title "Languages"
    echo "    Two-letter codes, e.g. de,en,fr. Speech in any other language is written in the fallback."
    while :; do
        reply=$(ask "Languages [$languages]:"); reply=${reply:-$languages}; reply=${reply// /}
        [[ $reply =~ ^[a-z]{2,3}(,[a-z]{2,3})*$ ]] && break
        warn "two-letter codes separated by commas, e.g. de,en"
    done
    languages=$reply
    [[ ,$languages, == *,"$fallback",* ]] || fallback=${languages%%,*}
    while :; do
        reply=$(ask "Fallback [$fallback]:"); reply=${reply:-$fallback}
        [[ $reply =~ ^[a-z]{2,3}$ ]] && break
        warn "a two-letter code, e.g. en"
    done
    fallback=$reply

    title "Other"
    yes_no "Start/stop sounds?" "$sounds" && sounds=y || sounds=n
    yes_no "Space after each dictation?" "$space" && space=y || space=n
    echo
fi
if (( delay == 4 )); then opt_set --key-delay; else opt_set --key-delay "$delay"; fi
if [[ $languages == de,en ]]; then opt_set --languages; else opt_set --languages "$languages"; fi
if [[ $fallback == en ]]; then opt_set --fallback-language; else opt_set --fallback-language "$fallback"; fi
if [[ $sounds == y ]]; then opt_set --sounds ""; else opt_set --sounds; fi
if [[ $space == y ]]; then opt_set --no-trailing-space; else opt_set --no-trailing-space ""; fi

case $delay in 0) speed_text=instant ;; 2) speed_text=fast ;; 4) speed_text=normal ;; 10) speed_text=careful ;;
               *) speed_text="$delay ms per key" ;; esac
ok "microphone:     $mic_text"
ok "dictation key:  ${key:-GNOME asks for it in a moment}"
ok "typing speed:   $speed_text"
ok "languages:      ${languages//,/, } (fallback $fallback)"
ok "sounds:         $([[ $sounds == y ]] && echo on || echo off), space after each dictation: $([[ $space == y ]] && echo on || echo off)"

# --------------------------------------------------------------------------- #
step "Service"
# before the start: the portals need it. Exec gets the full path - GLib ignores a
# desktop file whose program isn't on PATH, and ~/.local/bin may not be on it yet
mkdir -p "${DESKTOP%/*}"
sed "s|^Exec=npu-voice-to-text |Exec=\"$BIN/npu-voice-to-text\" |" \
    "$SRC/data/$APP_ID.desktop" > "$DESKTOP"
# The unit gets the paths this script used: systemd's environment may have other
# (or no) XDG_* values than this shell, and the daemon would look elsewhere.
unit_value() { local s=${1//\\/\\\\}; s=${s//\"/\\\"}; printf '"%s"' "${s//%/%%}"; }
env_lines="Environment=$(unit_value "LD_LIBRARY_PATH=$DATA/lib")"
for var in DATA:.local/share CONFIG:.config STATE:.local/state CACHE:.cache; do
    name=XDG_${var%%:*}_HOME
    value=${!name:-$HOME/${var#*:}}
    env_lines+=$'\n'"Environment=$(unit_value "$name=$value")"
done
unit=$(<"$SRC/systemd/npu-voice-to-text.service")
lib_line='Environment=LD_LIBRARY_PATH=%h/.local/share/npu-voice-to-text/lib'
[[ $unit == *"$lib_line"* ]] || die "systemd/npu-voice-to-text.service: no '$lib_line' line"
mkdir -p "$UNITS"
printf '%s\n' "${unit/"$lib_line"/"$env_lines"}" > "$UNITS/npu-voice-to-text.service"
systemctl --user daemon-reload
systemctl --user enable --quiet npu-voice-to-text
if [[ ! -s $TOKEN ]]; then
    cat <<'EOF'
    GNOME dialog "Remote Desktop": switch on "Allow Remote Interaction", keep
      "Remember This Selection" ticked, then click Share. This lets npu-voice-to-text
      type the dictated text into other windows: keyboard only, no mouse, no screen.
      (GNOME's dialog names no app for keyboard-only access.)
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
            die "no keyboard access - re-run ./install.sh; in the GNOME dialog switch on \"Allow Remote Interaction\", keep \"Remember This Selection\" ticked, click Share"
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
    Settings:        re-run ./install.sh and choose Custom
EOF
