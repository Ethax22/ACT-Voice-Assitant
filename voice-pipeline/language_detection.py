"""Voice language auto-detection (Phase 6).

CLAUDE.md decision #5 / the Implementation Plan's Phase 6 assumed Sarvam had
a dedicated audio-based Language Identification API — checked live
2026-09-25 and that's wrong: Sarvam's actual standalone LID endpoint
(`/text-lid`) is text-only. There is no dedicated audio LID endpoint.

Two real options exist for audio, tested live before picking one:
- SarvamRealtimeSTTService's own `language_code="auto"` (the realtime
  streaming STT this pipeline already uses) — tested by round-tripping
  synthesized English and Tamil clips through it. Unreliable: Tamil audio
  came back transcribed as English (transliterated), not detected as Tamil.
- Sarvam's batch STT endpoint (`POST /speech-to-text`, model `saaras:v4`,
  `language_code=unknown`) — tested the same way. Fast (~0.6-0.8s for a
  short clip) and accurate (both test clips detected at 1.0 confidence).

So detection here is a one-shot batch STT call on a short buffered snippet
of the visitor's mic audio, BEFORE opening the realtime pipeline — not a
continuous "auto" mode on the realtime service itself.
"""
import io
import os
import struct

import aiohttp

# Only the two languages this pipeline actually serves — a detection outside
# this set (a visitor speaking a third language) falls back to the caller's
# default rather than opening a pipeline we have no persona/voice for.
SUPPORTED_LANGUAGES = {"en", "ta"}

# Below this, treat the result as a coin flip rather than a real detection —
# a short or noisy snippet is exactly where Sarvam is least sure, and a wrong
# guess here means opening the WRONG language's whole pipeline (wrong
# persona, wrong TTS voice) rather than just one bad transcript.
MIN_CONFIDENCE = 0.5


def _pcm16_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw mono PCM16 bytes in a minimal WAV header.

    Sarvam's batch STT endpoint takes a file upload (WAV, MP3, etc.), not
    raw PCM — the widget's mic capture (and the realtime pipeline's own
    RawPCMSerializer) already produce headerless PCM16, so this is the one
    conversion needed to reuse that same audio for detection.
    """
    n_channels, sampwidth = 1, 2
    byte_rate = sample_rate * n_channels * sampwidth
    block_align = n_channels * sampwidth
    buf = io.BytesIO()
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + len(pcm)))
    buf.write(b"WAVE")
    buf.write(b"fmt ")
    buf.write(struct.pack("<IHHIIHH", 16, 1, n_channels, sample_rate, byte_rate, block_align, sampwidth * 8))
    buf.write(b"data")
    buf.write(struct.pack("<I", len(pcm)))
    buf.write(pcm)
    return buf.getvalue()


async def detect_language(pcm16_audio: bytes, sample_rate: int = 16000) -> str | None:
    """Detect en/ta from a short mono PCM16 snippet, or None if undetermined.

    Returns None on a Sarvam-side error, a detected language outside
    SUPPORTED_LANGUAGES, or a low-confidence result — callers should treat
    None as "fall back to the default language" per the Implementation
    Plan's own guidance to fall back gracefully rather than force a guess.
    """
    sarvam_api_key = os.environ["SARVAM_API_KEY"]
    wav_bytes = _pcm16_to_wav(pcm16_audio, sample_rate)

    form = aiohttp.FormData()
    form.add_field("file", wav_bytes, filename="snippet.wav", content_type="audio/wav")
    form.add_field("model", "saaras:v4")
    form.add_field("language_code", "unknown")

    headers = {"api-subscription-key": sarvam_api_key}
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.sarvam.ai/speech-to-text", data=form, headers=headers
        ) as response:
            if response.status != 200:
                return None
            data = await response.json()

    language_code = data.get("language_code")  # e.g. "en-IN", "ta-IN"
    if not language_code:
        return None
    confidence = data.get("language_probability")
    if confidence is not None and confidence < MIN_CONFIDENCE:
        return None
    short_code = language_code.split("-")[0]
    if short_code not in SUPPORTED_LANGUAGES:
        return None
    return short_code
