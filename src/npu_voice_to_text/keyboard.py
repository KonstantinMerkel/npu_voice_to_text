# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Konstantin Merkel
"""Keyboard: type text into the focused window.

Wayland lets no app type into another, so the text goes through ydotool (kernel
uinput). ydotool only sends key codes, so the codes for each character are worked
out from the active xkb layout with libxkbcommon (XkbKeymap). Characters the layout
can't produce are pasted through the clipboard.
"""

import ast
import ctypes
import os
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

from . import paths

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
                 paste_keys: str = "ctrl-v", restore_clipboard: bool = True):
        """`gate` is called before every ydotool call and may block to pause typing.
        It does nothing until the daemon sets it."""
        self.mode, self.layout, self.key_delay = mode, layout, key_delay_ms
        self.paste_keys, self.restore_clipboard = paste_keys, restore_clipboard
        self.gate = lambda: None
        self._keymaps: dict[tuple, XkbKeymap] = {}

    @staticmethod
    def _env() -> dict:
        """Environment for ydotool. Looked up on every call: ydotoold may be started
        after the daemon, and its socket path depends on how it runs."""
        env = dict(os.environ)
        if "YDOTOOL_SOCKET" not in env:
            for cand in (paths.RUNTIME_DIR / ".ydotool_socket", Path("/tmp/.ydotool_socket")):
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
