# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Konstantin Merkel
"""Keyboard: type text into the focused window.

Wayland lets no app type into another; the RemoteDesktop portal (portal.py) is the
sanctioned way, with the user's one-time consent. It sends key *codes*, which the
compositor interprets with the active layout, so the codes for each character are
worked out from that layout with libxkbcommon (XkbKeymap). Characters the layout has
no key for are replaced (SUBSTITUTES: ä -> ae, „ -> ", ...; else the letter without
its accent). Whatever is left - emoji, which speech recognition doesn't produce
anyway - is left out.
"""

import ast
import ctypes
import subprocess
import time
import unicodedata

# Linux input event codes (layout independent), see linux/input-event-codes.h
KEY_LEFTSHIFT, KEY_RIGHTALT, KEY_SPACE = 42, 100, 57
ISO_LEVEL3_SHIFT = 0xFE03  # keysym of the AltGr function
# xkb's virtual <LVL3> and <MDSW> keys: they exist in every keymap, but no keyboard
# has them, so they can't be sent
XKB_VIRTUAL_KEYS = {84, 195}
# keys we type with: the main block (codes 1-54) plus space, 102ND (<>|), RO and YEN.
# Characters only found elsewhere - keypad, KEY_EURO on us, media keys - count as
# missing, because many apps ignore those keys.
TYPING_KEYS = frozenset(range(1, 55)) | {57, 86, 89, 124}
# dead keysym -> (combining mark it adds, character it types when followed by space)
DEAD_KEYS = {
    0xFE50: ("\u0300", "`"), 0xFE51: ("\u0301", "´"), 0xFE52: ("\u0302", "^"),
    0xFE53: ("\u0303", "~"), 0xFE57: ("\u0308", "¨"), 0xFE58: ("\u030a", "°"),
    0xFE5B: ("\u0327", "¸"),
}
# Typed instead when the layout has no key for a character (German and English
# text on a layout without umlauts, typographic punctuation on a US layout, ...).
# Other characters fall back to their letter without the accent (ñ -> n).
SUBSTITUTES = {
    "ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss", "ẞ": "SS",
    "„": '"', "“": '"', "”": '"', "«": '"', "»": '"',
    "‚": "'", "‘": "'", "’": "'", "‹": "'", "›": "'",
    "–": "-", "—": "-", "\u2011": "-", "…": "...", "€": "EUR", "•": "-", "·": ".",
    "\u00a0": " ", "\u202f": " ",          # (narrow) no-break space
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
        # compose:ralt or lv3:caps_switch move it. None: AltGr characters count as
        # missing (substituted or left out).
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

    def spell(self, ch: str) -> list | None:
        """Strokes for one character - or for its substitute - or None."""
        strokes = self.strokes_for(ch)
        if strokes is not None:
            return [strokes]
        alt = SUBSTITUTES.get(ch) or unicodedata.normalize("NFD", ch)[0]
        if alt != ch:
            parts = [self.strokes_for(c) for c in alt]
            if None not in parts:
                return parts
        return None

    def plan(self, text: str) -> tuple[list[list[tuple[int, bool]]], str]:
        """([key events (code, pressed) for each typed character], characters left
        out). Modifiers are released in reverse order after each character, so Shift
        or AltGr is never left held down between two characters."""
        chars, dropped = [], ""
        for ch in text:
            spelled = self.spell(ch)
            if spelled is None:
                dropped += ch
                continue
            for strokes in spelled:
                ev = []
                for code, mods in strokes:
                    ev += [(m, True) for m in mods] + [(code, True), (code, False)]
                    ev += [(m, False) for m in reversed(mods)]
                chars.append(ev)
        return chars, dropped


class Keyboard:
    """Types text into the focused window, as keystrokes for the active layout, sent
    through the RemoteDesktop portal.

    remote: a portal.RemoteKeyboard (replaceable in tests). Each write() opens one
    session and closes it afterwards.
    """

    def __init__(self, layout: str | None = None, key_delay_ms: int = 4, remote=None):
        """`gate` is called before every character and may block to pause typing.
        It does nothing until the daemon sets it."""
        self.layout, self.key_delay = layout, key_delay_ms
        self._remote = remote
        self.gate = lambda: None
        self._keymaps: dict[tuple, XkbKeymap] = {}

    @property
    def remote(self):
        if self._remote is None:
            from .portal import RemoteKeyboard
            self._remote = RemoteKeyboard()
        return self._remote

    def check_access(self, timeout: float = 300):
        """Open and close one session: on the first start, GNOME asks for keyboard
        access now. Raises portal.AccessDenied if the user declines."""
        self.remote.open(timeout=timeout).close()

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
        chars, dropped = self.keymap().plan(text)
        if dropped:
            print(f"  no key on this layout, left out: {dropped!r}", flush=True)
        session = None
        try:
            for events in chars:
                self.gate()
                if session is None:                 # opened after the first gate: not
                    session = self.remote.open(timeout=60)   # while a recording runs
                for code, pressed in events:
                    session.key(code, pressed)
                    if self.key_delay:
                        time.sleep(self.key_delay / 1000)
        finally:
            if session is not None:
                session.close()
