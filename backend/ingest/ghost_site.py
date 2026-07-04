"""Generic Ghost-blog ingester: crawl a site via its sitemaps.

Config params: url (site root). Works for any Ghost site (sitemap-posts.xml +
sitemap-pages.xml); trafilatura handles extraction, so most article-shaped
sites behind those sitemap names work too.
"""
from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET

import httpx
import trafilatura

from .common import Doc, chunk_paragraphs

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
FETCH_DELAY_S = 0.4
SKIP_PATHS = {"/"}  # the landing page is nav, not prose


def _urls(client: httpx.Client, site: str) -> list[str]:
    urls: list[str] = []
    for sm_url in (f"{site}/sitemap-posts.xml", f"{site}/sitemap-pages.xml"):
        r = client.get(sm_url)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        for loc in root.findall(".//sm:url/sm:loc", NS):
            u = (loc.text or "").strip()
            if u and httpx.URL(u).path not in SKIP_PATHS:
                urls.append(u)
    return urls


def collect(cfg: dict, verbose: bool = True) -> list[Doc]:
    source_id = cfg["id"]
    site = cfg["url"].rstrip("/")
    docs: list[Doc] = []
    with httpx.Client(timeout=30, follow_redirects=True,
                      headers={"User-Agent": "LMA-ingest/1.0 (owner's own crawler)"}) as client:
        urls = _urls(client, site)
        if verbose:
            print(f"[{source_id}] {len(urls)} urls from sitemaps")
        for u in urls:
            try:
                r = client.get(u)
                r.raise_for_status()
            except httpx.HTTPError as e:
                print(f"[{source_id}] SKIP {u}: {e}")
                continue
            extracted = trafilatura.extract(r.text, output_format="json", with_metadata=True)
            time.sleep(FETCH_DELAY_S)
            if not extracted:
                continue
            meta = json.loads(extracted)
            text = (meta.get("text") or "").strip()
            title = (meta.get("title") or httpx.URL(u).path).strip()
            if len(text) < 200:   # nav/tag stubs
                continue
            chunks = chunk_paragraphs(text.split("\n"))
            if chunks:
                docs.append(Doc(source=source_id, doc_id=httpx.URL(u).path,
                                title=title, url=u, chunks=chunks))
    if verbose:
        print(f"[{source_id}] {len(docs)} docs, {sum(len(d.chunks) for d in docs)} chunks")
    return docs
