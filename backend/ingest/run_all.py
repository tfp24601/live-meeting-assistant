"""Ingestion orchestrator, driven by sources.yaml. Run where data + qdrant live.

Usage:
    .venv/bin/python -m backend.ingest.run_all [--source ID]... [--qdrant URL]

Idempotent: deterministic point ids overwrite in place; stale points of each
source are pruned only after that source's upserts succeed. Disabled sources
are skipped (their existing points remain, hidden by the retrieval filter).
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time

from ..app.sources_config import SOURCES_PATH, load_sources
from . import common, ghost_site, markdown_dir, transcript_dir

INGESTERS = {
    "ghost-site": ghost_site.collect,
    "markdown-dir": markdown_dir.collect,
    "transcript-dir": transcript_dir.collect,
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", action="append", help="limit to specific source id(s)")
    ap.add_argument("--qdrant", default=os.getenv("QDRANT_URL", "http://127.0.0.1:6333"))
    args = ap.parse_args()

    sources = load_sources()
    if not sources:
        print(f"no sources configured — create {SOURCES_PATH} (see sources.example.yaml)")
        sys.exit(1)
    if args.source:
        unknown = set(args.source) - {s["id"] for s in sources}
        if unknown:
            print(f"unknown source id(s): {sorted(unknown)}")
            sys.exit(1)
        sources = [s for s in sources if s["id"] in args.source]

    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    client = common.get_client(args.qdrant)
    common.ensure_collection(client)
    print(f"run={run_id} qdrant={args.qdrant} sources={[s['id'] for s in sources]}")

    failures = 0
    for cfg in sources:
        if not cfg.get("enabled", True):
            print(f"[{cfg['id']}] skipped (disabled)")
            continue
        ingester = INGESTERS.get(cfg["type"])
        if ingester is None:
            print(f"[{cfg['id']}] FAILED: unknown type {cfg['type']!r}")
            failures += 1
            continue
        t0 = time.time()
        try:
            docs = ingester(cfg)
            written = common.upsert_docs(client, docs, run_id)
            common.prune_stale(client, cfg["id"], run_id)
            print(f"[{cfg['id']}] OK: {written} points in {time.time()-t0:.1f}s")
        except Exception as e:  # noqa: BLE001 - one source failing must not stop the rest
            failures += 1
            print(f"[{cfg['id']}] FAILED: {e!r}")

    info = client.get_collection(common.COLLECTION)
    print(f"collection total points: {info.points_count}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
