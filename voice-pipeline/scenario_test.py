"""Scripted voice-call scenarios for the website bot (Voice Naturalness Plan,
step L): plays synthesized visitor speech into a running voice-pipeline over
its websocket, the way the widget does, and records what came back.

    python scenario_test.py                      # all scenarios, ws://127.0.0.1:8001
    python scenario_test.py --url ws://127.0.0.1:8011 --only en-pause
    python scenario_test.py --include-lead       # also run the lead-capture call

What it measures, per visitor turn: seconds from the end of the visitor's
speech to the bot's first audio, the bot's reply and its word count, and —
for turns spoken over the bot — whether and how fast the bot stopped.
Results go to scenario_results/<timestamp>.csv plus a printed summary against
the plan's targets.

What it cannot measure: how the bot SOUNDS (voice quality, prosody,
naturalness), and anything that depends on a real microphone — room noise,
speaker echo, a human's timing. The visitor here is a Sarvam stock voice, so
treat the numbers as a regression check between builds, not as a verdict. The
Tamil lines were written without a native speaker's review.

The lead-capture scenario is off by default because it writes a real row to
the admissions Google Sheet (name "Scenario Test"); delete that row afterwards.

Visitor clips are synthesized once with Sarvam TTS and cached in
.scenario_clips/. Needs SARVAM_API_KEY (voice-pipeline/.env) and both
services running.
"""
import argparse
import asyncio
import base64
import csv
import hashlib
import io
import json
import os
import statistics
import time
import wave
from datetime import datetime
from pathlib import Path

import aiohttp
import numpy as np
import websockets
from dotenv import load_dotenv

load_dotenv()

HERE = Path(__file__).parent
CLIPS_DIR = HERE / ".scenario_clips"
RESULTS_DIR = HERE / "scenario_results"
LOGS_DIR = HERE / "logs"
CALL_PATH = "/voice/{lang}"  # overridden by --path
FRAME_BYTES = 640           # 20 ms of 16 kHz PCM16, what the widget sends
BOT_BYTES_PER_SEC = 48000    # 24 kHz PCM16 mono
VISITOR_VOICE = "priya"

