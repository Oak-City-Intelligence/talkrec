# Security

## Scope

talkrec is a local voice-to-text tool. It makes no outbound network
connections — transcription runs entirely on-device via a local whisper
model. It listens on nothing network-facing; the only IPC is a Unix domain
socket at `/run/user/<uid>/talkrec.sock` used to relay the record/stop toggle
from `talkrec-toggle` (or your hotkey binding) to the running daemon. That
directory is created by systemd-logind as mode 0700 owned by you, so other
local users can't reach the socket at all (unlike a fixed name under shared
`/tmp`, which earlier drafts of this used and which any local user could
connect to or squat).

A few things worth knowing:

- **The socket accepts any process running as you.** Any process under your
  own UID can send it a `TOGGLE` command — that's the same trust boundary as
  any other process you run, no privilege escalation, but not authenticated
  beyond that.
- **Recorded audio is transcribed in memory**, never written to disk. The
  resulting text is placed on your system clipboard and optionally
  auto-pasted (off by default — see `TALKREC_AUTO_PASTE` in the README).
  Clipboard contents persist until something else overwrites them; if the
  transcribed text is sensitive, clear your clipboard manually afterward.
- **ydotool** requires its own daemon/permission setup (usually a udev rule
  or being in the `input` group) to simulate keypresses. talkrec only uses
  it for the optional auto-paste feature and degrades to clipboard-only if
  the daemon isn't reachable.
- **Logs** go to `~/.cache/talkrec/talkrec.log` (rotated, 1MB x 3 backups)
  and include transcribed text at INFO level. If your transcriptions are
  sensitive, treat that log file as sensitive too.

## Reporting

This is a pre-1.0 personal tool. If you find a security issue, please open a
private security advisory on the repository (or contact the maintainer
directly) instead of filing a public issue first.
