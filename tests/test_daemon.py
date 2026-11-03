"""Daemon state machine, queue ordering and the socket server, with fake components."""

import os
import stat
import threading
import time

import pytest

from npu_voice_to_text.client import request
from npu_voice_to_text.daemon import Daemon


class FakeRecorder:
    def __init__(self, clips):
        self.clips = list(clips)        # audio returned by successive stop() calls
        self.on_limit = None
        self.fail = False

    def start(self, skip=0.0):
        if self.fail:
            raise OSError("no microphone")

    def stop(self):
        return self.clips.pop(0)


class FakeTranscriber:
    """Returns texts in order. Before each one it waits for `go`, so a test can hold
    a transcription back."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.go = threading.Event()
        self.go.set()
        self.calls = 0

    def __call__(self, audio, speech):
        self.calls += 1
        self.go.wait(5)
        text = self.texts.pop(0)
        if isinstance(text, Exception):
            raise text
        return text, "de", 0.0


class FakeKeyboard:
    """Types character by character through the gate, like the real one."""

    def __init__(self):
        self.gate = None
        self.typed = ""

    def write(self, text):
        for ch in text:
            self.gate()
            self.typed += ch


def wait_until(cond, timeout=5):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.01)


@pytest.fixture
def make(audio):
    def make(texts=("hallo",), clips=None, **kw):
        word = audio.cat(audio.noise(0.5), audio.tone(1), audio.noise(0.5, seed=1))
        clips = clips if clips is not None else [word] * len(texts)
        rec, tr, kb = FakeRecorder(clips), FakeTranscriber(texts), FakeKeyboard()
        return Daemon(tr, rec, kb, **kw), rec, tr, kb
    return make


def test_dictation(make):
    d, _, _, kb = make()
    assert d.status() == "idle"
    assert d.handle("press") == "recording"
    assert d.handle("press") == "busy (recording)"
    assert d.status() == "recording"
    assert d.handle("release") == "transcribing"
    wait_until(lambda: d.status() == "idle")
    assert kb.typed == "hallo "


def test_no_trailing_space(make):
    d, _, _, kb = make(trailing_space=False)
    d.press(), d.release()
    wait_until(lambda: d.status() == "idle")
    assert kb.typed == "hallo"


def test_commands_out_of_order(make):
    d, _, _, _ = make()
    assert d.handle("release") == "not recording"
    assert d.handle("cancel") == "nothing to cancel"
    assert d.handle("bogus").startswith("error: unknown command")


def test_cancel_discards(make):
    d, _, tr, kb = make()
    d.press()
    assert d.cancel() == "cancelled"
    assert d.status() == "idle"
    assert tr.calls == 0 and kb.typed == ""


def test_silence_is_not_transcribed(make, audio):
    d, _, tr, kb = make(clips=[audio.noise(2)])
    d.press(), d.release()
    wait_until(lambda: d.status() == "idle")
    assert tr.calls == 0 and kb.typed == ""


def test_mic_failure(make):
    d, rec, _, _ = make()
    rec.fail = True
    assert d.press().startswith("error: could not start recording")
    assert d.status() == "idle"


def test_failed_transcription_keeps_the_daemon_alive(make):
    d, _, _, kb = make(texts=[RuntimeError("NPU gone"), "danach"])
    d.press(), d.release()
    wait_until(lambda: d.status() == "idle")
    d.press(), d.release()
    wait_until(lambda: d.status() == "idle")
    assert kb.typed == "danach "


def test_overlap_types_in_order_and_never_while_recording(make):
    d, _, tr, kb = make(texts=["eins", "zwei"])
    tr.go.clear()                                   # hold the first transcription back
    d.press(), d.release()
    wait_until(lambda: tr.calls == 1)
    assert d.press() == "recording"                 # the next one starts right away
    assert d.status() == "recording, transcribing 1"
    tr.go.set()                                     # first text is ready ...
    time.sleep(0.2)
    assert kb.typed == ""                           # ... but waits for the recording
    d.release()
    wait_until(lambda: d.status() == "idle")
    assert kb.typed == "eins zwei "


def test_toggle(make):
    d, _, _, kb = make()
    assert d.toggle() == "recording"
    assert d.toggle() == "transcribing"
    wait_until(lambda: d.status() == "idle")
    assert kb.typed == "hallo "


def test_toggle_after_time_limit_does_not_restart(make):
    d, _, _, _ = make()
    d.toggle()
    d._limit_reached()                              # the recorder's MAX_SECONDS callback
    assert d.status() != "recording"
    assert d.toggle() == "not recording (stopped by the time limit)"
    assert d.toggle() == "recording"                # the next press starts again


def test_components_are_wired(make):
    d, rec, _, kb = make()
    assert rec.on_limit == d._limit_reached
    assert kb.gate == d._wait_while_recording


def test_socket_server(make, tmp_path):
    d, _, _, _ = make()
    sock = tmp_path / "d.sock"
    threading.Thread(target=d.serve, args=(sock,), daemon=True).start()
    wait_until(lambda: request("status", path=sock) == "idle")
    assert stat.S_IMODE(os.stat(sock).st_mode) == 0o600
    assert request("press", path=sock) == "recording"
    assert request("nope", path=sock).startswith("error")
    assert request("status", path=tmp_path / "missing.sock") is None


def test_missing_pw_play_does_not_lose_the_dictation(make, monkeypatch):
    d, _, _, kb = make(sounds=True)
    monkeypatch.setattr("npu_voice_to_text.daemon.os.path.exists", lambda p: True)

    def no_pw_play(*a, **kw):
        raise FileNotFoundError("pw-play")
    monkeypatch.setattr("npu_voice_to_text.daemon.subprocess.Popen", no_pw_play)
    assert d.press() == "recording"
    assert d.release() == "transcribing"
    wait_until(lambda: d.status() == "idle")
    assert kb.typed == "hallo "


def test_time_limit_after_the_user_stopped(make):
    """The limit fires just after the user's own stop: nothing changes, and the next
    toggle starts a recording instead of being swallowed."""
    d, _, _, _ = make(texts=["eins", "zwei"])
    d.toggle(), d.toggle()
    d._limit_reached()
    assert d.status() != "recording"
    assert d.toggle() == "recording"


def test_stop_racing_the_time_limit(make):
    """The limit fires between the user's toggle check and its release: the release
    reports it, and the next toggle starts a recording."""
    d, _, _, _ = make(texts=["eins", "zwei"])
    d.toggle()
    d._limit_reached()
    assert d.release() == "not recording (stopped by the time limit)"
    assert d.toggle() == "recording"


def test_silent_client_does_not_block(make, tmp_path, monkeypatch):
    import socket as socketlib
    monkeypatch.setattr("npu_voice_to_text.daemon.CLIENT_TIMEOUT", 0.2)
    d, _, _, _ = make()
    sock = tmp_path / "d.sock"
    threading.Thread(target=d.serve, args=(sock,), daemon=True).start()
    wait_until(lambda: request("status", path=sock) == "idle")
    with socketlib.socket(socketlib.AF_UNIX) as silent:
        silent.connect(str(sock))                       # ... and never sends anything
        t0 = time.monotonic()
        assert request("press", path=sock) == "recording"
        assert time.monotonic() - t0 < 2


def test_daemon_running_and_empty_reply(tmp_path):
    import socket as socketlib
    from npu_voice_to_text.client import daemon_running
    sock = tmp_path / "busy.sock"
    assert not daemon_running(sock)
    srv = socketlib.socket(socketlib.AF_UNIX)
    srv.bind(str(sock)), srv.listen()
    assert daemon_running(sock)                          # listening, even if it never answers

    def hang_up():
        while True:                                      # every connection, also the probe's
            conn, _ = srv.accept()
            conn.recv(256)
            conn.close()                                 # dies without answering
    threading.Thread(target=hang_up, daemon=True).start()
    t0 = time.monotonic()
    assert request("status", path=sock) is None
    assert time.monotonic() - t0 < 1                     # the hang-up, not the timeout
    srv.close()


def test_probe_is_not_a_command(make, tmp_path, capsys):
    from npu_voice_to_text.client import daemon_running
    d, _, _, _ = make()
    sock = tmp_path / "d.sock"
    threading.Thread(target=d.serve, args=(sock,), daemon=True).start()
    wait_until(lambda: request("status", path=sock) == "idle")
    assert daemon_running(sock)
    assert request("status", path=sock) == "idle"
    out = capsys.readouterr()
    assert "command:  " not in out.out and "request failed" not in out.err