# A scenario is (name, starting language, steps). Steps:
#   ("say", text)                    wait for the bot to be quiet, speak, wait for the reply
#   ("say", text, lang)              same, in another language (mid-call switch)
#   ("pause", first, seconds, rest)  one sentence with a silent gap in the middle
#   ("over", text, delay)            speak `delay` seconds into the bot's current reply
#   ("ack", text, delay)             same, but a bare acknowledgement the bot should ignore
SCENARIOS = [
    ("en-courses", "en", [("say", "What courses do you offer?")]),
    ("en-fees", "en", [("say", "What is the fee for computer science engineering?")]),
    ("en-placements", "en", [("say", "How are the placements at your college?")]),
    ("en-hostel", "en", [("say", "Do you have hostel facilities?")]),
    ("en-apply", "en", [("say", "How do I apply for admission?")]),
    ("en-location", "en", [("say", "Where is the college located?")]),
    ("en-accreditation", "en", [("say", "Is the college accredited?")]),
    ("en-eligibility", "en", [("say", "What is the eligibility for B Tech?")]),
    ("en-transport", "en", [("say", "Is there a college bus from Chennai?")]),
    ("en-recruiters", "en", [("say", "Which companies come for campus recruitment?")]),
    ("en-greeting", "en", [("say", "Hello."), ("say", "Tell me about the campus.")]),
    ("en-followup-memory", "en", [
        ("say", "I'm interested in mechanical engineering."),
        ("say", "What about placements for that?"),
    ]),
    ("en-parent-memory", "en", [
        ("say", "My daughter has finished twelfth standard and wants to join computer science."),
        ("say", "What should we do next?"),
    ]),
    ("en-pause-1", "en", [("pause", "I wanted to ask about", 1.0, "the hostel facilities for first year students.")]),
    ("en-pause-2", "en", [("pause", "Can you tell me", 0.8, "which departments have the best placements?")]),
    ("en-pause-3", "en", [("pause", "My son scored ninety percent and", 1.2, "he wants to study electronics.")]),
    ("en-ack-1", "en", [("say", "What courses do you offer?"), ("ack", "Okay.", 1.0)]),
    ("en-ack-2", "en", [("say", "Tell me about the placements."), ("ack", "Hmm.", 1.2)]),
    ("en-ack-3", "en", [("say", "How do I apply for admission?"), ("ack", "Right.", 1.0)]),
    ("en-interrupt-1", "en", [("say", "What courses do you offer?"), ("over", "Wait, actually tell me about placements instead.", 1.0)]),
    ("en-interrupt-2", "en", [("say", "Tell me about the campus."), ("over", "Sorry, what I really want to know is the fee structure.", 1.2)]),
    ("en-interrupt-3", "en", [("say", "How are the placements at your college?"), ("over", "One second, is there a hostel for girls?", 0.8)]),
    ("en-ack-as-answer", "en", [("say", "I want to know about admissions."), ("say", "Yes.")]),
    ("en-thanks", "en", [("say", "What is the counselling code?"), ("say", "Thank you.")]),
    ("ta-courses", "ta", [("say", "உங்கள் கல்லூரியில் என்ன படிப்புகள் உள்ளன?")]),
    ("ta-fees", "ta", [("say", "கம்ப்யூட்டர் சயின்ஸ் படிப்புக்கு கட்டணம் எவ்வளவு?")]),
    ("ta-placements", "ta", [("say", "உங்கள் கல்லூரியில் வேலைவாய்ப்பு எப்படி இருக்கிறது?")]),
    ("ta-hostel", "ta", [("say", "விடுதி வசதி இருக்கிறதா?")]),
    ("ta-apply", "ta", [("say", "சேர்க்கைக்கு எப்படி விண்ணப்பிப்பது?")]),
    ("ta-location", "ta", [("say", "கல்லூரி எங்கே இருக்கிறது?")]),
    ("ta-pause", "ta", [("pause", "நான் கேட்க விரும்புவது", 1.0, "விடுதி கட்டணம் பற்றி.")]),
    ("ta-ack", "ta", [("say", "உங்கள் கல்லூரியில் என்ன படிப்புகள் உள்ளன?"), ("ack", "சரி.", 1.0)]),
    ("ta-interrupt", "ta", [("say", "கல்லூரி பற்றி சொல்லுங்கள்."), ("over", "ஒரு நிமிஷம், வேலைவாய்ப்பு பற்றி சொல்லுங்கள்.", 1.0)]),
    ("switch-en-to-ta", "en", [("say", "What courses do you offer?"), ("say", "சேர்க்கைக்கு எப்படி விண்ணப்பிப்பது?", "ta")]),
    ("switch-ta-to-en", "ta", [("say", "விடுதி வசதி இருக்கிறதா?"), ("say", "How are the placements at your college?", "en")]),
]
# Zimi P0 validation (2026-10-09): the nine inputs from the brief, one fresh call
# each (calls open on English now, so the Tamil lines also exercise the
# first-turn switch), plus one call that runs them back to back to check that a
# style carries across a bare "okay". Romanized lines are synthesized with the
# Tamil locale so they come out with Tamil pronunciation. Read the results in
# the voice-pipeline log's `[turn]` lines and the backend's `[turn-backend]`
# lines (docs/Zimi_P0_Validation.md says how).
P0_INPUTS = [
    ("p0-1-tamil-fee", "கட்டணம் எவ்வளவு?"),
    ("p0-2-fees-evlo-bro", "fees evlo bro?"),
    ("p0-3-btech-fees-evlo", "B.Tech fees evlo?"),
    ("p0-4-english-btech-fee", "What is the B.Tech fee?"),
    ("p0-5-application-epdi", "Anna application epdi panrathu?"),
    ("p0-6-hostel-iruka", "hostel iruka?"),
    ("p0-7-seri", "seri"),
    ("p0-8-okay", "okay"),
    ("p0-9-cse-fees-bro", "CSE fees evlo bro?"),
]
SCENARIOS += [
    (name, "en", [("say", text, "en" if text == "What is the B.Tech fee?" or text == "okay" else "ta")])
    for name, text in P0_INPUTS
]
SCENARIOS.append((
    "p0-sequence", "en",
    [("say", text, "en" if text in ("What is the B.Tech fee?", "okay") else "ta") for _, text in P0_INPUTS],
))
LEAD_SCENARIO = ("en-lead-capture", "en", [
    ("say", "I want to apply for computer science engineering."),
    ("say", "My name is Scenario Test."),
    ("say", "My number is nine eight seven six five four three two one zero."),
    ("say", "What is the next step?"),
])


