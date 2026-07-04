"""Turns the rolling transcript into talking-point suggestion cards.

Trigger discipline (speed without waste):
- debounced: a run starts only after SUGGEST_MIN_NEW_CHARS of fresh transcript
  AND SUGGEST_MIN_INTERVAL_S since the last run started
- single-flight: at most one `claude -p` alive per meeting; text arriving
  mid-run is picked up by an immediate follow-up run, never queued behind more
- "Suggest now" from the UI forces a run regardless of the debounce
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import time

from ..config import settings
from ..llm import LLMError, generate, provider_name, supports_web_search
from ..sources_config import label_map
from . import retrieval

log = logging.getLogger("lma.engine")

KINDS = {"talking_point", "question", "fact", "idea"}


def build_system_prompt() -> str:
    name = settings.user_name
    about = f"\nAbout {name}: {settings.user_context}" if settings.user_context else ""
    labels = label_map()
    example = next(iter(labels.values()), "source")
    knowledge = (
        f'\nThe prompt may include a "Knowledge base" section: passages retrieved from '
        f"{name}'s own material ({', '.join(labels.values())}). When a passage is genuinely "
        f"relevant, prefer grounding a card in it — {name} likes referencing their own "
        f"published work — and end that card's detail with the source in parentheses, "
        f'e.g. "({example})". Ignore passages that don\'t fit the moment.\n'
    ) if labels else "\n"
    return f"""You are a live meeting copilot for {name}. You see a rolling transcript: lines from "You" are {name} speaking; lines from "Them" are other participants. The transcription is automated, so expect occasional mis-heard words — infer intent.{about}

Your job: give {name} 2-4 immediately usable suggestion cards for THIS moment of the conversation, favoring whatever was said most recently.

Card kinds:
- "talking_point": a concrete point {name} could make next, grounded in what was just said
- "question": a sharp question {name} could ask to move the conversation forward
- "fact": a relevant, well-known fact or definition that adds substance (only if you are confident it is true)
- "idea": a connection, reframing, or next step worth proposing
{knowledge}
Rules:
- Reply with ONLY a JSON array of cards, each: {{"kind": "...", "title": "...", "detail": "..."}}. No prose, no markdown fences.
- title: at most 8 words. detail: one sentence, at most 30 words, written so {name} can say it nearly verbatim.
- Be specific to this conversation — never generic filler like "ask clarifying questions".
- Do not invent facts about the specific people or companies in the meeting.
- Do not repeat topics listed under "Recently suggested"."""

DEEP_ADDENDUM = """

For THIS run you also have the WebSearch tool. Search the web when current, verifiable facts would strengthen a card (recent news, versions, prices, dates, statistics). Prefer 1-2 targeted searches over many. End the detail of any web-sourced card with the source domain in parentheses, e.g. "(proxmox.com)". Your final reply must still be ONLY the JSON card array."""


def _parse_cards(text: str) -> list[dict]:
    """Extract a list of {kind,title,detail} cards from model output."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    start, end = t.find("["), t.rfind("]")
    if start >= 0 and end > start:
        t = t[start:end + 1]
    try:
        raw = json.loads(t)
    except json.JSONDecodeError:
        log.warning("unparseable cards, salvaging as note: %.120s", text)
        return [{"kind": "idea", "title": "Suggestion", "detail": text.strip()[:200]}]
    cards = []
    for item in raw if isinstance(raw, list) else [raw]:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        kind = str(item.get("kind", "idea")).strip().lower()
        cards.append({
            "kind": kind if kind in KINDS else "idea",
            "title": str(item["title"]).strip()[:80],
            "detail": str(item.get("detail", "")).strip()[:300],
        })
    return cards[:4]


