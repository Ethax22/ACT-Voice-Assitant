"""Voice pipeline: Sarvam STT -> shared RAG backend -> Sarvam TTS, composed
with Pipecat. Serves BOTH languages, parameterized by `language` — points at
the SAME rag-backend for both (via an OpenAI-compatible custom endpoint)
that's what keeps English and Tamil answers consistent, per the one-shared-
brain architecture (CLAUDE.md decision #2). English used to run on Deepgram's
Voice Agent; that path was retired 2026-09-25 (CLAUDE.md decision #1) in
favor of this same Sarvam/Pipecat pipeline for both languages.

Model choices (Sept 2026, latest per docs.sarvam.ai — see
docs/Voice_Model_Choices.md for rationale):
- STT: SarvamRealtimeSTTService always uses "saaras:v3-realtime" internally
  (hardcoded in Pipecat) but its `language_code` setting IS configurable per
  language — confirmed by reading Pipecat 1.10.0's actual installed source
  (pipecat/services/sarvam/stt.py), not just its docs. "saaras:v4" was never
  applicable to the realtime service; nothing to configure there.
- TTS: bulbul:v3 (bulbul:v2 is deprecated — Sarvam's API now rejects it)
"""
import os
from typing import Literal

from loguru import logger

Language = Literal["en", "ta"]

PERSONA_PROMPTS: dict[Language, str] = {
    "en": (
        "You are ACT AI, the voice assistant for Agni College of Technology. "
        "Talk like a helpful senior student showing someone around campus: warm, "
        "direct, and brief. Keep answers short since this is a spoken "
        "conversation, not a lookup. Respond in English."
    ),
    "ta": (
        "You are ACT AI, the voice assistant for Agni College of Technology. "
        "Talk like a helpful senior student showing someone around campus: warm, "
        "direct, and brief. Keep answers short since this is a spoken "
        "conversation, not a lookup. Respond in Tamil."
    ),
}