async def synthesize(session: aiohttp.ClientSession, text: str, lang: str) -> bytes:
    """Visitor speech as 16 kHz PCM16, silence trimmed so its end is the end of speech."""
    CLIPS_DIR.mkdir(exist_ok=True)
    path = CLIPS_DIR / (hashlib.sha1(f"{VISITOR_VOICE}|{lang}|{text}".encode()).hexdigest() + ".pcm")
    if not path.exists():
        async with session.post(
            "https://api.sarvam.ai/text-to-speech",
            headers={"api-subscription-key": os.environ["SARVAM_API_KEY"]},
            json={"text": text, "target_language_code": f"{lang}-IN", "speaker": VISITOR_VOICE,
                  "model": "bulbul:v3", "speech_sample_rate": 16000},
        ) as r:
            body = await r.json()
            if r.status != 200:
                raise RuntimeError(f"Sarvam TTS failed for {text!r}: {r.status} {body}")
        with wave.open(io.BytesIO(base64.b64decode(body["audios"][0]))) as w:
            assert w.getframerate() == 16000 and w.getnchannels() == 1
            path.write_bytes(w.readframes(w.getnframes()))
    samples = np.frombuffer(path.read_bytes(), dtype=np.int16)
    loud = np.flatnonzero(np.abs(samples) > 500)
    return samples[max(0, loud[0] - 320): loud[-1] + 320].tobytes()


class Call:
    """One websocket call: a mic that never stops (silence between clips)
    and a record of everything the bot sent back."""

    def __init__(self, ws):
        self.ws = ws
        self.t0 = time.monotonic()
        self.audio = []      # (arrival time, byte count) per bot audio chunk
        self.stops = []      # arrival times of the server's "stop playback" message
        self._outgoing = bytearray()
        self._closed = False
        self._tasks = [asyncio.create_task(self._receive()), asyncio.create_task(self._send())]

    def now(self) -> float:
        return time.monotonic() - self.t0

    async def _receive(self):
        try:
            async for message in self.ws:
                if isinstance(message, (bytes, bytearray)):
                    self.audio.append((self.now(), len(message)))
                elif json.loads(message).get("type") == "UserStartedSpeaking":
                    self.stops.append(self.now())
        except Exception:
            pass

    async def _send(self):
        next_send = time.monotonic()
        try:
            while not self._closed:
                frame = bytes(self._outgoing[:FRAME_BYTES])
                del self._outgoing[:FRAME_BYTES]
                await self.ws.send(frame.ljust(FRAME_BYTES, b"\0"))
                next_send += 0.02
                await asyncio.sleep(max(0, next_send - time.monotonic()))
        except Exception:
            pass

    async def speak(self, pcm: bytes) -> tuple[float, float]:
        start = self.now()
        self._outgoing.extend(pcm)
        await asyncio.sleep(len(pcm) / 32000)
        while self._outgoing:
            await asyncio.sleep(0.01)
        return start, self.now()

    def first_audio_after(self, t: float) -> float | None:
        return next((a for a, _ in self.audio if a > t), None)

    async def wait_for_reply_start(self, after: float, timeout: float = 12) -> float | None:
        deadline = self.now() + timeout
        while self.now() < deadline:
            first = self.first_audio_after(after)
            if first is not None:
                return first
            await asyncio.sleep(0.03)
        return None

    async def wait_until_quiet(self, quiet: float = 1.3, timeout: float = 45):
        """Until the bot has stopped sending audio and would have finished playing it."""
        deadline = self.now() + timeout
        while self.now() < deadline:
            if self.audio and self.now() - self.audio[-1][0] > quiet:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.6)

    async def close(self):
        self._closed = True
        await asyncio.sleep(0.05)
        try:
            await self.ws.close()
        except Exception:
            pass
        for task in self._tasks:
            task.cancel()


