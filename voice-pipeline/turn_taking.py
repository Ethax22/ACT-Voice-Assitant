"""Turn-taking for the voice pipeline: when the visitor's turn starts (and so
interrupts the bot) and when it ends (and so the bot answers).

Before 2026-10-07 both were left to Sarvam's server-side VAD: a turn started
when Sarvam heard any speech and ended after a fixed 700 ms of silence. Two
problems with that, both from the naturalness report:

- any sound — including a bare "okay" while the bot is talking — cut the bot
  off;
- a fixed silence either answers into a thinking pause or makes every reply
  wait the full 700 ms, with nothing in between.

`TURN_MODE=local` (the default) moves both decisions into this process:

- A local Silero VAD (bundled with Pipecat, runs on CPU) reports speech
  start/stop, and the strategies below decide turns from it.
- Sarvam STT stays on its own server-side segmentation (`endpointing="vad"`)
  but with a short silence window (STT_SEGMENT_SILENCE_MS), so the final
  transcript arrives soon after speech stops. It must NOT be switched to
  `endpointing="manual"`: that was tried first (2026-10-07) and Sarvam then
  only transcribed audio from the moment the local VAD confirmed speech,
  ~0.2 s late — every utterance lost its first word or two ("What courses do
  you offer?" came back as "What does the offer?").
- Start: `BargeInUserTurnStartStrategy` below. While the bot is silent, any
  speech starts the turn at once. While the bot is talking, the visitor has
  to keep talking for a moment, or say more than a bare acknowledgement,
  before the bot is interrupted.
- Stop, English: Pipecat's smart-turn v3 model listens to how the utterance
  ended and decides whether the visitor has finished. Finished -> answer
  straight away; not finished -> keep listening (up to SMART_TURN_MAX_WAIT of silence).
- Stop, Tamil: a plain silence timeout equal to the old behaviour. Smart-turn
  v3's published language list (23 languages, checked 2026-10-07) does not
  include Tamil, so it is not trusted to end Tamil turns. Set
  `TURN_STOP_TA=smart` to try it anyway.

The stop strategy is chosen from the language the call STARTS in; a call that
switches language midway keeps the one it started with.

`TURN_MODE=sarvam` restores the previous server-driven behaviour exactly.

None of the timings here have been tuned on a real microphone — they are
reasoned starting values, same caveat as VAD_TUNING in pipeline.py.
"""
import asyncio
import os
import time

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start.base_user_turn_start_strategy import BaseUserTurnStartStrategy

TURN_MODE = os.environ.get("TURN_MODE", "local").lower()

# How long the visitor must keep talking, after the VAD has confirmed speech,
# before a speaking bot is interrupted. With the VAD's own 0.2 s confirmation
# this means roughly 0.6 s of continuous speech — longer than "okay" or "mm",
# shorter than "wait, one question".
BARGE_IN_SUSTAIN_SECS = float(os.environ.get("BARGE_IN_SUSTAIN_SECS", "0.4"))
# A second burst of speech this soon after a short one means the visitor is
# really trying to talk ("Wait, ... actually"), so it interrupts at once.
BARGE_IN_RESUME_WINDOW_SECS = 1.0
# How long after the visitor's turn ends the bot still counts as "about to
# speak" if no audio has started (covers a slow reply; see the strategy).
REPLY_PENDING_MAX_SECS = 6.0
# Tamil end-of-turn: silence after the VAD's own 0.2 s stop window. 0.5 keeps
# the total at the 700 ms the pipeline used before.
SILENCE_TIMEOUT_SECS = float(os.environ.get("TURN_SILENCE_TIMEOUT_SECS", "0.5"))
# Longest smart-turn will hold a turn open on silence when it thinks the
# visitor hasn't finished (Pipecat's default is 3 s).
SMART_TURN_MAX_WAIT = float(os.environ.get("SMART_TURN_MAX_WAIT_SECS", "1.5"))

# Acknowledgements that should not cut the bot off mid-reply. Kept in line
# with rag-backend/main.py's _BACKCHANNELS, which handles the same words when
# they arrive as a turn of their own.
_BACKCHANNELS = frozenset({
    "yeah", "yes", "yep", "yup", "ya", "ok", "okay", "k", "right", "alright",
    "hmm", "hm", "mm", "mhm", "uh huh", "sure", "fine", "i see", "got it",
    "oh", "oh okay", "ok ok", "okay okay", "cool", "nice", "correct",
    "சரி", "ஆமா", "ஆமாம்", "ம்ம்", "ஓகே", "ஓ", "அப்படியா", "சரி சரி", "sari", "aama", "seri",
})
_PUNCTUATION = str.maketrans("", "", ".,!?;:\"'…")


def is_backchannel(text: str) -> bool:
    return (text or "").translate(_PUNCTUATION).strip().lower() in _BACKCHANNELS


