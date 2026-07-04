"""Speaker verification for in-person mode: is this utterance the enrolled user?

Binary enrollment verification, not N-way diarization: one enrolled reference
embedding; each utterance's ECAPA embedding is compared by cosine similarity —
at/above threshold -> "You", below -> everyone else in the room.

Blocking (GPU/CPU) — callers use asyncio.to_thread. Fail-open: if the model or
enrollment is unavailable, classify() returns None and the caller keeps the
default label.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import numpy as np

from ..config import settings

log = logging.getLogger("lma.speaker")

REPO_ROOT = Path(__file__).resolve().parents[3]
ENROLL_DIR = REPO_ROOT / "data" / "enrollment"
ENROLL_EMB = ENROLL_DIR / "user.npy"
ENROLL_META = ENROLL_DIR / "user.json"
MODEL_DIR = REPO_ROOT / "backend" / "models" / "speechbrain-ecapa"
MODEL_SOURCE = "speechbrain/spkrec-ecapa-voxceleb"
SAMPLE_RATE = 16000
# Embed enrollment audio in windows this long; the reference is their mean.
ENROLL_WINDOW_S = 4.0

_lock = threading.Lock()
_model = None
_reference: np.ndarray | None = None
_reference_mtime: float | None = None


def _get_model():
    global _model
    if _model is not None:
        return _model
    with _lock:
        if _model is not None:
            return _model
        try:
            import torch
            from speechbrain.inference.speaker import EncoderClassifier
        except ImportError as e:
            log.warning("speechbrain unavailable (%s); speaker verification disabled", e)
            return None
        device = settings.speaker_device
        if device == "cuda":
            try:
                if not torch.cuda.is_available():
                    device = "cpu"
            except Exception:  # noqa: BLE001
                device = "cpu"
        log.info("loading ECAPA speaker model on %s", device)
        _model = EncoderClassifier.from_hparams(
            source=MODEL_SOURCE,
            savedir=str(MODEL_DIR),
            run_opts={"device": device},
        )
        log.info("speaker model loaded")
        return _model


def embed(audio_f32_16k: np.ndarray) -> np.ndarray | None:
    """ECAPA embedding (L2-normalized) for a mono float32 16 kHz buffer."""
    model = _get_model()
    if model is None or audio_f32_16k.size < SAMPLE_RATE // 2:
        return None
    import torch
    wav = torch.from_numpy(np.ascontiguousarray(audio_f32_16k)).unsqueeze(0)
    with torch.no_grad():
        emb = model.encode_batch(wav).squeeze().detach().cpu().numpy().astype(np.float32)
    norm = float(np.linalg.norm(emb))
    return emb / norm if norm > 0 else None


def build_reference(speech: np.ndarray) -> np.ndarray | None:
    """Average windowed embeddings over the enrollment speech buffer."""
    window = int(ENROLL_WINDOW_S * SAMPLE_RATE)
    embs = []
    for start in range(0, max(1, len(speech) - window // 2), window):
        e = embed(speech[start:start + window])
        if e is not None:
            embs.append(e)
    if not embs:
        return None
    ref = np.mean(np.stack(embs), axis=0)
    norm = float(np.linalg.norm(ref))
    return ref / norm if norm > 0 else None


def save_enrollment(reference: np.ndarray, speech_seconds: float) -> None:
    ENROLL_DIR.mkdir(parents=True, exist_ok=True)
    np.save(ENROLL_EMB, reference)
    ENROLL_META.write_text(json.dumps({
        "speech_seconds": round(speech_seconds, 1),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": MODEL_SOURCE,
    }))
    _invalidate_reference()
    log.info("enrollment saved (%.1fs speech)", speech_seconds)


def _invalidate_reference() -> None:
    global _reference, _reference_mtime
    _reference = None
    _reference_mtime = None


def enrollment_status() -> dict:
    if not ENROLL_EMB.exists():
        return {"enrolled": False}
    try:
        meta = json.loads(ENROLL_META.read_text()) if ENROLL_META.exists() else {}
    except json.JSONDecodeError:
        meta = {}
    return {"enrolled": True, **meta}


def _get_reference() -> np.ndarray | None:
    global _reference, _reference_mtime
    if not ENROLL_EMB.exists():
        return None
    mtime = ENROLL_EMB.stat().st_mtime
    if _reference is None or _reference_mtime != mtime:
        _reference = np.load(ENROLL_EMB)
        _reference_mtime = mtime
    return _reference


def classify(audio_f32_16k: np.ndarray) -> tuple[str, float] | None:
    """("you"|"other", similarity) vs the enrolled reference; None if unavailable."""
    ref = _get_reference()
    if ref is None:
        return None
    emb = embed(audio_f32_16k)
    if emb is None:
        return None
    sim = float(np.dot(ref, emb))
    return ("you" if sim >= settings.speaker_threshold else "other", sim)


def warmup() -> None:
    """Preload the model at boot if an enrollment exists (in-person mode likely)."""
    if ENROLL_EMB.exists():
        _get_model()
