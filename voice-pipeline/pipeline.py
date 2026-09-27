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
VAD_TUNING = {
    "threshold": None,
    "silence_duration_ms": None,
    "min_speech_duration_ms": None,
    "prefix_padding_ms": None,
}

# Model choices — see docs/Voice_Model_Choices.md.
TTS_MODEL = "bulbul:v3"
# Placeholder picks, one per language — verify by ear against real ACT
# answers (Phase 5's native-speaker listening test) before locking in, same
# as the original Tamil choice. Voice cloning (Phase 7, deferred) replaces
# both once staff samples are ready.
#
# "en" was originally "amelia" (per Pipecat 1.10.0's own SarvamTTSSpeakerV3
# enum) but Sarvam's live API rejected it with 400 "Speaker 'amelia' is not
# recognized" — confirmed live 2026-09-25. The installed package's enum is
# stale relative to Sarvam's actual voice list (also missing newer speakers
# like anand/tanya/shruti/etc. that the live 400 error's "Available speakers"
# list included). Don't trust Pipecat's enum for this — if picking a
# different voice later, verify against a live call's error message or
# current Sarvam docs, not the enum.
TTS_VOICES: dict[Language, str] = {
    "en": "priya",
    "ta": "kavya",
}
LANGUAGE_CODES: dict[Language, str] = {
    "en": "en-IN",
    "ta": "ta-IN",
}


def build_pipeline(websocket, language: Language):
    """Builds the Pipecat pipeline for one browser voice session.

    `websocket` is an already-`accept()`-ed FastAPI WebSocket connection from
    the browser widget (see server.py) — audio flows browser <-> this
    pipeline <-> Sarvam, with the RAG backend called as a plain OpenAI-
    compatible LLM step in between. `language` selects STT/TTS language
    settings, persona prompt, and greeting; the RAG backend itself never
    forks by language (CLAUDE.md decision #2).
    """
    from pipecat.frames.frames import TTSSpeakFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
    from pipecat.services.openai.llm import OpenAILLMService
    from pipecat.services.sarvam.stt import SarvamRealtimeSTTService
    from pipecat.services.sarvam.tts import SarvamTTSService
    from pipecat.transports.websocket.fastapi import (
        FastAPIWebsocketParams,
        FastAPIWebsocketTransport,
    )

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

    # NOTE: language/voice/model MUST go through `settings=`, not as flat
    # kwargs — SarvamTTSService's __init__ only accepts api_key directly plus
    # a handful of deprecated/legacy args (model, voice_id); it silently
    # swallows unrecognized kwargs like `language=`/`voice=` via **kwargs
    # instead of erroring, which is what caused Sarvam to speak English with
    # the default voice instead of Tamil/kavya in earlier testing.
    tts = SarvamTTSService(
        api_key=sarvam_api_key,
        settings=SarvamTTSService.Settings(
            model=TTS_MODEL,
            voice=TTS_VOICES[language],
            language=language_code,
        ),
    )

    # Persona lives in llm's system_instruction (current, non-deprecated
    # pattern) rather than as an initial "system" message here — either way
    # our backend ignores whatever system content the client sends and
    # always injects its own persona + retrieved context server-side, so
    # this only matters for staying off Pipecat's deprecated path.
    context = LLMContext(messages=[])
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
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

    return worker
