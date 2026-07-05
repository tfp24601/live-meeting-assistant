"""FastAPI app: serves the capture page and ingests audio over WebSocket.

Phase 0 scope: browser PCM in -> live transcript out. No LLM / RAG yet.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import numpy as np

from . import meeting
from .config import settings
from .speaker import verify as speaker
from .suggestions.engine import SuggestionEngine
from .transcription.whisper_engine import StreamingTranscriber, get_model, transcribe_watched

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("lma.main")

# repo_root/frontend
FRONTEND = Path(__file__).resolve().parents[2] / "frontend"

app = FastAPI(title="LiveMeetingAssistant")
app.mount("/static", StaticFiles(directory=str(FRONTEND / "static")), name="static")

from .settings_api import router as settings_router  # noqa: E402
app.include_router(settings_router)


@app.get("/settings")
async def settings_page() -> FileResponse:
    return FileResponse(str(FRONTEND / "settings.html"))


@app.on_event("startup")
async def _warmup() -> None:
    # Load the whisper model and RAG embedder at boot (in threads) so the
    # first utterance / first suggestion run isn't slow.
    from .suggestions import retrieval
    await asyncio.to_thread(get_model)
    await asyncio.to_thread(retrieval.warmup)
    await asyncio.to_thread(speaker.warmup)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(FRONTEND / "index.html"))


@app.get("/health")
async def health() -> dict:
    from .llm import provider_name
    from .suggestions import retrieval
    model = get_model()
    return {
        "status": "ok",
        "model": settings.whisper_model,
        "device": settings.whisper_device,
        "compute_type": settings.whisper_compute_type,
        "model_loaded": model is not None,
        "rag": retrieval.status(),
        "provider": provider_name(),
    }


def _session_for(ws: WebSocket) -> meeting.MeetingSession:
    session = meeting.get_or_create(ws.query_params.get("meeting", "default"))
    if session.engine is None:
        session.engine = SuggestionEngine(session)
    mode = ws.query_params.get("mode")
    if mode in ("online", "inperson"):
        session.mode = mode
    party = ws.query_params.get("party")
    if party in ("one", "multi"):
        session.party = party
    return session


@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket) -> None:
    await ws.accept()
    source = ws.query_params.get("source", "mic")
    session = _session_for(ws)
    st = StreamingTranscriber(source=source)
    work: asyncio.Queue[object] = asyncio.Queue()
    log.info("ws connected source=%s", source)

    async def transcriber_worker() -> None:
        """Pull completed utterances and transcribe them off the receive loop."""
        while True:
            utt = await work.get()
            if utt is None:  # shutdown sentinel
                return
            try:
                text = await transcribe_watched(utt.audio)
            except Exception:  # noqa: BLE001 - never kill the socket on a transcription error
                log.exception("transcription failed")
                continue
            if text:
                line_source = source
                if session.mode == "inperson":
                    # Single mic hears the whole room: classify each utterance
                    # against the enrolled voice. "you" -> mic, else -> system
                    # so all downstream You/Them logic works unchanged.
                    verdict = await asyncio.to_thread(speaker.classify, utt.audio)
                    if verdict is not None:
                        line_source = "mic" if verdict[0] == "you" else "system"
                        log.debug("speaker: %s (sim %.2f)", verdict[0], verdict[1])
                action, line_id, retract_id = session.ingest(line_source, text)
                if action == "suppress":
                    continue
                await ws.send_text(json.dumps({
                    "type": "transcript",
                    "source": line_source,
                    "text": text,
                    "id": line_id,
                    "t0": round(utt.t0, 2),
                    "t1": round(utt.t1, 2),
                }))
                if retract_id is not None:
                    await session.broadcast({"type": "retract", "id": retract_id})

    worker = asyncio.create_task(transcriber_worker())
    await ws.send_text(json.dumps({"type": "status", "source": source, "msg": "connected"}))

    try:
        while True:
            data = await ws.receive_bytes()
            for utt in st.add_pcm(data):
                work.put_nowait(utt)
    except WebSocketDisconnect:
        log.info("ws disconnected source=%s", source)
    finally:
        final = st.flush_final()
        if final is not None:
            work.put_nowait(final)
        # let the worker drain, then stop it
        await work.put(None)
        try:
            await asyncio.wait_for(worker, timeout=10)
        except asyncio.TimeoutError:
            worker.cancel()


@app.get("/api/config")
async def api_config() -> dict:
    from . import sources_config
    from .llm import provider_name, supports_web_search
    return {
        "user_name": settings.user_name,
        "public_url": settings.public_url,
        "provider": provider_name(),
        "deep_dive": supports_web_search(),
        "sources": [
            {"id": s["id"], "label": s["label"], "enabled": s.get("enabled", True)}
            for s in sources_config.load_sources()
        ],
    }


@app.get("/api/enrollment")
async def enrollment_status() -> dict:
    return speaker.enrollment_status()


@app.websocket("/ws/enroll")
async def ws_enroll(ws: WebSocket) -> None:
    """Voice enrollment: stream mic PCM; we bank voiced audio until the target,
    then build and save the user's reference embedding."""
    await ws.accept()
    target = settings.enroll_target_s
    sr = settings.sample_rate
    speech = np.zeros(0, dtype=np.float32)
    last_reported = -1.0
    log.info("enrollment started (target %.0fs speech)", target)
    try:
        while True:
            data = await ws.receive_bytes()
            chunk = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            if chunk.size == 0:
                continue
            rms = float(np.sqrt(np.mean(chunk * chunk)))
            if rms >= settings.vad_threshold:
                speech = np.concatenate([speech, chunk])
            got = len(speech) / sr
            if got - last_reported >= 0.5:
                last_reported = got
                await ws.send_text(json.dumps({
                    "type": "enroll_progress", "speech_s": round(got, 1), "target_s": target,
                }))
            if got >= target:
                break
        ref = await asyncio.to_thread(speaker.build_reference, speech)
        if ref is None:
            await ws.send_text(json.dumps({
                "type": "enroll_error",
                "msg": "could not build a voice profile (speaker model unavailable?)",
            }))
            return
        await asyncio.to_thread(speaker.save_enrollment, ref, len(speech) / sr)
        await ws.send_text(json.dumps({
            "type": "enroll_done", "seconds": round(len(speech) / sr, 1),
        }))
    except WebSocketDisconnect:
        log.info("enrollment aborted (%.1fs speech banked)", len(speech) / sr)


@app.websocket("/ws/suggestions")
async def ws_suggestions(ws: WebSocket) -> None:
    await ws.accept()
    session = _session_for(ws)
    session.listeners.add(ws)
    log.info("suggestions ws connected meeting=%s", session.meeting_id)
    try:
        # A refreshed page still sees the latest cards.
        if session.last_cards:
            await ws.send_text(json.dumps({"type": "suggestions", "cards": session.last_cards,
                                           "at": "", "replay": True}))
        await ws.send_text(json.dumps({"type": "suggest_status", "state": "idle"}))
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "suggest_now":
                session.engine.poke(force=True)
            elif msg.get("type") == "deep_dive":
                session.engine.poke(force=True, deep=True)
    except WebSocketDisconnect:
        pass
    finally:
        session.listeners.discard(ws)
        log.info("suggestions ws disconnected meeting=%s", session.meeting_id)
