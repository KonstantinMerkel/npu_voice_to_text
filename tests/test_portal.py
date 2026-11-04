"""RemoteDesktop portal client, against a fake D-Bus connection."""

import os
import stat
from types import SimpleNamespace

import pytest

jeepney = pytest.importorskip("jeepney")
from jeepney.low_level import HeaderFields  # noqa: E402

from npu_voice_to_text.portal import AccessDenied, PortalError, RemoteKeyboard  # noqa: E402


class FakePortal:
    """Plays the portal: answers each Request-style call with a Response signal."""

    def __init__(self, start_code=0, devices=1, token="TOKEN-2", answer=True, error=None):
        self.unique_name = ":1.42"
        self.calls, self.closed, self.error = [], False, error
        self.start_code, self.devices, self.token, self.answer = start_code, devices, token, answer
        self._pending = None

    def send_and_get_reply(self, msg):
        member = msg.header.fields.get(HeaderFields.member)
        if member == "AddMatch":
            return None
        self.calls.append((member, msg.body))
        if self.error and member == self.error[0]:
            from jeepney.low_level import MessageType
            return SimpleNamespace(
                header=SimpleNamespace(message_type=MessageType.error,
                                       fields={HeaderFields.error_name: self.error[1]}),
                body=(self.error[2],))
        results = {"CreateSession": (0, {"session_handle": ("s", "/session/1")}),
                   "SelectDevices": (0, {}),
                   "Start": (self.start_code, {"devices": ("u", self.devices),
                                               **({"restore_token": ("s", self.token)}
                                                  if self.token else {})})}
        self._pending = results.get(member)
        return None

    def filter(self, rule):
        portal = self

        class Ctx:
            def __enter__(self):
                return "queue"

            def __exit__(self, *a):
                portal._pending = None
        return Ctx()

    def recv_until_filtered(self, queue, timeout=None):
        if not self.answer:
            raise TimeoutError
        return SimpleNamespace(body=self._pending)

    def close(self):
        self.closed = True

    def options(self, member):
        """The a{sv} options sent with a call."""
        body = next(b for m, b in self.calls if m == member)
        return body[-1]


@pytest.fixture
def token_file(tmp_path):
    return tmp_path / "state" / "remote-desktop-token"


def test_first_start_stores_the_token(token_file):
    portal = FakePortal()
    session = RemoteKeyboard(token_file, connect=lambda: portal).open()
    assert [m for m, _ in portal.calls] == ["Register", "CreateSession", "SelectDevices", "Start"]
    opts = portal.options("SelectDevices")
    assert opts["types"] == ("u", 1) and opts["persist_mode"] == ("u", 2)
    assert "restore_token" not in opts                      # nothing to restore yet
    assert token_file.read_text() == "TOKEN-2"
    assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600
    assert session.handle == "/session/1"


def test_later_starts_restore(token_file):
    token_file.parent.mkdir(parents=True)
    token_file.write_text("TOKEN-1\n")
    portal = FakePortal(token="TOKEN-2")
    RemoteKeyboard(token_file, connect=lambda: portal).open()
    assert portal.options("SelectDevices")["restore_token"] == ("s", "TOKEN-1")
    assert token_file.read_text() == "TOKEN-2"               # single use: the new one is kept


def test_every_request_has_its_own_handle_token(token_file):
    portal = FakePortal()
    RemoteKeyboard(token_file, connect=lambda: portal).open()
    tokens = [body[-1]["handle_token"][1] for m, body in portal.calls if m != "Register"]
    assert len(set(tokens)) == 3


def test_declined(token_file):
    portal = FakePortal(start_code=1)
    with pytest.raises(AccessDenied):
        RemoteKeyboard(token_file, connect=lambda: portal).open()
    assert portal.closed and not token_file.exists()


def test_no_keyboard_granted(token_file):
    portal = FakePortal(devices=2)                           # pointer only
    with pytest.raises(AccessDenied):
        RemoteKeyboard(token_file, connect=lambda: portal).open()


def test_not_remembered(token_file):
    portal = FakePortal(token=None)                          # "Remember This Selection" off
    with pytest.raises(AccessDenied, match="not remembered"):
        RemoteKeyboard(token_file, connect=lambda: portal).open()
    assert not token_file.exists()


def test_dialog_ignored(token_file):
    portal = FakePortal(answer=False)
    with pytest.raises(PortalError, match="no answer"):
        RemoteKeyboard(token_file, connect=lambda: portal).open(timeout=1)
    assert portal.closed


def test_session_keys_and_close(token_file):
    portal = FakePortal()
    with RemoteKeyboard(token_file, connect=lambda: portal).open() as session:
        session.key(30, True)
        session.key(30, False)
    keys = [b for m, b in portal.calls if m == "NotifyKeyboardKeycode"]
    assert keys == [("/session/1", {}, 30, 1), ("/session/1", {}, 30, 0)]
    assert ("Close", ()) in portal.calls and portal.closed


def test_error_reply_is_raised_at_once(token_file):
    portal = FakePortal(error=("CreateSession", "org.freedesktop.portal.Error.NotAllowed",
                               "An app id is required"))
    with pytest.raises(PortalError, match="NotAllowed: An app id is required"):
        RemoteKeyboard(token_file, connect=lambda: portal).open(timeout=300)
    assert portal.closed


def test_refused_key_event_is_raised(token_file):
    portal = FakePortal(error=("NotifyKeyboardKeycode", "org.freedesktop.DBus.Error.Failed",
                               "session closed"))
    session = RemoteKeyboard(token_file, connect=lambda: portal).open()
    with pytest.raises(PortalError, match="session closed"):
        session.key(30, True)