def _latest_transcript(started_at: float) -> list[dict]:
    """The transcript voice-pipeline saved for the call that just ended."""
    newest, newest_mtime = None, started_at - 1
    for path in LOGS_DIR.glob("*/*/transcript.json"):
        mtime = path.stat().st_mtime
        if mtime > newest_mtime:
            newest, newest_mtime = path, mtime
    if not newest:
        return []
    try:
        return json.loads(newest.read_text(encoding="utf-8")).get("messages", [])
    except Exception:
        return []


async def run_scenario(url: str, http: aiohttp.ClientSession, scenario) -> list[dict]:
    name, start_lang, steps = scenario
    # Synthesize everything first so no TTS call lands in the middle of the call.
    clips = {}
    for step in steps:
        lang = step[2] if step[0] == "say" and len(step) > 2 else start_lang
        for text in ([step[1], step[3]] if step[0] == "pause" else [step[1]]):
            clips[text] = await synthesize(http, text, lang)

    wall_start = time.time()
    ws = await websockets.connect(f"{url}{CALL_PATH.format(lang=start_lang)}", max_size=None)
    call = Call(ws)
    rows = []
    try:
        await call.wait_until_quiet()   # greeting
        reply_start = None
        for step in steps:
            kind, text = step[0], step[1]
            row = {"scenario": name, "language": start_lang, "step": kind, "visitor_said": text}
            if kind in ("over", "ack"):
                # Spoken over the reply to the previous step.
                if reply_start is None:
                    row["note"] = "no reply to speak over"
                    rows.append(row)
                    continue
                await asyncio.sleep(max(0, reply_start + step[2] - call.now()))
                audio_before = sum(n for _, n in call.audio)
                start, end = await call.speak(clips[text])
                await asyncio.sleep(1.5)
                stop = next((s for s in call.stops if start <= s <= end + 1.5), None)
                row["bot_stopped"] = stop is not None
                row["stop_after_speech_start_s"] = round(stop - start, 2) if stop else ""
                if kind == "over":
                    reply_start = await call.wait_for_reply_start(end)
                    row["first_audio_s"] = round(reply_start - end, 2) if reply_start else ""
                else:
                    row["bot_kept_talking"] = sum(n for _, n in call.audio) > audio_before
                rows.append(row)
                await call.wait_until_quiet()
                continue

            await call.wait_until_quiet(timeout=20)
            if kind == "pause":
                _, gap_start = await call.speak(clips[text])
                await asyncio.sleep(step[2])
                gap_end = call.now()
                _, end = await call.speak(clips[step[3]])
                row["visitor_said"] = f"{text} … {step[3]}"
                row["bot_spoke_during_pause"] = any(gap_start < a <= gap_end for a, _ in call.audio)
            else:
                _, end = await call.speak(clips[text])
            reply_start = await call.wait_for_reply_start(end)
            row["first_audio_s"] = round(reply_start - end, 2) if reply_start else ""
            rows.append(row)
        await call.wait_until_quiet()
    finally:
        await call.close()

    # Attach the bot's words from the saved transcript: the Nth visitor turn's
    # reply is the assistant message that follows the Nth user message.
    await asyncio.sleep(1.0)
    messages = [m for m in _latest_transcript(wall_start) if m.get("role") in ("user", "assistant")]
    user_turns = [i for i, m in enumerate(messages) if m["role"] == "user"]
    if rows:
        rows[-1]["user_turns_in_transcript"] = len(user_turns)
    spoken = [r for r in rows if r["step"] != "ack" and "note" not in r]
    for r, idx in zip(reversed(spoken), reversed(user_turns)):
        r["heard_as"] = str(messages[idx].get("content", ""))
        reply = next((m["content"] for m in messages[idx + 1:] if m["role"] == "assistant"), "")
        r["bot_reply"] = reply
        r["reply_words"] = len(str(reply).split())
    return rows


