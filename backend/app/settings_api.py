"""Settings screen API: runtime settings, knowledge sources, ingest trigger.

Same trust model as the rest of the app: tailnet/LAN-private, no auth. Add an
auth layer before ever exposing this publicly.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import shutil
import sys

from fastapi import APIRouter, HTTPException

from . import config, sources_config
from .config import settings

log = logging.getLogger("lma.settings")

router = APIRouter(prefix="/api")


# ---- runtime settings -------------------------------------------------------

@router.get("/settings")
async def get_settings() -> dict:
    return {
        "fields": config.describe(),
        "claude_cli_available": shutil.which(settings.claude_bin) is not None,
    }


@router.put("/settings")
async def put_settings(changes: dict) -> dict:
    # Secret fields echo "__SET__" back from the UI when untouched — drop those.
    changes = {k: v for k, v in changes.items() if v != "__SET__"}
    try:
        restart_needed = config.update_overrides(changes)
    except KeyError as e:
        raise HTTPException(400, f"unknown setting {e.args[0]!r}")
    log.info("settings updated: %s%s", sorted(changes),
             f" (restart needed: {restart_needed})" if restart_needed else "")
    return {"ok": True, "restart_needed": restart_needed}


# ---- knowledge sources ------------------------------------------------------

@router.get("/sources")
async def get_sources() -> dict:
    return {"sources": sources_config.load_sources(),
            "types": sorted(sources_config.VALID_TYPES)}


@router.post("/sources")
async def post_source(entry: dict) -> dict:
    try:
        sources_config.add_source({
            k: v for k, v in entry.items()
            if k in ("id", "type", "label", "url", "path", "enabled") and v not in ("", None)
        })
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.patch("/sources/{source_id}")
async def patch_source(source_id: str, body: dict) -> dict:
    if "enabled" not in body:
        raise HTTPException(400, "body needs {'enabled': bool}")
    try:
        sources_config.set_enabled(source_id, bool(body["enabled"]))
    except KeyError:
        raise HTTPException(404, f"no source {source_id!r}")
    return {"ok": True}


@router.delete("/sources/{source_id}")
async def delete_source(source_id: str) -> dict:
    try:
        sources_config.remove_source(source_id)
    except KeyError:
        raise HTTPException(404, f"no source {source_id!r}")
    return {"ok": True}


# ---- ingest trigger ---------------------------------------------------------
# Runs the ingestion pipeline on THIS host as a subprocess. Note: sources whose
# data paths live on another machine will fail here (visible in the log) —
# ingestion for those belongs on the machine that has the data (e.g. cron).

_ingest = {"proc": None, "log": collections.deque(maxlen=200), "rc": None}


async def _pump(proc) -> None:
    async for raw in proc.stdout:
        _ingest["log"].append(raw.decode(errors="replace").rstrip())
    _ingest["rc"] = await proc.wait()
    _ingest["proc"] = None
    _ingest["log"].append(f"[done rc={_ingest['rc']}]")


@router.post("/ingest")
async def post_ingest(body: dict | None = None) -> dict:
    if _ingest["proc"] is not None:
        raise HTTPException(409, "an ingest run is already in progress")
    argv = [sys.executable, "-m", "backend.ingest.run_all"]
    source_id = (body or {}).get("source")
    if source_id:
        argv += ["--source", source_id]
    _ingest["log"].clear()
    _ingest["rc"] = None
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(config.REPO_ROOT),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    _ingest["proc"] = proc
    asyncio.get_running_loop().create_task(_pump(proc))
    log.info("ingest started: %s", argv)
    return {"ok": True}


@router.get("/ingest")
async def get_ingest() -> dict:
    return {
        "running": _ingest["proc"] is not None,
        "rc": _ingest["rc"],
        "log": list(_ingest["log"]),
    }
