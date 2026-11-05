# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Konstantin Merkel
"""The two desktop portals npu-voice-to-text uses (xdg-desktop-portal, over D-Bus).

Wayland lets no app read global key presses or type into another app. Portals are
the sanctioned way around both, each with the user's one-time consent in a GNOME
dialog:

- GlobalShortcuts (GlobalShortcut): the dictation key. The user confirms (or picks)
  the key once; GNOME then reports its press and release.
- RemoteDesktop (RemoteKeyboard): typing. The user allows keyboard access once;
  the app may then send key events to the focused window. Only the keyboard is
  requested - no mouse, no screen sharing.

Portals identify a host (non-Flatpak) app by the app ID it registers, which needs
an installed desktop file of that name (install.sh puts it into
~/.local/share/applications). GNOME's dialogs show its name.

The permission is stored by the portal (permission store table `remote-desktop`)
under a restore token. The token is kept in TOKEN_FILE (mode 0600); with it, later
sessions start without asking. `uninstall.sh` deletes the permission again.

Each dictation opens its own short session (~20 ms with the token): a session -
and anything GNOME shows for it - only exists while text is typed.
"""

import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path

from . import paths

PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
REMOTE_DESKTOP = "org.freedesktop.portal.RemoteDesktop"
GLOBAL_SHORTCUTS = "org.freedesktop.portal.GlobalShortcuts"
APP_ID = "io.github.KonstantinMerkel.NpuVoiceToText"      # = the desktop file's name
SHORTCUT_ID = "dictate"
DEFAULT_TRIGGER = "F8"       # suggested in GNOME's dialog; the user may pick another key
KEYBOARD = 1                 # device type
PERSIST_UNTIL_REVOKED = 2    # persist_mode
TOKEN_FILE = paths.STATE_DIR / "keyboard-access.token"
PERMISSION_TABLE = "remote-desktop"   # permission store table, used by uninstall.sh
REPEAT_GAP = 3.0             # s without autorepeat after which `Activated` is a new press


class PortalError(RuntimeError):
    pass


class AccessDenied(PortalError):
    """The user declined the dialog, or the permission was revoked and declined."""


class NoAnswer(PortalError):
    """No response within the timeout: normally a dialog left open."""


class RemoteKeyboard:
    """Opens RemoteDesktop sessions with keyboard access.

    connect: returns a jeepney blocking connection (replaceable in tests).
    """

    def __init__(self, token_file: Path | None = None, connect=None):
        self.token_file = token_file or TOKEN_FILE
        if connect is None:
            from jeepney.io.blocking import open_dbus_connection
            connect = open_dbus_connection
        self._connect = connect

    def open(self, timeout: float = 300) -> "Session":
        """Start a session. Without a stored permission, GNOME asks the user first;
        `timeout` is how long to wait for that answer."""
        from jeepney import DBusAddress, new_method_call

        conn = self._connect()
        try:
            register_app(conn)
            portal = DBusAddress(PORTAL_PATH, bus_name=PORTAL, interface=REMOTE_DESKTOP)
            res = _request(conn, portal, "CreateSession", "a{sv}", [
                {"session_handle_token": ("s", "nvt" + secrets.token_hex(8))}], 0, timeout)
            session = res["session_handle"][1]
            options = {"types": ("u", KEYBOARD), "persist_mode": ("u", PERSIST_UNTIL_REVOKED)}
            token = self._read_token()
            if token:
                options["restore_token"] = ("s", token)
            _request(conn, portal, "SelectDevices", "oa{sv}", [session, options], 1, timeout)
            res = _request(conn, portal, "Start", "osa{sv}", [session, "", {}], 2, timeout)
            if not res.get("devices", ("u", 0))[1] & KEYBOARD:
                raise AccessDenied("the portal granted no keyboard")
            if "restore_token" not in res:
                # "Remember This Selection" was unticked: every dictation would ask again
                raise AccessDenied("not remembered (\"Remember This Selection\" was off)")
            self._write_token(res["restore_token"][1])
            return Session(conn, portal, session, new_method_call)
        except BaseException:
            conn.close()
            raise

    def _read_token(self) -> str | None:
        try:
            return self.token_file.read_text().strip() or None
        except FileNotFoundError:
            return None

    def _write_token(self, token: str):
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token)
        tmp.replace(self.token_file)


