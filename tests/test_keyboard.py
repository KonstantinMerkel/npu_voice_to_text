"""Text -> keystrokes. Uses the system's libxkbcommon and xkb data, but no ydotool."""

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


def test_plan_splits_typed_and_pasted(de):
    plan = de.plan("Aä😀ñb")
    assert [kind for kind, _ in plan] == ["keys", "paste", "keys"]
    assert plan[0][1] == [["42:1", "30:1", "30:0", "42:0"], ["40:1", "40:0"]]
    assert plan[1] == ("paste", "😀ñ")                 # neighbours share one paste
    assert plan[2][1] == [["48:1", "48:0"]]


def test_plan_releases_modifiers_in_reverse(de):
    [(kind, [events])] = de.plan("@")
    assert events == ["100:1", "16:1", "16:0", "100:0"]


# --------------------------------------------------------------------------- #
# Keyboard.write, with ydotool and the clipboard replaced by recorders
# --------------------------------------------------------------------------- #
class Recording(Keyboard):
    def __init__(self, **kw):
        super().__init__(layout="de", **kw)
        self.calls = []
        self.gate = lambda: self.calls.append(("gate",))

    def _keys(self, events, delay_ms=12):
        self.calls.append(("keys", tuple(events)))

    def paste(self, text):
        self.calls.append(("paste", text))


def test_write_gates_every_character():
    k = Recording()
    k.write("ab")
    assert k.calls == [("gate",), ("keys", ("30:1", "30:0")),
                       ("gate",), ("keys", ("48:1", "48:0"))]


def test_write_turns_newlines_into_spaces():
    k = Recording()
    k.write("a\nb")
    keys = [c[1] for c in k.calls if c[0] == "keys"]
    assert keys[1] == ("57:1", "57:0")


def test_write_pastes_what_the_layout_lacks():
    k = Recording()
    k.write("a😀")
    assert ("paste", "😀") in k.calls


def test_paste_mode_pastes_everything():
    k = Recording(mode="paste")
    k.write("hallo\twelt")
    assert k.calls == [("paste", "hallo welt")]


def test_keymap_follows_gnome_layout(monkeypatch):
    monkeypatch.setattr(kb, "gnome_layout", lambda: ("us", "", ""))
    k = Keyboard()
    assert k.keymap().name == "us"
    monkeypatch.setattr(kb, "gnome_layout", lambda: ("de", "", ""))
    assert k.keymap().name == "de"


def test_ydotool_socket_lookup(monkeypatch, tmp_path):
    monkeypatch.delenv("YDOTOOL_SOCKET", raising=False)
    monkeypatch.setattr(kb.paths, "RUNTIME_DIR", tmp_path)
    (tmp_path / ".ydotool_socket").touch()
    assert Keyboard._env()["YDOTOOL_SOCKET"] == str(tmp_path / ".ydotool_socket")
    monkeypatch.setenv("YDOTOOL_SOCKET", "/custom")
    assert Keyboard._env()["YDOTOOL_SOCKET"] == "/custom"
