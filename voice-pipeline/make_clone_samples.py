"""Samples of Sarvam cloned voices (POST /voices/clone) for choosing by ear.

    python make_clone_samples.py Ramya=svc-... Anas=svc-... [--pace 1.0]

Writes voice_samples/clones/<name>_<n>.wav and index.html. Same sentences
and clean-up as make_voice_samples.py. Cloned voices are not streamed, so each
sentence is one request/response (see cloned_voice_tts.py).
"""
import argparse, asyncio, base64, html, os
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
from make_voice_samples import SENTENCES
from speech_text_filter import to_speech

OUT = Path(__file__).parent / "voice_samples" / "clones"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("voices", nargs="+", help="Name=voice_id")
    ap.add_argument("--pace", type=float, default=1.0)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    sections = []
    async with aiohttp.ClientSession() as s:
        for lang in ("ta", "en"):
            rows = []
            for item in args.voices:
                name, vid = item.split("=", 1)
                cells = []
                for n, sentence in enumerate(SENTENCES[lang], 1):
                    form = aiohttp.FormData()
                    form.add_field("voice_id", vid)
                    form.add_field("text", await to_speech(sentence))
                    form.add_field("language_code", f"{lang}-IN")
                    form.add_field("pace", str(args.pace))
                    async with s.post("https://api.sarvam.ai/voices/clone", data=form,
                                      headers={"api-subscription-key": os.environ["SARVAM_API_KEY"]}) as r:
                        body = await r.json()
                    b64 = body.get("audio_b64") or body.get("audio")
                    if r.status != 200 or not b64:
                        print(f"{name} {lang}: {r.status} {body}")
                        cells.append("<td>unavailable</td>")
                        continue
                    fn = f"{name}_{lang}_{n}.wav"
                    (OUT / fn).write_bytes(base64.b64decode(b64))
                    cells.append(f'<td><audio controls preload="none" src="{fn}"></audio></td>')
                rows.append(f"<tr><th>{html.escape(name)}</th>{''.join(cells)}</tr>")
                print(f"{name} {lang}: done")
            head = "".join(f"<th>{html.escape(t)}</th>" for t in SENTENCES[lang])
            sections.append(f"<h2>{'Tamil' if lang == 'ta' else 'English'}</h2><table><thead><tr><th>Voice</th>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>")
    (OUT / "index.html").write_text(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Clone samples</title>
<style>body{{font:15px/1.5 system-ui;margin:24px;max-width:1100px}}th,td{{padding:8px 10px;border-bottom:1px solid #ccc;text-align:left;vertical-align:top}}audio{{width:260px}}</style></head><body>
<h1>Cloned voice samples</h1>{''.join(sections)}</body></html>""",
        encoding="utf-8")
    print(f"Open {OUT / 'index.html'}")


asyncio.run(main())
