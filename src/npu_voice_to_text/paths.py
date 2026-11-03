# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Konstantin Merkel
"""File locations (XDG base directories)."""

import os
from pathlib import Path

HOME = Path.home()
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
SOCKET_PATH = RUNTIME_DIR / "npu-voice-to-text.sock"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share")) / "npu-voice-to-text"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "npu-voice-to-text"
MODELS_DIR = DATA_DIR / "models"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "npu-voice-to-text"
OPTIONS_FILE = CONFIG_DIR / "options"
