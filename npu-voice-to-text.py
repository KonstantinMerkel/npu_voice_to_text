#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Konstantin Merkel
"""npu-voice-to-text - hold-to-talk dictation for GNOME Wayland, Whisper on the Intel NPU.

Commands:
  serve               run the daemon (normally via the systemd user service)
  press / release     start / stop recording (sent by npu-voice-to-text-keywatch)
  toggle              start or stop recording (for a normal GNOME shortcut)
  cancel              stop recording and throw the audio away
  status              print the daemon state
  download            fetch a Whisper model from Hugging Face

The client commands use only the standard library, so they start instantly.
numpy, sounddevice and openvino_genai are imported by `serve` / `download` only.
See docs/ARCHITECTURE.md for how the pieces fit together.
"""

import argparse
import ast
import collections
import ctypes
import json
import os
import queue
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

HOME = Path.home()
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
SOCKET_PATH = RUNTIME_DIR / "npu-voice-to-text.sock"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share")) / "npu-voice-to-text"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "npu-voice-to-text"
MODELS_DIR = DATA_DIR / "models"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "npu-voice-to-text"
OPTIONS_FILE = CONFIG_DIR / "options"

DEFAULT_MODEL = "OpenVINO/whisper-large-v3-turbo-int8-ov"
DETECT_MODEL = "OpenVINO/whisper-small-int8-ov"
SAMPLE_RATE = 16000
MAX_SECONDS = 120          # safety stop for a stuck key / forgotten recording
FRAME_SECONDS = 0.03       # speech detection works on 30 ms frames
SPEECH_RMS_MIN = 0.002     # a frame is speech if louder than this AND 3x the noise floor
MIN_SPEECH_SECONDS = 0.15  # less speech than this: accidental tap / key click / silence
DETECT_MIN_SPEECH = 1.0    # language detection is unreliable below ~1 s of speech
                           # (single words come out as "en"), so short clips reuse
                           # the last detected language instead

# Phrases Whisper "hears" in silence or noise (subtitle credits from its training
# data). Always dropped.
HALLUCINATIONS = {
    "untertitel der amara.org-community",
    "untertitel im auftrag des zdf, 2017",
    "untertitel im auftrag des zdf für funk, 2017",
    "vielen dank fürs zuschauen.",
    "swr 2020",
    "swr 2021",
    "thanks for watching!",
}
# Phrases Whisper also hallucinates, but that people really say. Dropped only when
# the clip had barely any speech (a key click or a breath that just passed the
# speech detector), so a dictated "Vielen Dank." is typed.
SHORT_HALLUCINATIONS = {"vielen dank.", "thank you.", "you"}
SHORT_HALLUCINATION_SPEECH = 0.3   # seconds

SOUND_START = "/usr/share/sounds/freedesktop/stereo/device-added.oga"
SOUND_STOP = "/usr/share/sounds/freedesktop/stereo/device-removed.oga"
SOUND_START_SKIP = 0.4     # seconds of audio dropped after the start sound (0.22 s + latency)
NOISE_FLOOR_MIN = 1e-5     # quieter "noise floors" are digital silence, not a measurement


