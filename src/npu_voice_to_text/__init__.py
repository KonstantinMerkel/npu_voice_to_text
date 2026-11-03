# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Konstantin Merkel
"""npu-voice-to-text - hold-to-talk dictation for GNOME Wayland, Whisper on the Intel NPU.

Modules:
  cli          command line; `serve` starts the daemon, the rest are socket clients
  client       socket client (standard library only, so shortcuts react instantly)
  daemon       state machine (record / queue / transcribe / type) + socket server
  audio        microphone recorder and the speech detector
  transcriber  language choice (CPU) and transcription (NPU) with OpenVINO GenAI
  keyboard     text -> keystrokes for the active xkb layout, typed through ydotool
  models       Whisper model ids, where they live, `download`
  paths        file locations

numpy, sounddevice and openvino_genai are only imported by `serve`,
huggingface_hub only by `download`.
See docs/ARCHITECTURE.md for how the pieces fit together.
"""

__version__ = "0.2.0"
