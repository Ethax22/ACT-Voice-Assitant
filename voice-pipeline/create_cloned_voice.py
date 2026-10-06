"""Phase 7 onboarding script — turns a recorded reference sample into a
Sarvam cloned voice_id.

Usage (from voice-pipeline/, with its venv activated):

    python create_cloned_voice.py <audio_file> <en|ta> [--name NAME]

Prints the resulting voice_id — put it in voice-pipeline/.env as one of
CLONED_VOICE_ID_ABOUT_US / _ADMISSIONS / _COURSES / _PLACEMENTS (see
.env.example), matching which topic this recording is for, and restart the
voice-pipeline service. No other code change is needed; pipeline.py already
reads these and conversation_router.py switches that topic's TTS to the
cloned voice for both languages (see cloned_voice_tts.py for why that's a
real TTS-service swap, not just a settings tweak, and for the live
confirmation that one cloned voice works for both English and Tamil).

The <en|ta> argument only affects the reference recording's OWN language
metadata sent to Sarvam at creation time — confirmed live 2026-09-29 that
the resulting voice_id works for synthesis in the OTHER language too, so
which one you pass here doesn't determine which language(s) the clone can
later speak. Record once per topic (four recordings total), not once per
topic per language (eight).

Reference clip requirements, confirmed live against Sarvam's actual
validation (not just assumed from docs): at least ~5 seconds of real speech
— a too-short clip is rejected with "Recording is too short." Sarvam's own
playground additionally recommends WAV, max 10 MB, max 30 s, single
speaker, and a clean recording (minimal background noise/echo).
"""
import argparse
import asyncio
import mimetypes
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import aiohttp

LANGUAGE_CODES = {"en": "en-IN", "ta": "ta-IN"}


async def create_cloned_voice(audio_path: Path, language: str, name: str) -> dict:
    api_key = os.environ["SARVAM_API_KEY"]
    content_type, _ = mimetypes.guess_type(str(audio_path))
    content_type = content_type or "audio/wav"

    form = aiohttp.FormData()
    form.add_field("name", name)
    form.add_field("language", LANGUAGE_CODES[language])
    form.add_field(
        "file",
        audio_path.read_bytes(),
        filename=audio_path.name,
        content_type=content_type,
    )

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.sarvam.ai/voices/create",
            data=form,
            headers={"api-subscription-key": api_key},
        ) as response:
            body = await response.json()
            if response.status not in (200, 201):
                raise RuntimeError(f"Sarvam API error ({response.status}): {body}")
            return body


def main():
    parser = argparse.ArgumentParser(
        description="Create a Sarvam cloned voice from a recorded reference sample."
    )
    parser.add_argument(
        "audio_file", type=Path, help="Path to the reference recording (WAV recommended)"
    )
    parser.add_argument(
        "language",
        choices=["en", "ta"],
        help="Language metadata for the reference clip itself — doesn't limit which "
        "language(s) the resulting voice can later speak (confirmed cross-lingual, "
        "see module docstring).",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Label for this voice in Sarvam's dashboard (default: derived from filename + language)",
    )
    parser.add_argument(
        "--topic",
        choices=["about-us", "admissions", "courses", "placements"],
        default=None,
        help="Which topic this recording is for — just makes the final printed .env "
        "line exact; doesn't affect the API call.",
    )
    args = parser.parse_args()

    if not args.audio_file.exists():
        sys.exit(f"File not found: {args.audio_file}")

    name = args.name or f"agni-ai-{args.language}-{args.audio_file.stem}"

    try:
        result = asyncio.run(create_cloned_voice(args.audio_file, args.language, name))
    except RuntimeError as e:
        sys.exit(str(e))

    data = result.get("data", result)
    voice_id = data.get("voice_id")
    print(f"\nCreated voice: {voice_id}")
    print(f"Auto-transcribed reference text: {data.get('reference_text')!r}")
    if args.topic:
        env_key = f"CLONED_VOICE_ID_{args.topic.upper().replace('-', '_')}"
        print(
            f"\nNext step: put this in voice-pipeline/.env as {env_key}={voice_id}\n"
            "then restart the voice-pipeline service — no other code change needed. "
            "This same ID works for both English and Tamil (cross-lingual, confirmed "
            "live)."
        )
    else:
        print(
            "\nNext step: put this in voice-pipeline/.env as ONE of "
            "CLONED_VOICE_ID_ABOUT_US / _ADMISSIONS / _COURSES / _PLACEMENTS, "
            "matching which topic this recording is for (pass --topic next time to "
            "get this printed exactly), then restart the voice-pipeline service — no "
            "other code change needed. This same ID works for both English and Tamil "
            "(cross-lingual, confirmed live)."
        )


if __name__ == "__main__":
    main()