def summarize(rows: list[dict]) -> str:
    def values(key, pred=lambda r: True):
        return [r[key] for r in rows if pred(r) and r.get(key) not in ("", None)]

    lines = []
    for lang in ("en", "ta"):
        first = values("first_audio_s", lambda r: r["language"] == lang and r["step"] in ("say", "pause"))
        if first:
            under = sum(1 for v in first if v < 0.7)
            lines.append(
                f"First audio after visitor stops ({lang}): median {statistics.median(first):.2f}s, "
                f"range {min(first):.2f}-{max(first):.2f}s, {under}/{len(first)} under the 0.7s target"
            )
    words = values("reply_words", lambda r: r.get("reply_words", 0) > 0)
    if words:
        short = sum(1 for w in words if 30 <= w <= 55)
        long_ = sum(1 for w in words if w > 70)
        lines.append(
            f"Reply length: median {statistics.median(words):.0f} words; {short}/{len(words)} in the 30-55 "
            f"word range; {long_}/{len(words)} over 70 words (target under 10%)"
        )
    overs = [r for r in rows if r["step"] == "over" and "note" not in r]
    if overs:
        stopped = [r for r in overs if r["bot_stopped"]]
        times = [r["stop_after_speech_start_s"] for r in stopped]
        lines.append(
            f"Real interruptions: bot stopped in {len(stopped)}/{len(overs)}"
            + (f"; stop came {statistics.median(times):.2f}s (median) after the visitor began, "
               f"range {min(times):.2f}-{max(times):.2f}s (target 0.2s)" if times else "")
        )
    acks = [r for r in rows if r["step"] == "ack" and "note" not in r]
    if acks:
        ignored = sum(1 for r in acks if not r["bot_stopped"])
        lines.append(f"Acknowledgements over the bot: correctly ignored in {ignored}/{len(acks)}")
    pauses = [r for r in rows if r["step"] == "pause"]
    if pauses:
        clean = sum(1 for r in pauses if not r["bot_spoke_during_pause"])
        lines.append(f"Mid-sentence pauses: bot stayed quiet during the pause in {clean}/{len(pauses)}")
    unheard = [r for r in rows if r["step"] in ("say", "pause") and r.get("first_audio_s") == ""]
    if unheard:
        lines.append(f"No reply at all within 12s: {len(unheard)} turn(s) — " + ", ".join(r["scenario"] for r in unheard))
    return "\n".join(lines)


async def main():
    parser = argparse.ArgumentParser(description="Scripted voice-call scenarios for the website bot.")
    parser.add_argument("--url", default="ws://127.0.0.1:8001", help="voice-pipeline websocket base URL")
    parser.add_argument("--only", help="run only scenarios whose name contains this text")
    parser.add_argument("--path", default="/voice/{lang}",
                        help="websocket path on --url; {lang} is the scenario's starting language "
                             "(the phone agent's browser test line is /test/ws)")
    parser.add_argument("--include-lead", action="store_true",
                        help="also run the lead-capture call (writes a real row to the leads Sheet)")
    args = parser.parse_args()
    global CALL_PATH
    CALL_PATH = args.path

    # The Zimi P0 scenarios only run when asked for (--only p0): they are a
    # separate check, not part of the regression set.
    scenarios = [s for s in SCENARIOS if args.only or not s[0].startswith("p0-")]
    scenarios += [LEAD_SCENARIO] if args.include_lead else []
    if args.only:
        scenarios = [s for s in scenarios if args.only in s[0]]
    rows = []
    async with aiohttp.ClientSession() as http:
        for i, scenario in enumerate(scenarios, 1):
            print(f"[{i}/{len(scenarios)}] {scenario[0]}", flush=True)
            try:
                rows.extend(await run_scenario(args.url, http, scenario))
            except Exception as e:
                print(f"    failed: {e}")
                rows.append({"scenario": scenario[0], "language": scenario[1], "step": "error", "note": str(e)})

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.csv"
    columns = ["scenario", "language", "step", "visitor_said", "heard_as", "first_audio_s", "bot_reply",
               "reply_words", "bot_spoke_during_pause", "bot_stopped", "stop_after_speech_start_s",
               "bot_kept_talking", "user_turns_in_transcript", "note"]
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print("\n" + summarize(rows))
    print(f"\nPer-turn detail: {out}")


if __name__ == "__main__":
    asyncio.run(main())
