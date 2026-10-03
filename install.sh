#!/usr/bin/env bash
# install.sh — interactive setup for talkrec. Asks a few questions (speech
# backend, model size, auto-paste, whether to start it now), then does the
# mechanical work:
#   1. creates a venv in this repo and installs the chosen backend's Python
#      dependencies into it
#   2. symlinks bin/talkrec and bin/talkrec-toggle into ~/.local/bin
#   3. symlinks the systemd user unit into ~/.config/systemd/user
#   4. writes your answers to ~/.config/talkrec/talkrec.env
#   5. checks for the external system tools talkrec shells out to
# It never overwrites existing files it didn't create, and is safe to run
# again anytime (re-running keeps your existing config unless you say
# otherwise). Pass --yes (or run non-interactively, e.g. piped/CI) to accept
# every default without being asked.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${TALKREC_BIN_DIR:-$HOME/.local/bin}"
SYSTEMD_USER_DIR="${TALKREC_SYSTEMD_DIR:-$HOME/.config/systemd/user}"
CONFIG_DIR="${TALKREC_CONFIG_DIR:-$HOME/.config/talkrec}"
CONFIG_FILE="$CONFIG_DIR/talkrec.env"

say() { printf '%s\n' "$*"; }

ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --yes|-y) ASSUME_YES=1 ;;
  esac
done
if [ "$ASSUME_YES" = 1 ] || [ ! -t 0 ] || [ ! -t 1 ]; then
  INTERACTIVE=0
else
  INTERACTIVE=1
fi

# ask NAME PROMPT DEFAULT — prints $NAME via nameref; skips the prompt (uses
# DEFAULT) when running non-interactively.
ask() {
  local __var="$1" __prompt="$2" __default="$3" __reply
  if [ "$INTERACTIVE" = 0 ]; then
    printf -v "$__var" '%s' "$__default"
    return
  fi
  read -r -p "$__prompt [$__default]: " __reply || true
  printf -v "$__var" '%s' "${__reply:-$__default}"
}

# ask_yn NAME PROMPT DEFAULT(y|n)
ask_yn() {
  local __var="$1" __prompt="$2" __default="$3" __reply
  if [ "$INTERACTIVE" = 0 ]; then
    printf -v "$__var" '%s' "$__default"
    return
  fi
  read -r -p "$__prompt [$([ "$__default" = y ] && echo Y/n || echo y/N)]: " __reply || true
  __reply="${__reply:-$__default}"
  case "$__reply" in
    y|Y|yes|Yes) printf -v "$__var" 'y' ;;
    *) printf -v "$__var" 'n' ;;
  esac
}

say "talkrec setup"
say "============="
[ "$INTERACTIVE" = 0 ] && say "(non-interactive: using defaults for every question)"
say ""

# ── Configuration ------------------------------------------------------------
if [ -f "$CONFIG_FILE" ]; then
  say "Found existing config at $CONFIG_FILE:"
  sed 's/^/    /' "$CONFIG_FILE"
  KEEP_CONFIG=y
  ask_yn KEEP_CONFIG "Keep it as-is?" y
else
  KEEP_CONFIG=n
fi

if [ "$KEEP_CONFIG" = n ]; then
  say ""
  say "Which speech-to-text backend?"
  say "  1) whisper — OpenAI's whisper on torch; many languages, several model"
  say "               sizes, big install (torch is several GB) (default)"
  say "  2) whistle — small 17 MB CPU model by Cactus Compute; 7 languages"
  say "               (en de fr es it nl pl), small install, no torch"
  ask BACKEND_CHOICE "Choice" "1"
  case "$BACKEND_CHOICE" in
    2) TALKREC_BACKEND=whistle ;;
    *) TALKREC_BACKEND=whisper ;;
  esac
fi

