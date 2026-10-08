"""Phase 7 — voice cloning TTS via a Sarvam cloned voice_id.

**UPDATE 2026-10-08: streaming is now available for cloned voices** (Sarvam changelog
2026-10-01: `wss://api.sarvam.ai/voices/clone/ws`). `ClonedVoiceTTSService` below
uses it, with the non-streaming call described in this docstring as fallback.
The account-specific findings below about non-streaming still hold for the
stock `/text-to-speech` endpoints.

**Architecture note — verified live 2026-09-27, not assumed from docs.**
Sarvam's cloned voices (`svc-{uuid}`, created via `POST /voices/create`)
are NOT usable through the streaming TTS path this pipeline normally uses
for stock voices (`SarvamTTSService` in pipeline.py, which wraps
`wss://api.sarvam.ai/text-to-speech/ws`). Confirmed by direct testing
against the real API: both the REST `/text-to-speech` endpoint and its
websocket equivalent reject a cloned `voice_id` passed as `speaker` with a
400/422 "not recognized" error, listing only Sarvam's fixed stock-speaker
names as valid — a cloned voice_id is simply never in that list. Cloned
voices only work through the separate, non-streaming `POST /voices/clone`
endpoint, confirmed live to accept `voice_id` and return one complete
base64-encoded WAV per call under the key `audio_b64` (not `audios`, which
is what `/text-to-speech` returns, and not the single `audio` key some of
Sarvam's own docs/playground code samples suggest — this module reads
either key defensively, but `audio_b64` is what the real API actually
returned in testing).

This is a real deviation from the Implementation Plan's Phase 7 assumption
("swap into pipeline.py's per-language TTS_VOICE settings... a simple
parameter swap") — flagged to Prakash rather than silently built around, per
CLAUDE.md's instruction to surface exactly this kind of discrepancy. The
practical consequence: a language using a cloned voice pays one full
non-streaming HTTP request/response round-trip per bot utterance instead of
receiving progressive low-latency audio chunks the way stock voices do, so
replies using a cloned voice will have a beat of silence before speech
starts that stock-voice languages don't have.

**Cross-lingual, confirmed live 2026-09-29**: a voice cloned from an
English reference recording (created with `language="en-IN"` in
`create_cloned_voice.py`) was successfully used to synthesize real, audible
Tamil speech by passing `language_code="ta-IN"` to `/voices/clone` — same
cross-lingual behavior Sarvam's stock voices already have. So ONE staff
recording per topic serves BOTH languages; recording a separate Tamil
sample of the same person isn't needed. This is what makes per-topic
cloned-voice switching in conversation_router.py practical instead of
needing eight recordings.

**Per-topic switching, added 2026-09-29** (conversation_router.py):
pipeline.py now gives each topic that has a cloned voice_id configured its
own `ClonedVoiceTTSService` instance, and wraps them together with the
stock `SarvamTTSService` in a Pipecat `ServiceSwitcher` — switching topics
mid-call means switching which instance is *active*, not reconfiguring one
instance, since a cloned instance's `voice_id` is fixed for its life
(there's exactly one voice per instance, so there's nothing to switch
there). `language` is the one thing that DOES need to update live on an
already-built instance, for mid-call language switching to reach a cloned
voice — handled below by reading `self._settings.language` at call time
instead of a fixed constructor value, the same pattern `SarvamTTSService`
already uses for its own live language updates.
"""
import base64
import json

import aiohttp
from loguru import logger

from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService
from pipecat.utils.tracing.service_decorators import traced_tts
from pipecat.utils.types import is_given

# Sarvam's hard limit on /voices/clone's `text` field — longer replies must
# be split client-side per the docs. The persona prompt already asks the LLM
# to keep answers short/conversational (2-4 sentences) for voice, so this
# should rarely bind; truncating rather than erroring keeps a long reply from
# hard-failing the turn, at the cost of a clipped sentence in the rare case.
MAX_CLONE_TEXT_CHARS = 1000


