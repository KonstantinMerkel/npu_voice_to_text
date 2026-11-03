# How npu-voice-to-text works

This document explains every part of npu-voice-to-text and why it is built the way it is. The [README](../README.md) covers installation and usage.

## The big picture

```
          ┌──────────────── root ────────────────┐   ┌───────────────────── your user ─────────────────────┐
keyboard ─▶ npu-voice-to-text-keywatch                  │   │ npu-voice-to-text serve (systemd user service)            │
 F8 down  │  reads /dev/input/event*              │   │                                                     │
 F8 up    │  "press" / "release" ────────────────────▶│ Daemon ── recording → queue → transcribe → type     │
          └──────────────────────────────────────┘ socket │                                                  │
                                    /run/user/UID/npu-voice-to-text.sock                                          │
                                                       │   Recorder     mic → 16 kHz mono buffer (PortAudio)  │
                                                       │   Transcriber  whisper-small, CPU → language         │
                                                       │                whisper-turbo, NPU → text             │
                                                       │   Keyboard     text → keystrokes for your layout ────┼─▶ Remote Desktop portal ─▶ focused window
                                                       └─────────────────────────────────────────────────────┘
```

One dictation, step by step:

1. **F8 down.** The kernel reports a key event on `/dev/input/eventN`. `npu-voice-to-text-keywatch` sees it and sends `press` to the daemon's Unix socket.
2. The daemon opens the microphone, and audio chunks are appended to a buffer.
3. **F8 up.** The watcher sends `release`. The daemon closes the mic and starts a thread for steps 4–6, so the socket stays responsive.
4. The small Whisper model on the CPU listens to the recording and produces a single token: the language.
5. The large Whisper model on the NPU transcribes the recording, with that language set explicitly.
6. The text is converted into keystrokes for your current keyboard layout and sent through GNOME's Remote Desktop portal, which types them like a second keyboard.

Typical timing for a 5 s sentence: ~0.5 s language detection + ~1.6 s transcription + typing.

## Files

| Path | What |
|---|---|
| `src/npu_voice_to_text/` → venv | The daemon and CLI, a Python package (`pyproject.toml`) that `install.sh` installs into the venv (built from a temporary copy, so a stale `build/` can never leak in). See "Code layout" below. |
| `~/.local/bin/npu-voice-to-text` | A two-line launcher written by `install.sh`: `venv/bin/python -I -m npu_voice_to_text`. `-I` keeps `PYTHONPATH` and the current directory out of the imports. |
| `tests/` | pytest suite. Needs only `numpy` and libxkbcommon: `uvx --with numpy pytest` |
| `npu-voice-to-text-keywatch.py` → `/usr/local/bin/npu-voice-to-text-keywatch` | Root key watcher, standard library only |
| `systemd/npu-voice-to-text.service` → `~/.config/systemd/user/` | User service. Sets `LD_LIBRARY_PATH` to the NPU libs. |
| `systemd/npu-voice-to-text-keywatch@.service` → `/etc/systemd/system/` | Template unit. The instance name is your UID. |
| `/etc/npu-voice-to-text/keywatch-<uid>.env` | The hold key (`KEY=F8`), written by `install.sh` |
| `tools/gnome-shortcut.py` | Used by `install.sh`/`uninstall.sh` to manage the one `npu-voice-to-text` GNOME shortcut |
| `~/.local/share/npu-voice-to-text/venv` | Python: `openvino-genai`, `numpy`, `sounddevice`, `huggingface_hub` |
| `~/.local/share/npu-voice-to-text/lib` | Intel NPU userspace driver and compiler (see "The NPU software stack") |
| `~/.local/share/npu-voice-to-text/models` | Whisper models in OpenVINO format (~1.1 GB) |
| `~/.cache/npu-voice-to-text` | Compiled model blobs, so later starts take seconds instead of minutes |
| `~/.config/npu-voice-to-text/options` | `serve` options, one per line, e.g. the microphone (written by `install.sh`) |
| `tools/microphones.py` | Lists microphones for `install.sh` |
| `tools/remove-keywatch.sh` | Removes the key watcher (`install.sh` when switching to toggle mode, `uninstall.sh`) |
| `$XDG_RUNTIME_DIR/npu-voice-to-text.sock` | Control socket (mode 0600) |
| `~/.local/state/npu-voice-to-text/remote-desktop-token` | Restore token of the keyboard permission (mode 0600). Removed, and the permission revoked, by `uninstall.sh` |