# =========================================================================== #
# Client (stdlib only)
# =========================================================================== #
def request(cmd: str, timeout: float = 5) -> str | None:
    """Send one command to the daemon and return its reply (None if not running)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(SOCKET_PATH))
            s.sendall(cmd.encode() + b"\n")
            return s.recv(4096).decode().strip()
    except OSError:                     # not running, stale socket, timeout, …
        return None


def run_client(cmd: str, quiet: bool = False) -> int:
    """quiet: no output, only the exit code (for scripts)."""
    reply = request(cmd)
    if reply is None:
        if not quiet:
            print("npu-voice-to-text is not running (systemctl --user start npu-voice-to-text)",
                  file=sys.stderr)
        return 1
    if not quiet:
        print(reply)
    return 1 if reply.startswith("error") else 0


# =========================================================================== #
# Keyboard: type text into the focused window
# =========================================================================== #
# Linux input event codes (layout independent), see linux/input-event-codes.h
KEY_LEFTSHIFT, KEY_RIGHTALT, KEY_SPACE = 42, 100, 57
ISO_LEVEL3_SHIFT = 0xFE03  # keysym of the AltGr function
# xkb's virtual <LVL3> and <MDSW> keys: they exist in every keymap, but no keyboard
# has them, and ydotoold's virtual keyboard can't send them either
XKB_VIRTUAL_KEYS = {84, 195}
# keys we type with: the main block (codes 1-54) plus space, 102ND (<>|), RO and YEN.
# Characters only found elsewhere - keypad, KEY_EURO on us, media keys - are pasted
# instead, because many apps ignore those keys.
TYPING_KEYS = frozenset(range(1, 55)) | {57, 86, 89, 124}
PASTE_KEYS = {
    "ctrl-v": "29:1 47:1 47:0 29:0",
    "ctrl-shift-v": "29:1 42:1 47:1 47:0 42:0 29:0",   # terminals
    "shift-insert": "42:1 110:1 110:0 42:0",
}
CLIPBOARD_FILE_TYPES = ("x-special/gnome-copied-files",)
CLIPBOARD_TEXT_TYPES = ("text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "STRING",
                        "TEXT")
# dead keysym -> (combining mark it adds, character it types when followed by space)
DEAD_KEYS = {
    0xFE50: ("̀", "`"), 0xFE51: ("́", "´"), 0xFE52: ("̂", "^"),
    0xFE53: ("̃", "~"), 0xFE57: ("̈", "¨"), 0xFE58: ("̊", "°"),
    0xFE5B: ("̧", "¸"),
}


def gnome_layout() -> tuple[str, str, str]:
    """(layout, variant, options) of the active GNOME xkb input source."""
    def get(key):
        out = subprocess.run(["gsettings", "get", "org.gnome.desktop.input-sources", key],
                             capture_output=True, text=True).stdout.strip()
        out = out.split(" ", 1)[1] if out.startswith("@") else out   # strip "@a(ss) " type tag
        try:
            return ast.literal_eval(out) if out else []
        except (ValueError, SyntaxError):
            return []

    sources = [s for s in get("mru-sources") + get("sources") if s[0] == "xkb"]
    layout, _, variant = (sources[0][1] if sources else "us").partition("+")
    return layout, variant, ",".join(get("xkb-options"))


class _RuleNames(ctypes.Structure):
    _fields_ = [(f, ctypes.c_char_p) for f in ("rules", "model", "layout", "variant", "options")]


class XkbKeymap:
    """Which physical key (+ Shift/AltGr, + dead key) produces each character on a layout.

    Built from libxkbcommon - the same library GNOME uses - so the result matches
    what the compositor does with the keycodes we send.
    """

    def __init__(self, layout: str, variant: str = "", options: str = ""):
        x = ctypes.CDLL("libxkbcommon.so.0")
        P, U32 = ctypes.c_void_p, ctypes.c_uint32
        x.xkb_context_new.restype = P
        x.xkb_keymap_new_from_names.restype = P
        x.xkb_keymap_new_from_names.argtypes = [P, ctypes.POINTER(_RuleNames), ctypes.c_int]
        for fn in ("xkb_keymap_min_keycode", "xkb_keymap_max_keycode"):
            getattr(x, fn).restype, getattr(x, fn).argtypes = U32, [P]
        x.xkb_keymap_mod_get_index.restype = U32
        x.xkb_keymap_mod_get_index.argtypes = [P, ctypes.c_char_p]
        x.xkb_keymap_num_layouts_for_key.argtypes = [P, U32]
        x.xkb_keymap_num_levels_for_key.argtypes = [P, U32, U32]
        x.xkb_keymap_key_get_syms_by_level.argtypes = [P, U32, U32, U32,
                                                       ctypes.POINTER(ctypes.POINTER(U32))]
        x.xkb_keymap_key_get_mods_for_level.restype = ctypes.c_size_t
        x.xkb_keymap_key_get_mods_for_level.argtypes = [P, U32, U32, U32,
                                                        ctypes.POINTER(U32), ctypes.c_size_t]
        x.xkb_keysym_to_utf32.restype, x.xkb_keysym_to_utf32.argtypes = U32, [U32]
        x.xkb_keymap_unref.argtypes = [P]
        x.xkb_context_unref.argtypes = [P]

        self.name = layout + (f"+{variant}" if variant else "")
        ctx = x.xkb_context_new(0)
        names = _RuleNames(b"evdev", b"pc105", layout.encode(), variant.encode(), options.encode())
        km = x.xkb_keymap_new_from_names(ctx, ctypes.byref(names), 0)
        if not km:
            x.xkb_context_unref(ctx)
            raise RuntimeError(f"xkbcommon could not compile layout {self.name!r}")

        shift = 1 << x.xkb_keymap_mod_get_index(km, b"Shift")
        altgr = 1 << x.xkb_keymap_mod_get_index(km, b"Mod5")      # ISO_Level3_Shift
        best: dict[str, tuple] = {}     # char -> (cost, stroke)
        dead: dict[str, tuple] = {}     # combining mark / spacing char -> (cost, stroke)
        keycodes = [kc for kc in range(x.xkb_keymap_min_keycode(km),
                                       x.xkb_keymap_max_keycode(km) + 1)
                    if x.xkb_keymap_num_layouts_for_key(km, kc) >= 1]

        # The key that acts as AltGr: Right Alt on most layouts, but options like
        # compose:ralt or lv3:caps_switch move it. None: AltGr characters get pasted.
        level3 = []
        for kc in keycodes:
            syms = ctypes.POINTER(U32)()
            if (kc - 8 not in XKB_VIRTUAL_KEYS
                    and x.xkb_keymap_key_get_syms_by_level(km, kc, 0, 0, ctypes.byref(syms)) == 1
                    and syms[0] == ISO_LEVEL3_SHIFT):
                level3.append(kc - 8)
        self.altgr_key = KEY_RIGHTALT if KEY_RIGHTALT in level3 else next(iter(level3), None)

        for kc in keycodes:
            for lvl in range(x.xkb_keymap_num_levels_for_key(km, kc, 0)):
                syms = ctypes.POINTER(U32)()
                if x.xkb_keymap_key_get_syms_by_level(km, kc, 0, lvl, ctypes.byref(syms)) != 1:
                    continue
                masks = (U32 * 16)()
                n = x.xkb_keymap_key_get_mods_for_level(km, kc, 0, lvl, masks, 16)
                usable = [m for m in masks[:n] if not m & ~(shift | altgr)]
                if not usable:
                    continue
                mask = min(usable, key=lambda m: bin(m).count("1"))
                if mask & altgr and self.altgr_key is None:
                    continue
                mods = ((self.altgr_key,) * bool(mask & altgr)
                        + (KEY_LEFTSHIFT,) * bool(mask & shift))
                code = kc - 8                           # xkb keycode = evdev keycode + 8
                if code not in TYPING_KEYS:
                    continue
                stroke = (code, mods)
                cost = (len(mods), kc)                  # fewest modifiers first
                sym = syms[0]
                if sym in DEAD_KEYS:
                    for key in DEAD_KEYS[sym]:
                        if key not in dead or cost < dead[key][0]:
                            dead[key] = (cost, stroke)
                    continue
                cp = x.xkb_keysym_to_utf32(sym)
                if cp >= 0x20 and cp != 0x7F:          # printable only (no Return/Tab/BS)
                    ch = chr(cp)
                    if ch not in best or cost < best[ch][0]:
                        best[ch] = (cost, stroke)

        x.xkb_keymap_unref(km)
        x.xkb_context_unref(ctx)
        self.keys = {c: [s] for c, (_, s) in best.items()}
        self.dead = {c: s for c, (_, s) in dead.items()}
        space = self.keys.get(" ", [(KEY_SPACE, ())])
        for _mark, spacing in DEAD_KEYS.values():      # e.g. "^" = dead_circumflex, space
            if spacing not in self.keys and spacing in self.dead:
                self.keys[spacing] = [self.dead[spacing]] + space

    def strokes_for(self, ch: str):
        """Strokes that produce one character, or None if the layout can't."""
        if ch in self.keys:
            return self.keys[ch]
        decomp = unicodedata.normalize("NFD", ch)        # é -> e + U+0301 -> dead_acute, e
        if len(decomp) == 2 and decomp[0] in self.keys and decomp[1] in self.dead:
            return [self.dead[decomp[1]]] + self.keys[decomp[0]]
        return None

    def plan(self, text: str) -> list[tuple[str, object]]:
        """Split text into ("keys", [[ydotool events of one character], ...]) and
        ("paste", str) segments."""
        segs: list = []
        for ch in text:
            strokes = self.strokes_for(ch)
            if strokes is None:
                if segs and segs[-1][0] == "paste":
                    segs[-1] = ("paste", segs[-1][1] + ch)
                else:
                    segs.append(("paste", ch))
                continue
            ev = []
            for code, mods in strokes:
                ev += [f"{m}:1" for m in mods] + [f"{code}:1", f"{code}:0"]
                ev += [f"{m}:0" for m in reversed(mods)]
            if segs and segs[-1][0] == "keys":
                segs[-1][1].append(ev)
            else:
                segs.append(("keys", [ev]))
        return segs