if [ "$KEEP_CONFIG" = n ] && [ "$TALKREC_BACKEND" = whisper ]; then
  say ""
  say "Which whisper model?"
  say "  1) tiny   — fastest, least accurate"
  say "  2) base   — balanced (default)"
  say "  3) small  — more accurate, slower, bigger download"
  say "  4) medium — most accurate, slowest, biggest download"
  ask MODEL_CHOICE "Choice" "2"
  case "$MODEL_CHOICE" in
    1) TALKREC_MODEL=tiny ;;
    3) TALKREC_MODEL=small ;;
    4) TALKREC_MODEL=medium ;;
    *) TALKREC_MODEL=base ;;
  esac
fi

if [ "$KEEP_CONFIG" = n ]; then
  say ""
  ask_yn AUTO_PASTE_YN "Auto-paste (Ctrl+V) after copying to clipboard, instead of clipboard-only?" n
  [ "$AUTO_PASTE_YN" = y ] && TALKREC_AUTO_PASTE=1 || TALKREC_AUTO_PASTE=0

  mkdir -p "$CONFIG_DIR"
  {
    echo "TALKREC_BACKEND=$TALKREC_BACKEND"
    [ "$TALKREC_BACKEND" = whisper ] && echo "TALKREC_MODEL=$TALKREC_MODEL"
    # cap whisper's CPU threads so a transcription doesn't take over every core
    [ "$TALKREC_BACKEND" = whisper ] && echo "TALKREC_THREADS=4"
    echo "TALKREC_AUTO_PASTE=$TALKREC_AUTO_PASTE"
  } > "$CONFIG_FILE"
  say "wrote:  $CONFIG_FILE"
else
  say "ok:    keeping existing config"
  # configs written before the backend question existed mean whisper
  TALKREC_BACKEND="$(sed -n 's/^TALKREC_BACKEND=//p' "$CONFIG_FILE" | tail -n 1)"
  TALKREC_BACKEND="${TALKREC_BACKEND:-whisper}"
fi
case "$TALKREC_BACKEND" in
  whisper|whistle) ;;
  *) say "error: unknown TALKREC_BACKEND '$TALKREC_BACKEND' in $CONFIG_FILE (expected whisper or whistle)"
     exit 1 ;;
esac

# 1. venv + Python deps -------------------------------------------------------
say ""
if [ ! -x "$REPO_DIR/venv/bin/python3" ]; then
  say "creating venv at $REPO_DIR/venv ..."
  python3 -m venv "$REPO_DIR/venv"
else
  say "ok:    venv already exists at $REPO_DIR/venv"
fi
if [ "$TALKREC_BACKEND" = whisper ]; then
  say "installing/updating Python dependencies for whisper (openai-whisper pulls"
  say "in torch — first run downloads several GB; pip's progress shows below) ..."
else
  say "installing/updating Python dependencies for whistle (small; the ~17 MB"
  say "model itself downloads the first time talkrec starts) ..."
fi
"$REPO_DIR/venv/bin/pip" install --quiet --upgrade pip
"$REPO_DIR/venv/bin/pip" install -r "$REPO_DIR/requirements-$TALKREC_BACKEND.txt"
say "ok:    dependencies installed"

# 2. symlink the launcher scripts --------------------------------------------
say ""
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

# 5. start it now? ------------------------------------------------------------
say ""
ask_yn START_NOW "Start talkrec now and enable it at login?" y
if [ "$START_NOW" = y ]; then
  systemctl --user enable --now talkrec.service
  say "ok:    talkrec.service enabled and started"
else
  say "skip:  not starting. Run this when ready:"
  say "         systemctl --user enable --now talkrec.service"
fi

say ""
say "Setup complete. Last step — bind a global hotkey to toggle recording:"
say "On KDE: System Settings > Shortcuts > Custom Shortcuts > New > Global"
say "Shortcut > Command/URL, command: $BIN_DIR/talkrec-toggle"
say ""
say "Then click the tray icon or press your hotkey to record; press again to"
say "stop and transcribe. Text lands on your clipboard."
