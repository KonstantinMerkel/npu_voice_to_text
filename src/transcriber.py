# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Konstantin Merkel
"""Transcriber: language detection on the CPU, transcription on the NPU (OpenVINO GenAI)."""

import sys
import time
from pathlib import Path

import numpy as np

from . import paths
from .audio import SAMPLE_RATE
from .models import model_path

DETECT_MIN_SPEECH = 1.0    # language detection is unreliable below ~1 s of speech
                           # (single words come out as "en"), so short clips reuse
                           # the last detected language instead

# Phrases Whisper "hears" in silence or noise (subtitle credits from its training
# data). Always dropped.
HALLUCINATIONS = {
    "untertitel der amara.org-community",
    "untertitel im auftrag des zdf, 2017",
    "untertitel im auftrag des zdf für funk, 2017",
    "vielen dank fürs zuschauen.",
    "swr 2020",
    "swr 2021",
    "thanks for watching!",
}
# Phrases Whisper also hallucinates, but that people really say. Dropped only when
# the clip had barely any speech (a key click or a breath that just passed the
# speech detector), so a dictated "Vielen Dank." is typed.
SHORT_HALLUCINATIONS = {"vielen dank.", "thank you.", "you"}
SHORT_HALLUCINATION_SPEECH = 0.3   # seconds


def lang_code(tok: str) -> str:
    """'<|de|>' / 'de' / 'de-DE' -> 'de'"""
    return tok.strip().strip("<|>").split("-")[0].lower()


def is_hallucination(text: str, speech_seconds: float) -> bool:
    t = text.lower()
    return t in HALLUCINATIONS or (t in SHORT_HALLUCINATIONS
                                   and speech_seconds < SHORT_HALLUCINATION_SPEECH)


class Transcriber:
    """Whisper via OpenVINO GenAI.

    Language choice per recording:
      - forced (--language): always that one
      - >= DETECT_MIN_SPEECH of speech: detected by a small model on the CPU,
        restricted to `languages` (else `fallback`), and remembered
      - shorter: the remembered language (initially languages[0]) - Whisper can't
        tell the language of one or two words and mostly answers "en"
    The language is always passed explicitly to the main model. Its own
    auto-detection is never used: on the NPU it sticks to the first language it
    ever detected (seen with OpenVINO GenAI 2026.4).

    pipe / detector: objects with WhisperPipeline's `generate` (the detector is
    None when the language is forced). `Transcriber.load()` creates the real ones.
    """

    def __init__(self, pipe, detector, languages: list[str], fallback: str,
                 forced: str | None, detect_device: str = "CPU"):
        self.pipe, self.detector = pipe, detector
        self.languages, self.fallback, self.forced = languages, fallback, forced
        self.detect_device = detect_device
        self.last_language = languages[0] if languages else fallback

    @classmethod
    def load(cls, model: str, device: str, detect_model: str, detect_device: str,
             languages: list[str], fallback: str, forced: str | None) -> "Transcriber":
        """Load (on first use: compile) the models and warm them up."""
        import openvino_genai as ov_genai

        paths.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        silence = np.zeros(SAMPLE_RATE, dtype=np.float32).tolist()

        print(f"loading {model} on {device} (the first start compiles it, can take minutes)",
              flush=True)
        pipe = ov_genai.WhisperPipeline(str(cls._require(model)), device,
                                        CACHE_DIR=str(paths.CACHE_DIR))
        pipe.generate(silence, task="transcribe", language=f"<|{fallback}|>")   # warm-up

        detector = None
        if not forced:
            detector = ov_genai.WhisperPipeline(str(cls._require(detect_model)),
                                                detect_device, CACHE_DIR=str(paths.CACHE_DIR))
            detector.generate(silence, task="transcribe", max_new_tokens=1)
        return cls(pipe, detector, languages, fallback, forced, detect_device)

    @staticmethod
    def _require(model_id: str) -> Path:
        path = model_path(model_id)
        if not path.is_dir():
            sys.exit(f"model not found at {path} - run: "
                     f"npu-voice-to-text download --model {model_id}")
        return path

    def detect_language(self, audio) -> str | None:
        """Language of the first 30 s, from one generated token of the small model."""
        res = self.detector.generate(audio[:30 * SAMPLE_RATE].tolist(), task="transcribe",
                                     max_new_tokens=1)
        found = getattr(res, "language", None)
        return lang_code(found) if found else None

    def choose_language(self, audio, speech_seconds: float) -> str:
        if self.forced:
            return self.forced
        if speech_seconds < DETECT_MIN_SPEECH:
            print(f"  language {self.last_language} (short clip, kept from before)", flush=True)
            return self.last_language
        t0 = time.monotonic()
        detected = self.detect_language(audio)
        if detected in self.languages:
            self.last_language = detected
            note = ""
        else:
            note = f", detected {detected!r} -> fallback"
        lang = detected if detected in self.languages else self.fallback
        print(f"  language {lang} ({time.monotonic() - t0:.2f}s on {self.detect_device}{note})",
              flush=True)
        return lang

    def __call__(self, audio, speech_seconds: float) -> tuple[str, str, float]:
        """-> (text, language, seconds)"""
        t0 = time.monotonic()
        lang = self.choose_language(audio, speech_seconds)
        res = self.pipe.generate(audio.tolist(), task="transcribe", language=f"<|{lang}|>")
        text = res.texts[0].strip()
        if is_hallucination(text, speech_seconds):
            print(f"  dropped {text!r} (Whisper hallucinates it in noise)", flush=True)
            text = ""
        return text, lang, time.monotonic() - t0
