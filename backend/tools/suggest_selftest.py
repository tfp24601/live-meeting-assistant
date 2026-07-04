"""End-to-end suggestion engine test: fake transcript in, real claude -p out.

Usage:
    .venv/bin/python backend/tools/suggest_selftest.py [--deep]

--deep exercises the web-search tier (slower; expects a source-domain citation).
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import meeting  # noqa: E402
from app.suggestions.engine import SuggestionEngine  # noqa: E402

CONVERSATION = [
    ("system", "So the big question for our department is whether we should be letting students use AI tools in their first-year writing courses at all."),
    ("mic", "I think outright bans just push the use underground. We saw that with calculators and with Wikipedia."),
    ("system", "Sure, but accreditation is going to ask us how we assess authentic student work. The provost wants a policy draft by end of month."),
    ("mic", "There's also a real equity angle. Students who pay for premium AI tools get an advantage over the ones who can't."),
    ("system", "Right, and the faculty senate is split. Some want detection software, some say detectors are snake oil."),
]


class FakeListener:
    def __init__(self):
        self.messages = []

    async def send_text(self, msg: str):
        data = json.loads(msg)
        self.messages.append(data)
        if data.get("type") == "suggestions":
            print(f"\n=== CARDS (elapsed {data.get('elapsed_ms')}ms) ===")
            for c in data["cards"]:
                print(f"  [{c['kind']:^13}] {c['title']}\n{'':16}{c['detail']}")
        elif data.get("type") == "suggest_status":
            print(f"  status -> {data.get('state')}")


async def main() -> None:
    session = meeting.get_or_create("selftest")
    engine = SuggestionEngine(session)
    session.engine = engine
    listener = FakeListener()
    session.listeners.add(listener)

    deep = "--deep" in sys.argv
    for source, text in CONVERSATION:
        session.ingest(source, text)
    print(f"injected {len(CONVERSATION)} lines ({session.transcript_chars()} chars), deep={deep}")

    t0 = time.monotonic()
    engine.poke(force=True, deep=deep)
    # wait until a suggestions message lands or timeout
    for _ in range(120):
        await asyncio.sleep(0.5)
        if any(m.get("type") == "suggestions" for m in listener.messages):
            break
    total = time.monotonic() - t0
    got = [m for m in listener.messages if m.get("type") == "suggestions"]
    if not got:
        print("FAIL: no suggestions arrived (see service logs)")
        sys.exit(1)
    print(f"\nPASS: suggestions in {total:.1f}s wall")


if __name__ == "__main__":
    asyncio.run(main())
