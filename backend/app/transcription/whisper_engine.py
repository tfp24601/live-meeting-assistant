"""Streaming-ish transcription on top of faster-whisper.

faster-whisper is batch (it transcribes a buffer, not a true stream), so we do
*utterance-level* streaming: a cheap energy VAD watches the incoming PCM and,
when it sees enough trailing silence after speech, it closes the utterance and
hands that audio buffer off to be transcribed. This keeps GPU work proportional
to speech and emits a clean transcript line per utterance.

The audio path:
  browser -> 16 kHz mono Int16 PCM over WS -> StreamingTranscriber.add_pcm()
  add_pcm() is cheap (numpy energy math only) and returns completed utterances
  as float32 arrays. The WebSocket handler transcribes those off the event loop.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

import numpy as np

from ..config import settings

log = logging.getLogger("lma.whisper")

# One shared model for the whole process; GPU calls are serialized with a lock
# so two sources (mic + system) don't trample each other on the device.
_model = None
_model_lock = threading.Lock()
_transcribe_lock = threading.Lock()


def get_model():
    """Lazily load and cache the WhisperModel. Returns None if unavailable."""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            log.warning("faster-whisper not installed; transcription disabled")
            return None
        log.info(
            "loading whisper model=%s device=%s compute=%s",
            settings.whisper_model, settings.whisper_device, settings.whisper_compute_type,
        )
        _model = WhisperModel(
            settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
        )
        log.info("whisper model loaded")
        return _model


def transcribe(audio: np.ndarray) -> str:
    """Transcribe a float32 mono 16 kHz buffer to text. Blocking (GPU/CPU)."""
    model = get_model()
    if model is None:
        return ""
    language = settings.whisper_language or None
    if settings.whisper_model.endswith(".en"):
        language = "en"
    with _transcribe_lock:
        segments, _info = model.transcribe(
            audio,
            language=language,
            beam_size=settings.whisper_beam_size,
            vad_filter=False,            # we already did endpointing ourselves
            condition_on_previous_text=False,  # avoid hallucinated carry-over between utterances
        )
        return " ".join(s.text.strip() for s in segments).strip()


@dataclass
class Utterance:
    audio: np.ndarray   # float32 mono 16 kHz
    t0: float           # seconds since stream start
    t1: float


class StreamingTranscriber:
    """Buffers incoming PCM and emits completed utterances on a silence gap.

    Key invariant: **all** audio since the last flush is kept and transcribed
    together. The energy check only decides *when* to flush (a speech-then-pause
    boundary), never *what* to keep -- so misjudging the threshold can at worst
    make us flush late (a longer chunk), never drop words.

    Not thread-safe; use one instance per WebSocket connection.
    """

    def __init__(self, source: str = "mic"):
        self.source = source
        sr = settings.sample_rate
        self._sr = sr
        self._buf = np.zeros(0, dtype=np.float32)
        self._consumed_samples = 0      # samples flushed so far (for timestamps)
        self._has_speech = False        # any speech seen since last flush?
        self._silence_ms = 0.0          # trailing run of below-threshold audio
        self._preroll = int(settings.preroll_ms * sr / 1000)
        self._min_utt = int(settings.min_utt_ms * sr / 1000)
        self._max_utt = int(settings.max_utt_ms * sr / 1000)
        self._endpoint_silence = settings.endpoint_silence_ms
        self._threshold = settings.vad_threshold

    def add_pcm(self, pcm_bytes: bytes) -> list[Utterance]:
        """Feed raw Int16 PCM bytes; return any utterances that just completed."""
        if not pcm_bytes:
            return []
        chunk = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        if chunk.size == 0:
            return []
        self._buf = np.concatenate([self._buf, chunk])
        chunk_ms = chunk.size * 1000.0 / self._sr
        rms = float(np.sqrt(np.mean(chunk * chunk)))

        if rms >= self._threshold:
            self._has_speech = True
            self._silence_ms = 0.0
        elif self._has_speech:
            self._silence_ms += chunk_ms

        # If we've not heard any speech yet, don't let leading silence pile up:
        # keep only a short preroll tail so a word onset isn't clipped.
        if not self._has_speech and len(self._buf) > self._preroll * 3:
            drop = len(self._buf) - self._preroll
            self._buf = self._buf[drop:].copy()
            self._consumed_samples += drop
            return []

        ready: list[Utterance] = []
        endpointed = (
            self._has_speech
            and self._silence_ms >= self._endpoint_silence
            and len(self._buf) >= self._min_utt
        )
        capped = self._has_speech and len(self._buf) >= self._max_utt
        if endpointed or capped:
            u = self._flush()
            if u is not None:
                ready.append(u)
        return ready

    def _flush(self) -> Utterance | None:
        """Emit everything buffered so far as one utterance; reset the buffer."""
        audio = self._buf
        n = len(audio)
        t0 = self._consumed_samples / self._sr
        t1 = (self._consumed_samples + n) / self._sr
        self._consumed_samples += n
        self._buf = np.zeros(0, dtype=np.float32)
        self._has_speech = False
        self._silence_ms = 0.0
        if n < self._min_utt:
            return None
        return Utterance(audio=audio.copy(), t0=t0, t1=t1)

    def flush_final(self) -> Utterance | None:
        """Flush any buffered speech (e.g. on disconnect)."""
        if not self._has_speech:
            return None
        return self._flush()
