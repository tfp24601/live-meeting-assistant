"""Quick offline check that the whisper engine works on this machine/GPU.

Decodes any audio file to 16 kHz mono via ffmpeg, then either transcribes the
whole thing or feeds it through StreamingTranscriber in chunks to exercise the
VAD/endpointing path.

Usage:
    .venv/bin/python backend/tools/selftest.py <audio-file> [--stream]
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/ on path

from app.config import settings  # noqa: E402
from app.transcription.whisper_engine import StreamingTranscriber, get_model, transcribe  # noqa: E402


def decode(path: str) -> np.ndarray:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar",
         str(settings.sample_rate), "-f", "s16le", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(out, dtype=np.int16).astype(np.float32) / 32768.0


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    stream = "--stream" in sys.argv
    if not args:
        print(__doc__)
        sys.exit(1)
    audio = decode(args[0])
    print(f"decoded {len(audio)/settings.sample_rate:.1f}s of audio")

    t = time.time()
    if get_model() is None:
        print("ERROR: model failed to load")
        sys.exit(2)
    print(f"model loaded in {time.time()-t:.1f}s")

    if stream:
        st = StreamingTranscriber(source="test")
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
        chunk = settings.sample_rate * 2 // 12  # ~80 ms of int16 bytes
        t = time.time()
        for i in range(0, len(pcm), chunk):
            for utt in st.add_pcm(pcm[i:i + chunk]):
                print(f"  [{utt.t0:6.2f}-{utt.t1:6.2f}] {transcribe(utt.audio)}")
        final = st.flush_final()
        if final is not None:
            print(f"  [{final.t0:6.2f}-{final.t1:6.2f}] {transcribe(final.audio)}")
        print(f"streamed in {time.time()-t:.1f}s")
    else:
        t = time.time()
        text = transcribe(audio)
        print(f"transcribed in {time.time()-t:.1f}s:\n{text}")


if __name__ == "__main__":
    main()
