"""Text -> keystrokes. Uses the system's libxkbcommon and xkb data; the portal is faked."""

import ctypes.util

import pytest

from npu_voice_to_text import keyboard as kb
from npu_voice_to_text.keyboard import Keyboard, XkbKeymap

pytestmark = pytest.mark.skipif(not ctypes.util.find_library("xkbcommon"),
                                reason="libxkbcommon not installed")

# evdev codes (linux/input-event-codes.h)
SHIFT, ALTGR, CAPS, SPACE = 42, 100, 58, 57
Q, E, Y_US, Z_US, V, B = 16, 18, 21, 44, 47, 48
A, MINUS, EQUAL, GRAVE, APOSTROPHE, RIGHTBRACE = 30, 12, 13, 41, 40, 27


@pytest.fixture(scope="module")
def de():
    return XkbKeymap("de")


@pytest.fixture(scope="module")
def us():
    return XkbKeymap("us")


@pytest.mark.parametrize("ch, strokes", [
    ("a", [(A, ())]),
    ("A", [(A, (SHIFT,))]),
    ("y", [(Z_US, ())]),                    # QWERTZ: y and z swapped
    ("z", [(Y_US, ())]),
    ("ä", [(APOSTROPHE, ())]),
    ("ß", [(MINUS, ())]),
    ("@", [(Q, (ALTGR,))]),
    ("€", [(E, (ALTGR,))]),
    ("~", [(RIGHTBRACE, (ALTGR,))]),
    ("„", [(V, (ALTGR,))]),
    ("“", [(B, (ALTGR,))]),
    (" ", [(SPACE, ())]),
])
def test_german_layout(de, ch, strokes):
    assert de.strokes_for(ch) == strokes


def test_dead_keys(de):
    assert de.strokes_for("é") == [(EQUAL, ()), (E, ())]           # dead acute, e
    assert de.strokes_for("^") == [(GRAVE, ()), (SPACE, ())]       # dead circumflex, space
    assert de.strokes_for("`") == [(EQUAL, (SHIFT,)), (SPACE, ())]  # dead grave, space


def test_us_layout(us):
    assert us.strokes_for("y") == [(Y_US, ())]
    assert us.strokes_for("@") == [(3, (SHIFT,))]
    assert us.altgr_key is None
    for ch in "äé€":
        assert us.strokes_for(ch) is None


@pytest.mark.parametrize("ch", ["ñ", "😀", "\n"])
def test_not_on_layout(de, ch):
    assert de.strokes_for(ch) is None


def test_altgr_follows_options():
    # compose:ralt leaves only xkb's virtual <LVL3> key: AltGr characters get pasted
    no_altgr = XkbKeymap("de", "", "compose:ralt")
    assert no_altgr.altgr_key is None
    assert no_altgr.strokes_for("@") is None
    assert no_altgr.strokes_for("ä") == [(APOSTROPHE, ())]
    # ... unless another key takes over AltGr
    caps = XkbKeymap("de", "", "compose:ralt,lv3:caps_switch")
    assert caps.altgr_key == CAPS
    assert caps.strokes_for("@") == [(Q, (CAPS,))]


def test_unknown_layout_raises():
    with pytest.raises(RuntimeError):
        XkbKeymap("no-such-layout-xyz")


def test_plan_events(de):
    chars, dropped = de.plan("Aä")
    assert chars == [[(SHIFT, True), (A, True), (A, False), (SHIFT, False)],
                     [(APOSTROPHE, True), (APOSTROPHE, False)]]
    assert dropped == ""


def test_plan_releases_modifiers_in_reverse(de):
    [events], _ = de.plan("@")
    assert events == [(ALTGR, True), (Q, True), (Q, False), (ALTGR, False)]


def typed(keymap, text):
    """What a plan types, read back through the keymap."""
    by_strokes = {tuple(s): ch for ch, s in keymap.keys.items()}
    chars, dropped = keymap.plan(text)
    out = ""
    for events in chars:
        strokes, mods, pressed = [], [], set()
        for code, down in events:
            if down:
                pressed.add(code)
                if code in (SHIFT, ALTGR, CAPS):
                    mods.append(code)
                else:
                    strokes.append((code, tuple(m for m in (ALTGR, SHIFT, CAPS) if m in mods)))
            else:
                pressed.discard(code)
                if code in mods:
                    mods.remove(code)
        out += by_strokes.get(tuple(strokes), "?")
        assert not pressed, "a key was left pressed"
    return out, dropped


@pytest.mark.parametrize("text, expected", [
    ("Mädchen, Grüße!", "Maedchen, Gruesse!"),
    ("„Zitat“ – ok…", '"Zitat" - ok...'),
    ("café señor", "cafe senor"),
    ("5 €", "5 EUR"),
])
def test_us_layout_substitutes(us, text, expected):
    assert typed(us, text) == (expected, "")


def test_german_layout_needs_no_substitutes(de):
    text = "Mädchen „Grüße“ – Straße, 5 € @ café… ÄÖÜ"
    chars, dropped = de.plan(text)
    assert dropped == "" and len(chars) == len(text)   # one entry per character: none replaced


def test_emoji_and_unknown_are_left_out(de, us):
    for keymap in (de, us):
        _, dropped = keymap.plan("ok 😀👍 ✓")
        assert dropped == "😀👍✓"
    assert typed(de, "señor")[0] == "senor"


# --------------------------------------------------------------------------- #
# Keyboard.write, with the portal replaced by a recorder
# --------------------------------------------------------------------------- #
class FakeSession:
    def __init__(self, log):
        self.log = log

    def key(self, code, pressed):
        self.log.append(("key", code, pressed))

    def close(self):
        self.log.append(("close",))


class FakeRemote:
    def __init__(self):
        self.log = []

    def open(self, timeout=300):
        self.log.append(("open",))
        return FakeSession(self.log)


@pytest.fixture
def keyboard():
    k = Keyboard(layout="de", key_delay_ms=0, remote=FakeRemote())
    k.gate = lambda: k.remote.log.append(("gate",))
    return k


def test_write_one_session_gated_per_character(keyboard):
    keyboard.write("ab")
    assert keyboard.remote.log == [
        ("gate",), ("open",), ("key", A, True), ("key", A, False),
        ("gate",), ("key", 48, True), ("key", 48, False), ("close",)]


def test_write_turns_newlines_into_spaces(keyboard):
    keyboard.write("a\nb")
    keys = [e for e in keyboard.remote.log if e[0] == "key"]
    assert keys[2:4] == [("key", SPACE, True), ("key", SPACE, False)]


def test_write_nothing_typeable_opens_no_session(keyboard):
    keyboard.write("😀")
    assert keyboard.remote.log == []


def test_session_closed_when_typing_fails(keyboard):
    def broken(code, pressed):
        raise RuntimeError("portal gone")
    keyboard.remote.open = lambda timeout=300: (
        keyboard.remote.log.append(("open",)) or
        type("S", (), {"key": staticmethod(broken),
                       "close": lambda self: keyboard.remote.log.append(("close",))})())
    with pytest.raises(RuntimeError):
        keyboard.write("ab")
    assert keyboard.remote.log[-1] == ("close",)


def test_keymap_follows_gnome_layout(monkeypatch):
    monkeypatch.setattr(kb, "gnome_layout", lambda: ("us", "", ""))
    k = Keyboard()
    assert k.keymap().name == "us"
    monkeypatch.setattr(kb, "gnome_layout", lambda: ("de", "", ""))
    assert k.keymap().name == "de"
