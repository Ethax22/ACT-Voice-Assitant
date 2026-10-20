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
import re
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

# Added 2026-10-08 (Prakash: wind, breath and room noise were interrupting the
# bot and it then did not answer): while the bot is talking or about to talk,
# sound alone no longer interrupts it. The visitor's speech has to turn into
# real words first — see `is_real_interruption`. Set BARGE_IN_REQUIRE_WORD=0 to
# get the earlier behaviour back (sustained speech interrupts by itself).
BARGE_IN_REQUIRE_WORD = os.environ.get("BARGE_IN_REQUIRE_WORD", "1") != "0"
# A transcript only counts if the local VAD heard speech this recently, so a
# word the speech-to-text invents from noise the VAD rejected is ignored.
VAD_RECENT_SECS = 2.5
# A transcript of at least this many real words is accepted as an interruption even when the VAD has
# not reported speech lately. The VAD only logs the start and end of speech, so for a sentence longer
# than VAD_RECENT_SECS the transcript always looked "stale", and when the VAD flickered on the
# phone line the caller's whole sentence was thrown away (2026-10-20). 0 = off (the website).
BARGE_IN_TRUST_WORDS = int(os.environ.get("BARGE_IN_TRUST_WORDS", "0"))
# With BARGE_IN_REQUIRE_WORD on, an interruption waits for the speech-to-text to return words,
# which on the phone line took 2 to 3 s from the first sound (measured 2026-10-20: the caller
# talked over the bot and it kept talking). If speech then goes on for this many seconds without
# stopping, that is a person talking, so interrupt without waiting for words. 0 = off, the
# website's behaviour; the phone agent sets it (phone-agent/config.py).
TRACE = os.environ.get("TURN_TAKING_TRACE", "0") == "1"  # log every event the barge-in rule sees
BARGE_IN_VAD_ONLY_SECS = float(os.environ.get("BARGE_IN_VAD_ONLY_SECS", "0"))

# Single words that interrupt on their own ("wait", "stop"...). Anything else
# needs at least two words.
_INTERRUPT_WORDS = frozenset({
    "wait", "stop", "hold", "sorry", "excuse", "pardon", "hello", "no", "actually",
    "நிறுத்து", "நிறுத்துங்க", "நில்லுங்க", "இருங்க", "இல்ல", "வேண்டாம்", "ஒரு",
})
_WORD = re.compile(r"[A-Za-z஀-௿]{2,}")

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


