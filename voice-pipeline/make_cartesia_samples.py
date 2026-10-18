"""Generate a small listening page for the Cartesia voice (TTS trial, 2026-10-13).

    python make_cartesia_samples.py

Speaks three real bot replies taken from voice-pipeline/logs (one English, one
Tamil, one Tanglish) with the voice in CARTESIA_VOICE_ID and writes
voice_samples/cartesia/index.html, with the audio embedded so the one file can
be opened or sent on its own. The text goes through the same clean-up as live
replies (speech_text_filter.to_speech). Judging how it sounds needs a listener.
"""
import asyncio
import base64
import html
import os
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from pipeline import CARTESIA_MODEL, CARTESIA_VOICE_ID, cartesia_api_key, cartesia_generation_settings
from speech_text_filter import to_speech

OUT = Path(__file__).parent / "voice_samples" / "cartesia"
# (label, Cartesia language, visitor's question as heard, bot reply) - from the
# 2026-10-13 call logs.
SAMPLES = [
    ("English", "en", "Anna application epdi panrathu?",
     "You can apply online through the Agni website at act.edu.in or via the TNEA single-window portal for UG seats."),
    ("Tamil", "ta", "கட்டணம் எவ்வளவு?",
     "Hostel fee with mess annual-ஆ approximately 70,000 rupees வரும். Exact figure-அ confirm பண்ணிக்க admissions-அ contact பண்ணுங்க."),
    ("Tanglish", "ta", "seri",
     # The logged reply began "Sari bro,"; "bro" is banned since 2026-10-13.
     "Sari, unga doubt edhunaalum kelunga. Admissions, courses, placements illana campus life pathi naan solluren."),
]


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cards = []
    async with aiohttp.ClientSession() as session:
        for label, language, question, reply in SAMPLES:
            payload = {
                "model_id": CARTESIA_MODEL, "transcript": await to_speech(reply), "language": language,
                "voice": {"mode": "id", "id": CARTESIA_VOICE_ID},
                "output_format": {"container": "wav", "encoding": "pcm_s16le", "sample_rate": 24000},
            }
            generation = cartesia_generation_settings()
            if generation:
                payload["generation_config"] = generation
            async with session.post(
                "https://api.cartesia.ai/tts/bytes", json=payload,
                headers={"X-API-Key": cartesia_api_key(), "Cartesia-Version": "2026-03-01"},
            ) as r:
                if r.status != 200:
                    raise RuntimeError(f"Cartesia TTS error {r.status}: {await r.text()}")
                wav = await r.read()
            (OUT / f"{label.lower()}.wav").write_bytes(wav)
            print(f"{label}: {len(wav) / 48000:.1f}s")
            cards.append(
                f"<section><h2>{label}</h2><p class='q'>Visitor: {html.escape(question)}</p>"
                f"<p>{html.escape(reply)}</p>"
                f"<audio controls src='data:audio/wav;base64,{base64.b64encode(wav).decode()}'></audio></section>"
            )
    (OUT / "index.html").write_text(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cartesia voice samples</title>
<style>body{{font-family:system-ui,sans-serif;max-width:720px;margin:24px auto;padding:0 16px;line-height:1.5}}
section{{border:1px solid #ccc;border-radius:8px;padding:12px 16px;margin:16px 0}}
h2{{margin:0 0 4px;font-size:1.1rem}}.q{{color:#666;margin:0}}audio{{width:100%}}</style></head><body>
<h1>Cartesia voice samples</h1>
<p>Voice {html.escape(CARTESIA_VOICE_ID or '')}, model {html.escape(CARTESIA_MODEL)}. Three bot replies from the 2026-10-13 call logs.</p>
{''.join(cards)}
</body></html>""",
        encoding="utf-8",
    )
    print(f"Wrote {OUT / 'index.html'}")


if __name__ == "__main__":
    asyncio.run(main())
