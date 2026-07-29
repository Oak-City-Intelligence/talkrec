# Contributing

Thanks for taking a look. talkrec is a **pre-1.0 personal tool** that is
published so it can improve in the open. Contributions are welcome, but the
maintainer daily-drives this exact setup, so anything that breaks the core
workflow moves slowly.

## Ground rules

- Keep the daemon single-file (`libexec/talkrec.py`) — this is a small tool,
  not a framework. Resist splitting into modules until there's an actual
  reason to.
- No new runtime dependencies without a good reason. PyQt6 + sounddevice +
  numpy + openai-whisper is already a lot for what this does.
- Audio device handling must keep going through PipeWire's virtual device
  (see the comment in `find_input_device()`) — raw ALSA hw devices only
  support their native sample rate and will fail at whisper's required
  16kHz.
- Anything that touches a Qt signal/slot must not let exceptions escape:
  PyQt6's default behavior on an uncaught exception inside a slot is a hard
  `abort()`, not a Python traceback. Test tray-icon interactions directly,
  don't assume "it imports fine" means "it won't crash on click."

## Handy checks

```sh
python3 -m py_compile libexec/talkrec.py bin/talkrec-toggle
bash -n install.sh bin/talkrec
```

## Good first contributions

- Configurable recording duration cap (`DURATION_LIMIT`) via environment
  variable, matching the pattern used for `TALKREC_MODEL`/`TALKREC_DEVICE`.
- Language selection instead of the hardcoded `language="en"`.
- A non-KDE hotkey-binding doc section (GNOME, Sway, etc.) — the daemon and
  `talkrec-toggle` are already desktop-agnostic, only the README's binding
  instructions are KDE-specific.