class Keyboard:
    """Writes text into the focused window through ydotool (kernel uinput).

    mode "type":  keystrokes computed for the active layout; characters the layout
                  can't produce are pasted via the clipboard.
    mode "paste": everything via clipboard + paste shortcut.
    """

    def __init__(self, mode: str = "type", layout: str | None = None, key_delay_ms: int = 4,
                 paste_keys: str = "ctrl-v", restore_clipboard: bool = True, gate=None):
        """gate: called before every ydotool call; may block to pause typing."""
        self.mode, self.layout, self.key_delay = mode, layout, key_delay_ms
        self.paste_keys, self.restore_clipboard = paste_keys, restore_clipboard
        self.gate = gate or (lambda: None)
        self._keymaps: dict[tuple, XkbKeymap] = {}

    @staticmethod
    def _env() -> dict:
        """Environment for ydotool. Looked up on every call: ydotoold may be started
        after the daemon, and its socket path depends on how it runs."""
        env = dict(os.environ)
        if "YDOTOOL_SOCKET" not in env:
            for cand in (RUNTIME_DIR / ".ydotool_socket", Path("/tmp/.ydotool_socket")):
                if cand.exists():
                    env["YDOTOOL_SOCKET"] = str(cand)
                    break
        return env

    def keymap(self) -> XkbKeymap:
        """Keymap of the layout GNOME uses right now (cached per layout)."""
        if self.layout:
            layout, _, variant = self.layout.partition("+")
            key = (layout, variant, "")
        else:
            key = gnome_layout()
        if key not in self._keymaps:
            self._keymaps[key] = XkbKeymap(*key)
            print(f"keymap {self._keymaps[key].name}: {len(self._keymaps[key].keys)} chars",
                  flush=True)
        return self._keymaps[key]

    def write(self, text: str):
        # newlines/tabs become spaces: Enter would send chat messages
        text = text.translate({ord("\n"): " ", ord("\r"): " ", ord("\t"): " "})
        if self.mode == "paste":
            return self.paste(text)
        for kind, payload in self.keymap().plan(text):
            if kind == "paste":
                print(f"not on layout, pasting: {payload!r}", flush=True)
                try:
                    self.paste(payload)
                except (OSError, subprocess.SubprocessError) as e:   # keep typing the rest
                    print(f"  could not paste {payload!r}, skipped: {e!r}", file=sys.stderr,
                          flush=True)
            else:
                self._type(payload)

    def _type(self, chars: list[list[str]]):
        """One ydotool call per character (~0.4 ms each), with the gate before each:
        typing can pause between any two characters, and never with Shift or AltGr
        held down."""
        for events in chars:
            self.gate()
            self._keys(events, self.key_delay)

    def _keys(self, events: list[str], delay_ms: int = 12):
        subprocess.run(["ydotool", "key", "-d", str(delay_ms), *events],
                       env=self._env(), check=True, timeout=60)

    def paste(self, text: str):
        # gated once, before the clipboard is touched: waiting between the copy and
        # Ctrl+V would leave the fragment in the clipboard for a whole recording
        self.gate()
        old = self._save_clipboard() if self.restore_clipboard else None
        subprocess.run(["wl-copy", "--type", "text/plain;charset=utf-8", "--", text],
                       check=True, timeout=5)
        time.sleep(0.15)                            # let the compositor see the new selection
        self._keys(PASTE_KEYS[self.paste_keys].split())
        if old is not None:
            time.sleep(0.5)                         # let the target app read the clipboard
            mime, data = old
            try:
                subprocess.run(["wl-copy", "--type", mime], input=data, timeout=5, check=False)
            except subprocess.TimeoutExpired:
                print("  could not restore the clipboard", flush=True)

    @staticmethod
    def _save_clipboard() -> tuple[str, bytes] | None:
        """(mime type, data) of the clipboard, or None if it is empty or unreadable.

        wl-copy can offer only one type, so one is chosen: copied files (GNOME's file
        list type - file managers also offer them as text), else text, else the first
        real mime type (an image, ...).
        """
        try:
            r = subprocess.run(["wl-paste", "--list-types"], capture_output=True, text=True,
                               timeout=2)
            types = r.stdout.splitlines() if r.returncode == 0 else []
            mime = (next((t for t in CLIPBOARD_FILE_TYPES if t in types), None)
                    or next((t for t in CLIPBOARD_TEXT_TYPES if t in types), None)
                    or next((t for t in types if "/" in t), None))
            if mime is None:
                return None
            r = subprocess.run(["wl-paste", "--no-newline", "--type", mime], capture_output=True,
                               timeout=2)
            return (mime, r.stdout) if r.returncode == 0 else None
        except subprocess.TimeoutExpired:           # clipboard owner not responding
            print("  could not read the clipboard, it won't be restored", flush=True)
            return None


