# npu_voice_to_text

Hold-to-talk dictation for GNOME on Wayland. The speech recognition runs locally on the **Intel NPU** ("AI Boost") of Core Ultra laptops.

Hold a key (F8 by default), speak German or English, release. About two seconds later the text is typed into whatever window has focus. For longer dictations, tap the key instead, speak hands-free, and tap it again.

- **Offline.** Whisper large-v3-turbo runs on the NPU via [OpenVINO GenAI](https://github.com/openvinotoolkit/openvino.genai). Nothing leaves the machine, and the CPU and GPU stay free.
- **Works on Wayland, the sanctioned way.** Text is typed through GNOME's Remote Desktop portal, with keyboard access you allow once in a GNOME dialog. No ydotool, no clipboard.
- **Correct characters on any layout.** The keystrokes are computed from your active GNOME layout with libxkbcommon. On a German layout, `y`/`z`, `ä ö ü ß`, `€`, `@`, `„“` and dead-key accents (`é`) come out right. Characters your layout has no key for are replaced: on a US layout `ä` becomes `ae`, `„“` become `"`.
- **German and English, detected per recording.** Other languages fall back to English. Single words, which Whisper can't identify, keep the language of your previous dictation.

> **Status:** personal project, tested on one machine: ThinkPad E14 Gen 7, Core Ultra 7 255H (Arrow Lake, NPU 3720), Fedora 44, GNOME 50, kernel 7.2. Other Core Ultra generations (Meteor Lake, Lunar Lake, Panther Lake) and distributions should work, but are untested.


## Requirements

| What | Notes |
|---|---|
| Intel Core Ultra CPU with NPU | `/dev/accel/accel0` must exist (kernel driver `intel_vpu`, in mainline since 6.3) and be read/writable for you. |
| NPU firmware | From your distro (`intel-npu-firmware` on Fedora, `linux-firmware` elsewhere). |
| GNOME 48 or newer, on Wayland | The dictation key uses the global shortcuts portal (new in GNOME 48), typing the Remote Desktop portal (`xdg-desktop-portal-gnome`, installed with GNOME). The layout is read from GNOME settings. |
| Python 3.10+ | |
| System libraries | Level Zero loader, oneTBB, PortAudio, libxkbcommon, PyGObject, `binutils` (for `ar`) |

```bash
# Fedora
sudo dnf install oneapi-level-zero tbb portaudio libxkbcommon python3-gobject binutils
# Ubuntu / Debian
sudo apt install libze1 libtbb12 libportaudio2 libxkbcommon0 python3-gi binutils
```

You don't need distro OpenVINO or NPU-compiler packages. `install.sh` fetches the exact versions that work together. Fedora's `intel-npu-compiler`, for example, is too old for current OpenVINO, and it pulls in about 8 GB of dependencies.

Earlier versions typed through ydotool. It isn't needed any more: if you set up `ydotoold` only for npu-voice-to-text, you can disable it (`sudo systemctl disable --now ydotool`).


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
6. asks for the **settings**: Recommended or Custom (see below);
7. starts the systemd user service and waits until it's ready. On the very first start, **GNOME shows two dialogs**:
   - **"Remote Desktop"**: allow keyboard access, otherwise nothing can be typed.
   - **"Add Keyboard Shortcuts"**: confirm F8 as the dictation key, or press the key you want.

   See [Privacy and security](#privacy-and-security) for what exactly they allow. The first start also compiles the model for the NPU, which takes a few minutes.

Everything goes into your home directory; no root, no sudo. Re-run `./install.sh` at any time to update.

**Settings.** *Recommended* takes a wired or built-in mic, normal typing speed, German and English, and keeps the dictation key. *Custom* asks for each setting, with your current choices as the defaults:
- **Microphone** (or none). The preferred mic is used while connected; otherwise, or with none, dictation records from the system default mic set in your desktop's sound settings. With none, dictation follows that default, also to Bluetooth earbuds when they connect, so the installer suggests a wired or built-in mic. Avoid Bluetooth headsets: opening their mic switches them into headset mode, which takes ~1 s, so the start of every dictation (or a whole single word) is lost. Empty jacks are marked.
- **Dictation key**: change it, and GNOME's dialog asks for the new one.
- **Typing speed**: fast, normal or careful (2, 4 or 10 ms per key event). Choose careful if an app loses characters.
- **Languages** you dictate in, and the fallback language.
- **Sounds** at start and stop, and the **space after each dictation**.

Both end with a summary. The settings are saved as options in `~/.config/npu-voice-to-text/options` (see below); options at their default are left out, and lines you added yourself are kept. Choosing Recommended on a later run resets the settings above to the recommended ones.


## Usage

| Action | How |
|---|---|
| Dictate | Hold your key, speak, release |
| Dictate hands-free | Tap your key (shorter than 0.4 s), speak, tap it again |
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
| `--layout` | active GNOME layout | e.g. `de+nodeadkeys`, for non-GNOME desktops |
| `--key-delay` | `4` | ms between key events. Raise it if an app drops characters. |
| `--no-trailing-space` | | Don't add a space after each dictation |
| `--input-device` | set by `install.sh` | Preferred microphone: a PipeWire source name from `pactl list sources short`. Used while connected, otherwise the system default. |
| `--sounds` | | Start/stop sounds. The first 0.4 s of each recording (the start sound) is dropped, so start speaking after it |

**Key:** re-run `./install.sh` and choose Custom. The key is a GNOME global shortcut: to change it, answer "yes" to "Change it?", and GNOME's dialog asks for the new key. Any key or combination GNOME accepts works (F-keys, Pause, Insert, `Ctrl+F9`, …), but not a lone modifier like Right Ctrl. The key is swallowed: apps don't receive it while npu-voice-to-text runs.


## How it works

```
key down/up ─▶ GNOME global shortcut (portal) ──────────────▶ npu-voice-to-text (user daemon)
                                                          │ record mic while held
                                                          │ on release:
                                                          │   whisper-small on CPU → language
                                                          │   whisper-turbo on NPU → text
                                                          ▼
              Remote Desktop portal ─▶ keystrokes for your layout ─▶ focused window
```

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains every component and the reasons behind each decision.


## Privacy and security

- Audio is processed locally and never stored. Transcripts appear in your user journal (`journalctl --user -u npu-voice-to-text`).
- **Nothing runs as root.** npu-voice-to-text never reads your keyboard: GNOME tells it when the dictation key goes down and up (the global shortcuts portal), and nothing else. The binding is stored by GNOME in dconf (`/org/gnome/settings-daemon/global-shortcuts/io.github.KonstantinMerkel.NpuVoiceToText/`); `./uninstall.sh` removes it.
- Both portals identify npu-voice-to-text by the app ID `io.github.KonstantinMerkel.NpuVoiceToText`, from a hidden desktop file in `~/.local/share/applications`. That's also the name GNOME's dialogs show.
- The daemon socket is `0600`, so only you (and root) can start a recording.
- **Keyboard access.** In GNOME's dialog you allow npu-voice-to-text to send key presses to the window that has focus, the way a second keyboard would. Only the keyboard is requested: no mouse, no screen sharing. GNOME stores the permission (table `remote-desktop` of its permission store) under a secret restore token, which npu-voice-to-text keeps in `~/.local/state/npu-voice-to-text/keyboard-access.token` (mode `0600`). With the token, later dictations need no dialog. Programs not sandboxed by Flatpak run as you and could read that file, the same trust level as anything else in your home directory. `./uninstall.sh` revokes the permission; to revoke it by hand: `gdbus call --session --dest org.freedesktop.impl.portal.PermissionStore --object-path /org/freedesktop/impl/portal/PermissionStore --method org.freedesktop.impl.portal.PermissionStore.Delete remote-desktop "$(cat ~/.local/state/npu-voice-to-text/keyboard-access.token)"`.


## Troubleshooting

| Problem | Fix |
|---|---|
| `NPU` missing from the installer's device list | Check `ls -l /dev/accel/accel0` and that NPU firmware is installed (`dmesg \| grep intel_vpu`). |
| `Compilation failed … vclAllocatedExecutableCreate2` | The NPU compiler doesn't match OpenVINO. Upgrade `openvino-genai` in `pyproject.toml` and `NPU_DRIVER_*` in `install.sh` together, then re-run it. |
| The key does nothing | `journalctl --user -u npu-voice-to-text -f` should say `dictation key: Press F8` after the start, and `key down` / `key up` when you press it. `no dictation key` means the GNOME dialog was declined: re-run `./install.sh`. On laptops, the F-keys are often media keys: use Fn or FnLock, or pick another key. |
| Short words get lost, or the start of sentences is missing | The log says `warning: the microphone delivered nothing for the first …s`. Typically that's a Bluetooth headset switching modes. Set another preferred mic by re-running `./install.sh` (Custom), or change the system default. |
| "Nothing heard" although you spoke | Check the log line `[… speech, peak …]`. A peak below ~0.005 means the mic is too quiet or muted (a laptop's built-in mic with the lid closed, for example). Pick another mic or raise its input volume. |
| Nothing typed / wrong window | The log should not say `no keyboard access`. If it does (dialog declined or revoked), re-run `./install.sh` and allow it. Text goes to the window focused at **release**. |
| The installer says `no keyboard access` | The GNOME dialog was declined or left open for 5 min. Re-run `./install.sh`. |
| Wrong characters | The log prints `keymap <layout>`. Set `--layout` if that's wrong. Check that Caps Lock is off. Characters your layout has no key for are replaced (`ä` → `ae`) or, like emoji, left out (`no key on this layout, left out: …` in the log). |
| Characters missing | Re-run `./install.sh`, change the settings, typing speed "careful" (or `--key-delay` 8–12). |
| English comes out as German | Don't use `--language`. See also Known issues. |
| "Untertitel der Amara.org-Community" etc. | A Whisper hallucination on silence. Add it to `HALLUCINATIONS` in `src/npu_voice_to_text/transcriber.py` and re-run `./install.sh`. |


## Known issues

- **Whisper on the NPU keeps the first detected language** (OpenVINO GenAI 2026.4). Every later recording is transcribed, or rather translated, in that language. That's why the language is detected by a separate small model on the CPU (~0.5 s) and passed to the NPU explicitly. Upstream issue: *(link once filed)*.
- Whisper can't identify the language of one or two words. Short clips therefore use the language of your previous dictation, so a single word right after switching languages may come out in (or translated into) the previous language.
- The NPU needs fixed input sizes, so every recording costs about the same as 30 s of audio (~1.6 s with turbo), even a 2 s one.


## Development

The program is the Python package in `src/npu_voice_to_text/` (one module per component, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#files)). The tests need neither the NPU, OpenVINO nor a microphone, only `numpy`, `jeepney` and libxkbcommon:

```bash
uvx --with numpy --with jeepney pytest
# or with the installed venv:
~/.local/share/npu-voice-to-text/venv/bin/pip install pytest
~/.local/share/npu-voice-to-text/venv/bin/python -m pytest
```

After a change, re-run `./install.sh`. It builds the package from a fresh copy of `src/`; a bare `pip install .` in the checkout can reuse a stale `build/` directory and install modules you deleted.


## Uninstall

```bash
./uninstall.sh          # revokes the keyboard access, forgets the key; keeps venv, NPU libs and models
./uninstall.sh --all    # removes everything
```


## License

[GPL-3.0-or-later](LICENSE). Models: [Whisper](https://github.com/openai/whisper) (MIT) in [OpenVINO's conversions](https://huggingface.co/OpenVINO). NPU driver: [intel/linux-npu-driver](https://github.com/intel/linux-npu-driver) (MIT).
