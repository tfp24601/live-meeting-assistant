"""Generic diarized-transcript ingester ('**Speaker**' markdown exports).

Config params: path (directory of .md/.txt transcripts). Chunks accumulate
speaker turns with one-turn overlap so retrieved passages keep their
conversational context.
"""
from __future__ import annotations

import re
from pathlib import Path

from .common import MAX_CHUNK_CHARS, Doc, chunk_paragraphs

_SPEAKER_RE = re.compile(r"^\*\*([^*]{1,40})\*\*\s*$", flags=re.M)


def _turns(md: str) -> list[str]:
    """Split '**Speaker**\\n\\ntext' markdown into 'Speaker: text' turns."""
    parts = _SPEAKER_RE.split(md)
    if len(parts) < 3:
        return [p.strip() for p in md.split("\n\n") if p.strip()]
    turns = []
    # parts = [preamble, speaker1, text1, speaker2, text2, ...]
    for i in range(1, len(parts) - 1, 2):
        speaker = parts[i].strip()
        text = re.sub(r"\s+", " ", parts[i + 1]).strip()
        if text:
            turns.append(f"{speaker}: {text}")
    return turns


def _chunk_turns(turns: list[str]) -> list[str]:
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for t in turns:
        if len(t) > MAX_CHUNK_CHARS:
            if cur:
                chunks.append("\n".join(cur))
                cur, cur_len = [], 0
            chunks.extend(chunk_paragraphs([t]))
            continue
        if cur_len + len(t) + 1 > MAX_CHUNK_CHARS and cur:
            chunks.append("\n".join(cur))
            cur = [cur[-1]]                      # one-turn overlap
            cur_len = len(cur[0]) + 1
        cur.append(t)
        cur_len += len(t) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def collect(cfg: dict, verbose: bool = True) -> list[Doc]:
    source_id = cfg["id"]
    root = Path(cfg["path"])
    docs: list[Doc] = []
    for path in sorted(root.glob("*")):
        if path.suffix.lower() not in {".md", ".txt"}:
            continue
        text = path.read_text(errors="replace")
        chunks = _chunk_turns(_turns(text))
        if chunks:
            docs.append(Doc(source=source_id, doc_id=path.name, title=path.stem,
                            url=f"{source_id}/{path.name}", chunks=chunks))
    if verbose:
        print(f"[{source_id}] {len(docs)} docs, {sum(len(d.chunks) for d in docs)} chunks")
    return docs
