import numpy as np
import pytest

from npu_voice_to_text.audio import SAMPLE_RATE


def tone(seconds: float, amplitude: float = 0.1, freq: float = 220.0):
    """A sine wave: loud enough to count as speech."""
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def noise(seconds: float, amplitude: float = 0.0005, seed: int = 0):
    """Quiet room noise."""
    rng = np.random.default_rng(seed)
    return (amplitude * rng.standard_normal(int(seconds * SAMPLE_RATE))).astype(np.float32)


def silence(seconds: float):
    """Exact digital silence (a mic still waking up)."""
    return np.zeros(int(seconds * SAMPLE_RATE), np.float32)


@pytest.fixture
def audio():
    class Audio:
        pass
    a = Audio()
    a.tone, a.noise, a.silence = tone, noise, silence
    a.cat = lambda *parts: np.concatenate(parts)
    return a