class ClonedVoiceTTSService(TTSService):
    """Generates speech from a Sarvam cloned voice.

    Streams over `wss://api.sarvam.ai/voices/clone/ws` (added by Sarvam on
    2026-10-01, verified live 2026-10-08): one websocket per utterance, with
    audio frames yielded as chunks arrive. If the websocket fails before any
    audio has been produced, falls back to the old non-streaming
    `POST /voices/clone` call (module docstring) so a Sarvam hiccup costs
    latency rather than a silent turn.
    """

    def __init__(
        self,
        *,
        api_key: str,
        aiohttp_session: aiohttp.ClientSession,
        voice_id: str,
        language_code: str,
        base_url: str = "https://api.sarvam.ai",
        sample_rate: int = 24000,
        pace: float = 1.0,
        temperature: float | None = None,
        min_buffer_size: int | None = None,
        max_chunk_length: int | None = None,
        **kwargs,
    ):
        super().__init__(
            sample_rate=sample_rate,
            push_stop_frames=True,
            push_start_frame=True,
            # Stored in the standard settings mechanism (not a bare
            # constructor attribute) so a live TTSUpdateSettingsFrame can
            # change it later — see run_tts below and the module docstring's
            # "Per-topic switching" note. `voice` is deliberately left unset:
            # this instance's identity IS its voice_id, fixed for its life;
            # only `language` needs to move.
            settings=TTSSettings(language=language_code),
            **kwargs,
        )
        self._api_key = api_key
        self._session = aiohttp_session
        self._voice_id = voice_id
        # Constructor default, used until/unless a settings update sets
        # self._settings.language — see run_tts.
        self._default_language_code = language_code
        self._base_url = base_url
        self._pace = pace
        self._temperature = temperature
        self._min_buffer_size = min_buffer_size
        self._max_chunk_length = max_chunk_length

    def can_generate_metrics(self) -> bool:
        return True

    @traced_tts
    async def run_tts(self, text: str, context_id: str):
        language_code = (
            self._settings.language
            if is_given(self._settings.language)
            else self._default_language_code
        )
        yielded_audio = False
        try:
            async for frame in self._run_stream(text, context_id, language_code):
                yielded_audio = True
                yield frame
            return
        except Exception as e:
            if yielded_audio:
                # Mid-utterance failure: a retry would repeat what was already
                # spoken, so end the utterance here.
                logger.exception("Cloned-voice stream broke mid-utterance")
                yield ErrorFrame(error=f"Cloned-voice stream error: {e}", exception=e)
                return
            logger.warning(f"Cloned-voice stream failed before audio ({e}); using batch call")
        async for frame in self._run_batch(text, context_id):
            yield frame

    async def _run_stream(self, text: str, context_id: str, language_code: str):
        """Websocket streaming. Chunks are raw PCM16 (content_type audio/pcm)."""
        url = self._base_url.replace("https://", "wss://").replace("http://", "ws://") + "/voices/clone/ws"
        async with self._session.ws_connect(
            url, headers={"api-subscription-key": self._api_key}, heartbeat=20
        ) as ws:
            await ws.send_str(
                json.dumps(
                    {
                        "type": "config",
                        "data": {
                            "target_language_code": language_code,
                            "voice_id": self._voice_id,
                            "speech_sample_rate": self.sample_rate,
                            "output_audio_codec": "linear16",
                            "pace": self._pace,
                            # Same naturalness tuning the stock voice uses
                            # (pipeline.py) — only sent when set.
                            **{
                                k: v
                                for k, v in (
                                    ("temperature", self._temperature),
                                    ("min_buffer_size", self._min_buffer_size),
                                    ("max_chunk_length", self._max_chunk_length),
                                )
                                if v is not None
                            },
                        },
                    }
                )
            )
            await ws.send_str(json.dumps({"type": "text", "data": {"text": text[:MAX_CLONE_TEXT_CHARS]}}))
            await ws.send_str(json.dumps({"type": "flush"}))
            await self.start_tts_usage_metrics(text)

            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                        raise RuntimeError("voice-clone websocket closed before the final event")
                    continue
                data = json.loads(msg.data)
                kind = data.get("type")
                if kind == "audio":
                    await self.stop_ttfb_metrics()
                    yield TTSAudioRawFrame(
                        audio=base64.b64decode(data["data"]["audio"]),
                        sample_rate=self.sample_rate,
                        num_channels=1,
                        context_id=context_id,
                    )
                elif kind == "error":
                    raise RuntimeError(f"voice-clone websocket error: {data.get('data')}")
                elif kind == "event" and data.get("data", {}).get("event_type") == "final":
                    return
            raise RuntimeError("voice-clone websocket ended without a final event")

    async def _run_batch(self, text: str, context_id: str):
        try:
            # Read live from self._settings (updated by a TTSUpdateSettingsFrame
            # reaching this instance — see module docstring's "Per-topic
            # switching" note) rather than the constructor-time value, so a
            # mid-call language switch reaches a cloned voice too.
            language_code = (
                self._settings.language
                if is_given(self._settings.language)
                else self._default_language_code
            )
            form = aiohttp.FormData()
            form.add_field("voice_id", self._voice_id)
            form.add_field("text", text[:MAX_CLONE_TEXT_CHARS])
            form.add_field("language_code", language_code)
            form.add_field("pace", str(self._pace))
            # speech_sample_rate deliberately omitted: passing "24000" here
            # got a live 400 ("Input should be 8000, 16000, ...") even though
            # 24000 is in that exact allowed list — a real API quirk, not a
            # typo (confirmed by testing). Sarvam's own documented default is
            # already 24000, which matches self.sample_rate below, so leaving
            # it unset produces the same result without fighting the bug.

            headers = {"api-subscription-key": self._api_key}
            url = f"{self._base_url}/voices/clone"

            async with self._session.post(url, data=form, headers=headers) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Sarvam voice-clone API error ({response.status}): {error_text}")
                    yield ErrorFrame(error=f"Sarvam voice-clone API error: {error_text}")
                    return
                response_data = await response.json()

            await self.start_tts_usage_metrics(text)

            # Confirmed live: the real field is `audio_b64`. `audio` is kept
            # as a fallback only because Sarvam's own playground-generated
            # code sample destructures `audio` — that sample did not match
            # what this endpoint actually returned in testing, so don't trust
            # it over a real response without re-verifying.
            base64_audio = response_data.get("audio_b64") or response_data.get("audio")
            if not base64_audio:
                logger.error(f"No audio in voice-clone response: {response_data}")
                yield ErrorFrame(error=f"No audio in voice-clone response: {response_data}")
                return

            audio_data = base64.b64decode(base64_audio)
            if len(audio_data) > 44 and audio_data.startswith(b"RIFF"):
                audio_data = audio_data[44:]  # strip WAV header, same as SarvamHttpTTSService does

            yield TTSAudioRawFrame(
                audio=audio_data,
                sample_rate=self.sample_rate,
                num_channels=1,
                context_id=context_id,
            )
        except Exception as e:
            logger.exception("Error generating cloned-voice TTS")
            yield ErrorFrame(error=f"Error generating cloned-voice TTS: {e}", exception=e)
        finally:
            await self.stop_ttfb_metrics()


