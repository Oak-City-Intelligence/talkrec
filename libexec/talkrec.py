#!/usr/bin/env python3
"""talkrec — voice-to-text daemon with tray icon and hotkey toggle.

Click the tray icon or press a hotkey (bound via your desktop's global
shortcuts to talkrec-toggle, which sends a TOGGLE command over a local
socket): first press starts recording, second press stops + transcribes with
the configured backend (whisper or whistle, see talkrec_backends.py), copies
the text to your clipboard, dings.

Requires: PyQt6, sounddevice, numpy, plus openai-whisper or cactus-needle
depending on TALKREC_BACKEND; ydotool, wl-copy, paplay.
"""

import sys
import os

# Bound the math libraries' thread pools before numpy/torch get imported and
# size them to the whole machine. Explicit settings in the environment win.
THREADS_DEFAULT = "4"
_threads_env = os.environ.get("TALKREC_THREADS", THREADS_DEFAULT).strip()
if _threads_env.isdigit() and int(_threads_env) > 0:
    for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(_var, _threads_env)

import shutil
import subprocess
import time
import logging
import logging.handlers
import numpy as np
import sounddevice as sd
from talkrec_backends import make_backend, parse_language, parse_threads
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QObject
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor

# ─── Config ───────────────────────────────────────────────
SAMPLE_RATE = 16000          # native rate for both backends
CHANNELS = 1
DURATION_LIMIT = 120         # seconds max per recording
BACKEND = os.environ.get("TALKREC_BACKEND", "whisper").strip().lower()  # whisper | whistle
LANGUAGE = parse_language(os.environ.get("TALKREC_LANGUAGE", "en"))  # "auto" to detect
THREADS = parse_threads(os.environ.get("TALKREC_THREADS", THREADS_DEFAULT))  # 0 = no cap
WHISPER_MODEL = os.environ.get("TALKREC_MODEL", "base")  # tiny/base/small/...
WHISPER_DEVICE = os.environ.get("TALKREC_DEVICE", "cpu")  # cpu keeps this safe
                                                           # to run alongside GPU-bound work
AUTO_PASTE = os.environ.get("TALKREC_AUTO_PASTE", "0") == "1"  # 0: clipboard only

YDOTOOL_BIN = shutil.which("ydotool") or "/usr/bin/ydotool"
YDOTOOL_SOCKET = f"/run/user/{os.getuid()}/.ydotool_socket"
WL_COPY_BIN = shutil.which("wl-copy") or "wl-copy"
PAPLAY_BIN = shutil.which("paplay") or "paplay"

SINGLE_INSTANCE_SERVER_NAME = f"/run/user/{os.getuid()}/talkrec.sock"  # user-owned
# runtime dir (mode 700), not shared/world-connectable /tmp
ERROR_SENTINEL = "__TALKREC_ERR__"  # prefix for transcription errors
TOGGLE_COMMAND = b"TOGGLE"

# ─── Logging (rotating, max 1MB, keep 3) ─────────────────
LOG_PATH = os.path.expanduser("~/.cache/talkrec/talkrec.log")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1*1024*1024, backupCount=3),
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("talkrec")

# ─── Audio buffer ─────────────────────────────────────────
audio_chunks = []
is_recording = False


def find_input_device():
    """Prefer the PipeWire device — it resamples to whatever rate we ask
    for. Raw ALSA hw devices only support their fixed native rate and
    raise 'Invalid sample rate' when opened at whisper's required 16kHz."""
    for dev in sd.query_devices():
        if dev["name"] == "pipewire" and dev["max_input_channels"] > 0:
            return dev["index"]
    return sd.default.device[0]