class SuggestionEngine:
    def __init__(self, session):
        self.session = session
        self._consumed = 0          # transcript chars already covered by a run
        self._last_run_t = 0.0      # monotonic time the last run STARTED
        self._force = False
        self._deep_next = False     # next run may use web search
        self._runner: asyncio.Task | None = None
        self._recent_titles: list[str] = []
        self._stopped = False

    # -- triggers ------------------------------------------------------------
    def poke(self, force: bool = False, deep: bool = False) -> None:
        if self._stopped or not settings.suggest_enabled:
            return
        if deep:
            self._deep_next = True
        if force:
            self._force = True
        if self._runner is None or self._runner.done():
            self._runner = asyncio.create_task(self._run_when_ready())

    def stop(self) -> None:
        self._stopped = True
        if self._runner and not self._runner.done():
            self._runner.cancel()

    def _new_chars(self) -> int:
        return self.session.transcript_chars() - self._consumed

    # -- the single-flight loop ----------------------------------------------
    async def _run_when_ready(self) -> None:
        try:
            while not self._stopped:
                if not self._force:
                    since = time.monotonic() - self._last_run_t
                    wait_interval = settings.suggest_min_interval_s - since
                    if self._new_chars() < settings.suggest_min_new_chars and wait_interval <= 0:
                        return  # not enough new material; a future poke restarts us
                    if wait_interval > 0:
                        await asyncio.sleep(min(wait_interval, 2.0))
                        continue
                    if self._new_chars() < settings.suggest_min_new_chars:
                        return
                self._force = False
                await self._run_once()
                if self._new_chars() < settings.suggest_min_new_chars and not self._force:
                    return
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - engine must never take the app down
            log.exception("suggestion loop crashed; will restart on next poke")

    async def _run_once(self) -> None:
        deep = self._deep_next
        self._deep_next = False
        if deep and not supports_web_search():
            log.warning("deep dive requested but provider %s lacks web search; running normal",
                        provider_name())
            await self.session.broadcast({
                "type": "suggest_status", "state": "error",
                "msg": f"web deep dive isn't supported by the {provider_name()} provider; ran a normal suggestion instead",
            })
            deep = False
        self._last_run_t = time.monotonic()
        snapshot = list(self.session.lines)
        self._consumed = sum(len(l.text) for l in snapshot)
        knowledge = await self._retrieve(snapshot)
        prompt = self._build_prompt(snapshot, knowledge)
        await self.session.broadcast({
            "type": "suggest_status",
            "state": "researching" if deep else "thinking",
        })
        t0 = time.monotonic()
        system_prompt = build_system_prompt() + (DEEP_ADDENDUM if deep else "")
        try:
            result, meta = await generate(system_prompt, prompt,
                                          fast=not deep, web_search=deep)
        except LLMError as e:
            log.error("LLM call failed (%s): %s", provider_name(), e)
            await self.session.broadcast({
                "type": "suggest_status", "state": "error", "msg": str(e)[:200],
            })
            return
        cards = _parse_cards(result)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        for c in cards:
            self._recent_titles.append(c["title"])
        self._recent_titles = self._recent_titles[-12:]
        payload = {
            "type": "suggestions",
            "cards": cards,
            "at": dt.datetime.now().strftime("%H:%M:%S"),
            "elapsed_ms": elapsed_ms,
            "deep": deep,
        }
        self.session.last_cards = cards
        log.info("suggestions: %d cards in %dms (model %s)", len(cards), elapsed_ms, meta.get("model"))
        await self.session.broadcast(payload)
        await self.session.broadcast({"type": "suggest_status", "state": "idle", "last_ms": elapsed_ms})

    # -- retrieval -------------------------------------------------------------
    async def _retrieve(self, lines) -> str:
        """Fetch knowledge-base context for the current moment (fail-open)."""
        if not settings.rag_enabled or not lines:
            return ""
        query_parts: list[str] = []
        used = 0
        for line in reversed(lines):
            if used + len(line.text) > settings.rag_query_chars:
                break
            query_parts.append(line.text)
            used += len(line.text)
        query = " ".join(reversed(query_parts))
        t0 = time.monotonic()
        hits = await asyncio.to_thread(retrieval.search, query)
        if hits:
            log.info("retrieval: %d hits in %dms (top %.2f)",
                     len(hits), int((time.monotonic() - t0) * 1000), hits[0]["score"])
        return retrieval.format_context(hits)

    # -- prompt --------------------------------------------------------------
    def _build_prompt(self, lines, knowledge: str = "") -> str:
        parts = []
        budget = settings.suggest_transcript_chars
        used = 0
        tail: list[str] = []
        for line in reversed(lines):
            s = f"{line.speaker()}: {line.text}"
            if used + len(s) > budget:
                break
            tail.append(s)
            used += len(s)
        tail.reverse()
        mode = "in-person (one mic hears the room; lines were voice-matched)" \
            if getattr(self.session, "mode", "online") == "inperson" else "online"
        party = "group meeting — 'Them' is multiple different people" \
            if getattr(self.session, "party", "one") == "multi" else "one-on-one"
        parts.append(f"Meeting context: {mode}, {party}.")
        parts.append("Live meeting transcript (most recent last):")
        parts.append("\n".join(tail) if tail else "(no speech captured yet)")
        if knowledge:
            parts.append(f"\nKnowledge base ({settings.user_name}'s own published/documented material):")
            parts.append(knowledge)
        if self._recent_titles:
            parts.append("\nRecently suggested (avoid repeats):")
            parts.append("\n".join(f"- {t}" for t in self._recent_titles))
        parts.append("\nGenerate the JSON card array now.")
        return "\n".join(parts)
