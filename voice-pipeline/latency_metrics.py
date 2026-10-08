"""Per-turn latency logging for the voice pipeline (web voice bot, Phase 1 of
the latency work — measure before tuning).

Two things are logged, both at INFO with a "[latency]" prefix so they are easy
to grep out of the server log:

1. Pipecat's own per-service metrics (PipelineParams(enable_metrics=True)):
   time to first byte for STT, the LLM (our rag-backend) and TTS, as
   MetricsFrames emitted by each service.
2. The number the visitor actually feels: seconds from the user's turn ending
   to the bot's first audio. Pipecat emits UserStoppedSpeakingFrame when the
   user's turn ends and BotStartedSpeakingFrame when the bot's first audio is
   about to play; this processor pairs them.

2026-10-09: one more line per spoken reply, `[turn] ...`, that gathers what the
visitor and the bot did in that turn in one place (transcript, detected and
reply style, language-detection wait, LLM and TTS first byte, user-stopped ->
first audio, the voice in use). The retrieval time and the LLM's first token
as seen by the backend are in the backend's `[turn-backend] session=<same 8
characters>` lines, since they happen in another process.

Place it anywhere downstream of the services that emit metrics — the end of the
pipeline is fine, system frames flow through every processor. It never
modifies or drops a frame.
"""
import asyncio
import time

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    MetricsFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import ProcessingMetricsData, TTFBMetricsData
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from style_classifier import detect_style


class TurnTrace:
    """What ConversationRouter learned about the current turn, for LatencyLogger
    to print next to the timings. The router writes, the logger reads."""

    def __init__(self, voice_id: str):
        self.voice_id = voice_id
        self.turn: dict | None = None

    def begin_turn(self, **fields):
        # Language detection no longer holds the transcript, so its wait is 0.
        self.turn = {"lang_detect_wait_s": 0.0, **fields}


class LatencyLogger(FrameProcessor):
    def __init__(self, session_id: str, trace: TurnTrace | None = None, context=None, **kwargs):
        super().__init__(**kwargs)
        self._session_id = session_id[:8]
        self._trace = trace
        # The shared LLMContext: TTSTextFrames do not reliably reach the end of
        # the pipeline, so the spoken reply is read from the context instead.
        self._context = context
        self._llm_ttfb: float | None = None
        self._tts_ttfb: float | None = None
        self._reply_text: list[str] = []
        self._pending: dict | None = None
        self._user_stopped_at: float | None = None
        self._user_started_at: float | None = None
        self._bot_started_at: float | None = None
        self._turn = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        try:
            self._observe(frame)
        except Exception:
            # Logging must never break a live call.
            logger.exception("latency logger failed on a frame — ignoring")
        await self.push_frame(frame, direction)

    def _observe(self, frame: Frame):
        if isinstance(frame, UserStartedSpeakingFrame):
            now = time.monotonic()
            self._emit_turn()
            if self._bot_started_at is not None:
                # Barge-in: the bot was still talking when the user's turn began.
                logger.info(
                    f"[latency] session={self._session_id} turn={self._turn} "
                    f"user interrupted the bot {now - self._bot_started_at:.2f}s into its reply"
                )
                self._bot_started_at = None
            self._user_stopped_at = None
            self._user_started_at = now
        elif isinstance(frame, TTSTextFrame):
            self._reply_text.append(frame.text)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            # BotStoppedSpeaking is a system frame and overtakes the data frames
            # the assistant aggregator uses to commit the reply to the context,
            # so give it a moment before reading the reply back.
            self.create_task(self._emit_turn_soon())
            if self._bot_started_at is not None:
                logger.info(
                    f"[latency] session={self._session_id} turn={self._turn} "
                    f"bot spoke for {time.monotonic() - self._bot_started_at:.2f}s"
                )
                self._bot_started_at = None
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._turn += 1
            self._llm_ttfb = self._tts_ttfb = None
            self._reply_text = []
            self._user_stopped_at = time.monotonic()
            if self._user_started_at is not None:
                logger.info(
                    f"[latency] session={self._session_id} turn={self._turn} "
                    f"user spoke for {self._user_stopped_at - self._user_started_at:.2f}s"
                )
                self._user_started_at = None
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot_started_at = time.monotonic()
            gap_for_turn = None
            if self._user_stopped_at is not None:
                gap_for_turn = time.monotonic() - self._user_stopped_at
            self._pending = {
                "turn": self._turn,
                "stop_to_audio": gap_for_turn,
                "llm_ttfb": self._llm_ttfb,
                "tts_ttfb": self._tts_ttfb,
                "fields": self._trace.turn if self._trace else None,
            }
            if self._trace:
                self._trace.turn = None
            if self._user_stopped_at is not None:
                gap = time.monotonic() - self._user_stopped_at
                logger.info(
                    f"[latency] session={self._session_id} turn={self._turn} "
                    f"user stopped -> bot audio starts: {gap:.2f}s"
                )
                self._user_stopped_at = None
        elif isinstance(frame, MetricsFrame):
            for item in frame.data:
                if isinstance(item, TTFBMetricsData):
                    if "LLM" in item.processor:
                        self._llm_ttfb = item.value
                    elif "TTS" in item.processor:
                        self._tts_ttfb = item.value
                    logger.info(
                        f"[latency] session={self._session_id} turn={self._turn} "
                        f"{item.processor} first byte: {item.value:.2f}s"
                    )
                elif isinstance(item, ProcessingMetricsData):
                    logger.info(
                        f"[latency] session={self._session_id} turn={self._turn} "
                        f"{item.processor} processing: {item.value:.2f}s"
                    )

    async def _emit_turn_soon(self):
        await asyncio.sleep(0.6)
        self._emit_turn()

    def _emit_turn(self):
        """One `[turn]` line per spoken reply, once the bot has finished or been
        cut off (so the reply text and its actual style are known)."""
        pending, self._pending = self._pending, None
        if pending is None:
            return
        fields = pending["fields"] or {}
        reply = "".join(self._reply_text).strip()
        self._reply_text = []
        if not reply and self._context is not None:
            try:
                last = self._context.messages[-1] if self._context.messages else None
                if last and last.get("role") == "assistant":
                    reply = str(last.get("content") or "").strip()
            except Exception:
                pass

        def secs(value):
            return "n/a" if value is None else f"{value:.2f}s"

        logger.info(
            f"[turn] session={self._session_id} turn={pending['turn']} "
            f"transcript={fields.get('transcript', '(greeting / no transcript)')!r} "
            f"detected_style={fields.get('detected_style', 'n/a')} "
            f"reply_style_requested={fields.get('reply_style', 'n/a')} "
            f"reply_style_actual={(detect_style(reply) or 'n/a') if reply else 'n/a'} "
            f"stt_language={fields.get('stt_language', 'n/a')} "
            f"lang_detect_wait={secs(fields.get('lang_detect_wait_s'))} "
            f"llm_first_token={secs(pending['llm_ttfb'])} "
            f"tts_first_audio={secs(pending['tts_ttfb'])} "
            f"user_stop_to_first_audio={secs(pending['stop_to_audio'])} "
            f"voice={self._trace.voice_id if self._trace else 'n/a'} "
            f"reply={reply[:120]!r}"
        )
