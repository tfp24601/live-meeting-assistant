"""Watchdog mechanism test — no GPU needed (injects a fake blocking call).

Usage:
    python3 backend/tools/watchdog_selftest.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.transcription import whisper_engine as we  # noqa: E402

FAILS = 0


def check(name, got, want):
    global FAILS
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    FAILS += 0 if ok else 1


async def main() -> None:
    settings.whisper_watchdog_s = 0.5
    died = []

    def fake_die(elapsed):
        died.append(elapsed)

    # fast call passes through untouched
    out = await we.transcribe_watched(None, _fn=lambda a: "quick result", _die=fake_die)
    check("fast call returns", out, "quick result")
    check("no watchdog trigger", len(died), 0)

    # wedged call trips the watchdog
    out = await we.transcribe_watched(None, _fn=lambda a: time.sleep(3) or "late", _die=fake_die)
    check("wedged call trips die()", len(died), 1)
    check("wedged call yields empty", out, "")
    check("elapsed ≈ watchdog budget", round(died[0], 1) >= 0.5, True)

    # watchdog disabled -> waits it out
    settings.whisper_watchdog_s = 0
    out = await we.transcribe_watched(None, _fn=lambda a: "slow but fine", _die=fake_die)
    check("disabled watchdog passes through", out, "slow but fine")
    check("still one trigger total", len(died), 1)

    print("\nALL PASS" if FAILS == 0 else f"\n{FAILS} FAILURES")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    asyncio.run(main())
