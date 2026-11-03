# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Konstantin Merkel
"""Keyboard input through the xdg-desktop-portal RemoteDesktop portal.

Wayland lets no app type into another. The RemoteDesktop portal is the sanctioned
way around that: the user allows it once in a GNOME dialog, and the app may then
send key events to the focused window. Only the keyboard is requested - no mouse,
no screen sharing.

The permission is stored by the portal (permission store table `remote-desktop`)
under a restore token. The token is kept in TOKEN_FILE (mode 0600); with it, later
sessions start without asking. `uninstall.sh` deletes the permission again.

Each dictation opens its own short session (~20 ms with the token): a session -
and anything GNOME shows for it - only exists while text is typed.
"""

import os
import secrets
from pathlib import Path

from . import paths

PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
REMOTE_DESKTOP = "org.freedesktop.portal.RemoteDesktop"
KEYBOARD = 1                 # device type
PERSIST_UNTIL_REVOKED = 2    # persist_mode
TOKEN_FILE = paths.STATE_DIR / "remote-desktop-token"
PERMISSION_TABLE = "remote-desktop"   # permission store table, used by uninstall.sh


class PortalError(RuntimeError):
    pass


class AccessDenied(PortalError):
    """The user declined the dialog, or the permission was revoked and declined."""


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
            if "restore_token" in res:
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
        self.conn.send_and_get_reply(self._call(
            self.portal, "NotifyKeyboardKeycode", "oa{sv}iu",
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


def _request(conn, portal, method: str, signature: str, args: list, options_index: int,
             timeout: float) -> dict:
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
    conn.send_and_get_reply(message_bus.AddMatch(rule))
    with conn.filter(rule) as responses:
        conn.send_and_get_reply(new_method_call(portal, method, signature, tuple(args)))
        try:
            code, results = conn.recv_until_filtered(responses, timeout=timeout).body
        except TimeoutError:
            raise PortalError(f"{method}: no answer within {timeout:.0f} s") from None
    if code == 1:
        raise AccessDenied(f"{method}: keyboard access was denied")
    if code != 0:
        raise PortalError(f"{method}: failed (response {code})")
    return results
