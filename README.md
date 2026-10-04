# talkrec

A tiny click-to-record voice-to-text tool: click the tray icon (or press a
hotkey), speak, click/press again, and the transcription lands on your
clipboard a couple seconds later. Runs speech-to-text locally on CPU — no
cloud API, nothing leaves your machine (the only network traffic is the
one-time model download).

**Status: personal tool, pre-1.0, evolving.** Built for one operator's daily
setup and published as-is. Tested on Arch Linux, KDE Plasma 6, Wayland,
PipeWire. Should work on other Linux desktops with a working system tray, but
the tray-click and hotkey-binding steps below are KDE-specific.

## How it works

```
tray click / hotkey → toggle
  idle -> recording:  starts capturing audio via PipeWire
  recording -> idle:  stops capture, transcribes locally (CPU),
                       copies text to clipboard, plays a done sound
```

The daemon and the hotkey are separate processes talking over a local Unix
socket (`/run/user/$UID/talkrec.sock`) — `talkrec-toggle` is a small
stdlib-only script so a hotkey press doesn't pay Python/Qt startup cost. The
daemon itself is a normal PyQt6 tray-icon app; only one instance ever runs
(a second launch detects the running one and exits).

## Backends

talkrec can transcribe with either of two engines, picked by the install
wizard or by `TALKREC_BACKEND`:

- **whisper** (default) — [openai-whisper](https://github.com/openai/whisper)
  on torch. Many languages and several model sizes (`TALKREC_MODEL`). The
  install is big: torch alone is several GB.
- **whistle** — a small (~17 MB) CPU speech model by Cactus Compute, run
  through the [`cactus-needle`](https://pypi.org/project/cactus-needle/)
  package (Apache-2.0). English, German, French, Spanish, Italian, Dutch and
  Polish. No torch; the engine library and model download on first start.
  - Whistle only accepts up to 30 s of audio per call, so talkrec splits
    longer recordings into chunks of at most 30 s, cutting each one at the
    quietest 20 ms stretch in its last 10 s (so cuts land in pauses, not
    mid-word) and joins the texts with a space.
  - `cactus-needle` sends anonymous usage telemetry by default. talkrec turns
    it off (`NEEDLE_TELEMETRY=0`) unless you've set that variable yourself.

Whisper runs with a bounded number of CPU threads (`TALKREC_THREADS`,
default 4, torch's thread count) so a transcription doesn't take over every
core. Whistle has no thread setting: it uses up to ~12 cores in a short burst
(well under a second for a typical dictation) and then goes idle.

## Requirements

- Linux with PipeWire (used for audio capture and for its `pipewire` virtual
  ALSA device, which resamples to whatever rate whisper wants — a raw ALSA
  hardware device usually can't and will fail to open at 16kHz)
- A working system tray (any freedesktop StatusNotifierItem host)
- Python 3.10+
- System tools: `ydotool` (optional, for auto-paste), `wl-copy`, `paplay`
- Python packages: `requirements.txt` (PyQt6, sounddevice, numpy) plus the
  backend's own file — `requirements-whisper.txt` (openai-whisper, which
  pulls in torch) or `requirements-whistle.txt` (cactus-needle)

## Install

```sh
git clone <this-repo> && cd talkrec
./install.sh
```

`install.sh` is an interactive wizard. It asks a few questions up front —
which backend (whisper or whistle), which whisper model size (whisper only),
whether to auto-paste, whether to start the service now — writes your answers to `~/.config/talkrec/talkrec.env`, then
does the mechanical work:

1. creates a venv in the repo (`./venv`) and installs the Python
   dependencies for the backend you picked (for whisper, openai-whisper
   pulls in torch — first run downloads several GB, and pip's normal
   progress bar stays visible so it doesn't look stuck)
2. symlinks `bin/talkrec` and `bin/talkrec-toggle` into `~/.local/bin`
3. symlinks `systemd/talkrec.service` into `~/.config/systemd/user`
4. checks that `ydotool`/`wl-copy`/`paplay` are on your `PATH`
5. optionally starts it right there (`systemctl --user enable --now`)

It's safe to re-run anytime — re-running offers to keep your existing
config instead of re-asking, and never overwrites a file it didn't create.
Pass `--yes` (or run it piped/non-interactively) to accept every default
(whisper, `base` model) without being asked anything.

To switch backends later, re-run `install.sh` and say no to "keep existing
config" — it installs the other backend's dependencies into the same venv.

### Binding a hotkey (KDE)

talkrec has no built-in global hotkey grab (that's a desktop-level concern,
and KDE Wayland doesn't let plain apps grab keys globally anyway). Instead:

**System Settings → Shortcuts → Custom Shortcuts** → right-click → **New →
Global Shortcut → Command/URL**, set the trigger to whatever key combo you
want, and the command to `~/.local/bin/talkrec-toggle`.

## Configuration

`install.sh` writes `TALKREC_BACKEND`, `TALKREC_MODEL` and `TALKREC_THREADS`
(whisper only), and `TALKREC_AUTO_PASTE` to
`~/.config/talkrec/talkrec.env` from your answers to its prompts — both
`bin/talkrec` and the systemd unit read that file automatically. To change
your answer later, either edit that file directly, or re-run `install.sh`
and say no to "keep existing config" when asked.

All variables (can also be exported before running manually, or added to
the systemd unit via `Environment=`):

| variable | default | meaning |
|----------|---------|---------|
| `TALKREC_BACKEND` | `whisper` | `whisper` or `whistle` (see [Backends](#backends)); the venv needs that backend's requirements installed |
| `TALKREC_LANGUAGE` | `en` | spoken language code, or `auto` to let the model detect it; whistle supports `en de fr es it nl pl` |
| `TALKREC_THREADS` | `4` | max CPU threads whisper uses for transcription; `0` removes the cap; ignored by whistle |
| `TALKREC_MODEL` | `base` | whisper model size (`tiny`, `base`, `small`, ...) — bigger is slower but more accurate; ignored by whistle |
| `TALKREC_DEVICE` | `cpu` | inference device passed to whisper; ignored by whistle |
| `TALKREC_AUTO_PASTE` | `0` | set to `1` to also send Ctrl+V via ydotool after copying, instead of clipboard-only |

## Known limitations

- No visual confirmation beyond the tray icon color and sound cues — no
  toast/notification popups, because that requires a running
  `org.freedesktop.Notifications` service, which not every minimal KDE setup
  has running. Sound is the reliable channel here.
- 120-second hard cap per recording (`DURATION_LIMIT` in `libexec/talkrec.py`).
- One language per setup (`TALKREC_LANGUAGE`); `auto` detection works but
  is less reliable on short clips.
- Whistle's chunking can still split a word if someone talks without a
  pause for the last 10 s of a 30 s chunk.
- Whistle's CPU use can't be capped: it has no thread setting and uses up
  to ~12 cores for each (short) transcription.
- ydotool auto-paste needs the ydotool daemon socket; if it's not available,
  talkrec silently falls back to clipboard-only (which is also the default
  behavior regardless, via `TALKREC_AUTO_PASTE=0`).

## Uninstall

```sh
systemctl --user disable --now talkrec.service
rm ~/.config/systemd/user/talkrec.service ~/.local/bin/talkrec ~/.local/bin/talkrec-toggle
```
(only removes the symlinks `install.sh` created; the repo/venv itself is
untouched — delete the cloned directory yourself if you're done with it)
