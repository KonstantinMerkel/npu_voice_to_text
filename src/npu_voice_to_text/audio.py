# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Konstantin Merkel
"""Audio: microphone -> 16 kHz mono float32 buffer, and a simple speech detector."""

import json
import os
import subprocess
import threading
import time

import numpy as np

SAMPLE_RATE = 16000
MAX_SECONDS = 120          # safety stop for a stuck key / forgotten recording
FRAME_SECONDS = 0.03       # speech detection works on 30 ms frames
SPEECH_RMS_MIN = 0.002     # a frame is speech if louder than this AND 3x the noise floor
MIN_SPEECH_SECONDS = 0.15  # less speech than this: accidental tap / key click / silence
NOISE_FLOOR_MIN = 1e-5     # quieter "noise floors" are digital silence, not a measurement
DIGITAL_SILENCE = 1e-6     # samples this quiet are exact zeros from a mic still waking up


# =========================================================================== #
# Speech detection
# =========================================================================== #
def speech_stats(audio, floors=()) -> tuple[float, float, float]:
    """(seconds of speech, loudest frame RMS, noise floor of this clip) - a simple
    energy-based voice detector.

    A 30 ms frame counts as speech when it is louder than SPEECH_RMS_MIN and 3x the
    noise floor, so it adapts to the mic's gain and background noise. The noise
    floor is the 20th percentile of the clip's frames (exact digital silence left
    out), or the lowest floor of the last few recordings (`floors`) if that is
    lower: when the key is held tightly around a single word, the whole clip is
    speech and its own 20th percentile is far above the real noise. Unlike the RMS
    of the whole clip, a short or quiet word in a mostly silent recording still
    counts.
    """
    frame = int(FRAME_SECONDS * SAMPLE_RATE)
    n = len(audio) // frame
    if n == 0:
        return 0.0, 0.0, 0.0
    rms = np.sqrt(np.mean(audio[:n * frame].reshape(n, frame) ** 2, axis=1))
    # digital silence (a Bluetooth mic waking up) says nothing about the room's noise
    real = rms[rms > NOISE_FLOOR_MIN]
    floor = float(np.percentile(real, 20)) if len(real) else 0.0
    threshold = max(SPEECH_RMS_MIN, 3 * min([floor, *floors]))
    return float((rms > threshold).sum() * FRAME_SECONDS), float(rms.max()), floor


def silent_start(audio) -> float:
    """Seconds of exact digital silence at the start: the mic was still waking up
    (typically a Bluetooth headset switching to headset mode)."""
    sound = (np.abs(audio) > DIGITAL_SILENCE).nonzero()[0]
    return (sound[0] if len(sound) else len(audio)) / SAMPLE_RATE


# =========================================================================== #
# Recorder
# =========================================================================== #
def connected_sources() -> set[str] | None:
    """Names of the PipeWire/PulseAudio input sources that can record right now.

    Monitors and analog jacks with nothing plugged in (active port "not available",
    see microphones()) are left out. None if pactl is missing
    or doesn't answer within 1 s - the caller holds the daemon lock, and the key
    watcher gives up after 3 s.
    """
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "sources"], capture_output=True,
                             text=True, timeout=1, check=True).stdout
        sources = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return parse_connected_sources(sources)


def microphones(sources: list[dict]) -> list[tuple[dict, bool]]:
    """The microphones in `pactl -f json list sources` output, each with whether it
    can record: monitors are left out, and an analog jack whose active port is "not
    available" has nothing plugged in. Also used by tools/microphones.py (install.sh),
    so the installer offers the mics the daemon will use."""
    mics = []
    for src in sources:
        props = src.get("properties", {})
        if src["name"].endswith(".monitor") or props.get("device.class") == "monitor":
            continue
        active = [p for p in src.get("ports", []) if p.get("name") == src.get("active_port")]
        mics.append((src, not (active and active[0].get("availability") == "not available")))
    return mics


def parse_connected_sources(sources: list[dict]) -> set[str]:
    """The recordable sources in `pactl -f json list sources` output."""
    return {src["name"] for src, plugged in microphones(sources) if plugged}


class Recorder:
    """Records from the microphone while a dictation is running.

    preferred: a PipeWire source name (`pactl list sources short`, e.g.
    alsa_input....Mic1__source). It is used whenever it is connected; otherwise -
    or with None - the system default source is used, i.e. whatever is selected in
    the desktop's sound settings (GNOME, KDE, ... all set PipeWire's default).
    Avoid Bluetooth headsets: opening their mic switches them to headset mode,
    which takes ~1 s (the start of the recording is silence).

    on_limit: called (in a new thread) when a recording reaches MAX_SECONDS. The
    daemon sets it.
    """

    def __init__(self, preferred: str | None = None):
        import sounddevice as sd            # loads PortAudio; not needed by the tests

        self.sd = sd
        self.preferred, self.on_limit = preferred, None
        self.stream, self.skip = None, 0
        self.chunks: list = []
        self.lock = threading.Lock()

    def start(self, skip: float = 0.0):
        """Start recording from the preferred mic, else the system default.
        Raises only if the system default can't be opened either.
        skip: seconds dropped from the start of the recording (the start sound)."""
        self.chunks, self.started, self.limit_hit = [], time.monotonic(), False
        self.skip = int(skip * SAMPLE_RATE)
        node = self.preferred
        if node:
            # checked on every press: the preferred mic may have been unplugged or plugged in
            connected = connected_sources()
            if connected is None:
                print("  could not list microphones (pactl), trying the preferred one anyway",
                      flush=True)
            elif node not in connected:
                print(f"  preferred microphone {node} is not connected, using the system default",
                      flush=True)
                node = None
        if node:
            try:
                self.stream = self._open(node)
                return
            except Exception as e:
                print(f"  preferred microphone {node} failed to open ({e!r}), using the system "
                      "default", flush=True)
        self.stream = self._open(None)

    def _open(self, node: str | None):
        """Open and start a stream on a PipeWire source (None: the system default)."""
        if node:
            # read by PipeWire's ALSA plugin when the stream opens. Removed right after,
            # so programs we start later (pw-play) don't inherit it
            os.environ["PIPEWIRE_NODE"] = node
        try:
            stream = self.sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                         device="pipewire" if node else None,
                                         callback=self._callback)
        finally:
            os.environ.pop("PIPEWIRE_NODE", None)
        try:
            stream.start()
        except Exception:
            stream.close()
            raise
        return stream

    def _callback(self, indata, frames, t, status):
        with self.lock:
            self.chunks.append(indata[:, 0].copy())
        if not self.limit_hit and time.monotonic() - self.started > MAX_SECONDS:
            self.limit_hit = True
            if self.on_limit:
                threading.Thread(target=self.on_limit, daemon=True).start()

    def stop(self):
        """Stop recording and return the audio as one float32 array."""
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        with self.lock:
            chunks, self.chunks = self.chunks, []
        audio = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
        return audio[self.skip:]
