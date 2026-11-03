"""The root key watcher (a standalone script, loaded from its file)."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "keywatch", Path(__file__).parents[1] / "npu-voice-to-text-keywatch.py")
kw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kw)

F8 = 66
EV_SYN, EV_MSC = 0, 4


def event(ev_type, code, value):
    return kw.INPUT_EVENT.pack(0, 0, ev_type, code, value)


@pytest.mark.parametrize("name, code", [("F1", 59), ("F8", 66), ("F10", 68), ("F11", 87),
                                        ("F12", 88), ("F13", 183), ("F24", 194),
                                        ("RIGHTCTRL", 97), ("PAUSE", 119)])
def test_key_codes(name, code):
    assert kw.KEYS[name][0] == code


def test_modifiers_have_no_gnome_accelerator():
    assert kw.KEYS["RIGHTCTRL"][1] is None and kw.KEYS["RIGHTMETA"][1] is None


def test_press_autorepeat_release():
    data = b"".join([
        event(EV_MSC, 4, 0x3a), event(kw.EV_KEY, F8, 1), event(EV_SYN, 0, 0),
        event(kw.EV_KEY, F8, 2), event(kw.EV_KEY, F8, 2),           # autorepeat: ignored
        event(kw.EV_KEY, 30, 1), event(kw.EV_KEY, 30, 0),           # other key: ignored
        event(kw.EV_KEY, F8, 0),
    ])
    assert list(kw.key_commands(data, F8)) == ["press", "release"]


def test_partial_event_is_ignored():
    data = event(kw.EV_KEY, F8, 1) + event(kw.EV_KEY, F8, 0)[:10]
    assert list(kw.key_commands(data, F8)) == ["press"]