class BargeInUserTurnStartStrategy(BaseUserTurnStartStrategy):
    """Starts the user's turn — immediately when the bot is idle, and only on
    sustained speech or real words when the bot is talking or about to.

    "About to" covers the gap between the visitor finishing and the bot's
    first audio. Without it, any short noise in that gap (a breath, a click,
    the tail of the visitor's own last word) started a new, empty turn: the
    reply being generated was thrown away and the call stalled for seconds
    waiting for a transcript that never came — seen in testing 2026-10-07."""

    def __init__(self, *, sustain_secs: float = BARGE_IN_SUSTAIN_SECS, **kwargs):
        super().__init__(**kwargs)
        self._sustain_secs = sustain_secs
        self._bot_speaking = False
        self._sustain_task: asyncio.Task | None = None
        self._short_burst_at = 0.0
        self._reply_pending_until = 0.0

    async def handle_user_turn_started(self):
        self._reply_pending_until = 0.0
        await self._cancel_sustain_timer()

    async def handle_user_turn_stopped(self):
        # A reply is now on its way. Bounded, in case none ever arrives.
        self._reply_pending_until = time.monotonic() + REPLY_PENDING_MAX_SECS

    def _bot_busy(self) -> bool:
        return self._bot_speaking or time.monotonic() < self._reply_pending_until

    async def cleanup(self):
        await super().cleanup()
        await self._cancel_sustain_timer()

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._reply_pending_until = 0.0
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            if not self._bot_busy():
                await self.trigger_user_turn_started()
                return ProcessFrameResult.STOP
            if time.monotonic() - self._short_burst_at < BARGE_IN_RESUME_WINDOW_SECS:
                logger.debug(f"{self}: second burst of speech — interrupting")
                await self.trigger_user_turn_started()
                return ProcessFrameResult.STOP
            await self._cancel_sustain_timer()
            self._sustain_task = self.task_manager.create_task(
                self._sustain_timer(), f"{self}::_sustain_timer"
            )
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            # Speech ended before the timer ran out: a short burst. Whether it
            # interrupts is now up to what it turns out to say (below), or to
            # the visitor speaking again straight away (above).
            if self._sustain_task:
                self._short_burst_at = time.monotonic()
            await self._cancel_sustain_timer()
        elif isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            return await self._handle_transcript(frame)
        return ProcessFrameResult.CONTINUE

    async def _handle_transcript(self, frame) -> ProcessFrameResult:
        text = (frame.text or "").strip()
        if not text:
            return ProcessFrameResult.CONTINUE
        if self._bot_busy() and isinstance(frame, InterimTranscriptionFrame) and len(text.split()) < 3:
            # A partial like "H" or "Hm" says nothing yet about whether this
            # is "Hmm." or the start of a sentence. Acting on it cut the bot
            # off 60 ms before the final "Hmm." arrived and was ignored (seen
            # in the 2026-10-07 scenario run). The sustain timer and the final
            # transcript decide instead.
            return ProcessFrameResult.CONTINUE
        if self._bot_busy() and is_backchannel(text):
            if isinstance(frame, TranscriptionFrame):
                logger.debug(f"{self}: ignoring backchannel {text!r} while the bot is speaking")
                # Drop it, or it would be prepended to the visitor's next turn.
                await self.trigger_reset_aggregation()
            return ProcessFrameResult.CONTINUE
        await self.trigger_user_turn_started()
        return ProcessFrameResult.STOP

    async def _sustain_timer(self):
        try:
            await asyncio.sleep(self._sustain_secs)
        except asyncio.CancelledError:
            return
        self._sustain_task = None
        logger.debug(f"{self}: sustained speech over the bot — interrupting")
        await self.trigger_user_turn_started()

    async def _cancel_sustain_timer(self):
        task, self._sustain_task = self._sustain_task, None
        if task and task is not asyncio.current_task():
            await self.task_manager.cancel_task(task)


def stt_silence_duration_ms(server_driven_default: int) -> int:
    """Sarvam's own end-of-segment silence. Under TURN_MODE=sarvam this IS the
    end of the turn, so the caller's tuned value applies. Under local mode it
    only decides how soon the final transcript is released — the turn itself
    ends when the stop strategy below says so — so it can be much shorter."""
    if TURN_MODE != "local":
        return server_driven_default
    return int(os.environ.get("STT_SEGMENT_SILENCE_MS", "300"))


def build_user_aggregator_params(language: str):
    """LLMUserAggregatorParams for this call, or None for TURN_MODE=sarvam
    (Pipecat then follows Sarvam's own turn signals, as before)."""
    if TURN_MODE != "local":
        return None

    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregatorParams
    from pipecat.turns.user_stop import (
        SpeechTimeoutUserTurnStopStrategy,
        TurnAnalyzerUserTurnStopStrategy,
    )
    from pipecat.turns.user_turn_strategies import UserTurnStrategies

    stop_mode = os.environ.get(
        f"TURN_STOP_{language.upper()}", "smart" if language == "en" else "silence"
    ).lower()
    if stop_mode == "smart":
        from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
        from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

        stop = TurnAnalyzerUserTurnStopStrategy(
            turn_analyzer=LocalSmartTurnAnalyzerV3(
                params=SmartTurnParams(stop_secs=SMART_TURN_MAX_WAIT)
            )
        )
    else:
        stop = SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=SILENCE_TIMEOUT_SECS)
    logger.info(f"Turn-taking: local VAD, end-of-turn={stop_mode!r} ({language})")

    return LLMUserAggregatorParams(
        vad_analyzer=SileroVADAnalyzer(),
        user_turn_strategies=UserTurnStrategies(
            start=[BargeInUserTurnStartStrategy()], stop=[stop]
        ),
    )
