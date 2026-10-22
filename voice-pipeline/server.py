"""Standalone server for BOTH voice paths (English and Tamil). Deployed
separately from rag-backend (per the repo structure) since it's a different
runtime — a persistent Pipecat process, not a stateless request handler —
but it calls the exact same rag-backend for answers on both routes.

The widget connects here directly at /voice/en or /voice/ta depending on the
selected language. English used to be relayed by rag-backend's Deepgram
integration (voice_agent.py, retired 2026-09-25) — it now goes through this
same Sarvam/Pipecat service Tamil already used, per CLAUDE.md decision #1.
"""
import os
import uuid

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pipecat.pipeline.runner import PipelineRunner

from conversation_logger import prune_old_logs
from language_detection import detect_language
from pipeline import PipelineProfile, build_pipeline
import web_call_limits

app = FastAPI(title="Agni AI Voice Pipeline")


@app.on_event("startup")
async def _prune_logs_on_startup():
    """Phase 10: 90-day retention, auto-pruned at startup — see
    rag-backend/main.py's identical startup hook for the same reasoning."""
    prune_old_logs()


@app.on_event("startup")
async def _prerender_greetings():
    """Render the greeting clips in the background so the first call after a
    start (or after a voice/pace/text change) already finds them cached."""
    import asyncio

    import greeting_cache

    asyncio.create_task(greeting_cache.prewarm())

WIDGET_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("WIDGET_ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]

# The websocket routes below don't need CORS (browsers don't preflight
# WebSocket upgrades), but /detect-language is a plain POST the widget calls
# directly from the browser — same CORS treatment as rag-backend's
# /widget/chat.
app.add_middleware(
    CORSMiddleware,
    allow_origins=WIDGET_ALLOWED_ORIGINS,
    allow_methods=["POST"],
    allow_headers=["Content-Type"],
)


def _origin_allowed(websocket: WebSocket) -> bool:
    if "*" in WIDGET_ALLOWED_ORIGINS:
        return True
    return websocket.headers.get("origin", "") in WIDGET_ALLOWED_ORIGINS


async def _run_voice_session(websocket: WebSocket, language: str):
    if not _origin_allowed(websocket):
        await websocket.close(code=4403)
        return

    await websocket.accept()
    # Public limits (web_call_limits.py): too many calls at once, or from one visitor.
    address = web_call_limits.client_address(websocket.headers, websocket.client.host if websocket.client else None)
    refused = web_call_limits.gate.enter(address)
    if refused:
        await websocket.close(code=web_call_limits.BUSY_CODE, reason=refused)
        return
    try:
        session_id = uuid.uuid4().hex
        limits = web_call_limits.WebCallLimits()
        # Website values everywhere else (PipelineProfile's defaults); only the silence
        # timer and the hook for the nudge, goodbye and time cap are added.
        profile = PipelineProfile(user_idle_timeout=limits.idle_secs, on_built=limits.attach)
        worker = build_pipeline(websocket, language=language, session_id=session_id, profile=profile)
        runner = PipelineRunner()
        await runner.run(worker)
    finally:
        web_call_limits.gate.leave(address)


@app.websocket("/voice/en")
async def voice_en(websocket: WebSocket):
    await _run_voice_session(websocket, "en")


@app.websocket("/voice/ta")
async def voice_ta(websocket: WebSocket):
    await _run_voice_session(websocket, "ta")


@app.post("/detect-language")
async def detect_language_route(request: Request):
    """Auto-detect en/ta from a short raw PCM16 mono 16kHz mic snippet the
    widget records before opening a voice session (Phase 6). No vendor key
    reaches the browser — this route holds SARVAM_API_KEY server-side, same
    principle as rag-backend's /widget/chat.

    Body: raw PCM16 bytes (application/octet-stream), NOT JSON — the widget
    already produces this exact format for the realtime pipeline, so no
    encoding step is needed on either side.
    """
    pcm16_audio = await request.body()
    if web_call_limits.DETECT_LANGUAGE_MAX_BYTES and len(pcm16_audio) > web_call_limits.DETECT_LANGUAGE_MAX_BYTES:
        return JSONResponse({"detail": "audio_too_long"}, status_code=413)
    language = await detect_language(pcm16_audio)
    return {"language": language}


@app.get("/health")
async def health():
    return {"status": "ok"}
