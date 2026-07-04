"""Retrieval quality check against the knowledge collection.

Usage:
    # info mode: show top hits for ad-hoc queries
    .venv/bin/python backend/tools/rag_selftest.py [--qdrant URL] "some query" ...

    # pass/fail mode: assert which source should win each query
    .venv/bin/python backend/tools/rag_selftest.py [--qdrant URL] \
        "my-blog:how do I think about AI partnership" \
        "my-docs:how is monitoring set up"

Queries are deployment-specific — use topics you know your sources cover.
A "source:query" argument passes when that source id appears in the top 3.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastembed import TextEmbedding  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402

COLLECTION = "lma_knowledge"
MODEL = "BAAI/bge-base-en-v1.5"


def main() -> None:
    args = sys.argv[1:]
    url = "http://127.0.0.1:6333"
    if "--qdrant" in args:
        i = args.index("--qdrant")
        url = args[i + 1]
        args = args[:i] + args[i + 2:]
    if not args:
        print(__doc__)
        sys.exit(1)

    client = QdrantClient(url=url, timeout=10)
    embedder = TextEmbedding(model_name=MODEL)
    info = client.get_collection(COLLECTION)
    print(f"collection: {COLLECTION} @ {url} — {info.points_count} points\n")

    failures = 0
    checked = 0
    for arg in args:
        expect, query = (arg.split(":", 1) if ":" in arg else (None, arg))
        vec = list(embedder.embed([query]))[0].tolist()
        hits = client.query_points(COLLECTION, query=vec, limit=3, with_payload=True).points
        sources = [h.payload.get("source") for h in hits]
        if expect is not None:
            checked += 1
            ok = expect in sources
            if not ok:
                failures += 1
            print(f"{'PASS' if ok else 'WEAK'}  [{expect}] \"{query}\"")
        else:
            print(f"INFO  \"{query}\"")
        for h in hits:
            p = h.payload
            print(f"      {h.score:.3f} [{p.get('source')}] {p.get('title', '')[:60]}")
        print()

    if checked:
        print("ALL PASS" if failures == 0 else f"{failures}/{checked} queries missed their expected source")
        sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
