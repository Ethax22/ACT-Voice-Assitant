"""Voice pipeline: Sarvam STT -> shared RAG backend -> Cartesia TTS, composed
with Pipecat. Serves BOTH languages, parameterized by `language` — points at
the SAME rag-backend for both (via an OpenAI-compatible custom endpoint)
that's what keeps English and Tamil answers consistent, per the one-shared-
brain architecture (CLAUDE.md decision #2). English used to run on Deepgram's
Voice Agent; that path was retired 2026-09-25 (CLAUDE.md decision #1) in
favor of this same Sarvam/Pipecat pipeline for both languages.

Model choices (Sept 2026, latest per docs.sarvam.ai — see
docs/Voice_Model_Choices.md for rationale):
- STT: saaras:v4 (Sarvam's current default/recommended), same model as
  language_detection.py's batch call so one model serves both. Pipecat 1.12.0's
  SarvamRealtimeSTTService accepts it via Settings(model=...) — it defaults to
  "saaras:v3-realtime" otherwise (older Pipecat 1.10.0 hardcoded that; see
  pipecat/services/sarvam/stt.py `_SUPPORTED_REALTIME_MODELS`). Note the name
  is "saaras:v4", NOT "saaras:v4-realtime", which the endpoint rejects.
  `language_code` is configured per language.
- TTS: Cartesia Sonic (sonic-3.6) since 2026-10-18, replacing Sarvam bulbul:v3.
"""
import asyncio
import os
from dataclasses import dataclass, field
from typing import Any, Literal

from loguru import logger

Language = Literal["en", "ta"]

# Keyed by reply STYLE, not just language: "ta_en" is Tamil mixed with English
# (Tanglish), which gets its own persona line. The text after "reply_language:"
# is read by rag-backend (main.py's _reply_language), which enforces it and
# writes the real style instructions itself.
PERSONA_PROMPTS: dict[str, str] = {
    "en": (
        "You are Agni AI, the voice assistant for Agni College of Technology. "
        "Talk like a helpful senior student showing someone around campus: warm, "
        "direct, and brief. Keep answers short since this is a spoken "
        "conversation, not a lookup. Respond in English.\n"
        "reply_language: en"
    ),
    "ta": (
        "You are Agni AI, the voice assistant for Agni College of Technology. "
        "Talk like a helpful senior student showing someone around campus: warm, "
        "direct, and brief. Keep answers short since this is a spoken "
        "conversation, not a lookup. Respond in everyday spoken Chennai Tamil.\n"
        "reply_language: ta"
    ),
    "ta_en": (
        "You are Agni AI, the voice assistant for Agni College of Technology. "
        "Talk like a helpful senior student showing someone around campus: warm, "
        "direct, and brief. Keep answers short since this is a spoken "
        "conversation, not a lookup. Respond in natural spoken Tamil mixed with "
        "English (Tanglish), the way the visitor does.\n"
        "reply_language: ta_en"
    ),
}

GREETINGS: dict[Language, str] = {
    "en": "Hi! I'm Agni AI. Ask me about admissions, courses, placements, or campus life at Agni College of Technology.",
    "ta": "வணக்கம்! நான் Agni AI. சேர்க்கை, படிப்புகள், வேலைவாய்ப்பு பற்றி என்னிடம் கேளுங்கள்.",
}

