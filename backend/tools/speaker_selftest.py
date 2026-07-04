"""Speaker verification selftest: same-voice vs different-voice separation.

Usage:
    .venv/bin/python backend/tools/speaker_selftest.py <voiceA.(wav|mp3)> <voiceB.(wav|mp3)>

A and B must be DIFFERENT people. Checks:
  - same-speaker similarity (two halves of A) is high
  - cross-speaker similarity (A vs B) is low
  - the two are separated by a healthy margin around the threshold
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.speaker import verify as speaker  # noqa: E402


def decode(path: str) -> np.ndarray:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar",
         str(settings.sample_rate), "-f", "s16le", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(out, dtype=np.int16).astype(np.float32) / 32768.0


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    a = decode(sys.argv[1])
    b = decode(sys.argv[2])
    half = len(a) // 2

    ref = speaker.build_reference(a[:half])
    if ref is None:
        print("FAIL: could not build reference (model missing?)")
        sys.exit(2)
    same = speaker.embed(a[half:])
    diff = speaker.embed(b)
    if same is None or diff is None:
        print("FAIL: embedding failed")
        sys.exit(2)

    sim_same = float(np.dot(ref, same))
    sim_diff = float(np.dot(ref, diff))
    thr = settings.speaker_threshold
    print(f"same-speaker similarity: {sim_same:.3f}  (want > {thr})")
    print(f"diff-speaker similarity: {sim_diff:.3f}  (want < {thr})")
    print(f"threshold: {thr}, margin: {sim_same - sim_diff:.3f}")

    ok = sim_same > thr and sim_diff < thr and (sim_same - sim_diff) > 0.15
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