# =========================================================================== #
# Transcriber: language detection on the CPU, transcription on the NPU
# =========================================================================== #
def model_path(model_id: str) -> Path:
    p = Path(model_id).expanduser()
    return p if p.is_dir() else MODELS_DIR / model_id.split("/")[-1]


def lang_code(tok: str) -> str:
    """'<|de|>' / 'de' / 'de-DE' -> 'de'"""
    return tok.strip().strip("<|>").split("-")[0].lower()


def speech_stats(audio, floors=()) -> tuple[float, float, float]:
    """(seconds of speech, loudest frame RMS, noise floor of this clip) - a simple
    energy-based voice detector.

    A 30 ms frame counts as speech when it is louder than SPEECH_RMS_MIN and 3x the
    noise floor, so it adapts to the mic's gain and background noise. The noise
    floor is the 20th percentile of the clip's frames (exact digital silence left
    out), or the lowest floor of the last few recordings (`floors`) if that is lower: when the key is held tightly
    around a single word, the whole clip is speech and its own 20th percentile is
    far above the real noise. Unlike the RMS of the whole clip, a short or quiet
    word in a mostly silent recording still counts.
    """
    import numpy as np

    frame = int(FRAME_SECONDS * SAMPLE_RATE)
    n = len(audio) // frame
    if n == 0:
        return 0.0, 0.0, 0.0
    rms = np.sqrt(np.mean(audio[:n * frame].reshape(n, frame) ** 2, axis=1))
    # digital silence (a Bluetooth mic waking up) says nothing about the room's noise
    real = rms[rms > NOISE_FLOOR_MIN]
    floor = float(np.percentile(real, 20)) if len(real) else 0.0
    threshold = max(SPEECH_RMS_MIN, 3 * min([floor, *floors]))
    return float((rms > threshold).sum() * FRAME_SECONDS), float(rms.max()), floor


