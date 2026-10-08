"""Generate listening samples of Sarvam Bulbul voices, for choosing the bot's
voice by ear (Voice Naturalness Plan, step J).

    python make_voice_samples.py            # Tamil, the default candidate list
    python make_voice_samples.py en         # English
    python make_voice_samples.py ta kavya mani --pace 0.9

Writes voice_samples/<lang>/<voice>_<n>.wav and an index.html to play them
side by side. The text goes through the same clean-up as live replies
(speech_text_filter.to_speech), so what you hear is what a caller would hear.
Nothing here can pick the voice for you — that needs a listener, ideally a
native speaker for Tamil.
"""
import argparse
import asyncio
import base64
import html
import os
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from speech_text_filter import to_speech

OUT = Path(__file__).parent / "voice_samples"
# The four voices live calls use today (conversation_router.TOPIC_VOICES),
# then other bulbul:v3 speakers to compare against.
DEFAULT_VOICES = ["ritu", "aayan", "sunny", "shreya", "kavya", "kavitha", "priya", "mani", "gokul", "vijay"]
SENTENCES = {
    "ta": [
        "மாணவர்களுக்கும் மாணவிகளுக்கும் தனித்தனி விடுதி இருக்கு. Wi-Fi, உணவு வசதி எல்லாம் உண்டு.",
        "CSE படிப்பு 2001-ல ஆரம்பிச்சது. சேர்க்கை TNEA கவுன்சிலிங் மூலமா நடக்கும், கோடு 1316.",
        "எங்க கல்லூரி சென்னை பழைய மாமல்லபுரம் சாலையில தாழம்பூர்ல இருக்கு. வேற என்ன தெரிஞ்சுக்கணும்?",
    ],
    "en": [
        "Yes, we have separate hostels for boys and girls, with Wi-Fi and a dining hall.",
        "CSE started in 2001. Admission is through TNEA counselling, and our code is 1316.",
        "We're on Old Mahabalipuram Road in Thalambur, Chennai. What else would you like to know?",
    ],
}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("language", nargs="?", default="ta", choices=["ta", "en"])
    parser.add_argument("voices", nargs="*", default=DEFAULT_VOICES)
    parser.add_argument("--pace", type=float, default=None, help="0.5-2.0; omit for Sarvam's default")
    parser.add_argument("--temperature", type=float, default=None, help="0.01-2.0; omit for Sarvam's default")
    args = parser.parse_args()

    out = OUT / args.language
    out.mkdir(parents=True, exist_ok=True)
    tag = "".join(f"_{k}{v}" for k, v in (("pace", args.pace), ("temp", args.temperature)) if v is not None)
    rows = []
    async with aiohttp.ClientSession() as session:
        for voice in args.voices:
            cells = []
            for n, sentence in enumerate(SENTENCES[args.language], 1):
                payload = {
                    "text": await to_speech(sentence),
                    "target_language_code": f"{args.language}-IN",
                    "speaker": voice,
                    "model": "bulbul:v3",
                    "speech_sample_rate": 24000,
                }
                if args.pace is not None:
                    payload["pace"] = args.pace
                if args.temperature is not None:
                    payload["temperature"] = args.temperature
                async with session.post(
                    "https://api.sarvam.ai/text-to-speech",
                    headers={"api-subscription-key": os.environ["SARVAM_API_KEY"]},
                    json=payload,
                ) as r:
                    body = await r.json()
                if r.status != 200:
                    print(f"{voice}: {body.get('error', body)}")
                    cells.append("<td>unavailable</td>")
                    continue
                name = f"{voice}{tag}_{n}.wav"
                (out / name).write_bytes(base64.b64decode(body["audios"][0]))
                cells.append(f'<td><audio controls preload="none" src="{name}"></audio></td>')
            rows.append(f"<tr><th>{html.escape(voice)}</th>{''.join(cells)}</tr>")
            print(f"{voice}: done")

    head = "".join(f"<th>{html.escape(s)}</th>" for s in SENTENCES[args.language])
    page = out / f"index{tag}.html"
    page.write_text(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Voice samples</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;margin:24px;max-width:1100px}}
table{{border-collapse:collapse}}th,td{{padding:8px 10px;border-bottom:1px solid #ccc;text-align:left;vertical-align:top}}
thead th{{font-weight:500;max-width:300px}}audio{{width:260px}}</style></head><body>
<h1>Bulbul voice samples — {args.language}{html.escape(tag.replace('_', ' '))}</h1>
<p>The first four voices are the ones live calls use today, one per topic.</p>
<table><thead><tr><th>Voice</th>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>
</body></html>""",
        encoding="utf-8",
    )
    print(f"Open {page}")


if __name__ == "__main__":
    asyncio.run(main())
