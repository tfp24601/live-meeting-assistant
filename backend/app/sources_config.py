"""Knowledge-source configuration, loaded from sources.yaml at the repo root.

sources.yaml is deployment-local (gitignored) — see sources.example.yaml for
the committed template. Both the ingestion pipeline and retrieval read it, so
adding/removing/disabling a source is a config change, not a code change.

Schema per source:
  id:      stable key; becomes the qdrant `source` payload value (changing it
           orphans previously-ingested points, so don't)
  type:    ghost-site | markdown-dir | transcript-dir
  label:   human label used in prompts and citations
  enabled: false -> ingestion skips it AND retrieval filters its points out
           (the "ignore" switch; points stay in qdrant for cheap re-enable)
  url/path and other keys are type-specific params.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

log = logging.getLogger("lma.sources")

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCES_PATH = Path(os.getenv("LMA_SOURCES", REPO_ROOT / "sources.yaml"))

_cache: tuple[float, list[dict]] | None = None
_CACHE_TTL_S = 30  # settings screen edits show up without a restart


def load_sources() -> list[dict]:
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < _CACHE_TTL_S:
        return _cache[1]
    sources: list[dict] = []
    if SOURCES_PATH.exists():
        try:
            import yaml
            data = yaml.safe_load(SOURCES_PATH.read_text()) or {}
            for s in data.get("sources", []):
                if isinstance(s, dict) and s.get("id") and s.get("type"):
                    s.setdefault("label", s["id"])
                    s.setdefault("enabled", True)
                    sources.append(s)
        except Exception as e:  # noqa: BLE001 - a bad config must not kill the app
            log.warning("failed to parse %s: %s", SOURCES_PATH, e)
    _cache = (now, sources)
    return sources


def invalidate() -> None:
    global _cache
    _cache = None


def _write(sources: list[dict]) -> None:
    import yaml
    header = (
        "# Knowledge sources — managed by the settings screen (hand-edits are\n"
        "# preserved in substance but comments are not). Template: sources.example.yaml\n"
    )
    SOURCES_PATH.write_text(header + yaml.safe_dump({"sources": sources}, sort_keys=False))
    invalidate()


VALID_TYPES = {"ghost-site", "markdown-dir", "transcript-dir"}


def add_source(entry: dict) -> None:
    if not entry.get("id") or not entry.get("type"):
        raise ValueError("source needs id and type")
    if entry["type"] not in VALID_TYPES:
        raise ValueError(f"type must be one of {sorted(VALID_TYPES)}")
    if entry["type"] == "ghost-site" and not entry.get("url"):
        raise ValueError("ghost-site needs url")
    if entry["type"] in ("markdown-dir", "transcript-dir") and not entry.get("path"):
        raise ValueError(f"{entry['type']} needs path")
    sources = load_sources()
    if any(s["id"] == entry["id"] for s in sources):
        raise ValueError(f"source id {entry['id']!r} already exists")
    entry.setdefault("label", entry["id"])
    entry.setdefault("enabled", True)
    _write(sources + [entry])


def remove_source(source_id: str) -> None:
    sources = load_sources()
    kept = [s for s in sources if s["id"] != source_id]
    if len(kept) == len(sources):
        raise KeyError(source_id)
    _write(kept)


def set_enabled(source_id: str, enabled: bool) -> None:
    sources = load_sources()
    for s in sources:
        if s["id"] == source_id:
            s["enabled"] = bool(enabled)
            _write(sources)
            return
    raise KeyError(source_id)


def enabled_sources() -> list[dict]:
    return [s for s in load_sources() if s.get("enabled", True)]


def disabled_ids() -> list[str]:
    return [s["id"] for s in load_sources() if not s.get("enabled", True)]


def label_map() -> dict[str, str]:
    return {s["id"]: s["label"] for s in load_sources()}
