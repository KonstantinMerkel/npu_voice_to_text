"""Language choice and hallucination filter, with fake Whisper pipelines."""

from types import SimpleNamespace

import pytest

from npu_voice_to_text.transcriber import (DETECT_MIN_SPEECH, Transcriber, is_hallucination,
                                           lang_code)


class FakePipe:
    """WhisperPipeline stand-in: answers with fixed text / language, records calls."""

    def __init__(self, text="hallo", language=None):
        self.text, self.language, self.calls = text, language, []

    def generate(self, audio, **kw):
        self.calls.append(kw)
        return SimpleNamespace(texts=[f" {self.text} "], language=self.language)


def make(detected="<|en|>", text="hallo", languages=("de", "en"), fallback="en", forced=None):
    pipe, detector = FakePipe(text), FakePipe(language=detected)
    t = Transcriber(pipe, None if forced else detector, list(languages), fallback, forced)
    return t, pipe, detector


LONG, SHORT = DETECT_MIN_SPEECH + 1, DETECT_MIN_SPEECH / 2


@pytest.mark.parametrize("tok, code", [("<|de|>", "de"), ("de", "de"), ("de-DE", "de"),
                                       (" EN ", "en")])
def test_lang_code(tok, code):
    assert lang_code(tok) == code


def test_detected_language_is_passed_explicitly(audio):
    t, pipe, detector = make(detected="<|en|>")
    text, lang, _ = t(audio.tone(3), LONG)
    assert (text, lang) == ("hallo", "en")
    assert pipe.calls[-1]["language"] == "<|en|>"
    assert detector.calls[-1]["max_new_tokens"] == 1


def test_short_clip_keeps_last_language(audio):
    t, pipe, detector = make(detected="<|en|>")
    assert t.choose_language(audio.tone(3), SHORT) == "de"        # first of --languages
    assert not detector.calls
    t.choose_language(audio.tone(3), LONG)                       # detects en
    assert t.choose_language(audio.tone(3), SHORT) == "en"


def test_other_language_falls_back_and_is_not_remembered(audio):
    t, _, _ = make(detected="<|ja|>")
    assert t.choose_language(audio.tone(3), LONG) == "en"
    assert t.last_language == "de"


def test_forced_language_skips_detection(audio):
    t, pipe, _ = make(forced="de")
    assert t(audio.tone(3), LONG)[1] == "de"
    assert pipe.calls[-1]["language"] == "<|de|>"


def test_hallucinations_are_dropped(audio):
    t, _, _ = make(text="Untertitel der Amara.org-Community")
    assert t(audio.tone(3), LONG)[0] == ""


@pytest.mark.parametrize("text, speech, dropped", [
    ("Thanks for watching!", 5.0, True),          # never really dictated
    ("Vielen Dank.", 0.1, True),                  # a click that passed the detector
    ("Vielen Dank.", 1.0, False),                 # really said
    ("Vielen Dank, das war gut.", 0.1, False),
])
def test_is_hallucination(text, speech, dropped):
    assert is_hallucination(text, speech) is dropped