class PersistentClonedVoiceTTSService(SarvamTTSService):
    """Cloned voice over ONE long-lived websocket per call (default transport).

    Added 2026-10-08 after reading real-call `[latency]` lines: the per-sentence
    class above opened a fresh websocket for every sentence (0.68 s to connect)
    and each sentence then paid Sarvam's own ~1.5 s start-up again, so a
    multi-sentence reply had audible gaps and the first sentence started ~2 s
    late. This class reuses Pipecat's own SarvamTTSService machinery — one
    connection opened when the call starts, text sent as each sentence arrives,
    a flush at the end of the reply, keep-alive pings, interruption handling,
    config re-sent on a settings change (so a mid-call language switch works) —
    pointed at the clone endpoint with a clone-style config (`voice_id` instead
    of `speaker`/`model`).

    Verified live against `wss://api.sarvam.ai/voices/clone/ws`: it accepts the
    `?model=...&send_completion_event=true` query the base class adds, `ping`,
    and a second `config` message on an open connection. Sarvam's ~1.5 s
    per-utterance start-up is not removed by this; only connection setup and
    inter-sentence gaps are.
    """

    def __init__(
        self,
        *,
        api_key: str,
        voice_id: str,
        language_code: str,
        sample_rate: int = 24000,
        pace: float | None = None,
        temperature: float | None = None,
        min_buffer_size: int | None = None,
        max_chunk_length: int | None = None,
        base_url: str = "wss://api.sarvam.ai",
        **kwargs,
    ):
        fields = {"model": "bulbul:v3", "language": language_code}
        for name, value in (
            ("pace", pace),
            ("temperature", temperature),
            ("min_buffer_size", min_buffer_size),
            ("max_chunk_length", max_chunk_length),
        ):
            if value is not None:
                fields[name] = value
        super().__init__(
            api_key=api_key,
            url=f"{base_url}/voices/clone/ws",
            sample_rate=sample_rate,
            settings=SarvamTTSService.Settings(**fields),
            **kwargs,
        )
        self._clone_voice_id = voice_id

    async def _send_config(self):
        """Clone-style config: `voice_id` replaces the stock `speaker`/`model`."""
        if not self._websocket:
            raise Exception("WebSocket not connected")
        s = self._settings
        data = {
            "target_language_code": s.language,
            "voice_id": self._clone_voice_id,
            "speech_sample_rate": str(self.sample_rate),
            "output_audio_codec": "linear16",
            "min_buffer_size": s.min_buffer_size,
            "max_chunk_length": s.max_chunk_length,
            "pace": s.pace,
        }
        if is_given(s.temperature) and s.temperature is not None:
            data["temperature"] = s.temperature
        logger.debug(f"Cloned-voice config being sent: {data}")
        await self._websocket.send(json.dumps({"type": "config", "data": data}))
