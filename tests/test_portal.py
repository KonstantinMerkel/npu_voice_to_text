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

    def __init__(self, start_code=0, devices=1, token="TOKEN-2", answer=True):
        self.unique_name = ":1.42"
        self.calls, self.closed = [], False
        self.start_code, self.devices, self.token, self.answer = start_code, devices, token, answer
        self._pending = None

    def send_and_get_reply(self, msg):
        member = msg.header.fields.get(HeaderFields.member)
        if member == "AddMatch":
            return None
        self.calls.append((member, msg.body))
        results = {"CreateSession": (0, {"session_handle": ("s", "/session/1")}),
                   "SelectDevices": (0, {}),
                   "Start": (self.start_code, {"devices": ("u", self.devices),
                                               "restore_token": ("s", self.token)})}
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
    assert [m for m, _ in portal.calls] == ["CreateSession", "SelectDevices", "Start"]
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
    tokens = [body[-1]["handle_token"][1] for _, body in portal.calls]
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
