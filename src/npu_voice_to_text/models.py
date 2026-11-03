# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Konstantin Merkel
"""Which Whisper models are used, where they live, and how to fetch them.
Standard library only (the CLI imports it)."""

from pathlib import Path

from . import paths

DEFAULT_MODEL = "OpenVINO/whisper-large-v3-turbo-int8-ov"
DETECT_MODEL = "OpenVINO/whisper-small-int8-ov"


def model_path(model_id: str) -> Path:
    """A local model directory, or where `download` puts a Hugging Face id."""
    p = Path(model_id).expanduser()
    return p if p.is_dir() else paths.MODELS_DIR / model_id.split("/")[-1]


def download(model_id: str) -> int:
    """`npu-voice-to-text download`"""
    from huggingface_hub import snapshot_download

    dest = model_path(model_id)
    print(f"downloading {model_id} -> {dest}")
    snapshot_download(repo_id=model_id, local_dir=dest)
    return 0
