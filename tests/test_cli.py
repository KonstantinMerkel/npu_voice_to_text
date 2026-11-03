"""Command line and options file."""

import os
import subprocess
import sys
from pathlib import Path

from npu_voice_to_text import cli, paths


def test_options_file(tmp_path, monkeypatch):
    opts = tmp_path / "options"
    opts.write_text("# a comment\n--languages de,en,fr\n\n--input-device 'my mic'  # trailing\n"
                    "--sounds\n")
    monkeypatch.setattr(paths, "OPTIONS_FILE", opts)
    assert cli.read_options_file() == ["--languages", "de,en,fr", "--input-device", "my mic",
                                       "--sounds"]
    args = cli.parse_args(["serve", "--languages", "en"])      # command line wins
    assert args.languages == "en" and args.input_device == "my mic" and args.sounds


def test_missing_options_file(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "OPTIONS_FILE", tmp_path / "none")
    args = cli.parse_args(["serve"])
    assert args.device == "NPU" and args.key_delay == 4 and args.trailing_space


def test_options_file_is_only_for_serve(tmp_path, monkeypatch):
    opts = tmp_path / "options"
    opts.write_text("--sounds\n")
    monkeypatch.setattr(paths, "OPTIONS_FILE", opts)
    assert cli.parse_args(["status", "-q"]).quiet


def test_client_commands_stay_light():
    """Bound to a shortcut, the client must start instantly: no numpy / OpenVINO."""
    code = ("import sys, npu_voice_to_text.cli; "
            "print([m for m in ('numpy', 'openvino_genai', 'sounddevice') if m in sys.modules])")
    src = str(Path(paths.__file__).parents[1])
    out = subprocess.run([sys.executable, "-s", "-c", code], capture_output=True, text=True,
                         check=True, env={**os.environ, "PYTHONPATH": src})
    assert out.stdout.strip() == "[]"


def test_client_without_daemon(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(paths, "SOCKET_PATH", tmp_path / "none.sock")
    assert cli.main(["status"]) == 1
    assert "not running" in capsys.readouterr().err
    assert cli.main(["status", "-q"]) == 1
    assert capsys.readouterr().err == ""


def test_options_file_with_unclosed_quote(tmp_path, monkeypatch):
    import pytest
    opts = tmp_path / "options"
    opts.write_text("--sounds\n--input-device 'my mic\n")
    monkeypatch.setattr(paths, "OPTIONS_FILE", opts)
    with pytest.raises(SystemExit) as e:
        cli.parse_args(["serve"])
    assert "line 2" in str(e.value) and str(opts) in str(e.value)


def test_unknown_option_in_file_names_the_file(tmp_path, monkeypatch, capsys):
    import pytest
    opts = tmp_path / "options"
    opts.write_text("--output paste\n")                  # removed option
    monkeypatch.setattr(paths, "OPTIONS_FILE", opts)
    with pytest.raises(SystemExit):
        cli.parse_args(["serve"])
    err = capsys.readouterr().err
    assert "--output" in err and str(opts) in err
