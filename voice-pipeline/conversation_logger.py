"""Phase 10 — local-disk transcript + audio logging for voice sessions.

Kept separate from rag-backend/conversation_logger.py (different service,
different runtime) but writes into the same logs/<date>/<session_id>/
layout and shares the same 90-day retention rule.

Transcript logging taps the shared `LLMContext` that pipeline.py's
user_aggregator/assistant_aggregator already maintain — `TranscriptLogger`
is a small FrameProcessor dropped in at the end of the pipeline that
snapshots context.messages to disk every time it changes, so a transcript
survives even if the process dies mid-call rather than only being written
once at the end.

Audio logging uses Pipecat's own `AudioBufferProcessor` (see pipeline.py) —
this module just gives it somewhere to save the merged recording once a
session ends.
"""
import json
import logging
import re
import shutil
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipecat.frames.frames import Frame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger("voice_pipeline.conversation_logger")

LOGS_DIR = Path(__file__).parent / "logs"
RETENTION_DAYS = 90
_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]")


def _safe_session_id(session_id: str) -> str:
    cleaned = _SAFE_ID.sub("_", session_id)[:128]
    return cleaned or "unknown-session"


def _session_dir(session_id: str) -> Path:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    session_dir = LOGS_DIR / today / _safe_session_id(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


class TranscriptLogger(FrameProcessor):
    """Placed as the last stage in the pipeline (after assistant_aggregator)
    so every frame that reaches it has already been folded into `context` by
    the aggregator pair. Doesn't inspect frame content itself — just watches
    whether context.messages grew and, if so, re-dumps the whole thing."""

    def __init__(self, context, session_id: str, language, **kwargs):
        """`language`: a plain str, or a zero-arg callable returning the
        current language. Since 2026-09-29, a call can switch language
        mid-session (see conversation_router.py) — passing
        `lambda: router.language` instead of a fixed string keeps the
        transcript's "language" field accurate for calls that switch,
        without this module needing to import/know about the router."""
        super().__init__(**kwargs)
        self._context = context
        self._session_id = session_id
        self._language = language
        self._last_len = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        messages = self._context.messages
        if len(messages) != self._last_len:
            self._last_len = len(messages)
            self._write(messages)
        await self.push_frame(frame, direction)

    def _write(self, messages):
        try:
            path = _session_dir(self._session_id) / "transcript.json"
            payload = {
                "session_id": self._session_id,
                "source": "voice",
                "language": self._language() if callable(self._language) else self._language,
                "logged_at": datetime.now(timezone.utc).isoformat(),
                "messages": messages,
            }
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception:
            logger.exception(
                "Failed to write voice transcript for session %s", self._session_id
            )


def save_session_audio(
    session_id: str, audio: bytes, sample_rate: int, num_channels: int
) -> None:
    """Called from AudioBufferProcessor's `on_audio_data` handler once a
    session's recording stops (pipeline.py wires this up). Saves the merged
    user+bot audio as one WAV file — a single mixed track, not separate
    tracks per speaker, which is enough for this v1 requirement without
    doubling storage."""
    if not audio:
        return
    try:
        path = _session_dir(session_id) / "audio.wav"
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(num_channels)
            wf.setsampwidth(2)  # PCM16, matches every audio format used in this pipeline
            wf.setframerate(sample_rate)
            wf.writeframes(audio)
    except Exception:
        logger.exception("Failed to write voice audio for session %s", session_id)


def prune_old_logs(retention_days: int = RETENTION_DAYS) -> None:
    if not LOGS_DIR.exists():
        return
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=retention_days)
    for date_dir in LOGS_DIR.iterdir():
        if not date_dir.is_dir():
            continue
        try:
            folder_date = datetime.strptime(date_dir.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if folder_date < cutoff:
            shutil.rmtree(date_dir, ignore_errors=True)
            logger.info(
                "Pruned expired voice log folder %s (older than %d days)",
                date_dir, retention_days,
            )
