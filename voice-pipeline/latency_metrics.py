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

Place it anywhere downstream of the services that emit metrics — the end of the
pipeline is fine, system frames flow through every processor. It never
modifies or drops a frame.
"""
import time

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    Frame,
    MetricsFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import ProcessingMetricsData, TTFBMetricsData
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class LatencyLogger(FrameProcessor):
    def __init__(self, session_id: str, **kwargs):
        super().__init__(**kwargs)
        self._session_id = session_id[:8]
        self._user_stopped_at: float | None = None
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
            self._user_stopped_at = None
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._turn += 1
            self._user_stopped_at = time.monotonic()
        elif isinstance(frame, BotStartedSpeakingFrame):
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
                    logger.info(
                        f"[latency] session={self._session_id} turn={self._turn} "
                        f"{item.processor} first byte: {item.value:.2f}s"
                    )
                elif isinstance(item, ProcessingMetricsData):
                    logger.info(
                        f"[latency] session={self._session_id} turn={self._turn} "
                        f"{item.processor} processing: {item.value:.2f}s"
                    )