GREETINGS: dict[Language, str] = {
    "en": "Hi! I'm ACT AI. Ask me about admissions, courses, placements, or campus life at Agni College of Technology.",
    "ta": "வணக்கம்! நான் ACT AI. சேர்க்கை, படிப்புகள், வேலைவாய்ப்பு பற்றி என்னிடம் கேளுங்கள்.",
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
TTS_MODEL = "bulbul:v3"
# Placeholder picks — verify by ear against real ACT answers (Phase 5's
# native-speaker listening test) before locking in, same as the original
# Tamil choice. Voice cloning (Phase 7, deferred) overrides the "about-us"
# entry only, once staff samples are ready (see CLONED_VOICE_IDS below) —
# the other three topic voices stay stock even after cloning lands, since
# only one cloned identity has ever been planned.
#
# "en" was originally "amelia" (per Pipecat 1.10.0's own SarvamTTSSpeakerV3
# enum) but Sarvam's live API rejected it with 400 "Speaker 'amelia' is not
# recognized" — confirmed live 2026-09-25. The installed package's enum is
# stale relative to Sarvam's actual voice list (also missing newer speakers
# like anand/tanya/shruti/etc. that the live 400 error's "Available speakers"
# list included). Don't trust Pipecat's enum for this — if picking a
# different voice later, verify against a live call's error message or
# current Sarvam docs, not the enum.
#
# 2026-09-29: expanded from one voice per language to four, restoring a
# feature the old Deepgram-only English path had (CATEGORY_VOICES in the
# now-deleted rag-backend/voice_agent.py, git commit 848cbad) and adding it
# to Tamil for the first time — see conversation_router.py's module
# docstring for the full history and the live-verification that all four
# names work for both en-IN and ta-IN. TOPIC_VOICES has replaced the old
# flat TTS_VOICES dict; "about-us" is both the default/fallback voice and
# the one a call starts on, same role TTS_VOICES used to play alone.
# (Imported inside build_pipeline(), like every other pipecat-adjacent
# import in this file, not at module level.)

LANGUAGE_CODES: dict[Language, str] = {
    "en": "en-IN",
    "ta": "ta-IN",
}

# Phase 7 — voice cloning. Originally one cloned voice_id per LANGUAGE
# (overriding only the "about-us" slot); 2026-09-29 widened to one per
# TOPIC instead, shared across BOTH languages — a Sarvam cloned voice_id
# (format `svc-{uuid}`, created via `POST /voices/create` from a consented
# staff recording, see create_cloned_voice.py) turned out to be
# cross-lingual, same as a stock voice: confirmed live that a voice cloned
# from an English reference recording produced real, audible Tamil speech
# when asked to (see cloned_voice_tts.py's module docstring for the actual
# test). So each topic needs exactly ONE staff recording total, not one per
# language — four recordings covers "about-us"/"admissions"/"courses"/
# "placements" for both English and Tamil, not eight.
#
# A topic with no env var set here keeps using its stock TOPIC_VOICES entry
# (unaffected — this is the current, default state, since no staff
# recordings exist yet per CLAUDE.md/Implementation Plan). Any subset can be
# configured — e.g. only "admissions" cloned, the other three still stock —
# conversation_router.py's ServiceSwitcher-based routing handles a mix of
# stock and cloned topics, not just all-or-nothing.
# dict[Topic, str | None] conceptually — Topic itself isn't imported at
# module level (it lives in conversation_router.py, only imported lazily
# inside build_pipeline(), like every other pipecat-adjacent import here).
CLONED_VOICE_IDS: dict[str, str | None] = {
    "about-us": os.environ.get("CLONED_VOICE_ID_ABOUT_US") or None,
    "admissions": os.environ.get("CLONED_VOICE_ID_ADMISSIONS") or None,
    "courses": os.environ.get("CLONED_VOICE_ID_COURSES") or None,
    "placements": os.environ.get("CLONED_VOICE_ID_PLACEMENTS") or None,
}


def build_pipeline(websocket, language: Language, session_id: str):
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
    from pipecat.services.sarvam.tts import SarvamTTSService
    from pipecat.transports.websocket.fastapi import (
        FastAPIWebsocketParams,
        FastAPIWebsocketTransport,
    )

    from conversation_logger import TranscriptLogger, save_session_audio
    from conversation_router import ConversationRouter, ConversationRouterCallbacks, TOPIC_VOICES
    from raw_pcm_serializer import RawPCMSerializer

    sarvam_api_key = os.environ["SARVAM_API_KEY"]
    rag_backend_public_url = os.environ["RAG_BACKEND_PUBLIC_URL"].rstrip("/")
    rag_backend_auth_key = os.environ.get("RAG_BACKEND_AUTH_KEY", "")
    language_code = LANGUAGE_CODES[language]

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
            # default (24000, matching what Sarvam TTS natively generates),
            # while the widget was told to play Tamil audio back at 16000 Hz
            # — that mismatch is what made the voice sound robotic/slowed.
            audio_in_sample_rate=16000,   # matches the mic audio the widget sends
            audio_out_sample_rate=24000,  # matches Sarvam TTS's native output rate — no resampling needed
            # The browser widget speaks raw PCM16 over the WebSocket directly
            # (same minimal protocol regardless of language), not a telephony
            # format like Twilio's — serializer=None would silently discard
            # every incoming message, so this needs a real one.
            serializer=RawPCMSerializer(sample_rate=16000),
        ),
    )

    stt = SarvamRealtimeSTTService(
        api_key=sarvam_api_key,
        prefix_padding_ms=VAD_TUNING["prefix_padding_ms"],
        settings=SarvamRealtimeSTTService.Settings(
            language_code=language_code,
            threshold=VAD_TUNING["threshold"],
            silence_duration_ms=VAD_TUNING["silence_duration_ms"],
            min_speech_duration_ms=VAD_TUNING["min_speech_duration_ms"],
        ),
    )

    # RAG backend exposes /v1/chat/completions, so base_url is the backend's
    # /v1 root — the OpenAI-compatible client appends "/chat/completions".
    llm = OpenAILLMService(
        api_key=rag_backend_auth_key,
        base_url=f"{rag_backend_public_url}/v1",
        settings=OpenAILLMService.Settings(
            model="act-ai",  # ignored by our backend, which always uses its own Sarvam 105B model
            system_instruction=PERSONA_PROMPTS[language],
        ),
    )

    # Stock TTS always exists — every topic without a configured cloned
    # voice uses it, and it's the whole `tts` stage when NO topic is cloned
    # (the common/default case today). NOTE: language/voice/model MUST go
    # through `settings=`, not as flat kwargs — SarvamTTSService's __init__
    # only accepts api_key directly plus a handful of deprecated/legacy
    # args (model, voice_id); it silently swallows unrecognized kwargs like
    # `language=`/`voice=` via **kwargs instead of erroring, which is what
    # caused Sarvam to speak English with the default voice instead of
    # Tamil/kavya in earlier testing.
    stock_tts = SarvamTTSService(
        api_key=sarvam_api_key,
        settings=SarvamTTSService.Settings(
            model=TTS_MODEL,
            voice=TOPIC_VOICES[language]["about-us"],
            language=language_code,
        ),
    )

    # Phase 7 — voice cloning, one ClonedVoiceTTSService per topic that has
    # a voice_id configured (see CLONED_VOICE_IDS above — cross-lingual, so
    # this doesn't depend on `language`). Each is a non-streaming POST
    # /voices/clone call per utterance, not interchangeable with the
    # streaming stock service via a settings tweak (cloned_voice_tts.py's
    # docstring), which is why these are separate instances switched via a
    # Pipecat ServiceSwitcher below rather than one instance reconfigured in
    # place. The shared aiohttp session is closed on client disconnect.
    cloned_voice_http_session = None
    cloned_services_by_topic: dict[str, object] = {}
    configured_clones = {topic: vid for topic, vid in CLONED_VOICE_IDS.items() if vid}
    if configured_clones:
        import aiohttp

        from cloned_voice_tts import ClonedVoiceTTSService

        cloned_voice_http_session = aiohttp.ClientSession()
        for topic, voice_id in configured_clones.items():
            cloned_services_by_topic[topic] = ClonedVoiceTTSService(
                api_key=sarvam_api_key,
                aiohttp_session=cloned_voice_http_session,
                voice_id=voice_id,
                language_code=language_code,
                sample_rate=24000,
            )
        logger.info(f"Using cloned voices for topics: {sorted(configured_clones)}")

    if cloned_services_by_topic:
        from pipecat.pipeline.service_switcher import (
            ServiceSwitcher,
            ServiceSwitcherStrategyManual,
        )

        # "about-us" (cloned if configured, else stock) goes first so it's
        # the switcher's initial active member, matching the persona a call
        # starts on before any topic has been classified.
        default_service = cloned_services_by_topic.get("about-us", stock_tts)
        other_services = [
            svc
            for svc in [stock_tts, *cloned_services_by_topic.values()]
            if svc is not default_service
        ]
        tts = ServiceSwitcher(
            services=[default_service, *other_services],
            strategy_type=ServiceSwitcherStrategyManual,
        )
    else:
        tts = stock_tts

    # Persona lives in llm's system_instruction (current, non-deprecated
    # pattern) rather than as an initial "system" message here — either way
    # our backend ignores whatever system content the client sends and
    # always injects its own persona + retrieved context server-side, so
    # this only matters for staying off Pipecat's deprecated path.
    context = LLMContext(messages=[])
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)

    # 2026-09-29: per-topic voice switching + mid-call language switching —
    # see conversation_router.py's module docstring. Sits between STT and
    # the user-turn aggregator so it sees every finalized transcript (to
    # classify topic / detect a switch request) while still passing it
    # through unchanged for the aggregator to fold into `context` exactly as
    # before this was added.
    router = ConversationRouter(
        initial_language=language,
        callbacks=ConversationRouterCallbacks(persona_prompts=PERSONA_PROMPTS),
        stock_tts=stock_tts,
        cloned_services_by_topic=cloned_services_by_topic,
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
    audio_logger = AudioBufferProcessor(num_channels=1, buffer_size=0, auto_start_recording=True)

    @audio_logger.event_handler("on_audio_data")
    async def _on_audio_data(processor, audio, sample_rate, num_channels):
        save_session_audio(session_id, audio, sample_rate, num_channels)

    transcript_logger = TranscriptLogger(
        context, session_id=session_id, language=lambda: router.language
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            router,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            audio_logger,
            assistant_aggregator,
            transcript_logger,
        ]
    )

    # PipelineTask is deprecated since Pipecat 1.3.0 in favor of
    # PipelineWorker (confirmed against the installed 1.10.0 source, which
    # decorates PipelineTask with @deprecated) — same category of fix as the
    # LLMContext migration above, keeping this off Pipecat's deprecated path
    # rather than carrying a stale import forward.
    worker = PipelineWorker(pipeline, params=PipelineParams(allow_interruptions=True))

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
    @transport.event_handler("on_client_connected")
    async def _on_client_connected(transport, client):
        await worker.queue_frames([TTSSpeakFrame(GREETINGS[language])])

    # Phase 7 cleanup: close the aiohttp session ClonedVoiceTTSService opened
    # above, if this call used one. Stock-voice sessions never create one, so
    # this is a no-op for them.
    if cloned_voice_http_session is not None:

        @transport.event_handler("on_client_disconnected")
        async def _on_client_disconnected(transport, client):
            await cloned_voice_http_session.close()

    return worker
