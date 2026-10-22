"""Limits on the public website routes, so one visitor (or a script) cannot run up the
Sarvam bill or crowd out other visitors. Added 2026-10-22.

Every limit is read from the environment and 0 switches it off:

- WIDGET_RATE_PER_MINUTE (20) and WIDGET_RATE_PER_HOUR (300): typed-chat messages per visitor
  address. A person typing never gets near these.
- WIDGET_MAX_MESSAGE_CHARS (1000): longest visitor message accepted.
- WIDGET_MAX_MESSAGES (60): the most history sent to the model per turn; older turns are
  dropped, newest kept (a 30-question chat still goes through whole).

The visitor's address comes from the last X-Forwarded-For entry, the one nginx adds itself
(earlier entries can be faked by the browser); without nginx, from the connection. Counts are
kept in memory, so they reset when the service restarts, and each service keeps its own.
"""
import os
import time
from collections import defaultdict, deque

from fastapi import Request


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


RATE_PER_MINUTE = _env_int("WIDGET_RATE_PER_MINUTE", 20)
RATE_PER_HOUR = _env_int("WIDGET_RATE_PER_HOUR", 300)
MAX_MESSAGE_CHARS = _env_int("WIDGET_MAX_MESSAGE_CHARS", 1000)
MAX_MESSAGES = _env_int("WIDGET_MAX_MESSAGES", 60)


def client_address(headers, fallback: str | None) -> str:
    forwarded = [part.strip() for part in (headers.get("x-forwarded-for") or "").split(",") if part.strip()]
    return forwarded[-1] if forwarded else (fallback or "unknown")


def request_address(request: Request) -> str:
    return client_address(request.headers, request.client.host if request.client else None)


class RateLimiter:
    """Sliding windows per address: at most `per_minute` in 60 s and `per_hour` in 3600 s."""

    def __init__(self, per_minute: int, per_hour: int):
        self.per_minute, self.per_hour = per_minute, per_hour
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, address: str, now: float | None = None) -> bool:
        if not self.per_minute and not self.per_hour:
            return True
        now = time.monotonic() if now is None else now
        hits = self._hits[address]
        while hits and now - hits[0] >= 3600:
            hits.popleft()
        last_minute = sum(1 for t in hits if now - t < 60)
        if (self.per_minute and last_minute >= self.per_minute) or (self.per_hour and len(hits) >= self.per_hour):
            return False
        hits.append(now)
        if len(self._hits) > 10000:  # forget quiet addresses so memory stays small
            for key in [k for k, v in self._hits.items() if not v or now - v[-1] >= 3600]:
                del self._hits[key]
        return True


widget_chat_limiter = RateLimiter(RATE_PER_MINUTE, RATE_PER_HOUR)


def too_long(messages: list[dict]) -> bool:
    """True if the newest visitor message is over the length cap (older turns were already accepted)."""
    if not MAX_MESSAGE_CHARS:
        return False
    for m in reversed(messages):
        if m.get("role") == "user":
            return len(str(m.get("content") or "")) > MAX_MESSAGE_CHARS
    return False


def trim_history(messages: list[dict]) -> list[dict]:
    """Keep the newest MAX_MESSAGES turns, starting on a visitor turn."""
    if not MAX_MESSAGES or len(messages) <= MAX_MESSAGES:
        return messages
    kept = messages[-MAX_MESSAGES:]
    while kept and kept[0].get("role") != "user":
        kept = kept[1:]
    return kept or messages[-1:]
