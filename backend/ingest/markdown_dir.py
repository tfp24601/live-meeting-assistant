"""Generic markdown-directory ingester (e.g. an mkdocs/obsidian docs tree).

Config params: path (directory containing .md files, searched recursively).
"""
from __future__ import annotations

import re
from pathlib import Path

from .common import Doc, chunk_paragraphs

SKIP_DIRS = {"assets", ".obsidian", "node_modules"}


def _title_of(md: str, fallback: str) -> str:
    m = re.search(r"^#\s+(.+)$", md, flags=re.M)
    return m.group(1).strip() if m else fallback


def _sections(md: str) -> list[str]:
    """Split on h1-h3 headings so chunks align with topics; keep heading text."""
    parts = re.split(r"(?=^#{1,3}\s)", md, flags=re.M)
    return [p for p in (s.strip() for s in parts) if p]


def collect(cfg: dict, verbose: bool = True) -> list[Doc]:
    source_id = cfg["id"]
    root = Path(cfg["path"])
    docs: list[Doc] = []
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        md = path.read_text(errors="replace")
        if len(md.strip()) < 80:
            continue
        paragraphs: list[str] = []
        for section in _sections(md):
            paragraphs.extend(section.split("\n\n"))
        chunks = chunk_paragraphs(paragraphs)
        if chunks:
            docs.append(Doc(source=source_id, doc_id=str(rel),
                            title=_title_of(md, str(rel)), url=f"{source_id}/{rel}",
                            chunks=chunks))
    if verbose:
        print(f"[{source_id}] {len(docs)} docs, {sum(len(d.chunks) for d in docs)} chunks")
    return docs
