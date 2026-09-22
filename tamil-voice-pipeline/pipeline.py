"""Tamil voice pipeline: Sarvam STT -> shared RAG backend -> Sarvam TTS,
composed with Pipecat. Points at the SAME rag-backend as the Deepgram English
path (via an OpenAI-compatible custom endpoint) — that's what keeps English
and Tamil answers consistent, per the one-shared-brain architecture.

Model choices (Sept 2026, latest per docs.sarvam.ai — see
docs/Voice_Model_Choices.md for rationale):
- STT: SarvamRealtimeSTTService always uses "saaras:v3-realtime" internally
  (hardcoded in Pipecat — there is no supported way to request "saaras:v4" on
  the realtime/streaming service, confirmed by reading Pipecat's source; the
  original "saaras:v4" choice in this file's history just didn't apply here)
- TTS: bulbul:v3 (bulbul:v2 is deprecated — Sarvam's API now rejects it)

NOTE: verify these Pipecat class names/params against `pip show pipecat-ai`
at build time — Pipecat's Sarvam integration is young and has had breaking
changes across versions (see pipecat-ai/pipecat#3783). This was written
against current docs but not executed against a live Sarvam/Pipecat
environment (no Sarvam key available in this session).
"""
import os

PERSONA_PROMPT = (
    "You are ACT AI, the voice assistant for Agni College of Technology. "
    "Talk like a helpful senior student showing someone around campus: warm, "
    "direct, and brief. Keep answers short since this is a spoken "
    "conversation, not a lookup. Respond in Tamil."
)

GREETING = "வணக்கம்! நான் ACT AI. சேர்க்கை, படிப்புகள், வேலைவாய்ப்பு பற்றி என்னிடம் கேளுங்கள்."

# Model choices — see docs/Voice_Model_Choices.md.
TTS_MODEL = "bulbul:v3"
TTS_VOICE = "kavya"  # verify by ear in Sarvam's voice samples before locking in, like the Deepgram voice
LANGUAGE_CODE = "ta-IN"


def build_pipeline(websocket):
    """Builds the Pipecat pipeline for one browser voice session.

    `websocket` is an already-`accept()`-ed FastAPI WebSocket connection from
    the browser widget (see server.py) — audio flows browser <-> this
    pipeline <-> Sarvam, with the RAG backend called as a plain OpenAI-
    compatible LLM step in between.
    """
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.task import PipelineParams, PipelineTask
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
            # (same minimal protocol as the English /voice/en relay), not a
            # telephony format like Twilio's — serializer=None would silently
            # discard every incoming message, so this needs a real one.
            serializer=RawPCMSerializer(sample_rate=16000),
        ),
    )

    stt = SarvamRealtimeSTTService(
        api_key=sarvam_api_key,
        settings=SarvamRealtimeSTTService.Settings(
            language_code=LANGUAGE_CODE,
        ),
    )

    # RAG backend exposes /v1/chat/completions, so base_url is the backend's
    # /v1 root — the OpenAI-compatible client appends "/chat/completions".
    llm = OpenAILLMService(
        api_key=rag_backend_auth_key,
        base_url=f"{rag_backend_public_url}/v1",
        settings=OpenAILLMService.Settings(
            model="act-ai",  # ignored by our backend, which always uses its own Azure deployment
            system_instruction=PERSONA_PROMPT,
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
            voice=TTS_VOICE,
            language=LANGUAGE_CODE,
        ),
    )

    # Persona now lives in llm's system_instruction (current, non-deprecated
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

    return PipelineTask(pipeline, params=PipelineParams(allow_interruptions=True))