class Transcriber:
    """Whisper via OpenVINO GenAI.

    Language choice per recording:
      - forced (--language): always that one
      - >= DETECT_MIN_SPEECH of speech: detected by a small model on the CPU,
        restricted to `languages` (else `fallback`), and remembered
      - shorter: the remembered language (initially languages[0]) - Whisper can't
        tell the language of one or two words and mostly answers "en"
    The language is always passed explicitly to the main model. Its own
    auto-detection is never used: on the NPU it sticks to the first language it
    ever detected (seen with OpenVINO GenAI 2026.4).
    """

    def __init__(self, model: str, device: str, detect_model: str, detect_device: str,
                 languages: list[str], fallback: str, forced: str | None):
        import numpy as np
        import openvino_genai as ov_genai

        self.device, self.languages, self.fallback = device, languages, fallback
        self.forced, self.detect_device = forced, detect_device
        self.last_language = languages[0] if languages else fallback
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        silence = np.zeros(SAMPLE_RATE, dtype=np.float32).tolist()

        print(f"loading {model} on {device} (the first start compiles it, can take minutes)",
              flush=True)
        self.pipe = ov_genai.WhisperPipeline(str(self._require(model)), device,
                                             CACHE_DIR=str(CACHE_DIR))
        self.pipe.generate(silence, task="transcribe", language=f"<|{fallback}|>")   # warm-up

        self.detector = None
        if not forced:
            self.detector = ov_genai.WhisperPipeline(str(self._require(detect_model)),
                                                     detect_device, CACHE_DIR=str(CACHE_DIR))
            self.detector.generate(silence, task="transcribe", max_new_tokens=1)

    @staticmethod
    def _require(model_id: str) -> Path:
        path = model_path(model_id)
        if not path.is_dir():
            sys.exit(f"model not found at {path} - run: "
                     f"npu-voice-to-text download --model {model_id}")
        return path

    def detect_language(self, audio) -> str | None:
        """Language of the first 30 s, from one generated token of the small model."""
        res = self.detector.generate(audio[:30 * SAMPLE_RATE].tolist(), task="transcribe",
                                     max_new_tokens=1)
        found = getattr(res, "language", None)
        return lang_code(found) if found else None

    def choose_language(self, audio, speech_seconds: float) -> str:
        if self.forced:
            return self.forced
        if speech_seconds < DETECT_MIN_SPEECH:
            print(f"  language {self.last_language} (short clip, kept from before)", flush=True)
            return self.last_language
        t0 = time.monotonic()
        detected = self.detect_language(audio)
        if detected in self.languages:
            self.last_language = detected
            note = ""
        else:
            note = f", detected {detected!r} -> fallback"
        lang = detected if detected in self.languages else self.fallback
        print(f"  language {lang} ({time.monotonic() - t0:.2f}s on {self.detect_device}{note})",
              flush=True)
        return lang

    def __call__(self, audio, speech_seconds: float) -> tuple[str, str, float]:
        """-> (text, language, seconds)"""
        t0 = time.monotonic()
        lang = self.choose_language(audio, speech_seconds)
        res = self.pipe.generate(audio.tolist(), task="transcribe", language=f"<|{lang}|>")
        text = res.texts[0].strip()
        if text.lower() in HALLUCINATIONS or (text.lower() in SHORT_HALLUCINATIONS and
                                              speech_seconds < SHORT_HALLUCINATION_SPEECH):
            print(f"  dropped {text!r} (Whisper hallucinates it in noise)", flush=True)
            text = ""
        return text, lang, time.monotonic() - t0


