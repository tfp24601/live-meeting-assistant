"""Echo-suppression unit test (no whisper/claude needed).

Usage:
    python3 backend/tools/echo_selftest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import meeting  # noqa: E402

FAILS = 0


def check(name: str, got, want) -> None:
    global FAILS
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    if not ok:
        FAILS += 1


def main() -> None:
    s = meeting.get_or_create("echo-test")

    print("system first, echoed mic second -> mic suppressed")
    a, _, _ = s.ingest("system", "We saw that with calculators and with Wikipedia back then.")
    check("system accepted", a, "accept")
    a, _, r = s.ingest("mic", "We saw that with calculators and with Wikipedia back then")
    check("mic echo suppressed", a, "suppress")

    print("mic echo lands first, real system line second -> mic retracted")
    a, mic_id, _ = s.ingest("mic", "The provost wants a policy draft by end of month.")
    check("mic accepted (arrives first)", a, "accept")
    a, _, retract = s.ingest("system", "Sure, the provost wants a policy draft by end of month.")
    check("system accepted with retract", a, "accept_retract")
    check("retracts the earlier mic line", retract, mic_id)

    print("fragment echo (endpointing split) -> suppressed via containment")
    s.ingest("system", "Detection software is snake oil according to half the faculty senate members.")
    a, _, _ = s.ingest("mic", "detection software is snake oil")
    check("fragment suppressed", a, "suppress")

    print("legitimately different lines pass through")
    a, _, _ = s.ingest("mic", "I think outright bans just push the use underground.")
    check("distinct mic line accepted", a, "accept")
    a, _, _ = s.ingest("system", "What does accreditation actually require from us?")
    check("distinct system line accepted", a, "accept")

    kept = [(l.source, l.text[:30]) for l in s.lines]
    print(f"\nfinal transcript ({len(kept)} lines): {kept}")
    if FAILS:
        print(f"\n{FAILS} FAILURES")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    main()
