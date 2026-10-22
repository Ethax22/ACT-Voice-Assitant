"""Limits on website voice calls (added 2026-10-22), so one visitor or a script cannot hold the
voice open for hours or take every voice the Cartesia plan allows (5 at once, shared with the
phone agent).

Every limit is read from the environment and 0 switches it off:

- VOICE_MAX_SESSIONS (4): website voice calls at once, all visitors together. 4 leaves one
  Cartesia voice for the phone line.
- VOICE_MAX_SESSIONS_PER_ADDRESS (2): calls at once from one visitor address.
- VOICE_SESSIONS_PER_HOUR (30): calls one visitor address may start in an hour.
- WEB_VOICE_IDLE_SECS (20): after this much silence from the visitor the bot asks if they are
  still there; after the same again it says goodbye and ends the call.
- WEB_VOICE_MAX_MINUTES (15): longest website voice call; then a goodbye and the call ends.
- DETECT_LANGUAGE_MAX_BYTES (256000, about 8 s of audio): largest /detect-language upload.

A refused call is accepted and then closed with code 4429, so the widget can say why (a
browser cannot read the reason of a refused handshake). The visitor's address is the last
X-Forwarded-For entry (added by nginx), else the connection's. The phone agent has its own
housekeeping (phone-agent/call_control.py) and is not affected by this file.

The Tamil lines below were written by Claude and are not yet checked by a native speaker.
"""
import asyncio
import os
import time
from collections import defaultdict, deque

from loguru import logger
from pipecat.frames.frames import EndWorkerFrame, TTSSpeakFrame


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


MAX_SESSIONS = int(_env_float("VOICE_MAX_SESSIONS", 4))
MAX_PER_ADDRESS = int(_env_float("VOICE_MAX_SESSIONS_PER_ADDRESS", 2))
SESSIONS_PER_HOUR = int(_env_float("VOICE_SESSIONS_PER_HOUR", 30))
IDLE_SECS = _env_float("WEB_VOICE_IDLE_SECS", 20)
MAX_MINUTES = _env_float("WEB_VOICE_MAX_MINUTES", 15)
DETECT_LANGUAGE_MAX_BYTES = int(_env_float("DETECT_LANGUAGE_MAX_BYTES", 256000))
BUSY_CODE = 4429

NUDGE = {
    "en": "Are you still there? Ask me anything about Agni College of Technology.",
    "ta": "நீங்க இன்னும் இருக்கீங்களா? Agni College of Technology பத்தி எதுவும் கேளுங்க.",
}
GOODBYE = {
    "en": "I'll end the voice chat for now. You can start it again any time. Thank you!",
    "ta": "இப்போதைக்கு voice chat-ஐ முடிக்கிறேன். எப்போ வேணும்னாலும் மறுபடியும் start பண்ணலாம். நன்றி!",
}
TIME_LIMIT = {
    "en": "We've reached the time limit for this voice chat. Please start a new one if you have more questions. Thank you!",
    "ta": "இந்த voice chat-ன் நேர வரம்பு முடிஞ்சிடுச்சு. இன்னும் கேள்விகள் இருந்தா புதுசா start பண்ணுங்க. நன்றி!",
}


def client_address(headers, fallback: str | None) -> str:
    forwarded = [part.strip() for part in (headers.get("x-forwarded-for") or "").split(",") if part.strip()]
    return forwarded[-1] if forwarded else (fallback or "unknown")


class SessionGate:
    """Counts open website voice calls, overall and per address, and starts per address per hour."""

    def __init__(self, max_sessions=MAX_SESSIONS, max_per_address=MAX_PER_ADDRESS, per_hour=SESSIONS_PER_HOUR):
        self.max_sessions, self.max_per_address, self.per_hour = max_sessions, max_per_address, per_hour
        self.open: dict[str, int] = defaultdict(int)
        self._starts: dict[str, deque] = defaultdict(deque)

    def enter(self, address: str, now: float | None = None) -> str | None:
        """None if the call may start (and counts it), else why not: "busy" or "too_many"."""
        now = time.monotonic() if now is None else now
        if self.max_sessions and sum(self.open.values()) >= self.max_sessions:
            return "busy"
        if self.max_per_address and self.open[address] >= self.max_per_address:
            return "too_many"
        starts = self._starts[address]
        while starts and now - starts[0] >= 3600:
            starts.popleft()
        if self.per_hour and len(starts) >= self.per_hour:
            return "too_many"
        starts.append(now)
        self.open[address] += 1
        return None

    def leave(self, address: str) -> None:
        self.open[address] = max(0, self.open[address] - 1)
        if not self.open[address]:
            del self.open[address]


gate = SessionGate()


class WebCallLimits:
    """Silence nudge, goodbye after more silence, and the call-length cap for one website call."""

    def __init__(self, idle_secs: float = IDLE_SECS, max_minutes: float = MAX_MINUTES):
        self.idle_secs, self.max_secs = idle_secs, max_minutes * 60
        self._ns = None
        self._idle = 0
        self._cap_task = None
        self._ending = False

    def attach(self, ns) -> None:
        """PipelineProfile.on_built hook."""
        self._ns = ns
        if self.idle_secs:
            ns.user_aggregator.add_event_handler("on_user_turn_idle", self._on_idle)
            ns.user_aggregator.add_event_handler("on_user_turn_started", self._on_user_started)
        ns.transport.add_event_handler("on_client_disconnected", self._on_disconnected)
        if self.max_secs:
            self._cap_task = asyncio.get_running_loop().create_task(self._cap())

    def _lang(self) -> str:
        return "ta" if self._ns.router.language == "ta" else "en"

    async def _on_user_started(self, *_):
        self._idle = 0

    async def _on_idle(self, *_):
        self._idle += 1
        if self._idle == 1:
            await self._ns.worker.queue_frames([TTSSpeakFrame(NUDGE[self._lang()])])
        else:
            await self._end(GOODBYE[self._lang()], "idle")

    async def _cap(self):
        await asyncio.sleep(self.max_secs)
        await self._end(TIME_LIMIT[self._lang()], "time_limit")

    async def _on_disconnected(self, *_):
        if self._cap_task:
            self._cap_task.cancel()

    async def _end(self, line: str, reason: str) -> None:
        if self._ending:
            return
        self._ending = True
        logger.info(f"[web-call] ending the call: {reason}")
        await self._ns.worker.queue_frames([TTSSpeakFrame(line), EndWorkerFrame(reason=reason)])