# =========================================================================== #
# Recorder: microphone -> 16 kHz mono float32 buffer
# =========================================================================== #
def connected_sources() -> set[str] | None:
    """Names of the PipeWire/PulseAudio input sources that can record right now.

    Monitors and analog jacks with nothing plugged in (active port "not available",
    the same check as tools/microphones.py) are left out. None if pactl is missing
    or doesn't answer within 1 s - the caller holds the daemon lock, and the key
    watcher gives up after 3 s.
    """
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "sources"], capture_output=True,
                             text=True, timeout=1, check=True).stdout
        sources = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    connected = set()
    for src in sources:
        props = src.get("properties", {})
        if src["name"].endswith(".monitor") or props.get("device.class") == "monitor":
            continue
        active = [p for p in src.get("ports", []) if p.get("name") == src.get("active_port")]
        if not (active and active[0].get("availability") == "not available"):
            connected.add(src["name"])
    return connected


class Recorder:
    """Records from the microphone while a dictation is running.

    preferred: a PipeWire source name (`pactl list sources short`, e.g.
    alsa_input....Mic1__source). It is used whenever it is connected; otherwise -
    or with None - the system default source is used, i.e. whatever is selected in
    the desktop's sound settings (GNOME, KDE, ... all set PipeWire's default).
    Avoid Bluetooth headsets: opening their mic switches them to headset mode,
    which takes ~1 s (the start of the recording is silence).
    """

    def __init__(self, preferred: str | None = None, on_limit=None):
        import numpy as np
        import sounddevice as sd

        self.np, self.sd = np, sd
        self.preferred, self.on_limit = preferred, on_limit
        self.stream, self.skip = None, 0
        self.chunks: list = []
        self.lock = threading.Lock()

    def start(self, skip: float = 0.0):
        """Start recording from the preferred mic, else the system default.
        Raises only if the system default can't be opened either.
        skip: seconds dropped from the start of the recording (the start sound)."""
        self.chunks, self.started, self.limit_hit = [], time.monotonic(), False
        self.skip = int(skip * SAMPLE_RATE)
        node = self.preferred
        if node:
            # checked on every press: the preferred mic may have been unplugged or plugged in
            connected = connected_sources()
            if connected is None:
                print("  could not list microphones (pactl), trying the preferred one anyway",
                      flush=True)
            elif node not in connected:
                print(f"  preferred microphone {node} is not connected, using the system default",
                      flush=True)
                node = None
        if node:
            try:
                self.stream = self._open(node)
                return
            except Exception as e:
                print(f"  preferred microphone {node} failed to open ({e!r}), using the system "
                      "default", flush=True)
        self.stream = self._open(None)

    def _open(self, node: str | None):
        """Open and start a stream on a PipeWire source (None: the system default)."""
        if node:
            # read by PipeWire's ALSA plugin when the stream opens. Removed right after,
            # so programs we start later (pw-play, ydotool) don't inherit it
            os.environ["PIPEWIRE_NODE"] = node
        try:
            stream = self.sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                         device="pipewire" if node else None,
                                         callback=self._callback)
        finally:
            os.environ.pop("PIPEWIRE_NODE", None)
        try:
            stream.start()
        except Exception:
            stream.close()
            raise
        return stream

    def _callback(self, indata, frames, t, status):
        with self.lock:
            self.chunks.append(indata[:, 0].copy())
        if not self.limit_hit and time.monotonic() - self.started > MAX_SECONDS:
            self.limit_hit = True
            if self.on_limit:
                threading.Thread(target=self.on_limit, daemon=True).start()

    def stop(self):
        """Stop recording and return the audio as one float32 array."""
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        with self.lock:
            chunks, self.chunks = self.chunks, []
        audio = self.np.concatenate(chunks) if chunks else self.np.zeros(0, self.np.float32)
        return audio[self.skip:]


