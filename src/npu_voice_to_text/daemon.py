# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Konstantin Merkel
"""Daemon: recording state machine, transcription queue and the socket server."""

import collections
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import paths
from .audio import (MAX_SECONDS, MIN_SPEECH_SECONDS, NOISE_FLOOR_MIN, SAMPLE_RATE, Recorder,
                    silent_start, speech_stats)
from .client import daemon_running
from .keyboard import Keyboard
from .portal import GlobalShortcut
from .transcriber import Transcriber, lang_code

SOUND_START = "/usr/share/sounds/freedesktop/stereo/device-added.oga"
SOUND_STOP = "/usr/share/sounds/freedesktop/stereo/device-removed.oga"
SOUND_START_SKIP = 0.4     # seconds of audio dropped after the start sound (0.22 s + latency)
SILENT_START_WARNING = 0.3  # seconds of digital silence at the start worth a warning
CLIENT_TIMEOUT = 2.0        # seconds a client may take to send its command
TAP_SECONDS = 0.4           # a shorter key press is a tap: keep recording until the next press
EXIT_NO_KEYBOARD = 3        # keyboard access declined: the unit doesn't restart on it


class Daemon:
    """Recording and transcribing overlap: the next dictation can start while the
    previous one is still being transcribed.

    release queues the recording; one worker thread transcribes and types the
    recordings in order. Text is only typed while no recording runs - never into
    the window while you are dictating, with the dictation key held down - so
    typing pauses (between whole characters) when one starts.
    Socket commands (main thread) and the dictation key (portal thread) are
    handled one at a time, under `commands`.

    transcriber: callable (audio, speech_seconds) -> (text, language, seconds)
    recorder:    start(skip) / stop() -> audio; its `on_limit` is set here
    keyboard:    write(text); its `gate` is set here
    """

    def __init__(self, transcriber, recorder, keyboard, *, sounds: bool = False,
                 trailing_space: bool = True):
        self.transcriber, self.recorder, self.keyboard = transcriber, recorder, keyboard
        self.sounds, self.trailing_space = sounds, trailing_space
        self._key_down_at = 0.0
        self._ignore_key_up = False   # this press stopped a recording; its release is no stop
        self.commands = threading.Lock()  # one socket command or key event at a time
        self.shortcut = None              # the GlobalShortcut, for status()
        self.lock = threading.Lock()
        self.mic_free = threading.Condition(self.lock)    # notified when a recording ends
        self.recording = False
        self.limit_stopped = False    # the time limit ended the recording, not the user
        self.pending = 0                                  # recordings not yet typed
        self.jobs: queue.SimpleQueue = queue.SimpleQueue()
        self.floors = collections.deque(maxlen=5)          # noise floors of recent clips
        recorder.on_limit = self._limit_reached
        keyboard.gate = self._wait_while_recording
        threading.Thread(target=self._worker, daemon=True).start()

    @classmethod
    def from_args(cls, args) -> "Daemon":
        """The real daemon: models on the NPU/CPU, the microphone, the portal."""
        t0 = time.monotonic()
        keyboard = Keyboard(args.layout, args.key_delay)
        # first, so the GNOME dialogs show up right away on the first start (install),
        # one after the other: keyboard access, then the dictation key
        print("checking keyboard access (the first start asks for it in a GNOME dialog)",
              flush=True)
        try:
            keyboard.check_access()
        except Exception as e:                      # declined, ignored, or no portal
            print(f"no keyboard access: {e}\n  re-run ./install.sh to be asked again",
                  file=sys.stderr, flush=True)
            sys.exit(EXIT_NO_KEYBOARD)              # systemd doesn't restart on this one
        # bound in the background while the models load: the first time, GNOME asks
        # to confirm the key. Presses before the daemon is ready are ignored.
        shortcut = GlobalShortcut(lambda: None, lambda: None)
        shortcut.start()
        languages = [lang_code(x) for x in args.languages.split(",") if x.strip()]
        transcriber = Transcriber.load(
            args.model, args.device, args.detect_model, args.detect_device, languages,
            lang_code(args.fallback_language), args.language and lang_code(args.language))
        recorder = Recorder(args.input_device)
        daemon = cls(transcriber, recorder, keyboard, sounds=args.sounds,
                     trailing_space=args.trailing_space)
        shortcut.on_press, shortcut.on_release = daemon.key_down, daemon.key_up
        daemon.shortcut = shortcut
        lang = (f"forced {args.language}" if args.language else
                f"auto within {','.join(languages)}, fallback {args.fallback_language}")
        print(f"ready after {time.monotonic() - t0:.1f}s (language: {lang})", flush=True)
        return daemon

    # ---- commands --------------------------------------------------------- #
    def status(self) -> str:
        parts = (["recording"] if self.recording else []) + \
                ([f"transcribing {self.pending}"] if self.pending else []) + \
                (["no dictation key"] if self.shortcut and self.shortcut.denied else [])
        return ", ".join(parts) or "idle"

    def press(self) -> str:
        with self.lock:
            if self.recording:
                return "busy (recording)"
            self.limit_stopped = False
            # the mic records the start sound, so that part of the audio is dropped
            try:
                self.recorder.start(skip=SOUND_START_SKIP if self.sounds else 0.0)
            except Exception as e:                  # not even the system default mic works
                print(f"could not start recording: {e!r}", file=sys.stderr, flush=True)
                return f"error: could not start recording: {e}"
            self.recording = True
        self._sound(SOUND_START)
        return "recording"

    def release(self) -> str:
        with self.lock:
            if not self.recording:
                # a stop that arrives after the time limit already stopped the
                # recording has done its job: the next toggle starts a new one
                stopped_by_limit, self.limit_stopped = self.limit_stopped, False
                return ("not recording (stopped by the time limit)" if stopped_by_limit
                        else "not recording")
            self._stop_and_queue()
        self._sound(SOUND_STOP)
        return "transcribing"

    def _stop_and_queue(self):
        """Stop the recording and queue it for transcription. Called with the lock held."""
        self.jobs.put(self.recorder.stop())
        self.recording = False
        self.pending += 1
        self.mic_free.notify_all()

    def toggle(self) -> str:
        if self.recording:
            return self.release()
        if self.limit_stopped:
            # the user's "stop" after the time limit already stopped the recording:
            # don't start a new one
            self.limit_stopped = False
            return "not recording (stopped by the time limit)"
        return self.press()

    def _limit_reached(self):
        # checked and stopped under one lock: a release + press in between must not
        # get its new recording stopped, nor leave limit_stopped set
        with self.lock:
            if not self.recording:
                return
            print(f"  recording stopped after the {MAX_SECONDS} s limit", flush=True)
            self._stop_and_queue()
            self.limit_stopped = True
        self._sound(SOUND_STOP)

    def cancel(self) -> str:
        with self.lock:
            if not self.recording:
                return "nothing to cancel"
            self.recorder.stop()
            self.recording = False
            self.mic_free.notify_all()
        return "cancelled"

    # ---- the dictation key (GlobalShortcuts portal) ------------------------ #
    # Hold the key: recording while held, stopped on release. Tap it (shorter than
    # TAP_SECONDS): recording continues, and the next press stops it.
    # A press that stops a recording (after a tap, or after the time limit stopped
    # it) has done its job on the way down: its release is ignored.
    def key_down(self):
        with self.commands:
            print(f"key down ({self.status()})", flush=True)
            self._key_down_at = time.monotonic()
            self._ignore_key_up = self.recording or self.limit_stopped
            self.toggle()

    def key_up(self):
        with self.commands:
            held = time.monotonic() - self._key_down_at
            print(f"key up after {held:.2f}s ({self.status()})", flush=True)
            if self._ignore_key_up:
                return
            if held < TAP_SECONDS:
                if self.recording:
                    print("  short press: recording until the next press", flush=True)
                return
            self.release()      # also clears limit_stopped if the limit stopped it while held

    def handle(self, cmd: str) -> str:
        actions = {"press": self.press, "release": self.release, "toggle": self.toggle,
                   "cancel": self.cancel, "status": self.status}
        with self.commands:
            print(f"command: {cmd} ({self.status()})", flush=True)
            if cmd in actions and cmd != "status":
                # a command while the key is down takes over: the key's release
                # must not stop what the command started
                self._ignore_key_up = True
            return actions[cmd]() if cmd in actions else f"error: unknown command {cmd!r}"

    # ---- transcription ---------------------------------------------------- #
    def _worker(self):
        while True:
            audio = self.jobs.get()
            try:
                self._finish(audio)
            finally:
                with self.lock:
                    self.pending -= 1

    def _wait_while_recording(self):
        with self.mic_free:
            if self.recording:
                print("  typing paused while recording", flush=True)
            self.mic_free.wait_for(lambda: not self.recording)

    def _finish(self, audio):
        try:
            secs = len(audio) / SAMPLE_RATE
            speech, peak, floor = speech_stats(audio, self.floors)
            # only clips of 1 s+ with real audio measure the noise reliably; a short tap
            # would otherwise drag the threshold down for the next recordings
            if secs >= 1.0 and floor > NOISE_FLOOR_MIN:
                self.floors.append(floor)
            levels = f"{secs:.1f}s audio, {speech:.1f}s speech, peak {peak:.3f}"
            startup = silent_start(audio)
            if startup > SILENT_START_WARNING:
                print(f"  warning: the microphone delivered nothing for the first {startup:.1f}s"
                      " (Bluetooth headset? set a preferred mic: ./install.sh or --input-device)",
                      flush=True)
            if speech < MIN_SPEECH_SECONDS:
                print(f"[{levels}] nothing heard", flush=True)
                return
            text, lang, dt = self.transcriber(audio, speech)
            print(f"[{levels}, {dt:.2f}s, {lang}] {text}", flush=True)
            if text:
                self.keyboard.write(text + (" " if self.trailing_space else ""))
        except Exception as e:                      # keep the daemon alive
            print(f"dictation failed: {e!r}", file=sys.stderr, flush=True)

    def _sound(self, path: str):
        if self.sounds and os.path.exists(path):
            try:
                subprocess.Popen(["pw-play", path], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except OSError as e:                    # pw-play missing: dictate without sound
                print(f"  could not play a sound: {e!r}", flush=True)

    # ---- socket server ---------------------------------------------------- #
    def serve(self, path: Path | None = None):
        """Answer socket commands until interrupted (one line in, one line out)."""
        path = path or paths.SOCKET_PATH
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)                 # created 0600: only this user (and root)
        try:
            srv.bind(str(path))
        finally:
            os.umask(old_umask)
        srv.listen(8)
        try:
            while True:
                conn, _ = srv.accept()
                with conn:
                    try:
                        # commands are handled one at a time: a client that connects
                        # and sends nothing must not block the dictation key
                        conn.settimeout(CLIENT_TIMEOUT)
                        cmd = conn.recv(256).decode().strip()
                        if not cmd:                 # a probe (daemon_running) or hang-up
                            continue
                        conn.sendall(self.handle(cmd).encode() + b"\n")
                    except Exception as e:
                        print(f"request failed: {e!r}", file=sys.stderr, flush=True)
                        try:
                            conn.sendall(f"error: {e}\n".encode())
                        except OSError:
                            pass
        except KeyboardInterrupt:
            pass
        finally:
            srv.close()
            path.unlink(missing_ok=True)


def run(args) -> int:
    """`npu-voice-to-text serve`"""
    if daemon_running():           # also when it is too busy to answer right now
        sys.exit("npu-voice-to-text is already running")
    paths.SOCKET_PATH.unlink(missing_ok=True)       # stale socket from a crash
    # systemd stops us with SIGTERM: turn it into SystemExit so serve()'s
    # `finally` removes the socket (signal.SIG_DFL would just kill the process)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    Daemon.from_args(args).serve()
    return 0
