"""Query the user's knowledge base (qdrant) for the suggestion prompt.

Blocking qdrant/fastembed calls — the engine runs them via asyncio.to_thread.
Fail-open by design: any retrieval problem returns [] and the suggestion run
proceeds without knowledge context.
"""
from __future__ import annotations

import logging
import threading
import time

from ..config import settings
from ..sources_config import disabled_ids, label_map

log = logging.getLogger("lma.retrieval")

EMBED_MODEL = "BAAI/bge-base-en-v1.5"   # must match ingestion (backend/ingest/common.py)

_lock = threading.Lock()
_client = None
_embedder = None
_available: bool | None = None   # None = never tried; drives transition logging
_last_error = ""
_RETRY_S = 30


def _mark(ok: bool, error: str = "") -> None:
    """Log only on state TRANSITIONS so gaps are visible in the journal."""
    global _available, _last_error
    if ok and _available is not True:
        log.info("retrieval ready (%s @ %s)%s", settings.rag_collection,
                 settings.qdrant_url, " — RECOVERED" if _available is False else "")
    elif not ok and _available is not False:
        log.warning("retrieval UNAVAILABLE (suggestions continue without RAG): %s", error)
    _available = ok
    _last_error = error


def status() -> str:
    if not settings.rag_enabled:
        return "disabled"
    if _available is True:
        return "ready"
    if _available is False:
        return f"unavailable: {_last_error[:120]}"
    return "not yet connected"


def warmup() -> None:
    """Connect at boot; on failure keep retrying in the background until the
    knowledge store answers (e.g. hosts booted in the wrong order)."""
    if not settings.rag_enabled:
        return
    try:
        _probe()
        _mark(True)
        return
    except Exception as e:  # noqa: BLE001
        _mark(False, str(e))

    def _retry_loop() -> None:
        while _available is not True and settings.rag_enabled:
            time.sleep(_RETRY_S)
            try:
                _probe()
                _mark(True)
            except Exception as e:  # noqa: BLE001
                _mark(False, str(e))  # transition-logged only, no spam

    threading.Thread(target=_retry_loop, name="rag-retry", daemon=True).start()


def _probe() -> None:
    """A real round-trip, not just client construction."""
    client, _ = _ensure()
    client.get_collection(settings.rag_collection)


def _ensure():
    global _client, _embedder
    with _lock:
        if _client is None:
            from qdrant_client import QdrantClient
            _client = QdrantClient(url=settings.qdrant_url, timeout=5)
        if _embedder is None:
            from fastembed import TextEmbedding
            _embedder = TextEmbedding(model_name=EMBED_MODEL)
    return _client, _embedder


def search(query: str) -> list[dict]:
    """Top-k knowledge chunks for `query`. Blocking; call in a thread."""
    if not settings.rag_enabled or not query.strip():
        return []
    try:
        client, embedder = _ensure()
        vector = list(embedder.embed([query]))[0].tolist()
        # Disabled ("ignored") sources are filtered at query time — their
        # points stay in qdrant so re-enabling is instant.
        query_filter = None
        ignored = disabled_ids()
        if ignored:
            from qdrant_client import models as qm
            query_filter = qm.Filter(must_not=[
                qm.FieldCondition(key="source", match=qm.MatchAny(any=ignored)),
            ])
        hits = client.query_points(
            collection_name=settings.rag_collection,
            query=vector,
            limit=settings.rag_top_k,
            score_threshold=settings.rag_min_score,
            query_filter=query_filter,
            with_payload=True,
        ).points
        labels = label_map()
        out = []
        for h in hits:
            p = h.payload or {}
            out.append({
                "score": round(h.score, 3),
                "source": labels.get(p.get("source", ""), p.get("source", "?")),
                "title": p.get("title", ""),
                "text": p.get("text", ""),
            })
        _mark(True)
        return out
    except Exception as e:  # noqa: BLE001 - RAG must never block suggestions
        _mark(False, str(e))
        return []


def format_context(hits: list[dict], budget_chars: int = 3500) -> str:
    """Render hits as a prompt section; trims to budget."""
    if not hits:
        return ""
    lines = []
    used = 0
    for h in hits:
        text = h["text"]
        entry = f"[{h['source']} — {h['title']}]\n{text}"
        if used + len(entry) > budget_chars:
            remaining = budget_chars - used - len(entry) + len(text)
            if remaining < 200:
                break
            entry = f"[{h['source']} — {h['title']}]\n{text[:remaining]}…"
        lines.append(entry)
        used += len(entry)
    return "\n\n".join(lines)