### Code layout

One module per component. Modules marked *stdlib* import only the standard library, so the client commands (bound to a shortcut) start in ~40 ms. `cli` imports `daemon` only for `serve`.

| Module | What | |
|---|---|---|
| `cli` | argparse, options file, dispatch to the commands | stdlib |
| `client` | `request()`: one line to the socket, one line back | stdlib |
| `paths`, `models` | XDG locations; model ids, `model_path()`, `download` | stdlib |
| `keyboard` | `XkbKeymap` (layout → keystrokes, substitutes), `Keyboard` (types a text) | stdlib + ctypes |
| `portal` | `RemoteKeyboard`: RemoteDesktop portal sessions, restore token | jeepney (D-Bus) |
| `audio` | `speech_stats()` (speech detector), `Recorder` (PortAudio) | numpy |
| `transcriber` | `Transcriber`: language choice, Whisper, hallucination filter | numpy, OpenVINO on `load()` |
| `daemon` | `Daemon`: state machine, queue, worker, socket server | all of the above |

**Dependencies are passed in.** `Daemon(transcriber, recorder, keyboard)` and `Transcriber(pipe, detector, …)` take their parts as arguments. `Daemon.from_args()` and `Transcriber.load()` build the real ones (NPU, microphone, portal). The tests pass fakes instead, so they cover the state machine, the portal's request flow (against a fake D-Bus connection), recording overlapping transcription, typing pausing while you record, language choice and the socket server without any hardware. The keymap tests use the real libxkbcommon, so they check the actual German and US layouts.

The key watcher stays a single standalone script outside the package: root runs a root-owned copy of it, and it must not import anything from a user-writable venv.

---

## Component by component

### npu-voice-to-text-keywatch: why a root process at all?

Hold-to-talk needs to know when a key is **released**. GNOME's custom shortcuts only fire on press, and Wayland deliberately doesn't let normal apps listen to global key events. The only reliable source is the kernel's input devices, `/dev/input/event*`. They belong to `root:input` with mode `0660`.

There were two options:
- **Add your user to `input`.** Simple, but then *every* program running as you could read every keystroke (passwords included). It's a keylogger permission.
- **A tiny root service** that forwards nothing but press/release of one key. This is the chosen option.

How it works:
- It finds keyboards through udev's symlinks (`/dev/input/by-path/*-event-kbd`) and rescans every 3 s, so hot-plugged keyboards (docks, receivers) are picked up.
- It reads raw `struct input_event` records (24 bytes: time, type, code, value).
- For `EV_KEY` events with our key code, `value` 1 means press and 0 means release. Value 2 is autorepeat and is ignored.
- It doesn't *grab* the device (`EVIOCGRAB`), because grabbing would block the whole keyboard. So the key also reaches the focused app. `install.sh` therefore binds it to a GNOME shortcut with command `true`, and GNOME consumes it. A lone modifier (RIGHTCTRL) produces no input anyway, so it needs no shortcut.

The unit hardening (`CapabilityBoundingSet=CAP_DAC_OVERRIDE`, `ProtectSystem=strict`, `PrivateNetwork`, `RestrictAddressFamilies=AF_UNIX`, …) means that even as root, this process can only read files, talk over Unix sockets, and bypass file permissions. It needs the last one to connect to your `0600` socket. It runs a root-owned copy in `/usr/local/bin`, never the user-writable one in `~/.local/bin`. Otherwise any program running as you could edit the file and get root.

### Daemon and socket protocol

`npu-voice-to-text serve` is a plain Unix-socket server. One line in, one line out:

| Command | Effect | Reply |
|---|---|---|
| `press` | start recording, unless one is running | `recording`, `busy (recording)` or `error: …` |
| `release` | stop recording and queue it for transcription | `transcribing` or `not recording` |
| `toggle` | `release` while recording, otherwise `press` | |
| `cancel` | stop recording and discard it | `cancelled` |
| `status` | | `idle`, `recording`, `transcribing N` or `recording, transcribing N` |

