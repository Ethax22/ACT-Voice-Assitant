"""Phase 7 — voice cloning TTS via a Sarvam cloned voice_id.

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

import aiohttp
from loguru import logger

from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
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
    """Generates speech from a Sarvam cloned voice via POST /voices/clone.

    Not streaming: yields exactly one TTSAudioRawFrame per utterance once the
    full response has arrived, unlike the websocket-based SarvamTTSService
    used for stock voices in pipeline.py.
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

    def can_generate_metrics(self) -> bool:
        return True

    @traced_tts
    async def run_tts(self, text: str, context_id: str):
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