class Session:
    """An open RemoteDesktop session: send key events, then close it."""

    def __init__(self, conn, portal, handle: str, new_method_call):
        self.conn, self.portal, self.handle = conn, portal, handle
        self._call = new_method_call

    def key(self, code: int, pressed: bool):
        """Press or release a key, by evdev code (interpreted with the active layout)."""
        _call(self.conn, self._call(self.portal, "NotifyKeyboardKeycode", "oa{sv}iu",
                                    (self.handle, {}, code, int(pressed))))

    def close(self):
        from jeepney import DBusAddress
        try:
            self.conn.send_and_get_reply(self._call(
                DBusAddress(self.handle, bus_name=PORTAL,
                            interface="org.freedesktop.portal.Session"), "Close", "", ()))
        except Exception:                           # already closed by the portal
            pass
        finally:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class GlobalShortcut:
    """The dictation key, bound through the GlobalShortcuts portal.

    The first time, GNOME shows a dialog to confirm the key (DEFAULT_TRIGGER is
    suggested); GNOME stores the binding (dconf, /org/gnome/settings-daemon/
    global-shortcuts/APP_ID/). After that, binding is silent.

    on_press / on_release are called when the key goes down / up. While a key is
    held, GNOME repeats `Activated` (keyboard autorepeat, every ~30 ms); only the
    first one counts. run() listens forever, in its own thread via start(), and
    reconnects when the portal goes away or restarts. If the user declines (or
    closes) the dialog, it logs that, sets `denied` and stops.
    """

    def __init__(self, on_press, on_release, trigger: str = DEFAULT_TRIGGER, connect=None):
        self.on_press, self.on_release, self.trigger = on_press, on_release, trigger
        if connect is None:
            from jeepney.io.blocking import open_dbus_connection
            connect = open_dbus_connection
        self._connect = connect
        self.bound: str | None = None       # GNOME's description of the key, once bound
        self.denied = False                 # the user declined the key dialog

    def start(self):
        threading.Thread(target=self.run, name="shortcut", daemon=True).start()

    def run(self, retry_seconds: float = 5):
        while True:
            try:
                self.listen()
            except AccessDenied as e:
                print(f"no dictation key: {e} - re-run ./install.sh to be asked again",
                      flush=True)
                self.denied = True
                return
            except Exception as e:                  # portal restarted, session closed, ...
                print(f"dictation key: {e!r} - binding again in {retry_seconds:.0f} s",
                      flush=True)
            self.bound = None
            time.sleep(retry_seconds)

    def listen(self):
        """Bind the key and dispatch its signals until the session or connection ends."""
        from jeepney import DBusAddress, MatchRule, message_bus
        from jeepney.low_level import HeaderFields

        conn = self._connect()
        try:
            register_app(conn)
            portal = DBusAddress(PORTAL_PATH, bus_name=PORTAL, interface=GLOBAL_SHORTCUTS)
            res = _request(conn, portal, "CreateSession", "a{sv}", [
                {"session_handle_token": ("s", "nvt" + secrets.token_hex(8))}], 0, 60)
            session = res["session_handle"][1]
            signals: deque = deque(maxlen=10000)
            # a restarted portal forgets the session without a word: watch its name
            restarted = MatchRule(type="signal", sender="org.freedesktop.DBus",
                                  interface="org.freedesktop.DBus", member="NameOwnerChanged",
                                  path="/org/freedesktop/DBus")
            restarted.add_arg_condition(0, PORTAL)
            rules = [MatchRule(type="signal", interface=GLOBAL_SHORTCUTS),
                     MatchRule(type="signal", interface="org.freedesktop.portal.Session",
                               member="Closed", path=session),
                     restarted]
            for rule in rules:
                _call(conn, message_bus.AddMatch(rule))
            with conn.filter(rules[0], queue=signals), conn.filter(rules[1], queue=signals), \
                    conn.filter(rules[2], queue=signals):
                # no timeout: the first time, this waits for the user's answer
                res = _request(conn, portal, "BindShortcuts", "oa(sa{sv})sa{sv}", [
                    session, [(SHORTCUT_ID, {
                        "description": ("s", "Dictate"),
                        "preferred_trigger": ("s", self.trigger)})], "", {}], 3, None)
                self.bound = _trigger_of(res) or "?"
                print(f"dictation key: {self.bound}", flush=True)
                held, last = False, 0.0
                while True:
                    msg = conn.recv_until_filtered(signals)
                    member = msg.header.fields.get(HeaderFields.member)
                    if member == "NameOwnerChanged":
                        raise PortalError("the portal restarted")
                    if member == "Closed":
                        raise PortalError("the portal closed the shortcut session")
                    if msg.body[:2] != (session, SHORTCUT_ID):
                        continue
                    if member == "Activated":
                        # autorepeat sends `Activated` every ~30 ms while the key is held;
                        # after a longer pause it is a new press, whose release (lock
                        # screen, a grab) never came: deliver that first
                        if held and time.monotonic() - last > REPEAT_GAP:
                            print("dictation key: a release went missing", flush=True)
                            held = False
                            self.on_release()
                        if not held:
                            held = True
                            self.on_press()
                        last = time.monotonic()     # after the callbacks: they may take a while
                    elif member == "Deactivated" and held:
                        held = False
                        self.on_release()
        finally:
            conn.close()


