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

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from pipecat.pipeline.runner import PipelineRunner

from language_detection import detect_language
from pipeline import build_pipeline

app = FastAPI(title="ACT AI Voice Pipeline")

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
    worker = build_pipeline(websocket, language=language)
    runner = PipelineRunner()
    await runner.run(worker)


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
    language = await detect_language(pcm16_audio)
    return {"language": language}


@app.get("/health")
async def health():
    return {"status": "ok"}
