"""Standalone server for the Tamil voice path. Deployed separately from
rag-backend (per the repo structure) since it's a different runtime — a
persistent Pipecat process, not a stateless request handler — but it calls
the exact same rag-backend for answers.

The widget connects here directly at /voice/ta, same pattern as the English
/voice/en relay in rag-backend/main.py.
"""
import os

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, WebSocket
from pipecat.pipeline.runner import PipelineRunner

from pipeline import build_pipeline

app = FastAPI(title="ACT AI Tamil Voice Pipeline")

WIDGET_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("WIDGET_ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]


def _origin_allowed(websocket: WebSocket) -> bool:
    if "*" in WIDGET_ALLOWED_ORIGINS:
        return True
    return websocket.headers.get("origin", "") in WIDGET_ALLOWED_ORIGINS


@app.websocket("/voice/ta")
async def voice_ta(websocket: WebSocket):
    if not _origin_allowed(websocket):
        await websocket.close(code=4403)
        return

    await websocket.accept()
    task = build_pipeline(websocket)
    runner = PipelineRunner()
    await runner.run(task)


@app.get("/health")
async def health():
    return {"status": "ok"}
