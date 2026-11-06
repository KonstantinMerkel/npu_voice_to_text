# Welcome to npu_voice_to_text

Voice to text dictation for GNOME on Wayland running on NPU.

Hold a key (F8 by default), speak, and release it, or tap it once to start and once to stop.\
About two seconds later the text is typed into the focused window.

- **Offline.** Whisper large-v3-turbo runs on the NPU through [OpenVINO GenAI](https://github.com/openvinotoolkit/openvino.genai). Nothing leaves the machine.
- **Wayland-native.** The key and the typing both go through GNOME's portals, which you allow once. No ydotool, no clipboard, no root daemon.

> **Status:** tested on one machine: ThinkPad E14 Gen 7, Core Ultra 7 255H (Arrow Lake), Fedora 44, GNOME 50. Other Core Ultra generations and distros should work but are untested.


## Privacy and security

- Audio is processed locally and never stored. Transcripts appear only in your user journal.
- Nothing runs as root
- Keyboard access means the program can send key presses to the focused window, like a second keyboard. It has no mouse or screen access. GNOME keeps the permission under a token in `~/.local/state/npu-voice-to-text/` (mode `0600`).
- The control socket has mode `0600`, so only you can start a recording.
- `./uninstall.sh` revokes the keyboard access and removes the key binding.


## Requirements

- Intel Core Ultra CPU with an NPU: `/dev/accel/accel0` exists (kernel driver `intel_vpu`) and you can read and write it
- NPU firmware from your distro (`intel-npu-firmware` on Fedora, `linux-firmware` elsewhere)
- GNOME 48 or newer, on Wayland, with `xdg-desktop-portal-gnome` (installed with GNOME)
- Python 3.10 to 3.14 (OpenVINO has no Python 3.15 packages yet) and a few system libraries:

```bash
# Fedora
sudo dnf install oneapi-level-zero tbb portaudio libxkbcommon python3-gobject binutils
# Ubuntu / Debian
sudo apt install libze1 libtbb12 libportaudio2 libxkbcommon0 python3-gi binutils
```

If `/dev/accel/accel0` isn't writable for you (common on Ubuntu), add yourself to its group and log in again: `sudo usermod -aG render $USER`.

You don't need OpenVINO or NPU compiler packages from your distro. The installer fetches the versions that work together.


## Install

```bash
git clone https://github.com/KonstantinMerkel/npu_voice_to_text && cd npu_voice_to_text
./install.sh
```

On install two Requests will open:
- **Remote Desktop:** switch on **Allow Remote Interaction**, then click Share. Grants keyboard input needed for typing
- **Add Keyboard Shortcuts:** confirm F8 or press the key you want.

The first start compiles the model for the NPU, which takes a few minutes.\
A model (~1.1GB) is downloaded.\
To update, `git pull` and re-run `./install.sh`.


## Uninstall

```bash
./uninstall.sh          # keeps the venv, NPU libs and models
./uninstall.sh --all    # removes everything
```



## Troubleshooting

Start with the log: `journalctl --user -u npu-voice-to-text -f`.

| Problem | Fix |
|---|---|
| Stopped working after a distro upgrade (`No module named npu_voice_to_text` or `openvino`) | The upgrade replaced the Python the venv was built with. OpenVINO needs Python 3.14 or older: install it (Fedora 45: `sudo dnf install python3.14`), then rebuild the venv with `python3.14 -m venv --clear ~/.local/share/npu-voice-to-text/venv && ~/.local/share/npu-voice-to-text/venv/bin/pip install .` and `systemctl --user restart npu-voice-to-text`. |
| `NPU` missing from the installer's device list | Check `ls -l /dev/accel/accel0` and the firmware (`dmesg \| grep intel_vpu`). |
| `Compilation failed … vclAllocatedExecutableCreate2` | The NPU compiler doesn't match OpenVINO. Bump `openvino-genai` in `pyproject.toml` and `NPU_DRIVER_*` in `install.sh` together. |
| The key does nothing | The log should say `dictation key: …`. If it says `no dictation key`, re-run `./install.sh`. On laptops, F-keys are often media keys: use Fn or FnLock. |
| Nothing is typed | If the log says `no keyboard access`, re-run `./install.sh` and allow it. |
| Start of sentences lost | Usually a Bluetooth headset switching modes. Pick a wired or built-in mic (`./install.sh`, Custom). |
| "Nothing heard" | A `peak` below ~0.005 in the log means the mic is muted or too quiet. |
| Wrong or missing characters | Check the `keymap` line in the log and that Caps Lock is off. Set `--layout` or choose a slower typing speed. |
| English comes out as German | Don't set `--language`: it forces Whisper to translate into that language. |


## Known issues

- On the NPU, Whisper keeps using the first language it detects (OpenVINO GenAI 2026.4, reported upstream). As a workaround, a small model on the CPU detects the language of each recording first (~0.5 s) and passes it to the NPU explicitly.
- Whisper can't identify the language of one or two words, so short clips use the language of your previous dictation.
- Every recording costs about as much as 30 s of audio (~1.6 s), because the NPU needs fixed input sizes.



## Scope

**Supported**
- Intel Core Ultra NPUs, GNOME 48+ on Wayland
- Any language Whisper knows, detected per recording among the ones you choose (default German and English, others via *Custom*)
- Any keyboard layout GNOME uses (first layout group). Characters the layout can't type are replaced (`ä` → `ae`, `é` → `e`) or left out, so for e.g. Russian you need a Russian layout.

**Maybe later**
- Other Wayland desktops that implement the same portals (KDE Plasma): GNOME-specific code is limited to reading the layout, the version check and resetting the key
- A proper voice-activity detector (e.g. Silero) instead of the volume threshold
- Dropping the CPU language detection once the OpenVINO bug is fixed
- Voice Streaming (Typing while speaking)

**Not planned**
- X11, or GNOME versions older than 48 (no global shortcuts portal)
- Cloud speech recognition
- Typing via the clipboard, ydotool or anything that needs root
- A lone modifier key (e.g. Right Ctrl) as the dictation key
- CPU/GPU support or fallback, there are plenty of good other programs for this



## License

[GPL-3.0-or-later](LICENSE). Models: [Whisper](https://github.com/openai/whisper) (MIT), converted by [OpenVINO](https://huggingface.co/OpenVINO). NPU driver: [intel/linux-npu-driver](https://github.com/intel/linux-npu-driver) (MIT).
