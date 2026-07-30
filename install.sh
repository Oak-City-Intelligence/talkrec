#!/usr/bin/env bash
# install.sh — idempotent setup for talkrec. Prints everything it does; safe
# to run again anytime. It does four things:
#   1. creates a venv in this repo and installs Python dependencies into it
#   2. symlinks bin/talkrec and bin/talkrec-toggle into ~/.local/bin
#   3. symlinks the systemd user unit into ~/.config/systemd/user
#   4. checks for the external system tools talkrec shells out to
# It never overwrites existing files it didn't create.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${TALKREC_BIN_DIR:-$HOME/.local/bin}"
SYSTEMD_USER_DIR="${TALKREC_SYSTEMD_DIR:-$HOME/.config/systemd/user}"

say() { printf '%s\n' "$*"; }

# 1. venv + Python deps -------------------------------------------------------
if [ ! -x "$REPO_DIR/venv/bin/python3" ]; then
  say "creating venv at $REPO_DIR/venv ..."
  python3 -m venv "$REPO_DIR/venv"
else
  say "ok:    venv already exists at $REPO_DIR/venv"
fi
say "installing/updating Python dependencies (openai-whisper pulls in torch —"
say "first run downloads several GB; you'll see pip's normal progress below) ..."
"$REPO_DIR/venv/bin/pip" install --quiet --upgrade pip
"$REPO_DIR/venv/bin/pip" install -r "$REPO_DIR/requirements.txt"
say "ok:    dependencies installed"

# 2. symlink the launcher scripts --------------------------------------------
mkdir -p "$BIN_DIR"
for f in "$REPO_DIR"/bin/*; do
  name="$(basename "$f")"
  target="$BIN_DIR/$name"
  if [ -L "$target" ] && [ "$(readlink "$target")" = "$f" ]; then
    say "ok:    $target already points at this repo"
  elif [ -e "$target" ]; then
    say "skip:  $target exists and isn't our symlink — leaving it alone"
  else
    ln -s "$f" "$target"
    say "linked: $target -> $f"
  fi
done

# 3. symlink the systemd user unit --------------------------------------------
mkdir -p "$SYSTEMD_USER_DIR"
unit_target="$SYSTEMD_USER_DIR/talkrec.service"
unit_src="$REPO_DIR/systemd/talkrec.service"
if [ -L "$unit_target" ] && [ "$(readlink "$unit_target")" = "$unit_src" ]; then
  say "ok:    $unit_target already points at this repo"
elif [ -e "$unit_target" ]; then
  say "skip:  $unit_target exists and isn't our symlink — leaving it alone"
else
  ln -s "$unit_src" "$unit_target"
  say "linked: $unit_target -> $unit_src"
fi
systemctl --user daemon-reload
say "ok:    systemd user units reloaded"

# 4. check external tools ----------------------------------------------------
say ""
say "Checking system tools talkrec shells out to (not pip-installable):"
for tool in ydotool wl-copy paplay; do
  if command -v "$tool" >/dev/null 2>&1; then
    say "  ok:      $tool found"
  else
    say "  MISSING: $tool — install it via your package manager"
  fi
done

say ""
say "Setup complete. Next steps:"
say "  1. Start it now and enable it at login:"
say "       systemctl --user enable --now talkrec.service"
say "  2. Bind a global hotkey to toggle recording. On KDE: System Settings >"
say "     Shortcuts > Custom Shortcuts > New > Global Shortcut > Command/URL,"
say "     command: $BIN_DIR/talkrec-toggle"
say "  3. Click the tray icon or press your hotkey to record; press again to"
say "     stop and transcribe. Text lands on your clipboard."