def _trigger_of(results: dict) -> str | None:
    """GNOME's description of the bound key ("Press F8") from BindShortcuts' results."""
    for sid, props in results.get("shortcuts", ("", []))[1]:
        if sid == SHORTCUT_ID and "trigger_description" in props:
            return props["trigger_description"][1]
    return None


def register_app(conn):
    """Tell the portal who we are (host apps have no app ID otherwise). Must be the
    connection's first portal call. GlobalShortcuts refuses apps without an ID."""
    from jeepney import DBusAddress, new_method_call

    registry = DBusAddress(PORTAL_PATH, bus_name=PORTAL,
                           interface="org.freedesktop.host.portal.Registry")
    _call(conn, new_method_call(registry, "Register", "sa{sv}", (APP_ID, {})))


def _call(conn, msg):
    """Send a method call and return the reply's body. jeepney returns D-Bus error
    replies like normal ones; they are raised here as PortalError."""
    from jeepney import MessageType
    from jeepney.low_level import HeaderFields

    reply = conn.send_and_get_reply(msg)
    if reply is not None and reply.header.message_type == MessageType.error:
        name = reply.header.fields.get(HeaderFields.error_name, "error")
        detail = reply.body[0] if reply.body else ""
        member = msg.header.fields.get(HeaderFields.member, "?")
        raise PortalError(f"{member}: {name}: {detail}")
    return reply.body if reply is not None else ()


def _request(conn, portal, method: str, signature: str, args: list, options_index: int,
             timeout: float | None) -> dict:
    """Call a portal method that answers through a Request object's Response signal
    (https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Request.html).
    Returns the results; raises AccessDenied / PortalError on a non-zero response."""
    from jeepney import MatchRule, message_bus, new_method_call

    token = "nvt" + secrets.token_hex(8)
    sender = conn.unique_name[1:].replace(".", "_")
    path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
    rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request",
                     member="Response", path=path)
    args[options_index]["handle_token"] = ("s", token)
    # subscribe before calling, or a fast answer is missed
    _call(conn, message_bus.AddMatch(rule))
    with conn.filter(rule) as responses:
        _call(conn, new_method_call(portal, method, signature, tuple(args)))
        try:
            code, results = conn.recv_until_filtered(responses, timeout=timeout).body
        except TimeoutError:
            raise NoAnswer(f"{method}: no answer within {timeout} s") from None
    if code in (1, 2):           # 2: the dialog was closed (Escape, window close button)
        raise AccessDenied(f"{method}: {'declined' if code == 1 else 'dialog closed'}")
    if code != 0:
        raise PortalError(f"{method}: failed (response {code})")
    return results