`npu-voice-to-text press|release|toggle|cancel|status` is the client. It only imports the standard library, so it reacts instantly when bound to a shortcut. A press while the previous dictation is still being transcribed starts the next recording right away. Recordings are transcribed and typed one at a time, in order, so two dictations never type over each other. Text is only typed while no recording runs: with RIGHTCTRL or RIGHTMETA as the hold key, typing while the key is held would turn the characters into shortcuts. Typing therefore pauses when a recording starts. It checks before each character, so it pauses between whole characters and never leaves Shift or AltGr held down. Only the character being typed at the moment of the key press can still be hit (a few ms). A `toggle` after the 120 s limit already stopped the recording doesn't start a new one.

Why a socket and not D-Bus or signals: it needs no extra dependencies, the permissions are simply file permissions, and root (the key watcher) can reach it without joining your session bus.

### Recorder

[sounddevice](https://python-sounddevice.readthedocs.io/) wraps PortAudio, which goes through PipeWire's ALSA plugin. The stream is opened at 16 kHz mono float32, the format Whisper expects, so PipeWire does the resampling. The mic is only open while recording. GNOME's microphone indicator shows exactly when.

**Which microphone.** `--input-device` names a *preferred* mic, as a PipeWire source name (`pactl list sources short`). On every press the recorder asks `pactl` whether it is connected; an analog jack with nothing plugged in counts as not connected, since its source exists but records silence. If `pactl` doesn't answer within 1 s, the preferred mic is tried anyway. If it is connected, the recorder opens ALSA's `pipewire` device with `PIPEWIRE_NODE` set to that name; `PIPEWIRE_NODE` is set only while the stream opens, so programs the daemon starts later don't inherit it. If the preferred mic is missing or fails to open, or none is set, the stream opens ALSA's `default` device, which PipeWire routes to the default source: the mic chosen in the desktop's sound settings. GNOME, KDE and `pactl set-default-source` all set that same PipeWire default, so nothing here is desktop-specific. A press only fails if not even the default mic can be opened. `install.sh` lists the mics with `tools/microphones.py` (`pactl -f json`), marks Bluetooth and unplugged jacks, offers "None", and writes the choice to `~/.config/npu-voice-to-text/options`.

Why that matters: if the default mic is a Bluetooth headset, opening it switches the headset from music mode (A2DP) to headset mode (HFP). The switch takes about a second, and the mic only delivers digital silence meanwhile. A single word spoken right after pressing the key is gone, and GNOME shows its volume popup because the audio device changes. HFP also records at a low sample rate. The daemon logs a warning when a recording starts with more than 0.3 s of exact zeros.

Safety rules:
- Recordings stop by themselves after 120 s, for example if a key gets stuck.
- **Is there speech at all?** The recording is cut into 30 ms frames. A frame counts as speech if it's louder than 0.002 RMS *and* 3× the noise floor, so the check adapts to mic gain and background noise. The noise floor is the 20th percentile of the clip's frames (exact digital silence left out), or the lowest floor of the last 5 recordings of 1 s or more if that is lower: if the key is held tightly around a single word like "Ja", the whole clip is speech, and its own 20th percentile would put the threshold above the word. Recordings with under 0.15 s of speech (an accidental tap, the click of the key itself, silence) are never sent to Whisper, which would hallucinate on them (see below).
- An earlier version averaged the volume over the *whole* clip. A single quiet word with a second of silence around it then averaged out below the threshold and was silently dropped. The log line `[2.8s audio, 0.4s speech, peak 0.020, …]` shows the measured values for every recording.

### Transcriber

**Whisper on the NPU.** [OpenVINO GenAI](https://github.com/openvinotoolkit/openvino.genai)'s `WhisperPipeline` runs `whisper-large-v3-turbo` in an int8 OpenVINO conversion from [huggingface.co/OpenVINO](https://huggingface.co/OpenVINO):
- **Fixed cost per recording.** NPUs need fixed tensor shapes, and Whisper processes audio in 30 s windows anyway. So every recording is padded to 30 s, and a 2 s clip costs as much as a 30 s one: ~1.6 s with turbo, ~0.3 s with `whisper-small`.
- **Compile cache.** The first load compiles the model for the NPU (minutes). The result is cached in `~/.cache/npu-voice-to-text`.

**Language detection on the CPU (the workaround).** Whisper normally detects the language itself: its first output token *is* the language. On the NPU, OpenVINO GenAI 2026.4 detects it **once** and then reuses that language for every later call. In practice, if your first dictation was German, all later English dictations were *translated* into German. On the CPU the same model detects correctly every time. So, for recordings with at least 1 s of speech:
- `whisper-small` runs on the CPU and generates exactly one token (`max_new_tokens=1`), which is the language. That takes ~0.45 s.
- If that language isn't in `--languages` (`de,en`), `--fallback-language` (`en`) is used.
- The NPU model then runs with `language=<|de|>` or `<|en|>` set explicitly, so its broken auto-detection is never used.
- The chosen language is remembered.

**Short clips keep the last language.** With less than ~1 s of speech (one or two words), Whisper can't tell the language. Tested with single German words ("Ja", "Danke", "Genau", "Tschüss"): both the small model and turbo answer "English" almost every time, turbo once "Japanese". From about three words on (~1 s), detection was correct in every test. So a short clip uses the remembered language from the last longer dictation, and before the first one, the first entry of `--languages`. A one-word reply in the language you were just speaking comes out right. A single word in the *other* language right after a switch doesn't.

`--language xx` skips all of this, but then Whisper translates any other language into `xx`. That's how Whisper behaves when the language is forced.

Comparing how confident the model is with each language forced (de vs en) would be the better test for short clips. OpenVINO GenAI's Whisper pipeline returns no usable scores for that, though (`scores` is always 1.0).

**Hallucination filter.** On silence or noise, Whisper produces phrases it saw a lot in training, such as "Untertitel der Amara.org-Community" or "Thanks for watching!". Exact matches of known ones are dropped. Phrases people also really say ("Vielen Dank.", "Thank you.", "you") are only dropped when the clip had under 0.3 s of speech, i.e. a click or breath that just passed the speech detector.

### Keyboard (text output)

**How text gets into another window.** Wayland doesn't let one app type into another. The sanctioned way is the [RemoteDesktop portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html) (`portal.py`, D-Bus through the pure-Python `jeepney`):
1. `CreateSession`, `SelectDevices` (keyboard only, `persist_mode=2`: until revoked), `Start`. The first time, GNOME shows a dialog and the user allows keyboard access. The portal stores that in its permission store (table `remote-desktop`, keyed by a *restore token*) and returns the token. npu-voice-to-text keeps it in `~/.local/state/npu-voice-to-text/remote-desktop-token` (0600). A token is single-use: each `Start` returns the next one, which replaces it.
2. With the token, `Start` succeeds without a dialog in ~20 ms. So each dictation opens its own session, types, and closes it: a session, and anything GNOME shows for it, only exists while text is typed.
3. Keys are sent with `NotifyKeyboardKeycode` (evdev codes, one D-Bus call per key event). The compositor interprets them with the active layout, exactly like a real keyboard.

The daemon opens one session at startup. On the first start that is when the dialog appears (during `install.sh`). If the user declines, the daemon exits with status 3, and the unit doesn't restart on it (`RestartPreventExitStatus=3`), so the dialog doesn't come back every 5 s. `install.sh` reports it. `uninstall.sh` deletes the permission (`PermissionStore.Delete remote-desktop <token>`), so a reinstall asks again.

**Why key codes, not characters.** The portal also has `NotifyKeyboardKeysym` ("type this character"), but mutter only types keysyms that the active layout has *directly*: in a test on GNOME 50 with a German layout, `ä ö ü ß € @ „ “ –` arrived; `é è ê` (dead keys), `…` (Shift+AltGr level), `ñ ç` and emoji were dropped. libei (`ConnectToEIS`) is no different: it sends key codes on the compositor's keymap. So npu-voice-to-text computes the key codes itself:
1. It reads the active input source from GNOME (`gsettings org.gnome.desktop.input-sources`), e.g. `de`.
2. It compiles that layout with **libxkbcommon** (via `ctypes`), the same library GNOME uses to turn key codes into characters.
3. It walks every key and every shift level and records which key code + Shift/AltGr produces each character. AltGr is whichever key produces `ISO_Level3_Shift` in that keymap: Right Alt normally, another key with options like `lv3:caps_switch`. If options like `compose:ralt` leave no real key for it (xkb's virtual `<LVL3>` key doesn't count, no keyboard has it), AltGr characters count as missing. It only uses the main typing block (plus space, `<>|`, and the Japanese/Brazilian RO and YEN keys). Characters only found on the keypad or on exotic keys like `KEY_EURO` (€ on `us`) count as missing, because many apps ignore those keys. If several keys produce the same character, it prefers fewer modifiers.
4. It also records **dead keys** (`´`, `` ` ``, `^`, …). A character with no key of its own is split with Unicode NFD (`é` → `e` + U+0301) and typed as dead key + base letter. A bare `^` is dead key + space. With key codes this works (the test above typed `é è ê ç …` correctly).
5. **Characters the layout has no key for are replaced**, never pasted: `ä ö ü ß` → `ae oe ue ss`, typographic quotes → `"` / `'`, dashes → `-`, `…` → `...`, `€` → `EUR` (`SUBSTITUTES` in `keyboard.py`), otherwise the letter without its accent (`ñ` → `n`). What is left - emoji, which speech recognition doesn't produce - is left out and logged. On a German layout nothing German or English needs a substitute; on a US layout, German text gets `ae`/`oe`/`ue`/`ss`.

Newlines become spaces (Enter would send chat messages). Events are sent 4 ms apart, which is roughly 1 s for 50 words. A few apps need `--key-delay 8`.

---

## The NPU software stack

```
OpenVINO GenAI (WhisperPipeline)                pip, venv
 └─ OpenVINO runtime + NPU plugin               pip, venv
     └─ Level Zero loader (libze_loader)        distro package
         └─ NPU userspace driver (libze_intel_npu)       ┐ ~/.local/share/npu-voice-to-text/lib
             └─ NPU compiler (libopenvino_intel_npu_compiler) ┘ from intel/linux-npu-driver
                 └─ kernel driver intel_vpu + firmware   kernel / distro
                     └─ /dev/accel/accel0
```

**Why install.sh fetches the driver and compiler itself:** the compiler turns OpenVINO models into NPU code, and it must understand what the OpenVINO version produces. Fedora 44 ships compiler 2025.1, and OpenVINO 2026.4 fails with `Compilation failed … vclAllocatedExecutableCreate2`. Intel publishes matching driver and compiler builds on [linux-npu-driver](https://github.com/intel/linux-npu-driver/releases). v1.38 pairs with OpenVINO 2026.4. The installer:
- unpacks only the two library packages from that release into a private folder;
- verifies the archive's checksum;
- points only this service at them with `LD_LIBRARY_PATH`.

No system files are replaced, and the firmware still comes from your distro. **Upgrading means bumping `openvino-genai` in `pyproject.toml` and `NPU_DRIVER_*` in `install.sh` together.**

How to check the NPU is really used: `cat /sys/bus/pci/devices/0000:00:0b.0/npu_busy_time_us` goes up with every dictation. The PCI address may differ on your machine (`lspci | grep NPU`).

---

## Decisions and rejected alternatives

| Idea | Why not |
|---|---|
| **Handy, Vocalinux, …** (existing apps) | No NPU support. On GNOME Wayland, Handy's text output didn't work in practice. |
| **IBus input method** for output | Needs a system-wide component and its own input source you'd switch to. Electron apps often ignore IBus on Wayland. Simulated keystrokes work wherever a real keyboard works. |
| **ydotool** (used until 0.2) | Needs `ydotoold`, a root daemon whose socket can type anything, set up by hand. The portal does the same with the user's consent and nothing to set up. |
| **Clipboard + paste** (as an output mode, or as the fallback for characters the layout lacks) | Overwrites the clipboard, terminals need a different shortcut, and restoring the clipboard is fragile. Dictation produces German and English text, which every layout can type with a few substitutes (`ä` → `ae` on a US layout). |
| **Keysyms** (`NotifyKeyboardKeysym`) or **libei** | Only reach what the layout has directly - no dead keys, no Shift+AltGr level (see Keyboard). |
| **User in the `input` group** | Lets every program you run read every keystroke. See the key watcher above. |
| **Live text while speaking** (re-transcribe every ~1.6 s, type words two passes agree on, fix the rest with Backspace) | Built and tried. Whisper isn't a streaming model and each NPU pass has a fixed ~1.6 s cost, so words appeared 3–5 s late and were then visibly corrected. Removed in favour of one clean result on release. |
| **Streaming models** (NVIDIA Nemotron 3.5 ASR Streaming, ~0.3 s latency, German and English) | A promising future option, but: no tested NPU build with small chunks, and noticeably more errors than Whisper turbo. Not worth it for now. |
| **Forced `--language de`** | Whisper translates English speech into German. Hence detection limited to de/en. |
| **Whisper's own language detection on the NPU** | Sticks to the first detected language (see Transcriber). |

## Limitations

- Text goes to the window that has focus **at release**, not at press.
- The mic opens on press, so the first ~100 ms may be cut. Start speaking a moment after pressing.
- Caps Lock inverts typed letters (keys are sent, not characters).
- Only one layout group: the first one of the active input source.
- Tested only on one Arrow Lake laptop with Fedora 44.

---

## Roadmap

**Goal: no root component.** The root key watcher is a workaround for something Wayland now offers through a desktop portal: global shortcuts with press *and* release. Typing already moved from ydotool to the RemoteDesktop portal (done, see Keyboard); its `portal.py` (session handling, the portal's Request/Response pattern, `jeepney`) is the base for the GlobalShortcuts portal too.

### 1. Hold key: GlobalShortcuts portal (replaces the root key watcher)

[`org.freedesktop.portal.GlobalShortcuts`](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.GlobalShortcuts.html), supported by GNOME since 48:

1. `CreateSession`, then `BindShortcuts` with one shortcut (`dictate`, preferred trigger `F8`). GNOME asks the user once to confirm the key and remembers it.
2. The signals `Activated` and `Deactivated` (key down / key up) call `Daemon.press()` / `release()` directly. No socket hop, no root service, and no "do-nothing" GNOME shortcut, since the compositor consumes the key itself.

What it removes: `npu-voice-to-text-keywatch`, its systemd unit, `/etc/npu-voice-to-text`, the sudo step in `install.sh` and `tools/remove-keywatch.sh`.

To find out first (a short spike):
- **Does GNOME emit `Deactivated` on key release, every time?** That is the whole point. If GNOME only emits it right after `Activated`, hold-to-talk can't use the portal, but toggle mode still could.
- **App identity.** A host app (not Flatpak) needs an app ID for the portal to remember its shortcuts: via `org.freedesktop.host.portal.Registry`, or from the name of the systemd unit / scope it runs in.
- **Which triggers are allowed.** A lone RIGHTCTRL probably isn't. Those keys would keep the key watcher, or be dropped.
- **Lifecycle.** The session is tied to the D-Bus connection, so it is re-created after a daemon restart (the binding itself persists).

### ~~2. Typing: RemoteDesktop portal (replaces ydotool)~~ - done

Implemented with key codes, see Keyboard. The open questions were answered by testing on GNOME 50: keysyms only reach what the layout has directly (no dead keys, no Shift+AltGr level, nothing off the layout), so key codes computed with libxkbcommon stay; characters the layout lacks are replaced instead of pasted; one short session per dictation means no session stays open between dictations.

### Smaller items

- **If the key watcher stays** (other desktops, modifier keys): run it as a dedicated non-root user in the `input` group (`DynamicUser=` + `SupplementaryGroups=input`) instead of root with `CAP_DAC_OVERRIDE`. It would then offer the socket, and the user daemon would connect to it.
- **Voice detection:** a small VAD model (e.g. Silero VAD on the CPU) instead of the energy detector. It would likely replace the hand-tuned thresholds and most of the hallucination list.
- **Re-check the CPU language-detection workaround** with every OpenVINO GenAI upgrade. If NPU auto-detection gets fixed, the second model can go.
