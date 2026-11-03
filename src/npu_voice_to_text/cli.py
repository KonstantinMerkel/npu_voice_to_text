# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Konstantin Merkel
"""npu-voice-to-text - hold-to-talk dictation for GNOME Wayland, Whisper on the Intel NPU.

Commands:
  serve               run the daemon (normally via the systemd user service)
  press / release     start / stop recording (sent by npu-voice-to-text-keywatch)
  toggle              start or stop recording (for a normal GNOME shortcut)
  cancel              stop recording and throw the audio away
  status              print the daemon state
  download            fetch a Whisper model from Hugging Face

The client commands only import the standard library, so they start instantly.
"""

import argparse
import shlex
import sys
from pathlib import Path

from . import paths
from .client import run_client
from .models import DEFAULT_MODEL, DETECT_MODEL

CLIENT_COMMANDS = ("press", "release", "toggle", "cancel", "status")


def read_options_file(path: Path | None = None) -> list[str]:
    """`serve` options from ~/.config/npu-voice-to-text/options: one per line, # comments.
    Exits with a message naming the file (and line) if it can't be used."""
    path = path or paths.OPTIONS_FILE
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as e:
        sys.exit(f"cannot read {path}: {e}")
    args = []
    for n, line in enumerate(lines, 1):
        try:
            args += shlex.split(line, comments=True)
        except ValueError as e:                     # e.g. an unclosed quote
            sys.exit(f"{path}, line {n}: {e}: {line}")
    return args


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="npu-voice-to-text", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sv = sub.add_parser("serve", help="run the daemon")
    g = sv.add_argument_group("models")
    g.add_argument("--model", default=DEFAULT_MODEL,
                   help="Hugging Face id (looked up in ~/.local/share/npu-voice-to-text/"
                        "models) or a local directory (default: %(default)s)")
    g.add_argument("--device", default="NPU", help="OpenVINO device: NPU, GPU or CPU")
    g.add_argument("--detect-model", default=DETECT_MODEL,
                   help="small model that only detects the language (default: %(default)s)")
    g.add_argument("--detect-device", default="CPU", help="device for --detect-model")
    g = sv.add_argument_group("language")
    g.add_argument("--languages", default="de,en",
                   help="languages detection may pick, comma separated (default: %(default)s)")
    g.add_argument("--fallback-language", default="en",
                   help="used when another language is detected (default: %(default)s)")
    g.add_argument("--language", default=None,
                   help="force ONE language and skip detection. Whisper then translates "
                        "speech in other languages into it")
    g = sv.add_argument_group("output")
    g.add_argument("--layout", default=None,
                   help="xkb layout[+variant] to type for, e.g. de+nodeadkeys "
                        "(default: the active GNOME input source)")
    g.add_argument("--key-delay", type=int, default=4,
                   help="ms between key events (default: %(default)s)")
    g.add_argument("--no-restore-clipboard", dest="restore_clipboard", action="store_false",
                   help="don't restore the clipboard after pasting a character the layout "
                        "can't type (emoji, ...)")
    g.add_argument("--no-trailing-space", dest="trailing_space", action="store_false",
                   help="don't add a space after each dictation")
    g = sv.add_argument_group("misc")
    g.add_argument("--input-device", default=None,
                   help="preferred microphone: a PipeWire source name from `pactl list sources "
                        "short`. Used while connected, otherwise the system default")
    g.add_argument("--sounds", action="store_true", help="play start/stop sounds")

    dl = sub.add_parser("download", help="download a Whisper model")
    dl.add_argument("--model", default=DEFAULT_MODEL)

    for c in CLIENT_COMMANDS:
        sub.add_parser(c).add_argument("-q", "--quiet", action="store_true",
                                       help="no output, only the exit code")
    return ap


def parse_args(argv: list[str]) -> argparse.Namespace:
    if argv[:1] != ["serve"]:
        return parser().parse_args(argv)
    from_file = read_options_file()
    try:                                            # options file first, command line wins
        return parser().parse_args(["serve", *from_file, *argv[1:]])
    except SystemExit as e:
        if e.code and from_file:                    # argparse printed what is wrong
            print(f"(options are also read from {paths.OPTIONS_FILE})", file=sys.stderr)
        raise


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.cmd == "serve":
        from .daemon import run                     # numpy, OpenVINO, PortAudio
        return run(args)
    if args.cmd == "download":
        from .models import download
        return download(args.model)
    return run_client(args.cmd, args.quiet)
