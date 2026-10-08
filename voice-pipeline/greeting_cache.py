"""Pre-rendered greeting audio, so the greeting starts the moment a call connects.

Cloned voices take ~1.7 s to produce their first audio (Voice cloning latency
measurements, 2026-10-08), so a greeting synthesized live started ~2.3 s after
connect, and anything the visitor said in that window cancelled it before it
was ever heard or logged. Instead each greeting is rendered once per
(voice, language, text) and cached on disk; on connect it is played from the
cache with no synthesis step.

The clip is emitted as the same frames a TTS service would produce
(TTSStarted / TTSText / TTSAudioRaw / TTSStopped), so it behaves like any other
bot speech: the visitor can talk over it (the usual interruption rules apply)
and its text lands in the conversation context and the transcript.

A missing cache file is not an error: the caller falls back to live TTS for
that call and renders the clip in the background for the next one.
"""
import asyncio
import base64
import hashlib
import json
import os
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


def _voice_key(cloned_voice_id: str | None, stock_voice: str) -> str:
    return cloned_voice_id or f"stock:{stock_voice}"


def cache_path(language: str, cloned_voice_id: str | None, stock_voice: str, text: str) -> Path:
    # Pace/temperature change how the voice sounds, so they are part of the key.
    key = "|".join(
        [_voice_key(cloned_voice_id, stock_voice), language, text,
         os.environ.get("TTS_PACE", ""), os.environ.get("TTS_TEMPERATURE", "")]
    )
    return CACHE_DIR / f"{language}_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}.pcm"


def load(language: str, cloned_voice_id: str | None, stock_voice: str, text: str) -> bytes | None:
    path = cache_path(language, cloned_voice_id, stock_voice, text)
    try:
        return path.read_bytes() if path.exists() else None
    except OSError:
        return None


async def render(
    *, api_key: str, language: str, cloned_voice_id: str | None, stock_voice: str,
    text: str, stock_model: str,
) -> bytes:
    """Synthesize the greeting and write it to the cache. Raises on failure."""
    from speech_text_filter import to_speech

    spoken = await to_speech(text)
    lang_code = f"{language}-IN"
    headers = {"api-subscription-key": api_key}
    pcm = bytearray()
    async with aiohttp.ClientSession() as session:
        if cloned_voice_id:
            async with session.ws_connect(
                "wss://api.sarvam.ai/voices/clone/ws", headers=headers
            ) as ws:
                config = {
                    "target_language_code": lang_code, "voice_id": cloned_voice_id,
                    "speech_sample_rate": SAMPLE_RATE, "output_audio_codec": "linear16",
                }
                await ws.send_str(json.dumps({"type": "config", "data": config}))
                await ws.send_str(json.dumps({"type": "text", "data": {"text": spoken}}))
                await ws.send_str(json.dumps({"type": "flush"}))
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    data = json.loads(msg.data)
                    if data.get("type") == "audio":
                        pcm += base64.b64decode(data["data"]["audio"])
                    elif data.get("type") == "error":
                        raise RuntimeError(f"voice-clone websocket error: {data.get('data')}")
                    elif data.get("type") == "event" and data.get("data", {}).get("event_type") == "final":
                        break
        else:
            payload = {
                "text": spoken, "target_language_code": lang_code, "speaker": stock_voice,
                "model": stock_model, "speech_sample_rate": SAMPLE_RATE,
            }
            for field, env in (("pace", "TTS_PACE"), ("temperature", "TTS_TEMPERATURE")):
                if os.environ.get(env, "").strip():
                    payload[field] = float(os.environ[env])
            async with session.post(
                "https://api.sarvam.ai/text-to-speech", headers=headers, json=payload
            ) as r:
                body = await r.json()
                if r.status != 200:
                    raise RuntimeError(f"Sarvam TTS error {r.status}: {body}")
            wav = base64.b64decode(body["audios"][0])
            pcm += wav[44:] if wav.startswith(b"RIFF") else wav
    if not pcm:
        raise RuntimeError("greeting render produced no audio")
    path = cache_path(language, cloned_voice_id, stock_voice, text)
    CACHE_DIR.mkdir(exist_ok=True)
    path.write_bytes(bytes(pcm))
    logger.info(f"Rendered {language} greeting ({len(pcm) / 2 / SAMPLE_RATE:.1f}s) -> {path.name}")
    return bytes(pcm)


def about_us_voice(language: str) -> tuple[str | None, str]:
    """(cloned voice id or None, stock voice name) for the voice a call starts on."""
    from conversation_router import TOPIC_VOICES
    from pipeline import CLONED_VOICE_IDS

    return CLONED_VOICE_IDS.get("about-us"), TOPIC_VOICES[language]["about-us"]


async def ensure_rendered(language: str) -> None:
    """Render the greeting for `language` if its cache file is missing."""
    from pipeline import GREETINGS, TTS_MODEL

    cloned, stock = about_us_voice(language)
    text = GREETINGS[language]
    if load(language, cloned, stock, text) is not None:
        return
    try:
        await render(
            api_key=os.environ["SARVAM_API_KEY"], language=language,
            cloned_voice_id=cloned, stock_voice=stock, text=text, stock_model=TTS_MODEL,
        )
    except Exception:
        logger.exception(f"Could not render the {language} greeting; calls will use live TTS")


async def prewarm() -> None:
    await asyncio.gather(*(ensure_rendered(lang) for lang in ("en", "ta")))
