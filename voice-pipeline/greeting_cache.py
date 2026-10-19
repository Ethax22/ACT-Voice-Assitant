"""Pre-rendered greeting audio, so the greeting starts the moment a call connects.

Built when the Sarvam cloned voice took ~1.7 s to produce its first audio
(2026-10-08): a greeting synthesized live started ~2.3 s after connect, and
anything the visitor said in that window cancelled it before it was heard or
logged. Kept with Cartesia (2026-10-18), which is fast, because a cached clip is
still instant and costs no API call. Each greeting is rendered once per
(voice, model, delivery settings, language, text) and cached on disk; on
connect it is played from the cache with no synthesis step.

The clip is emitted as the same frames a TTS service would produce
(TTSStarted / TTSText / TTSAudioRaw / TTSStopped), so it behaves like any other
bot speech: the visitor can talk over it (the usual interruption rules apply)
and its text lands in the conversation context and the transcript.

A missing cache file is not an error: the caller falls back to live TTS for
that call and renders the clip in the background for the next one.
"""
import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from loguru import logger

from pipecat.frames.frames import (
    DataFrame,
    Frame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

CACHE_DIR = Path(__file__).parent / "greeting_cache"
SAMPLE_RATE = 24000
CHUNK_BYTES = 4800  # 100 ms of 24 kHz mono PCM16
CARTESIA_VERSION = "2026-03-01"  # the version the installed Pipecat service is written against


@dataclass
class PlayGreetingFrame(DataFrame):
    text: str
    audio: bytes


class GreetingPlayer(FrameProcessor):
    """Placed right after the TTS stage. Turns a PlayGreetingFrame into the
    frames a TTS service would have produced for that text."""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if not isinstance(frame, PlayGreetingFrame):
            await self.push_frame(frame, direction)
            return
        context_id = uuid.uuid4().hex
        await self.push_frame(TTSStartedFrame(context_id=context_id))
        text_frame = TTSTextFrame(frame.text, aggregated_by="sentence", context_id=context_id)
        text_frame.will_be_spoken = True
        await self.push_frame(text_frame)
        for i in range(0, len(frame.audio), CHUNK_BYTES):
            await self.push_frame(
                TTSAudioRawFrame(
                    audio=frame.audio[i : i + CHUNK_BYTES],
                    sample_rate=SAMPLE_RATE,
                    num_channels=1,
                    context_id=context_id,
                )
            )
        await self.push_frame(TTSStoppedFrame(context_id=context_id))


def _cache_key(language: str, text: str) -> str:
    from pipeline import CARTESIA_MODEL, CARTESIA_VOICE_ID, cartesia_generation_settings

    settings = ",".join(f"{k}={v}" for k, v in sorted(cartesia_generation_settings().items()))
    return "|".join(["cartesia", CARTESIA_MODEL, CARTESIA_VOICE_ID or "", settings, language, text])


def cache_path(language: str, text: str) -> Path:
    key = _cache_key(language, text)
    return CACHE_DIR / f"{language}_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}.pcm"


def load(language: str, text: str) -> bytes | None:
    path = cache_path(language, text)
    try:
        return path.read_bytes() if path.exists() else None
    except OSError:
        return None


async def render(language: str, text: str) -> bytes:
    """Synthesize the greeting with the call's Cartesia voice and write it to the
    cache. Raises on failure."""
    from pipeline import (
        CARTESIA_MODEL, CARTESIA_VOICE_ID, cartesia_api_key, cartesia_generation_settings,
    )
    from speech_text_filter import to_speech

    payload = {
        "model_id": CARTESIA_MODEL, "transcript": await to_speech(text), "language": language,
        "voice": {"mode": "id", "id": CARTESIA_VOICE_ID},
        "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": SAMPLE_RATE},
    }
    generation = cartesia_generation_settings()
    if generation:
        payload["generation_config"] = generation
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.cartesia.ai/tts/bytes", json=payload,
            headers={"X-API-Key": cartesia_api_key(), "Cartesia-Version": CARTESIA_VERSION},
        ) as r:
            if r.status != 200:
                raise RuntimeError(f"Cartesia TTS error {r.status}: {await r.text()}")
            pcm = await r.read()
    if not pcm:
        raise RuntimeError("greeting render produced no audio")
    path = cache_path(language, text)
    CACHE_DIR.mkdir(exist_ok=True)
    path.write_bytes(pcm)
    logger.info(f"Rendered {language} greeting ({len(pcm) / 2 / SAMPLE_RATE:.1f}s) -> {path.name}")
    return pcm


async def ensure_rendered(language: str, text: str | None = None) -> None:
    """Render the greeting for `language` if its cache file is missing. `text`
    defaults to the website greeting; the phone agent passes its own."""
    from pipeline import GREETINGS

    text = text or GREETINGS[language]
    if load(language, text) is not None:
        return
    try:
        await render(language, text)
    except Exception:
        logger.exception(f"Could not render the {language} greeting; calls will use live TTS")


async def prewarm() -> None:
    await asyncio.gather(*(ensure_rendered(lang) for lang in ("en", "ta")))
