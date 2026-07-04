"""Query the user's knowledge base (qdrant) for the suggestion prompt.

Blocking qdrant/fastembed calls — the engine runs them via asyncio.to_thread.
Fail-open by design: any retrieval problem returns [] and the suggestion run
proceeds without knowledge context.
"""
from __future__ import annotations

import logging
import threading

from ..config import settings
from ..sources_config import disabled_ids, label_map

log = logging.getLogger("lma.retrieval")

EMBED_MODEL = "BAAI/bge-base-en-v1.5"   # must match ingestion (backend/ingest/common.py)

_lock = threading.Lock()
_client = None
_embedder = None


def warmup() -> None:
    """Load the embedding model + client at boot so the first run isn't slow."""
    if not settings.rag_enabled:
        return
    try:
        _ensure()
        log.info("retrieval ready (%s @ %s)", settings.rag_collection, settings.qdrant_url)
    except Exception as e:  # noqa: BLE001
        log.warning("retrieval warmup failed (suggestions will run without RAG): %s", e)


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
        return out
    except Exception as e:  # noqa: BLE001 - RAG must never block suggestions
        log.warning("retrieval failed (continuing without): %s", e)
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
