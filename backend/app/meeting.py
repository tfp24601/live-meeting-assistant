"""In-memory meeting sessions.

A MeetingSession joins the two audio sockets (mic + system) into one rolling
transcript, and fans suggestion events out to any listening UI sockets. The
page generates a meeting id per load, so stop/start within one page visit stays
one meeting. Sessions are pruned after an idle hour.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import time
from dataclasses import dataclass, field

from .config import settings

log = logging.getLogger("lma.meeting")

IDLE_PRUNE_S = 3600


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", s.lower()).strip()


def _similarity(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    # Endpointing can split an echo differently than the original, so full
    # containment of a substantial fragment counts as a match.
    if len(na) >= 15 and len(nb) >= 15:
        short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
        if short in long_:
            return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


@dataclass
class Line:
    id: int
    source: str   # "mic" | "system"
    text: str
    at: float     # server wall-clock (epoch seconds)

    def speaker(self) -> str:
        return "You" if self.source == "mic" else "Them"


@dataclass
class MeetingSession:
    meeting_id: str
    lines: list[Line] = field(default_factory=list)
    listeners: set = field(default_factory=set)   # suggestion websockets
    last_activity: float = field(default_factory=time.time)
    last_cards: list = field(default_factory=list)
    engine: object = None  # set by main on creation (SuggestionEngine)
    mode: str = "online"   # "online" | "inperson"
    party: str = "one"     # "one" (one-on-one) | "multi" (group)
    _next_id: int = 0

    def ingest(self, source: str, text: str) -> tuple[str, int | None, int | None]:
        """Add a transcript line, suppressing speaker echo between the streams.

        Speakers leaking into the mic produce a near-duplicate of a "system"
        line on the "mic" stream. Whichever copy arrives second reveals the
        echo:
        - mic arrives second  -> the mic line IS the echo: suppress it
        - system arrives second -> the earlier mic line WAS the echo: accept
          the system line and retract the mic line

        Returns (action, new_line_id, retract_line_id) where action is one of
        "accept" | "suppress" | "accept_retract".
        """
        self.last_activity = time.time()
        now = time.time()
        retract_id: int | None = None

        # In-person mode has one physical stream (mic), so speaker echo between
        # streams cannot exist; the similarity check would only eat legitimate
        # lines (e.g. someone agreeing verbatim).
        if settings.echo_suppress and self.mode != "inperson":
            match = self._find_recent_match(source, text, now)
            if match is not None:
                if source == "mic":
                    log.info("echo suppressed (mic dup of system line %d): %.60s", match.id, text)
                    return ("suppress", None, None)
                # source == "system": the earlier mic line was the echo
                self.lines.remove(match)
                retract_id = match.id
                log.info("echo retracted (mic line %d was dup of new system line): %.60s",
                         match.id, match.text)

        line = Line(id=self._next_id, source=source, text=text, at=now)
        self._next_id += 1
        self.lines.append(line)
        if self.engine is not None:
            self.engine.poke()
        action = "accept_retract" if retract_id is not None else "accept"
        return (action, line.id, retract_id)

    def _find_recent_match(self, source: str, text: str, now: float) -> Line | None:
        other = "system" if source == "mic" else "mic"
        window = settings.echo_window_s
        best: Line | None = None
        best_score = 0.0
        for line in reversed(self.lines):
            if now - line.at > window:
                break
            if line.source != other:
                continue
            score = _similarity(line.text, text)
            if score > best_score:
                best, best_score = line, score
        if best is not None and best_score >= settings.echo_similarity:
            return best
        return None

    def transcript_chars(self) -> int:
        return sum(len(l.text) for l in self.lines)

    async def broadcast(self, payload: dict) -> None:
        msg = json.dumps(payload)
        dead = []
        for ws in list(self.listeners):
            try:
                await ws.send_text(msg)
            except Exception:  # noqa: BLE001 - a gone socket must not break the rest
                dead.append(ws)
        for ws in dead:
            self.listeners.discard(ws)


_sessions: dict[str, MeetingSession] = {}


def get_or_create(meeting_id: str) -> MeetingSession:
    _prune()
    s = _sessions.get(meeting_id)
    if s is None:
        s = MeetingSession(meeting_id=meeting_id)
        _sessions[meeting_id] = s
        log.info("meeting session created: %s", meeting_id)
    return s


def _prune() -> None:
    now = time.time()
    for mid, s in list(_sessions.items()):
        if now - s.last_activity > IDLE_PRUNE_S and not s.listeners:
            stop = getattr(s.engine, "stop", None)
            if stop:
                stop()
            del _sessions[mid]
            log.info("meeting session pruned: %s", mid)