# =========================================================================== #
# Daemon: state machine + socket server
# =========================================================================== #
class Daemon:
    """Recording and transcribing overlap: the next dictation can start while the
    previous one is still being transcribed.

    release queues the recording; one worker thread transcribes and types the
    recordings in order. Text is only typed while no recording runs - a held
    RIGHTCTRL/RIGHTMETA dictation key would otherwise turn the typed characters
    into shortcuts - so typing pauses (between whole characters) when one starts.
    Socket commands are handled one at a time in the main thread.
    """

    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.mic_free = threading.Condition(self.lock)    # notified when a recording ends
        self.recording = False
        self.limit_stopped = False    # the time limit ended the recording, not the user
        self.pending = 0                                  # recordings not yet typed
        self.jobs: queue.SimpleQueue = queue.SimpleQueue()
        self.floors = collections.deque(maxlen=5)          # noise floors of recent clips
        t0 = time.monotonic()
        languages = [lang_code(x) for x in args.languages.split(",") if x.strip()]
        self.transcriber = Transcriber(
            args.model, args.device, args.detect_model, args.detect_device, languages,
            lang_code(args.fallback_language), args.language and lang_code(args.language))
        self.recorder = Recorder(args.input_device, on_limit=self._limit_reached)
        self.keyboard = Keyboard(args.output, args.layout, args.key_delay, args.paste_keys,
                                 args.restore_clipboard, gate=self._wait_while_recording)
        threading.Thread(target=self._worker, daemon=True).start()
        lang = (f"forced {args.language}" if args.language else
                f"auto within {','.join(languages)}, fallback {args.fallback_language}")
        print(f"ready after {time.monotonic() - t0:.1f}s (language: {lang})", flush=True)

    # ---- commands --------------------------------------------------------- #
    def status(self) -> str:
        parts = (["recording"] if self.recording else []) + \
                ([f"transcribing {self.pending}"] if self.pending else [])
        return ", ".join(parts) or "idle"

    def press(self) -> str:
        with self.lock:
            if self.recording:
                return "busy (recording)"
            self.limit_stopped = False
            # the mic records the start sound, so that part of the audio is dropped
            try:
                self.recorder.start(skip=SOUND_START_SKIP if self.args.sounds else 0.0)
            except Exception as e:                  # not even the system default mic works
                print(f"could not start recording: {e!r}", file=sys.stderr, flush=True)
                return f"error: could not start recording: {e}"
            self.recording = True
        self._sound(SOUND_START)
        return "recording"

    def release(self) -> str:
        with self.lock:
            if not self.recording:
                return "not recording"
            audio = self.recorder.stop()
            self.recording = False
            self.pending += 1
            self.mic_free.notify_all()
        self._sound(SOUND_STOP)
        self.jobs.put(audio)
        return "transcribing"

    def toggle(self) -> str:
        if self.recording:
            return self.release()
        if self.limit_stopped:
            # the user's "stop" after the time limit already stopped the recording:
            # don't start a new one
            self.limit_stopped = False
            return "not recording (stopped by the time limit)"
        return self.press()

    def _limit_reached(self):
        print(f"  recording stopped after the {MAX_SECONDS} s limit", flush=True)
        with self.lock:
            self.limit_stopped = self.recording
        self.release()

    def cancel(self) -> str:
        with self.lock:
            if not self.recording:
                return "nothing to cancel"
            self.recorder.stop()
            self.recording = False
            self.mic_free.notify_all()
        return "cancelled"

    def handle(self, cmd: str) -> str:
        print(f"command: {cmd} ({self.status()})", flush=True)
        actions = {"press": self.press, "release": self.release, "toggle": self.toggle,
                   "cancel": self.cancel, "status": self.status}
        return actions[cmd]() if cmd in actions else f"error: unknown command {cmd!r}"

    # ---- transcription ---------------------------------------------------- #
    def _worker(self):
        while True:
            audio = self.jobs.get()
            try:
                self._finish(audio)
            finally:
                with self.lock:
                    self.pending -= 1

    def _wait_while_recording(self):
        with self.mic_free:
            if self.recording:
                print("  typing paused while recording", flush=True)
            self.mic_free.wait_for(lambda: not self.recording)

    def _finish(self, audio):
        try:
            secs = len(audio) / SAMPLE_RATE
            speech, peak, floor = speech_stats(audio, self.floors)
            # only clips of 1 s+ with real audio measure the noise reliably; a short tap
            # would otherwise drag the threshold down for the next recordings
            if secs >= 1.0 and floor > NOISE_FLOOR_MIN:
                self.floors.append(floor)
            levels = f"{secs:.1f}s audio, {speech:.1f}s speech, peak {peak:.3f}"
            sound = (abs(audio) > 1e-6).nonzero()[0]
            startup = (sound[0] if len(sound) else len(audio)) / SAMPLE_RATE
            if startup > 0.3:   # exact digital silence at the start = the mic was still waking up
                print(f"  warning: the microphone delivered nothing for the first {startup:.1f}s"
                      " (Bluetooth headset? set a preferred mic: ./install.sh or --input-device)",
                      flush=True)
            if speech < MIN_SPEECH_SECONDS:
                print(f"[{levels}] nothing heard", flush=True)
                return
            text, lang, dt = self.transcriber(audio, speech)
            print(f"[{levels}, {dt:.2f}s, {lang}] {text}", flush=True)
            if text:
                self.keyboard.write(text + (" " if self.args.trailing_space else ""))
        except Exception as e:                      # keep the daemon alive
            print(f"dictation failed: {e!r}", file=sys.stderr, flush=True)

    def _sound(self, path: str):
        if self.args.sounds and os.path.exists(path):
            subprocess.Popen(["pw-play", path], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)

    # ---- socket server ---------------------------------------------------- #
    def serve(self):
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(SOCKET_PATH))
        os.chmod(SOCKET_PATH, 0o600)                # only this user (and root) may connect
        srv.listen(8)
        try:
            while True:
                conn, _ = srv.accept()
                with conn:
                    try:
                        cmd = conn.recv(256).decode().strip()
                        conn.sendall(self.handle(cmd).encode() + b"\n")
                    except Exception as e:
                        print(f"request failed: {e!r}", file=sys.stderr, flush=True)
                        try:
                            conn.sendall(f"error: {e}\n".encode())
                        except OSError:
                            pass
        except KeyboardInterrupt:
            pass
        finally:
            srv.close()
            SOCKET_PATH.unlink(missing_ok=True)


