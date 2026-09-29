"""Phase 10 — local-disk transcript logging for typed chat (/widget/chat).

Voice transcript+audio logging lives in its own
voice-pipeline/conversation_logger.py instead — that service owns the actual
websocket audio frames and the Pipecat pipeline, so it's a separate
implementation, not shared code, per this repo's two-service split. Both
write into a `logs/<date>/<session_id>/` layout and share the same 90-day
retention rule (Implementation Plan §11 / CLAUDE.md's logging must-have).
"""
import json
import logging
import re
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("rag_backend.conversation_logger")

LOGS_DIR = Path(__file__).parent / "logs"
RETENTION_DAYS = 90
_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]")
_write_lock = threading.Lock()


def _safe_session_id(session_id: str) -> str:
    cleaned = _SAFE_ID.sub("_", session_id)[:128]
    return cleaned or "unknown-session"


def log_turn(session_id: str, messages: list[dict], reply: str) -> None:
    """Overwrite this session's transcript with the full message history plus
    the latest reply. The widget always resends the complete conversation on
    every request (CLAUDE.md decision #3: the backend is stateful across a
    conversation), so the latest write is always the authoritative full
    transcript — no append/merge logic needed, and a crash mid-session still
    leaves the last completed turn on disk."""
    if not session_id:
        return
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    session_dir = LOGS_DIR / today / _safe_session_id(session_id)
    try:
        with _write_lock:
            session_dir.mkdir(parents=True, exist_ok=True)
            transcript = {
                "session_id": session_id,
                "source": "chat",
                "logged_at": datetime.now(timezone.utc).isoformat(),
                "messages": [*messages, {"role": "assistant", "content": reply}],
            }
            (session_dir / "transcript.json").write_text(
                json.dumps(transcript, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    except Exception:
        logger.exception("Failed to write transcript log for session %s", session_id)


def prune_old_logs(retention_days: int = RETENTION_DAYS) -> None:
    """Delete any date folder older than the retention window. Meant to be
    called once at startup (see main.py) rather than on a running schedule —
    good enough at expected volume, and avoids pulling in a scheduler
    dependency for a once-a-day housekeeping task."""
    if not LOGS_DIR.exists():
        return
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=retention_days)
    for date_dir in LOGS_DIR.iterdir():
        if not date_dir.is_dir():
            continue
        try:
            folder_date = datetime.strptime(date_dir.name, "%Y-%m-%d").date()
        except ValueError:
            continue  # not a date-named folder — leave it alone
        if folder_date < cutoff:
            shutil.rmtree(date_dir, ignore_errors=True)
            logger.info(
                "Pruned expired transcript log folder %s (older than %d days)",
                date_dir, retention_days,
            )