# Phase 4 (turn-taking/barge-in): Sarvam's realtime STT drives turn
# boundaries server-side by default (endpointing="vad" in
# SarvamRealtimeSTTService, left at its default below) and PipelineTask's
# allow_interruptions=True (bottom of this file) lets a new user utterance
# cut off TTS mid-reply — this is the whole turn-taking mechanism, and it's
# identical for both languages by construction (one build_pipeline(), no
# per-language branch here). Tamil was verified live before; English hasn't
# been listening-tested yet — that's this phase's actual work, and it needs a
# human with a mic and speakers, not a code change. If English (or Tamil)
# barge-in feels laggy or trigger-happy once tested, these are the knobs,
# passed as SarvamRealtimeSTTSettings fields (all None below = Sarvam's own
# server-side defaults, not yet overridden):
#   - threshold: VAD sensitivity (0.0-1.0ish per Sarvam's docs) — lower fires
#     on quieter/shorter sounds (more trigger-happy), higher requires louder,
#     clearer speech (less sensitive, more laggy-feeling).
#   - silence_duration_ms: how long a pause must last before Sarvam decides
#     the user has stopped talking. Lower = snappier end-of-turn but risks
#     cutting off someone mid-sentence during a natural pause; higher =
#     safer but feels sluggish.
#   - min_speech_duration_ms: shortest sound Sarvam will count as the start
#     of real speech, filtering out brief noise. Lower = catches soft/quick
#     starts but more prone to false triggers; higher = fewer false starts
#     but risks missing a quiet "um" or fast interruption.
#   - prefix_padding_ms: audio kept just before detected speech onset, so
#     the first word isn't clipped. Unlike the three above, this one is a
#     constructor argument on SarvamRealtimeSTTService itself, not a
#     Settings field — Pipecat's source draws that line because it's an
#     init-only connection parameter, not something sent as a live
#     config.update.
# Tune per language if English and Tamil end up needing different feel (set
# per-language in a dict here, same pattern as TTS_VOICES below) — no
# evidence yet that they will.
#
# Correction (2026-09-29): left at Sarvam's raw server defaults (all None),
# a real test session showed every single reply after the greeting getting
# cancelled before it ever reached TTS — confirmed via voice-pipeline logs:
# "user turn started" (broadcasting interruption) kept re-firing within
# ~70-300ms of the previous turn's own "stopped speaking" event, splitting
# one continuous utterance into many rapid-fire turns, each interrupting the
# still-in-flight LLM/TTS work from the one before it (rag-backend's own
# logs confirmed several of those replies WERE generated successfully
# server-side — they were discarded client-side, not failed upstream).
# silence_duration_ms raised below to stop that premature re-triggering,
# but the tester confirmed on a follow-up (headphones, not speakers) that
# it kept happening anyway, well under human reaction time (e.g. 13ms after
# the greeting's TTS log line, 72ms after another reply's) — ruling out
# acoustic echo and pointing at plain background noise (room noise, fan,
# breath) crossing Sarvam's VAD threshold. Checked Sarvam's own docs
# (docs.sarvam.ai, 2026-09-29 — this "threshold" field wasn't in the
# installed Pipecat package's docstring, which only said "optional VAD
# sensitivity threshold" with no range): default 0.3, range 0.0-1.0, higher
# = less sensitive to quiet sounds. Left at that default (via None) the
# whole time; raising it here is the actual lever for this problem, not
# silence_duration_ms/min_speech_duration_ms, which only affect timing, not
# amplitude sensitivity. Note min_speech_duration_ms=250 below is also just
# Sarvam's own default restated (not a real change) — kept explicit so the
# next person doesn't assume the None default already covers it.
# Still a reasoned starting value, not a verified-by-ear final tuning — this
# is the same "needs a real human listening/mic test" gap the Implementation
# Plan's Phase 4 flagged as unfinished. If 0.5 now misses real quiet speech
# onsets, come back down; if it's still too trigger-happy on background
# noise, go higher.
VAD_TUNING = {
    "threshold": 0.5,
    "silence_duration_ms": 700,
    "min_speech_duration_ms": 250,
    "prefix_padding_ms": None,
}

# Model choices — see docs/Voice_Model_Choices.md.
STT_MODEL = "saaras:v4"

LANGUAGE_CODES: dict[Language, str] = {
    "en": "en-IN",
    "ta": "ta-IN",
}

# Speaking is Cartesia only (Prakash, 2026-10-18). Listening (Saaras) and the
# answers (Sarvam 105B via rag-backend) stay on Sarvam. The Sarvam voices - stock
# Bulbul, the Sarvam cloned voice and its custom service - were removed; they are
# in git history (commit f86072e and earlier) if ever needed. A Cartesia voice,
# stock or cloned, is an ordinary voice id on the same streaming websocket.
CARTESIA_MODEL = os.environ.get("CARTESIA_MODEL", "").strip() or "sonic-3.6"
CARTESIA_VOICE_ID = os.environ.get("CARTESIA_VOICE_ID", "").strip() or None


