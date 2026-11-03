# npu_voice_to_text

Hold-to-talk dictation for GNOME on Wayland. The speech recognition runs locally on the **Intel NPU** ("AI Boost") of Core Ultra laptops.

Hold a key (F8 by default), speak German or English, release. About two seconds later the text is typed into whatever window has focus.

- **Offline.** Whisper large-v3-turbo runs on the NPU via [OpenVINO GenAI](https://github.com/openvinotoolkit/openvino.genai). Nothing leaves the machine, and the CPU and GPU stay free.
- **Works on Wayland.** Text is typed through [ydotool](https://github.com/ReimuNotMoe/ydotool) (kernel uinput), so Wayland doesn't block it, and your clipboard isn't touched.
- **Correct characters on any layout.** The keystrokes are computed from your active GNOME layout with libxkbcommon. On a German layout, `y`/`z`, `ä ö ü ß`, `€`, `@`, `„“` and dead-key accents (`é`) come out right.
- **German and English, detected per recording.** Other languages fall back to English. Single words, which Whisper can't identify, keep the language of your previous dictation.

> **Status:** personal project, tested on one machine: ThinkPad E14 Gen 7, Core Ultra 7 255H (Arrow Lake, NPU 3720), Fedora 44, GNOME 50, kernel 7.2. Other Core Ultra generations (Meteor Lake, Lunar Lake, Panther Lake) and distributions should work, but are untested.


## Requirements

| What | Notes |
|---|---|
| Intel Core Ultra CPU with NPU | `/dev/accel/accel0` must exist (kernel driver `intel_vpu`, in mainline since 6.3) and be read/writable for you. |
| NPU firmware | From your distro (`intel-npu-firmware` on Fedora, `linux-firmware` elsewhere). |
| GNOME on Wayland | The layout is read from GNOME settings. Other desktops: pass `--layout`. |
| Python 3.10+ | |
| System libraries | Level Zero loader, oneTBB, PortAudio, libxkbcommon, `wl-clipboard`, `ydotool`, `binutils` (for `ar`) |

```bash
# Fedora
sudo dnf install oneapi-level-zero tbb portaudio libxkbcommon wl-clipboard ydotool binutils
# Ubuntu / Debian
sudo apt install libze1 libtbb12 libportaudio2 libxkbcommon0 wl-clipboard ydotool binutils
```

You don't need distro OpenVINO or NPU-compiler packages. `install.sh` fetches the exact versions that work together. Fedora's `intel-npu-compiler`, for example, is too old for current OpenVINO, and it pulls in about 8 GB of dependencies.

### ydotool

`ydotoold` must run, and its socket must be writable by your user. On Fedora, let the shipped service create the socket owned by you (keyboard only, no mouse):

```bash
sudo systemctl edit ydotool       # add:
#   [Service]
#   ExecStart=
#   ExecStart=/usr/bin/ydotoold --socket-path=/tmp/.ydotool_socket --socket-own=1000:1000 --mouse-off
sudo systemctl enable --now ydotool
```

Replace `1000:1000` with the output of `id -u`:`id -g`. Whoever can write to this socket can type into your session, so don't make it world-writable (`chmod 666`). `npu-voice-to-text` looks for the socket at `$YDOTOOL_SOCKET`, `$XDG_RUNTIME_DIR/.ydotool_socket` or `/tmp/.ydotool_socket`.


## Install

```bash
git clone https://github.com/KonstantinMerkel/npu_voice_to_text && cd npu_voice_to_text
./install.sh
```

The installer:
1. checks the requirements;
2. creates a Python venv and installs the program into it;
3. fetches Intel's NPU driver and compiler (checksum-verified);
4. checks that OpenVINO sees the NPU;
5. downloads the two Whisper models (~1.1 GB);
6. asks for the **preferred microphone** (or none). It's used while connected; otherwise, or with none, dictation records from the system default mic set in your desktop's sound settings. With none, dictation follows that default, also to Bluetooth earbuds when they connect, so the installer suggests a wired or built-in mic. Avoid Bluetooth headsets: opening their mic switches them into headset mode, which takes ~1 s, so the start of every dictation (or a whole single word) is lost. Empty jacks are marked.
7. starts the systemd user service and waits until it's ready. The first start compiles the model for the NPU, which takes a few minutes.
8. asks for the **mode** and the **key**:
   - **hold-to-talk** (default): hold the key while speaking. Needs a small root service that reads the keyboards (sudo, see [Privacy and security](#privacy-and-security)).
   - **toggle**: press the key to start, press it again to stop. A plain GNOME shortcut, no root.
9. sets up the GNOME shortcut. In hold-to-talk mode it binds the key to a do-nothing command, so apps don't also receive it (F8 in a terminal would print `^[[19~`).

Everything except the key watcher goes into your home directory. Re-run `./install.sh` at any time to update, or to change the microphone, key or mode. It remembers your choices.


## Usage

| Action | How |
|---|---|
| Dictate (hold-to-talk) | Hold your key, speak, release |
| Dictate (toggle) | Press your key, speak, press it again |
| Cancel a recording | `npu-voice-to-text cancel` (bind it to a shortcut if you like) |
| Status | `npu-voice-to-text status` → `idle`, `recording`, `transcribing N` or both |
| Logs | `journalctl --user -u npu-voice-to-text -f` (transcripts, language, timings) |

Recordings are limited to 120 s. Longer recordings are transcribed in 30 s windows.


## Configuration

Options go into `~/.config/npu-voice-to-text/options`, one per line (`#` comments are allowed). Then run `systemctl --user restart npu-voice-to-text`. Full list: `npu-voice-to-text serve --help`. Example:

```
--input-device alsa_input.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__Mic1__source
--languages de,en
--sounds
```

| Option | Default | |
|---|---|---|
| `--languages` | `de,en` | Languages the detection may pick. The first one is used for short clips until a longer dictation sets the language. |
| `--fallback-language` | `en` | Used when another language is detected |
| `--language` | – | Force one language and skip detection. Whisper then **translates** speech in other languages into it. |
| `--model` | `OpenVINO/whisper-large-v3-turbo-int8-ov` | Any `OpenVINO/whisper-*-ov` model. Fetch it first with `npu-voice-to-text download --model …`. `whisper-small-int8-ov` is ~5× faster but less accurate. |
| `--device` | `NPU` | `GPU` or `CPU` also work |
| `--output` | `type` | `paste` = clipboard + Ctrl+V instead of typing |
| `--layout` | active GNOME layout | e.g. `de+nodeadkeys`, for non-GNOME desktops |
| `--key-delay` | `4` | ms between key events. Raise it if an app drops characters. |
| `--paste-keys` | `ctrl-v` | `ctrl-shift-v` for terminals. Used for `--output paste` and for characters your layout can't type (emoji). |
| `--no-trailing-space` | | Don't add a space after each dictation |
| `--input-device` | set by `install.sh` | Preferred microphone: a PipeWire source name from `pactl list sources short`. Used while connected, otherwise the system default. |
| `--sounds` | | Start/stop sounds. The first 0.4 s of each recording (the start sound) is dropped, so start speaking after it |

**Key and mode:** re-run `./install.sh`. Keys: F1–F24, PAUSE, SCROLLLOCK, INSERT, MENU, and, in hold-to-talk mode only, RIGHTCTRL and RIGHTMETA. A lone modifier produces no input in apps, but it can't be a GNOME shortcut on its own. The hold key is stored in `/etc/npu-voice-to-text/keywatch-<uid>.env` (`KEY=F8`). A numeric evdev code works there too; restart `npu-voice-to-text-keywatch@<uid>` after editing it.


## How it works

```
key down/up ─▶ npu-voice-to-text-keywatch (root) ─▶ socket ─▶ npu-voice-to-text (user daemon)
                                                          │ record mic while held
                                                          │ on release:
                                                          │   whisper-small on CPU → language
                                                          │   whisper-turbo on NPU → text
                                                          ▼
                                     ydotool ─▶ keystrokes for your layout ─▶ focused window
```

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains every component and the reasons behind each decision.


## Privacy and security

- Audio is processed locally and never stored. Transcripts appear in your user journal (`journalctl --user -u npu-voice-to-text`).
- **The key watcher runs as root**, because reading `/dev/input` needs root or the `input` group. Membership in `input` would let every program you run read all your keystrokes. The watcher is ~100 lines of standard-library Python. It forwards only press/release of one key, and the systemd unit drops every capability except the one needed to reach your socket.
- The daemon socket is `0600`, so only you (and root) can start a recording.
- ydotool can type anything. Keep its socket owned by you (see above).


## Troubleshooting

| Problem | Fix |
|---|---|
| `NPU` missing from the installer's device list | Check `ls -l /dev/accel/accel0` and that NPU firmware is installed (`dmesg \| grep intel_vpu`). |
| `Compilation failed … vclAllocatedExecutableCreate2` | The NPU compiler doesn't match OpenVINO. Upgrade `openvino-genai` in `pyproject.toml` and `NPU_DRIVER_*` in `install.sh` together, then re-run it. |
| Holding the key does nothing | `journalctl -u npu-voice-to-text-keywatch@$(id -u)` should list your keyboards, and `journalctl --user -u npu-voice-to-text -f` should show `command: press`. On laptops, the F-keys are often media keys: use Fn or FnLock, or pick another key. |
| Short words get lost, or the start of sentences is missing | The log says `warning: the microphone delivered nothing for the first …s`. Typically that's a Bluetooth headset switching modes. Set another preferred mic with `./install.sh`, or change the system default. |
| "Nothing heard" although you spoke | Check the log line `[… speech, peak …]`. A peak below ~0.005 means the mic is too quiet or muted (a laptop's built-in mic with the lid closed, for example). Pick another mic or raise its input volume. |
| Nothing typed / wrong window | Check `ydotool key 42:1 42:0` and `systemctl status ydotool`. Text goes to the window focused at **release**. |
| Wrong characters | The log prints `keymap <layout>`. Set `--layout` if that's wrong. Check that Caps Lock is off. |
| Characters missing | Raise `--key-delay` to 8–12. |
| English comes out as German | Don't use `--language`. See also Known issues. |
| "Untertitel der Amara.org-Community" etc. | A Whisper hallucination on silence. Add it to `HALLUCINATIONS` in `src/npu_voice_to_text/transcriber.py` and re-run `./install.sh`. |


## Known issues

- **Whisper on the NPU keeps the first detected language** (OpenVINO GenAI 2026.4). Every later recording is transcribed, or rather translated, in that language. That's why the language is detected by a separate small model on the CPU (~0.5 s) and passed to the NPU explicitly. Upstream issue: *(link once filed)*.
- Whisper can't identify the language of one or two words. Short clips therefore use the language of your previous dictation, so a single word right after switching languages may come out in (or translated into) the previous language.
- The NPU needs fixed input sizes, so every recording costs about the same as 30 s of audio (~1.6 s with turbo), even a 2 s one.


## Development

The program is the Python package in `src/npu_voice_to_text/` (one module per component, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#files)). The tests need neither the NPU, OpenVINO nor a microphone, only `numpy` and libxkbcommon:

```bash
uvx --with numpy pytest
# or with the installed venv:
~/.local/share/npu-voice-to-text/venv/bin/pip install pytest
~/.local/share/npu-voice-to-text/venv/bin/python -m pytest
```

After a change, re-run `./install.sh`. It builds the package from a fresh copy of `src/`; a bare `pip install .` in the checkout can reuse a stale `build/` directory and install modules you deleted.


## Uninstall

```bash
./uninstall.sh          # keeps venv, NPU libs and models
./uninstall.sh --all    # removes everything
```


## License

[GPL-3.0-or-later](LICENSE). Models: [Whisper](https://github.com/openai/whisper) (MIT) in [OpenVINO's conversions](https://huggingface.co/OpenVINO). NPU driver: [intel/linux-npu-driver](https://github.com/intel/linux-npu-driver) (MIT).