def is_real_interruption(text: str) -> bool:
    """True if `text` is words a person said to the bot, not a stray sound the
    speech-to-text turned into a syllable ("She.", "And", "Hm")."""
    if is_backchannel(text):
        return False
    words = [w.lower() for w in _WORD.findall(text or "")]
    if len(words) >= 2:
        return any(not is_backchannel(w) for w in words)
    return bool(words) and words[0] in _INTERRUPT_WORDS


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
        self._last_vad_speech_at = 0.0

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
        if TRACE and isinstance(frame, (BotStartedSpeakingFrame, BotStoppedSpeakingFrame, VADUserStartedSpeakingFrame,
                                        VADUserStoppedSpeakingFrame, InterimTranscriptionFrame, TranscriptionFrame)):
            logger.info(f"[trace] {type(frame).__name__} busy={self._bot_busy()} text={getattr(frame, 'text', '')[:40]!r}")
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._reply_pending_until = 0.0
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self._last_vad_speech_at = time.monotonic()
            if not self._bot_busy():
                await self.trigger_user_turn_started()
                return ProcessFrameResult.STOP
            logger.info(f"[interrupt] VAD heard speech while the bot is busy (words needed: {BARGE_IN_REQUIRE_WORD}, "
                        f"sustained-speech timer: {BARGE_IN_VAD_ONLY_SECS or 'off'})")
            if BARGE_IN_REQUIRE_WORD:
                # Bot is busy: wait for words (see _handle_transcript), unless the speech simply
                # keeps going (BARGE_IN_VAD_ONLY_SECS).
                if BARGE_IN_VAD_ONLY_SECS > 0:
                    await self._cancel_sustain_timer()
                    self._sustain_task = self.task_manager.create_task(
                        self._sustain_timer(BARGE_IN_VAD_ONLY_SECS), f"{self}::_sustain_timer"
                    )
                return ProcessFrameResult.CONTINUE
            if time.monotonic() - self._short_burst_at < BARGE_IN_RESUME_WINDOW_SECS:
                logger.info("[interrupt] second burst of speech within 1 s of a short one — interrupting")
                await self.trigger_user_turn_started()
                return ProcessFrameResult.STOP
            await self._cancel_sustain_timer()
            self._sustain_task = self.task_manager.create_task(
                self._sustain_timer(), f"{self}::_sustain_timer"
            )
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._last_vad_speech_at = time.monotonic()
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
        kind = "final" if isinstance(frame, TranscriptionFrame) else "interim"
        if self._bot_busy() and BARGE_IN_REQUIRE_WORD:
            heard_speech = time.monotonic() - self._last_vad_speech_at < VAD_RECENT_SECS
            if BARGE_IN_TRUST_WORDS and len(_WORD.findall(text)) >= BARGE_IN_TRUST_WORDS and is_real_interruption(text):
                heard_speech = True
            if not (heard_speech and is_real_interruption(text)):
                if kind == "final":
                    why = "not real words" if heard_speech else "no speech heard by the VAD"
                    logger.info(f"[interrupt] ignored {text!r} while the bot is busy ({why})")
                    # Drop it, or it would be prepended to the visitor's next turn.
                    await self.trigger_reset_aggregation()
                return ProcessFrameResult.CONTINUE
        elif self._bot_busy() and isinstance(frame, InterimTranscriptionFrame) and len(text.split()) < 3:
            # A partial like "H" or "Hm" says nothing yet about whether this
            # is "Hmm." or the start of a sentence. Acting on it cut the bot
            # off 60 ms before the final "Hmm." arrived and was ignored (seen
            # in the 2026-10-07 scenario run). The sustain timer and the final
            # transcript decide instead.
            return ProcessFrameResult.CONTINUE
        elif self._bot_busy() and is_backchannel(text):
            if isinstance(frame, TranscriptionFrame):
                logger.info(f"[interrupt] ignored backchannel {text!r} while the bot is speaking")
                await self.trigger_reset_aggregation()
            return ProcessFrameResult.CONTINUE
        if self._bot_busy():
            # Only an actual interruption is worth a log line; while the bot is
            # idle every partial transcript used to print one.
            logger.info(f"[interrupt] {kind} transcript {text!r} — interrupting the bot")
        await self.trigger_user_turn_started()
        return ProcessFrameResult.STOP

    async def _sustain_timer(self, secs: float | None = None):
        secs = secs or self._sustain_secs
        try:
            await asyncio.sleep(secs)
        except asyncio.CancelledError:
            return
        self._sustain_task = None
        logger.info(f"[interrupt] VAD speech kept going {secs}s over the bot — interrupting")
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


def _vad_params():
    """Silero VAD thresholds from the environment (VAD_CONFIDENCE, VAD_START_SECS, VAD_MIN_VOLUME);
    None keeps Pipecat's defaults, which is what the website uses."""
    from pipecat.audio.vad.vad_analyzer import VADParams

    given = {k: float(os.environ[e]) for k, e in
             (("confidence", "VAD_CONFIDENCE"), ("start_secs", "VAD_START_SECS"), ("min_volume", "VAD_MIN_VOLUME"))
             if os.environ.get(e)}
    return VADParams(**given) if given else None


def _vad():
    from pipecat.audio.vad.silero import SileroVADAnalyzer

    return SileroVADAnalyzer(params=_vad_params())


def _traced_vad():
    """A Silero VAD that logs each change of state (TURN_TAKING_TRACE=1 only)."""
    from pipecat.audio.vad.silero import SileroVADAnalyzer

    class TracedVAD(SileroVADAnalyzer):
        _seen = None

        async def analyze_audio(self, buffer):
            state = await super().analyze_audio(buffer)
            if state != self._seen:
                logger.info(f"[trace] VAD state -> {state.name}")
                self._seen = state
            return state

    return TracedVAD(params=_vad_params())


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
        vad_analyzer=_traced_vad() if TRACE else _vad(),
        user_turn_strategies=UserTurnStrategies(
            start=[BargeInUserTurnStartStrategy()], stop=[stop]
        ),
    )