def cartesia_api_key() -> str:
    # The local .env was first written with the name CARTESIAN_API_KEY; accept both.
    key = os.environ.get("CARTESIA_API_KEY") or os.environ.get("CARTESIAN_API_KEY")
    if not key:
        raise RuntimeError("CARTESIA_API_KEY is missing from voice-pipeline/.env")
    return key


def cartesia_generation_settings() -> dict[str, float | str]:
    """Delivery settings for the Cartesia voice, from the env; empty = voice defaults.

    CARTESIA_SPEED 0.6-1.5 (1.0 = normal), CARTESIA_VOLUME 0.5-2.0, and
    CARTESIA_EMOTION (e.g. "calm", "content", "enthusiastic"; Cartesia's docs say
    emotion works best on some voices, so judge it by ear). Used by the live voice
    and by greeting_cache.py, so greetings match calls.
    """
    out: dict[str, float | str] = {}
    speed = os.environ.get("CARTESIA_SPEED", "").strip()
    if speed:
        out["speed"] = min(1.5, max(0.6, float(speed)))
    volume = os.environ.get("CARTESIA_VOLUME", "").strip()
    if volume:
        out["volume"] = min(2.0, max(0.5, float(volume)))
    emotion = os.environ.get("CARTESIA_EMOTION", "").strip()
    if emotion:
        out["emotion"] = emotion
    return out


@dataclass
class PipelineProfile:
    """What differs between a website call and another kind of call (the phone
    agent). The defaults are the website's, so build_pipeline(ws, lang, id) with no
    profile behaves exactly as it did before profiles existed."""

    serializer: Any = None  # None: the browser widget's raw PCM16 over the socket
    audio_in_rate: int = 16000
    audio_out_rate: int = 24000
    tts_rate: int = 24000
    greetings: dict[str, str] | None = None  # None: GREETINGS above
    llm_headers: dict[str, str] = field(default_factory=dict)  # sent to rag-backend on every turn
    tools: Any = None  # a Pipecat ToolsSchema offered to the model
    function_handlers: dict[str, Any] = field(default_factory=dict)  # tool name -> async handler(params)
    user_idle_timeout: float = 0  # seconds of caller silence before on_user_turn_idle fires; 0 = never
    # Called once when the pipeline is built, with the parts a call needs to hook into
    # (worker, llm, context, user_aggregator, transport, router).
    on_built: Any = None
    audio_out_mixer: Any = None  # e.g. a looping background sound under the call (phone only)
    stt_language_code: str | None = None  # None: the call's language (en-IN / ta-IN); "auto" lets Sarvam pick
    # 2: the saved recording keeps the caller (left) and the bot (right) apart, so a call viewer can
    # draw them as two tracks. The website keeps one mixed track.
    audio_channels: int = 1
    observers: list = field(default_factory=list)  # Pipecat observers, e.g. the phone test page's live view
    # False: speak the greeting live and do not cache it (an outbound greeting holds the lead's name).
    cache_greeting: bool = True


