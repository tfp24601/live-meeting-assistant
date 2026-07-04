"""Shared ingestion plumbing: chunking, embedding, idempotent qdrant upserts.

Collection design: ONE collection (`lma_knowledge`) for all sources, with
payload {source, title, url, text, doc_id, chunk_idx, run}. Point ids are
uuid5(source:doc_id:chunk_idx) so re-ingesting overwrites in place; after a
successful source run we prune points whose `run` tag is stale. Ingest fails
midway -> old points survive (prune only happens after upserts).
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from fastembed import TextEmbedding
from qdrant_client import QdrantClient
from qdrant_client import models as qm

COLLECTION = "lma_knowledge"
EMBED_MODEL = "BAAI/bge-base-en-v1.5"   # 768-dim, strong retrieval quality, ONNX/CPU
DIMS = 768
MAX_CHUNK_CHARS = 1400

_NAMESPACE = uuid.UUID("f00dfeed-1111-4aaa-8000-000000000000")

_embedder: TextEmbedding | None = None


def get_embedder() -> TextEmbedding:
    global _embedder
    if _embedder is None:
        _embedder = TextEmbedding(model_name=EMBED_MODEL)
    return _embedder


def get_client(url: str) -> QdrantClient:
    return QdrantClient(url=url, timeout=60)


def ensure_collection(client: QdrantClient) -> None:
    if not client.collection_exists(COLLECTION):
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=qm.VectorParams(size=DIMS, distance=qm.Distance.COSINE),
        )
        client.create_payload_index(COLLECTION, "source", qm.PayloadSchemaType.KEYWORD)
        client.create_payload_index(COLLECTION, "run", qm.PayloadSchemaType.KEYWORD)


def chunk_paragraphs(paragraphs: list[str], max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Accumulate paragraphs into chunks <= max_chars; hard-split oversized ones."""
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        if len(p) > max_chars:
            # flush, then split the giant paragraph on sentence-ish boundaries
            if cur:
                chunks.append("\n".join(cur))
                cur, cur_len = [], 0
            sentences = re.split(r"(?<=[.!?])\s+", p)
            buf = ""
            for s in sentences:
                if len(buf) + len(s) + 1 > max_chars and buf:
                    chunks.append(buf.strip())
                    buf = ""
                buf += s + " "
            if buf.strip():
                chunks.append(buf.strip())
            continue
        if cur_len + len(p) + 1 > max_chars and cur:
            chunks.append("\n".join(cur))
            # one-paragraph overlap keeps cross-chunk context findable
            cur = [cur[-1]] if len(cur[-1]) < max_chars // 3 else []
            cur_len = sum(len(x) + 1 for x in cur)
        cur.append(p)
        cur_len += len(p) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


@dataclass
class Doc:
    source: str      # source id from sources.yaml, e.g. "my-blog"
    doc_id: str      # stable per document (url path / relpath / filename)
    title: str
    url: str
    chunks: list[str]


def upsert_docs(client: QdrantClient, docs: list[Doc], run_id: str, batch: int = 64) -> int:
    """Embed and upsert all chunks. Returns number of points written."""
    embedder = get_embedder()
    points: list[qm.PointStruct] = []
    total = 0

    def flush() -> None:
        nonlocal points, total
        if points:
            client.upsert(collection_name=COLLECTION, points=points, wait=True)
            total += len(points)
            points = []

    for doc in docs:
        texts = [f"{doc.title}\n{c}" for c in doc.chunks]
        vectors = list(embedder.embed(texts))
        for i, (chunk, vec) in enumerate(zip(doc.chunks, vectors)):
            pid = str(uuid.uuid5(_NAMESPACE, f"{doc.source}:{doc.doc_id}:{i}"))
            points.append(qm.PointStruct(
                id=pid,
                vector=vec.tolist(),
                payload={
                    "source": doc.source,
                    "doc_id": doc.doc_id,
                    "chunk_idx": i,
                    "title": doc.title,
                    "url": doc.url,
                    "text": chunk,
                    "run": run_id,
                },
            ))
            if len(points) >= batch:
                flush()
    flush()
    return total


def prune_stale(client: QdrantClient, source: str, run_id: str) -> None:
    """Remove points of `source` not written by this run (deleted/renamed docs)."""
    client.delete(
        collection_name=COLLECTION,
        points_selector=qm.FilterSelector(filter=qm.Filter(
            must=[qm.FieldCondition(key="source", match=qm.MatchValue(value=source))],
            must_not=[qm.FieldCondition(key="run", match=qm.MatchValue(value=run_id))],
        )),
        wait=True,
    )
