"""Speech-to-text backends for talkrec.

Each backend has the same two-call shape: load() once at boot (slow — may
download weights on first run), then transcribe(audio) for every recording,
where audio is 16 kHz mono float32 samples and the return value is plain text.
The backend library is imported inside load(), so a whistle-only install never
needs torch/openai-whisper and vice versa.

Kept free of Qt/sounddevice so the backends can be exercised on their own.
"""

import os
import logging
import numpy as np

log = logging.getLogger("talkrec")

SAMPLE_RATE = 16000
BACKENDS = ("whisper", "whistle")


def parse_language(value):
    """'' or 'auto' means let the model detect the language."""
    value = (value or "").strip().lower()
    return None if value in ("", "auto") else value


def parse_threads(value):
    """Thread cap from TALKREC_THREADS; 0 (or garbage) means no cap."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        log.warning("ignoring invalid TALKREC_THREADS=%r", value)
        return 0


# ─── whisper (openai-whisper on torch) ──────────────────────
class WhisperBackend:
    def __init__(self, model_name="base", device="cpu", language="en", threads=4):
        self.model_name = model_name
        self.device = device
        self.language = language
        self.threads = threads
        self.model = None

    def describe(self):
        return f"whisper {self.model_name} on {self.device}"

    def load(self):
        import torch
        import whisper

        if self.threads:
            # torch defaults to one intra-op thread per core, which on a big
            # machine pins every core at 100% for the length of a transcription.
            torch.set_num_threads(self.threads)
            try:
                # whisper runs eagerly, so the inter-op pool would sit idle anyway
                torch.set_num_interop_threads(1)
            except RuntimeError:
                pass  # already set (only allowed once per process)
        self.model = whisper.load_model(self.model_name, device=self.device)
        self.model.to(self.device)

    def transcribe(self, audio):
        result = self.model.transcribe(
            audio,
            language=self.language,
            task="transcribe",
            fp16=False,
        )
        return result["text"].strip()


# ─── whistle (cactus-needle) ─────────────────────────────────
class WhistleBackend:
    CHUNK_SECONDS = 30      # whistle rejects anything longer
    SEARCH_SECONDS = 10     # look for a quiet cut point in the last N s of a chunk
    FRAME_SECONDS = 0.02    # energy is measured over 20 ms frames
    SILENCE_PEAK = 1e-3     # ~ -60 dBFS; quieter than this is treated as no speech

    def __init__(self, language="en"):
        self.language = language
        self.model = None

    def describe(self):
        return "whistle"

    def load(self):
        # needle reports anonymous usage events by default; talkrec promises
        # nothing leaves the machine, so opt out unless the user opted back in.
        os.environ.setdefault("NEEDLE_TELEMETRY", "0")
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        import needle
        try:
            from needle.agent.whistle import LANGUAGES
        except ImportError:  # moved in some future release; let whistle complain
            LANGUAGES = None

        if LANGUAGES and self.language is not None and self.language not in LANGUAGES:
            raise ValueError(f"whistle doesn't support language {self.language!r} "
                             f"(supported: {' '.join(LANGUAGES)}, or auto)")
        # The engine sizes its own worker pool (up to 12) from the online CPU
        # count and has no thread setting. Don't cap it with CPU affinity: it
        # still starts the full pool, and squeezed onto fewer CPUs it stalls
        # (an 11 s clip went from under 1 s to ~40 s pinned to 4 CPUs).
        self.model = needle.Whistle()

    def transcribe(self, audio):
        texts = []
        for chunk in split_audio(audio, SAMPLE_RATE, self.CHUNK_SECONDS,
                                 self.SEARCH_SECONDS, self.FRAME_SECONDS):
            if is_silent(chunk, self.SILENCE_PEAK):
                continue
            text = self.model.transcribe(chunk, language=self.language)["text"].strip()
            if text:
                texts.append(text)
        return " ".join(texts)


def is_silent(audio, peak=1e-3):
    return audio.size == 0 or float(np.max(np.abs(audio))) < peak


def split_audio(audio, rate=SAMPLE_RATE, max_seconds=30, search_seconds=10, frame_seconds=0.02):
    """Split audio into chunks of at most max_seconds.

    Each cut lands in the middle of the quietest frame within the last
    search_seconds of the chunk, so it falls in a pause between words rather
    than through one, as far as the recording allows.
    """
    max_len = int(max_seconds * rate)
    search = int(search_seconds * rate)
    frame = int(frame_seconds * rate)
    chunks = []
    start = 0
    while len(audio) - start > max_len:
        window_start = start + max_len - search
        window = audio[window_start:start + max_len]
        frames = window[:len(window) // frame * frame].reshape(-1, frame)
        quietest = int(np.argmin(np.square(frames, dtype=np.float64).sum(axis=1)))
        cut = window_start + quietest * frame + frame // 2
        chunks.append(audio[start:cut])
        start = cut
    chunks.append(audio[start:])
    return chunks


def make_backend(name, model_name="base", device="cpu", language="en", threads=4):
    if name == "whisper":
        return WhisperBackend(model_name, device, language, threads)
    if name == "whistle":
        return WhistleBackend(language)  # threads: whisper only, see load()
    raise ValueError(f"unknown TALKREC_BACKEND {name!r} (expected one of: {', '.join(BACKENDS)})")