def cmd_serve(args) -> int:
    if request("status", timeout=1) is not None:
        sys.exit("npu-voice-to-text is already running")
    SOCKET_PATH.unlink(missing_ok=True)             # stale socket from a crash
    # systemd stops us with SIGTERM: turn it into SystemExit so serve()'s
    # `finally` removes the socket (signal.SIG_DFL would just kill the process)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    Daemon(args).serve()
    return 0


def cmd_download(args) -> int:
    from huggingface_hub import snapshot_download

    dest = model_path(args.model)
    print(f"downloading {args.model} -> {dest}")
    snapshot_download(repo_id=args.model, local_dir=dest)
    return 0


# =========================================================================== #
# CLI
# =========================================================================== #
def read_options_file() -> list[str]:
    """`serve` options from ~/.config/npu-voice-to-text/options: one per line, # comments."""
    try:
        lines = OPTIONS_FILE.read_text().splitlines()
    except FileNotFoundError:
        return []
    return [arg for line in lines for arg in shlex.split(line, comments=True)]


def main() -> int:
    ap = argparse.ArgumentParser(prog="npu-voice-to-text", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sv = sub.add_parser("serve", help="run the daemon")
    g = sv.add_argument_group("models")
    g.add_argument("--model", default=DEFAULT_MODEL,
                   help="Hugging Face id (looked up in ~/.local/share/npu-voice-to-text/"
                        "models) or a local directory (default: %(default)s)")
    g.add_argument("--device", default="NPU", help="OpenVINO device: NPU, GPU or CPU")
    g.add_argument("--detect-model", default=DETECT_MODEL,
                   help="small model that only detects the language (default: %(default)s)")
    g.add_argument("--detect-device", default="CPU", help="device for --detect-model")
    g = sv.add_argument_group("language")
    g.add_argument("--languages", default="de,en",
                   help="languages detection may pick, comma separated (default: %(default)s)")
    g.add_argument("--fallback-language", default="en",
                   help="used when another language is detected (default: %(default)s)")
    g.add_argument("--language", default=None,
                   help="force ONE language and skip detection. Whisper then translates "
                        "speech in other languages into it")
    g = sv.add_argument_group("output")
    g.add_argument("--output", choices=("type", "paste"), default="type",
                   help="type: keystrokes for the active layout (default); paste: clipboard")
    g.add_argument("--layout", default=None,
                   help="xkb layout[+variant] to type for, e.g. de+nodeadkeys "
                        "(default: the active GNOME input source)")
    g.add_argument("--key-delay", type=int, default=4,
                   help="ms between key events (default: %(default)s)")
    g.add_argument("--paste-keys", choices=PASTE_KEYS, default="ctrl-v",
                   help="paste shortcut for --output paste and for characters the layout "
                        "can't type (default: %(default)s)")
    g.add_argument("--no-restore-clipboard", dest="restore_clipboard", action="store_false",
                   help="don't restore the clipboard after pasting")
    g.add_argument("--no-trailing-space", dest="trailing_space", action="store_false",
                   help="don't add a space after each dictation")
    g = sv.add_argument_group("misc")
    g.add_argument("--input-device", default=None,
                   help="preferred microphone: a PipeWire source name from `pactl list sources "
                        "short`. Used while connected, otherwise the system default")
    g.add_argument("--sounds", action="store_true", help="play start/stop sounds")

    dl = sub.add_parser("download", help="download a Whisper model")
    dl.add_argument("--model", default=DEFAULT_MODEL)

    for c in ("press", "release", "toggle", "cancel", "status"):
        sub.add_parser(c).add_argument("-q", "--quiet", action="store_true",
                                       help="no output, only the exit code")

    argv = sys.argv[1:]
    if argv[:1] == ["serve"]:                       # options file first, command line wins
        argv = ["serve", *read_options_file(), *argv[1:]]
    args = ap.parse_args(argv)
    if args.cmd == "serve":
        return cmd_serve(args)
    if args.cmd == "download":
        return cmd_download(args)
    return run_client(args.cmd, args.quiet)


if __name__ == "__main__":
    sys.exit(main())