# ─── Ydotool daemon helper ────────────────────────────────
def ensure_ydotool_daemon():
    """Start ydotool daemon if not already running."""
    if os.path.exists(YDOTOOL_SOCKET):
        return True
    try:
        subprocess.run(["systemctl", "--user", "start", "ydotool"],
                       timeout=3, check=True,
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    except (subprocess.SubprocessError, FileNotFoundError):
        try:
            subprocess.Popen(
                [YDOTOOL_BIN, "daemon"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
    for _ in range(20):
        if os.path.exists(YDOTOOL_SOCKET):
            return True
        time.sleep(0.1)
    log.warning("ydotool daemon not available — auto-paste disabled")
    return False


def simulate_ctrl_v():
    """Send Ctrl+V via ydotool."""
    if not os.path.exists(YDOTOOL_SOCKET):
        return False
    try:
        subprocess.run([YDOTOOL_BIN, "key", "down", "ctrl"], timeout=1, check=True, capture_output=True)
        subprocess.run([YDOTOOL_BIN, "key", "down", "v"], timeout=1, check=True, capture_output=True)
        time.sleep(0.05)
        subprocess.run([YDOTOOL_BIN, "key", "up", "v"], timeout=1, check=True, capture_output=True)
        subprocess.run([YDOTOOL_BIN, "key", "up", "ctrl"], timeout=1, check=True, capture_output=True)
        return True
    except Exception:
        return False


# ─── Clipboard ────────────────────────────────────────────
def set_clipboard(text):
    """Put text on clipboard via wl-copy."""
    proc = subprocess.Popen(
        [WL_COPY_BIN],
        stdin=subprocess.PIPE,
    )
    proc.communicate(input=text.encode())


def play_sound(sound_name):
    """Play a short notification sound via paplay."""
    sound_map = {
        "ding": "/usr/share/sounds/freedesktop/stereo/service-login.oga",
        "record-start": "/usr/share/sounds/freedesktop/stereo/message.oga",
        "record-stop": "/usr/share/sounds/freedesktop/stereo/complete.oga",
        "error": "/usr/share/sounds/freedesktop/stereo/dialog-error.oga",
    }
    sound_path = sound_map.get(sound_name)
    if not sound_path or not os.path.exists(sound_path):
        sound_path = "/usr/share/sounds/freedesktop/stereo/message.oga"
    try:
        subprocess.run([PAPLAY_BIN, sound_path],
                       timeout=2, capture_output=True)
    except Exception:
        pass


# ─── Icon drawing (mic with state color) ──────────────────
def draw_icon(state: str):
    """Draw a mic icon. Gray idle, red recording, yellow processing."""
    size = 48
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    color = {
        "recording": QColor(220, 50, 50),
        "processing": QColor(200, 160, 30),
    }.get(state, QColor(100, 100, 100))
    painter.setBrush(color)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(4, 4, size - 8, size - 8)

    painter.setBrush(QColor(255, 255, 255, 230))
    pen = painter.pen()
    pen.setColor(QColor(255, 255, 255, 230))
    pen.setWidth(2)
    painter.setPen(pen)

    cx, cy = size // 2, size // 2 - 2
    mw, mh = 10, 16
    painter.drawRoundedRect(cx - mw // 2, cy - mh // 2, mw, mh, 4, 4)
    painter.drawLine(cx - 7, cy + mh // 2, cx + 7, cy + mh // 2)
    painter.drawLine(cx, cy + mh // 2, cx, cy + mh // 2 + 4)
    painter.drawEllipse(cx - 5, cy + mh // 2, 10, 8)

    painter.end()
    return QIcon(pixmap)


# ─── Audio capture thread ─────────────────────────────────
class RecorderThread(QThread):
    """Record audio, return numpy array in memory."""
    finished = pyqtSignal(object)  # numpy array or None

    def run(self):
        global audio_chunks
        audio_chunks = []

        def callback(indata, frames, time_info, status):
            if status:
                log.warning("audio status: %s", status)
            audio_chunks.append(indata.copy())

        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                device=find_input_device(),
                callback=callback,
                blocksize=int(SAMPLE_RATE * 0.05),  # 50ms blocks
            ):
                elapsed = 0.0
                while elapsed < DURATION_LIMIT and is_recording:
                    self.msleep(100)
                    elapsed += 0.1

            if audio_chunks:
                # sounddevice yields (frames, channels) even for mono; whisper
                # expects flat 1D samples — a stray channel axis makes its
                # internal broadcasting balloon memory without bound.
                audio = np.concatenate(audio_chunks, axis=0).astype(np.float32).flatten()
                self.finished.emit(audio)
            else:
                self.finished.emit(None)
        except Exception as e:
            log.error("record error: %s", e)
            self.finished.emit(None)
        finally:
            audio_chunks = []


# ─── Transcription thread ─────────────────────────────────
class TranscribeThread(QThread):
    """Transcribe audio array with the loaded backend."""
    finished = pyqtSignal(str)  # text or error sentinel

    def __init__(self, audio_array, backend):
        super().__init__()
        self.audio_array = audio_array
        self.backend = backend

    def run(self):
        try:
            self.finished.emit(self.backend.transcribe(self.audio_array))
        except Exception as e:
            log.error("transcribe error: %s", e)
            self.finished.emit(f"{ERROR_SENTINEL}{e}")


# ─── Tray icon + daemon ─────────────────────────────────────
class TalkRecTray(QSystemTrayIcon):
    def __init__(self, backend):
        super().__init__()
        self.state = "idle"  # idle | recording | processing
        self.recorder = None
        self.transcriber = None
        self.backend = backend

        menu = QMenu()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(self.quit)
        self.setContextMenu(menu)

        self.activated.connect(self.on_icon_click)
        self._set_state("idle")
        self.show()
        log.info("Ready. Click tray icon or press your hotkey (talkrec-toggle).")

    def _set_state(self, state):
        self.state = state
        self.setIcon(draw_icon(state))
        tooltips = {
            "idle": "TalkRec — click to record",
            "recording": "Recording... click to stop",
            "processing": "Transcribing...",
        }
        self.setToolTip(tooltips[state])

    def on_icon_click(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Context:
            return
        if reason == QSystemTrayIcon.ActivationReason.MiddleClick:
            return
        self.toggle()

    def toggle(self):
        log.info("toggle: state=%s", self.state)
        if self.state == "idle":
            self.start_recording()
        elif self.state == "recording":
            self.stop_recording()
        elif self.state == "processing":
            log.info("still transcribing, ignoring toggle")

    def start_recording(self):
        global is_recording
        is_recording = True
        self._set_state("recording")

        self.recorder = RecorderThread()
        self.recorder.finished.connect(self.on_recording_done)
        self.recorder.start()
        log.info("recording started")
        play_sound("record-start")

    def stop_recording(self):
        global is_recording
        is_recording = False
        log.info("stopping recording")
        play_sound("record-stop")

    def on_recording_done(self, audio_array):
        self._set_state("processing")
        if audio_array is None:
            log.warning("no audio captured")
            play_sound("error")
            self._set_state("idle")
            return
        log.info("recording done, %.1fs audio", len(audio_array)/SAMPLE_RATE)
        self.transcriber = TranscribeThread(audio_array, self.backend)
        self.transcriber.finished.connect(self.on_transcription_done)
        self.transcriber.start()

    def on_transcription_done(self, text: str):
        if text.startswith(ERROR_SENTINEL):
            err_msg = text[len(ERROR_SENTINEL):]
            log.error("transcription error: %s", err_msg)
            play_sound("error")
            self._set_state("idle")
            return
        if not text:
            # silence or nothing intelligible — leave the clipboard alone
            log.info("no speech detected")
            play_sound("error")
            self._set_state("idle")
            return
        log.info("transcription: %s", text)
        self._copy_and_paste(text)
        play_sound("ding")
        self._set_state("idle")

    def _copy_and_paste(self, text: str):
        set_clipboard(text)
        if not AUTO_PASTE:
            return
        pasted = simulate_ctrl_v()
        if not pasted:
            log.warning("auto-paste unavailable, text left on clipboard: %s", text[:80])

    def quit(self):
        if self.state == "recording":
            self.stop_recording()
            if self.recorder and self.recorder.isRunning():
                self.recorder.wait(2000)
        self.hide()
        QApplication.quit()


# ─── Boot ─────────────────────────────────────────────────
if __name__ == "__main__":
    # ── Single-instance check ──────────────────────────────
    try:
        test_socket = QLocalSocket()
        test_socket.connectToServer(SINGLE_INSTANCE_SERVER_NAME)
        if test_socket.waitForConnected(200):
            log.info("another instance already running - exiting")
            test_socket.close()
            sys.exit(0)
        test_socket.close()
    except Exception:
        pass

    os.environ["XDG_CURRENT_DESKTOP"] = os.environ.get("XDG_CURRENT_DESKTOP", "KDE")

    app = QApplication(sys.argv)
    app.setApplicationName("talkrec")
    app.setQuitOnLastWindowClosed(False)
    app.setDesktopFileName("talkrec")

    # Single-instance + toggle-command server
    QLocalServer.removeServer(SINGLE_INSTANCE_SERVER_NAME)
    server = QLocalServer()
    if not server.listen(SINGLE_INSTANCE_SERVER_NAME):
        log.error("failed to start single-instance server: %s", server.errorString())
    app.setProperty("instance_server", server)
    log.info("single-instance server listening on %s", SINGLE_INSTANCE_SERVER_NAME)

    # Ensure ydotool daemon
    ydotool_ok = ensure_ydotool_daemon()
    if ydotool_ok:
        log.info("ydotool daemon OK")

    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.error("system tray not available!")

    backend = make_backend(BACKEND, model_name=WHISPER_MODEL, device=WHISPER_DEVICE,
                           language=LANGUAGE, threads=THREADS)

    # Loading the model can take a while (first run also downloads the
    # weights) — show a placeholder icon so there's visible proof-of-life
    # instead of an empty tray with no explanation.
    loading_icon = QSystemTrayIcon(draw_icon("processing"))
    loading_icon.setToolTip(f"TalkRec — loading {backend.describe()} model...")
    loading_icon.show()

    log.info("Loading %s (language=%s, threads=%s) ...", backend.describe(),
             LANGUAGE or "auto", THREADS or "uncapped")
    backend.load()

    loading_icon.hide()
    loading_icon.deleteLater()
    daemon = TalkRecTray(backend)
    play_sound("ding")
    log.info("Ready — click the tray icon or press your bound hotkey")

    # Wire incoming socket connections to toggle
    client_sockets = []

    def handle_new_connection():
        sock = server.nextPendingConnection()
        if sock is None:
            return
        client_sockets.append(sock)

        def on_ready_read():
            data = bytes(sock.readAll())
            if data.strip() == TOGGLE_COMMAND:
                daemon.toggle()
            sock.disconnectFromServer()

        sock.readyRead.connect(on_ready_read)
        sock.disconnected.connect(lambda: client_sockets.remove(sock) if sock in client_sockets else None)

    server.newConnection.connect(handle_new_connection)

    sys.exit(app.exec())