def build_pipeline(websocket, language: Language, session_id: str, profile: PipelineProfile | None = None):
    """Builds the Pipecat pipeline for one browser voice session.

    `websocket` is an already-`accept()`-ed FastAPI WebSocket connection from
    the browser widget (see server.py) — audio flows browser <-> this
    pipeline <-> Sarvam, with the RAG backend called as a plain OpenAI-
    compatible LLM step in between. `language` selects STT/TTS language
    settings, persona prompt, and greeting; the RAG backend itself never
    forks by language (CLAUDE.md decision #2). `session_id` (assigned by
    server.py per websocket connection) namespaces this call's Phase 10
    transcript + audio log.
    """
    from pipecat.frames.frames import TTSSpeakFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
    from pipecat.services.openai.llm import OpenAILLMService
    from pipecat.services.sarvam.stt import SarvamRealtimeSTTService
    from pipecat.services.cartesia.tts import CartesiaTTSService, GenerationConfig
    from pipecat.services.tts_service import TextAggregationMode
    from pipecat.transports.websocket.fastapi import (
        FastAPIWebsocketParams,
        FastAPIWebsocketTransport,
    )

    import greeting_cache
    from conversation_logger import TranscriptLogger, save_session_audio
    from greeting_cache import GreetingPlayer, PlayGreetingFrame
    from conversation_router import ConversationRouter, ConversationRouterCallbacks
    from latency_metrics import LatencyLogger, TurnTrace
    from raw_pcm_serializer import RawPCMSerializer
    from speech_text_filter import to_speech
    from turn_taking import build_user_aggregator_params, stt_silence_duration_ms
    import sentence_split_fix

    # Tamil replies: keep "B. E." / "M. E." in one sentence for the voice (see the module).
    sentence_split_fix.install()
    profile = profile or PipelineProfile()
    greetings = profile.greetings or GREETINGS
    sarvam_api_key = os.environ["SARVAM_API_KEY"]
    rag_backend_public_url = os.environ["RAG_BACKEND_PUBLIC_URL"].rstrip("/")
    rag_backend_auth_key = os.environ.get("RAG_BACKEND_AUTH_KEY", "")
    language_code = profile.stt_language_code or LANGUAGE_CODES[language]

    transport = FastAPIWebsocketTransport(
        websocket,
        FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            # NOTE: TransportParams (FastAPIWebsocketParams's base class) has
            # NO "sample_rate" field — only these two. An earlier version of
            # this file passed `sample_rate=16000`, which is a Pydantic model
            # that silently drops unknown kwargs instead of erroring, so it
            # had ZERO effect: audio_out silently fell back to Pipecat's
            # default (24000, the rate the TTS is asked for),
            # while the widget was told to play Tamil audio back at 16000 Hz
            # — that mismatch is what made the voice sound robotic/slowed.
            audio_in_sample_rate=profile.audio_in_rate,   # website: matches the mic audio the widget sends
            audio_out_sample_rate=profile.audio_out_rate,  # website: the rate Cartesia is asked for, no resampling
            # The browser widget speaks raw PCM16 over the WebSocket directly
            # (same minimal protocol regardless of language), not a telephony
            # format like Twilio's — serializer=None would silently discard
            # every incoming message, so this needs a real one.
            serializer=profile.serializer or RawPCMSerializer(sample_rate=16000),
            audio_out_mixer=profile.audio_out_mixer,
        ),
    )

    stt = SarvamRealtimeSTTService(
        api_key=sarvam_api_key,
        prefix_padding_ms=VAD_TUNING["prefix_padding_ms"],
        settings=SarvamRealtimeSTTService.Settings(
            model=STT_MODEL,
            language_code=language_code,
            threshold=VAD_TUNING["threshold"],
            # Shortened when turn_taking.py's local VAD decides turns — see
            # stt_silence_duration_ms.
            silence_duration_ms=stt_silence_duration_ms(VAD_TUNING["silence_duration_ms"]),
            min_speech_duration_ms=VAD_TUNING["min_speech_duration_ms"],
        ),
    )

    # RAG backend exposes /v1/chat/completions, so base_url is the backend's
    # /v1 root — the OpenAI-compatible client appends "/chat/completions".
    llm = OpenAILLMService(
        api_key=rag_backend_auth_key,
        base_url=f"{rag_backend_public_url}/v1",
        # Lets rag-backend's conversation_state remember per-call facts the
        # message history can't show (a lead already saved on this call).
        default_headers={"X-Session-Id": session_id, **profile.llm_headers},
        settings=OpenAILLMService.Settings(
            model="agni-ai",  # ignored by our backend, which always uses its own Sarvam 105B model
            system_instruction=PERSONA_PROMPTS[language],
        ),
    )

    # How long Pipecat waits for a TTS reply's audio before it closes that reply's
    # audio context. Its default is 3.0 s; a Sarvam cloned voice once took 3.18 s
    # (2026-10-08) and the late audio was thrown away. Cartesia starts in ~0.1 s,
    # so this is only headroom now.
    tts_wait_secs = float(os.environ.get("TTS_STOP_FRAME_TIMEOUT_SECS", "10"))

    for tool_name, handler in profile.function_handlers.items():
        llm.register_function(tool_name, handler)

    # Speaking: Cartesia, one voice for the whole call. Buffering: by default
    # Pipecat sends one sentence at a time and turns Cartesia's own buffer off
    # (max_buffer_delay_ms=0), the combination Cartesia recommends; first audio
    # measured 0.06-0.20 s (2026-10-13/18). CARTESIA_TEXT_AGGREGATION=token
    # streams words as the LLM writes them instead, and then Cartesia buffers on
    # its side for CARTESIA_MAX_BUFFER_DELAY_MS (its default 3000; 0-5000). For
    # A/B tests against the [latency] lines. No backup voice: if Cartesia fails
    # for good during a call, the call has no voice (Prakash, 2026-10-18).
    if not CARTESIA_VOICE_ID:
        raise RuntimeError("CARTESIA_VOICE_ID is missing from voice-pipeline/.env")
    generation = cartesia_generation_settings()
    aggregation = os.environ.get("CARTESIA_TEXT_AGGREGATION", "").strip().lower() or "sentence"
    buffer_ms = os.environ.get("CARTESIA_MAX_BUFFER_DELAY_MS", "").strip()
    tts = CartesiaTTSService(
        api_key=cartesia_api_key(),
        sample_rate=profile.tts_rate,  # website: the same PCM16 24 kHz the widget already plays
        text_transforms=[("*", to_speech)],
        stop_frame_timeout_s=tts_wait_secs,
        text_aggregation_mode=TextAggregationMode(aggregation),
        max_buffer_delay_ms=int(buffer_ms) if buffer_ms else None,
        settings=CartesiaTTSService.Settings(
            model=CARTESIA_MODEL,
            voice=CARTESIA_VOICE_ID,
            # "en" / "ta". Mid-call changes arrive from conversation_router as
            # "en-IN" / "ta-IN"; the service reduces those to the base code.
            language=language,
            generation_config=GenerationConfig(**generation) if generation else None,
        ),
    )
    session_voice_id = CARTESIA_VOICE_ID
    logger.info(
        f"Using Cartesia {CARTESIA_MODEL} voice {CARTESIA_VOICE_ID} for the whole call "
        f"({aggregation}, settings {generation or 'default'})"
    )

    # Persona lives in llm's system_instruction (current, non-deprecated
    # pattern) rather than as an initial "system" message here — either way
    # our backend ignores whatever system content the client sends and
    # always injects its own persona + retrieved context server-side, so
    # this only matters for staying off Pipecat's deprecated path.
    context = LLMContext(messages=[], tools=profile.tools) if profile.tools else LLMContext(messages=[])
    user_params = build_user_aggregator_params(language)
    if profile.user_idle_timeout:
        from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregatorParams

        user_params = user_params or LLMUserAggregatorParams()
        user_params.user_idle_timeout = profile.user_idle_timeout
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context, user_params=user_params)

    # 2026-09-29: per-topic voice switching + mid-call language switching —
    # see conversation_router.py's module docstring. Sits between STT and
    # the user-turn aggregator so it sees every finalized transcript (to
    # classify topic / detect a switch request) while still passing it
    # through unchanged for the aggregator to fold into `context` exactly as
    # before this was added.
    turn_trace = TurnTrace(voice_id=session_voice_id)
    router = ConversationRouter(
        initial_language=language,
        callbacks=ConversationRouterCallbacks(persona_prompts=PERSONA_PROMPTS),
        trace=turn_trace,
        update_stt=profile.stt_language_code is None,
    )

    # Phase 10 — audio logging. Placed after transport.output() (not
    # earlier): STTService forwards InputAudioRawFrame on downstream after
    # transcribing it (confirmed by reading pipecat/services/stt_service.py),
    # so user audio still reaches this position; bot OutputAudioRawFrame
    # frames originate at `tts`, immediately upstream of this position, so
    # both tracks are visible here. buffer_size=0 + auto_start_recording
    # means "record continuously, flush once" rather than periodic chunked
    # flushes — appropriate for a single whole-call transcript file, not a
    # live streaming use case.
    #
    # enable_turn_audio=True added 2026-09-29 for automatic mid-call
    # language switching: PipelineWorker enables turn tracking by default
    # (enable_turn_tracking=True), so this alone is enough to get a
    # per-user-turn on_user_turn_audio event with that turn's raw PCM,
    # which the handler below re-runs through the SAME reliable batch
    # detector language_detection.py already uses once at call start
    # (realtime STT's own "auto" mode was tested and found unreliable —
    # see that module's docstring). Replaces requiring a spoken "switch to
    # Tamil" phrase with the visitor just naturally speaking the other
    # language — reacts within about one turn, not instantly, since
    # detection is itself a real API call (~0.6-0.8s).
    audio_logger = AudioBufferProcessor(
        num_channels=profile.audio_channels, buffer_size=0, auto_start_recording=True
    )

    @audio_logger.event_handler("on_audio_data")
    async def _on_audio_data(processor, audio, sample_rate, num_channels):
        save_session_audio(session_id, audio, sample_rate, num_channels)

    transcript_logger = TranscriptLogger(
        context, session_id=session_id, language=lambda: router.language
    )

    greeting_player = GreetingPlayer()

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            router,
            user_aggregator,
            llm,
            tts,
            greeting_player,
            transport.output(),
            audio_logger,
            assistant_aggregator,
            transcript_logger,
            LatencyLogger(session_id, trace=turn_trace, context=context),
        ]
    )

    # PipelineTask is deprecated since Pipecat 1.3.0 in favor of
    # PipelineWorker (confirmed against the installed 1.10.0 source, which
    # decorates PipelineTask with @deprecated) — same category of fix as the
    # LLMContext migration above, keeping this off Pipecat's deprecated path
    # rather than carrying a stale import forward.
    # enable_metrics: each service reports its time-to-first-byte (see
    # latency_metrics.py, which logs them) — measurement only, no behaviour change.
    worker = PipelineWorker(
        pipeline, params=PipelineParams(allow_interruptions=True, enable_metrics=True),
        observers=list(profile.observers) or None,
    )

    # Greeting (Phase 5): speaks GREETINGS[language] immediately once the
    # browser's WebSocket connects, going straight to TTS rather than
    # through the LLM — there's no reason to spend an LLM round-trip (and an
    # odd RAG retrieval against something like "introduce yourself") on a
    # fixed line we already know verbatim. Mirrors what Deepgram's
    # `agent.greeting` field used to do automatically for English before its
    # retirement (CLAUDE.md decision #1); Tamil's GREETING constant existed
    # since the original build but was never wired up until now — this
    # closes that gap for both languages at once, since they share one
    # pipeline. append_to_context defaults to True, so a visitor who refers
    # back to the greeting ("what did you just say?") has it in history.
    #
    # Updated 2026-10-08: played from a pre-rendered clip (greeting_cache.py)
    # instead of live TTS. The Sarvam cloned voice needed ~1.7 s before its first
    # audio, so a live greeting started ~2.3 s after connect and any early words
    # from the visitor cancelled it before it was heard or logged. Kept with
    # Cartesia: still instant, and no API call per greeting. The clip starts
    # at once and is still interruptible like any other bot speech. If the
    # clip isn't cached yet, this call uses live TTS and the clip is rendered
    # in the background for the next call.
    @transport.event_handler("on_client_connected")
    async def _on_client_connected(transport, client):
        clip = greeting_cache.load(language, greetings[language]) if profile.cache_greeting else None
        if clip:
            await worker.queue_frames([PlayGreetingFrame(greetings[language], clip)])
        else:
            await worker.queue_frames([TTSSpeakFrame(greetings[language])])
            if profile.cache_greeting:
                asyncio.create_task(greeting_cache.ensure_rendered(language, greetings[language]))

    # On hangup, tear the whole pipeline down. Without this the Sarvam STT
    # websocket stays open until its 60s inactivity timeout fires an error.
    @transport.event_handler("on_client_disconnected")
    async def _on_client_disconnected(transport, client):
        await worker.cancel(reason="client disconnected")

    if profile.on_built:
        from types import SimpleNamespace

        profile.on_built(SimpleNamespace(
            worker=worker, llm=llm, context=context, user_aggregator=user_aggregator,
            transport=transport, router=router,
        ))
    return worker