# --------------------------------------------------------------------------- #
# GlobalShortcut
# --------------------------------------------------------------------------- #
from npu_voice_to_text.portal import GlobalShortcut  # noqa: E402


def signal(member, *body):
    return SimpleNamespace(header=SimpleNamespace(fields={HeaderFields.member: member}),
                           body=body)


class FakeShortcutPortal:
    """Answers CreateSession / BindShortcuts, then delivers scripted signals."""

    def __init__(self, signals, bind_code=0):
        self.unique_name = ":1.7"
        self.calls, self.closed = [], False
        self.signals, self.bind_code, self._pending = list(signals), bind_code, None

    def send_and_get_reply(self, msg):
        member = msg.header.fields.get(HeaderFields.member)
        self.calls.append(member)
        if member == "CreateSession":
            self._pending = (0, {"session_handle": ("s", "/s/1")})
        elif member == "BindShortcuts":
            self._pending = (self.bind_code, {"shortcuts": ("a(sa{sv})", [
                ("dictate", {"trigger_description": ("s", "Press F8")})])})
        return None

    def filter(self, rule, queue=None):
        class Ctx:
            def __enter__(self):
                return queue if queue is not None else "responses"

            def __exit__(self, *a):
                pass
        return Ctx()

    def recv_until_filtered(self, queue, timeout=None):
        if self._pending is not None:
            body, self._pending = self._pending, None
            return SimpleNamespace(body=body)
        if not self.signals:
            raise ConnectionError("portal gone")
        return self.signals.pop(0)

    def close(self):
        self.closed = True


def run_shortcut(portal):
    events = []
    gs = GlobalShortcut(lambda: events.append("down"), lambda: events.append("up"),
                        connect=lambda: portal)
    with pytest.raises(ConnectionError):
        gs.listen()
    return gs, events


def test_shortcut_press_release_without_autorepeat():
    portal = FakeShortcutPortal([
        signal("Activated", "/s/1", "dictate", 1, {}),
        *[signal("Activated", "/s/1", "dictate", t, {}) for t in range(2, 30)],  # autorepeat
        signal("Deactivated", "/s/1", "dictate", 30, {}),
        signal("Activated", "/s/1", "dictate", 40, {}),
        signal("Deactivated", "/s/1", "dictate", 41, {}),
        signal("Deactivated", "/s/1", "dictate", 42, {}),            # stray: ignored
    ])
    gs, events = run_shortcut(portal)
    assert events == ["down", "up", "down", "up"]
    assert gs.bound == "Press F8"
    assert portal.calls[:3] == ["Register", "AddMatch", "CreateSession"]   # app ID first
    assert portal.closed


def test_shortcut_ignores_other_sessions_and_shortcuts():
    portal = FakeShortcutPortal([
        signal("Activated", "/s/OTHER", "dictate", 1, {}),
        signal("Activated", "/s/1", "something-else", 2, {}),
        signal("ShortcutsChanged", "/s/1", []),
    ])
    _, events = run_shortcut(portal)
    assert events == []


def test_closed_session_is_rebound():
    portal = FakeShortcutPortal([signal("Closed")])
    gs = GlobalShortcut(lambda: None, lambda: None, connect=lambda: portal)
    with pytest.raises(PortalError, match="closed"):
        gs.listen()


def test_declined_key_stops_without_asking_again():
    portals = []

    def connect():
        portals.append(FakeShortcutPortal([], bind_code=1))
        return portals[-1]
    GlobalShortcut(lambda: None, lambda: None, connect=connect).run(retry_seconds=0)
    assert len(portals) == 1                       # no second dialog


def test_lost_portal_reconnects():
    portals = []

    def connect():
        if len(portals) == 2:
            raise SystemExit                        # end the test after one retry
        portals.append(FakeShortcutPortal([]))
        return portals[-1]
    with pytest.raises(SystemExit):
        GlobalShortcut(lambda: None, lambda: None, connect=connect).run(retry_seconds=0)
    assert len(portals) == 2


def test_remote_keyboard_registers_the_app_first(token_file):
    portal = FakePortal()
    RemoteKeyboard(token_file, connect=lambda: portal).open()
    assert portal.calls[0][0] == "Register"


def test_portal_restart_rebinds():
    portal = FakeShortcutPortal([signal("NameOwnerChanged", "org.freedesktop.portal.Desktop",
                                        ":1.5", ":1.9")])
    gs = GlobalShortcut(lambda: None, lambda: None, connect=lambda: portal)
    with pytest.raises(PortalError, match="restarted"):
        gs.listen()


def test_closed_dialog_counts_as_declined():
    portals = []

    def connect():
        portals.append(FakeShortcutPortal([], bind_code=2))
        return portals[-1]
    gs = GlobalShortcut(lambda: None, lambda: None, connect=connect)
    gs.run(retry_seconds=0)
    assert len(portals) == 1 and gs.denied


def test_lost_release_is_delivered(monkeypatch):
    import npu_voice_to_text.portal as portal_mod
    monkeypatch.setattr(portal_mod, "REPEAT_GAP", -1)      # every repeat is "after a pause"
    portal = FakeShortcutPortal([
        signal("Activated", "/s/1", "dictate", 1, {}),
        signal("Activated", "/s/1", "dictate", 900, {}),     # its Deactivated never came
        signal("Deactivated", "/s/1", "dictate", 901, {}),
    ])
    _, events = run_shortcut(portal)
    assert events == ["down", "up", "down", "up"]
