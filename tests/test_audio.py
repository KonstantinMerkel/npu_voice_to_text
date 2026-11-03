"""Speech detection and microphone selection (no audio hardware needed)."""

import pytest

from npu_voice_to_text.audio import (FRAME_SECONDS, parse_connected_sources, silent_start,
                                     speech_stats)


def test_empty_clip(audio):
    assert speech_stats(audio.silence(0)) == (0.0, 0.0, 0.0)


def test_silence_and_noise_are_not_speech(audio):
    assert speech_stats(audio.silence(2))[0] == 0
    assert speech_stats(audio.noise(2))[0] == 0


def test_word_in_noise(audio):
    speech, peak, floor = speech_stats(audio.cat(audio.noise(1), audio.tone(0.5),
                                                 audio.noise(1, seed=1)))
    assert speech == pytest.approx(0.5, abs=2 * FRAME_SECONDS)
    assert peak == pytest.approx(0.1 / 2 ** 0.5, rel=0.05)    # RMS of the sine
    assert floor < 0.001


def test_short_quiet_word_in_long_silence_counts(audio):
    """The old whole-clip average dropped this; per-frame detection must not."""
    clip = audio.cat(audio.noise(2), audio.tone(0.3, amplitude=0.02), audio.noise(2, seed=1))
    assert speech_stats(clip)[0] == pytest.approx(0.3, abs=2 * FRAME_SECONDS)


def test_clip_that_is_all_speech_needs_earlier_floors(audio):
    """Key held tightly around one word: the clip's own percentile is the word itself."""
    word = audio.tone(0.6)
    assert speech_stats(word)[0] == 0                      # alone: threshold above the word
    assert speech_stats(word, floors=[0.0005])[0] == pytest.approx(0.6, abs=FRAME_SECONDS)


def test_digital_silence_is_ignored_for_the_floor(audio):
    clip = audio.cat(audio.silence(1), audio.noise(1), audio.tone(0.5))
    _, _, floor = speech_stats(clip)
    assert 0 < floor < 0.001


def test_silent_start(audio):
    assert silent_start(audio.cat(audio.silence(0.8), audio.noise(1))) == pytest.approx(0.8)
    assert silent_start(audio.noise(1)) == 0
    assert silent_start(audio.silence(1)) == 1


def source(name, port_available="yes", device_class="sound"):
    return {"name": name, "active_port": "p",
            "ports": [{"name": "p", "availability": port_available}],
            "properties": {"device.class": device_class}}


def test_connected_sources():
    sources = [
        source("mic"),
        source("empty-jack", port_available="not available"),
        source("speaker.monitor"),
        source("loopback", device_class="monitor"),
        {"name": "no-ports"},                       # virtual sources have no ports
    ]
    assert parse_connected_sources(sources) == {"mic", "no-ports"}
