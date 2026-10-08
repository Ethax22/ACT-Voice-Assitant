"""Replay every saved conversation through two chat models and compare.

    python compare_models.py                        # all saved logs, both Sarvam models
    python compare_models.py --days 3 --models sarvam-105b sarvam-105b-conversations

Takes the VISITOR's side of each saved transcript (typed chat and voice) and
has each model answer it turn by turn through the bot's real prompt and
knowledge base, each model building on its own earlier replies. Writes a
side-by-side CSV to reports/ and prints a summary.

Safe to run: lead capture is stubbed out, so replaying a conversation that
contains a name and phone number does NOT write to the admissions Sheet.

What it can't tell you: whether an answer is factually right, or how it
sounds. The counts below are things a script can check — length, repetition,
language, a few known-bad words. Read the CSV for the rest.
"""
import argparse
import asyncio
import csv
import json
import re
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import main

HERE = Path(__file__).parent
LOG_DIRS = [HERE / "logs", HERE.parent / "voice-pipeline" / "logs"]
TAMIL = re.compile(r"[஀-௿]")
SENTENCE_END = re.compile(r"(?<=[.?!])\s+")
BANNED_TA = ["ஆம்பளை", "பொம்பளை", "பசங்க", "பொண்ணு", "பையன்", "மச்சி"]
NOT_OFFERED = ["Nursing", "நர்சிங்", "MBA", "எம்.பி.ஏ", "MCA", "எம்.சி.ஏ", "B.Sc", "பி.எஸ்சி", "B.Com"]
OPENER = re.compile(r"^\W*(good question|that'?s a good|great question|sure\b|certainly|let me|நல்ல கேள்வி|நிச்சயமா)", re.I)
MAX_TURNS = 40

_leads_attempted = 0


def _fake_save_lead(**kwargs) -> bool:
    global _leads_attempted
    _leads_attempted += 1
    return True


main.save_lead = _fake_save_lead  # never touch the real Sheet from a replay


def load_conversations(days: int | None) -> list[dict]:
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days) if days else None
    out = []
    for logs in LOG_DIRS:
        for path in sorted(logs.glob("*/*/transcript.json")):
            try:
                day = datetime.strptime(path.parent.parent.name, "%Y-%m-%d").date()
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if cutoff and day < cutoff:
                continue
            turns, pending = [], []
            for m in data.get("messages", []):
                content = m.get("content")
                if not isinstance(content, str) or not content.strip():
                    continue
                if m.get("role") == "user":
                    pending.append(content.strip())  # consecutive user messages = one spoken turn
                elif m.get("role") == "assistant" and pending:
                    turns.append(" ".join(pending))
                    pending = []
            if pending:
                turns.append(" ".join(pending))
            if turns:
                out.append({
                    "id": path.parent.name[:8], "date": day.isoformat(),
                    "source": data.get("source", "chat"), "turns": turns[:MAX_TURNS],
                })
    return out


async def replay(conv: dict, model: str, gate: asyncio.Semaphore) -> list[dict]:
    rows, history = [], []
    voice = conv["source"] == "voice"
    async with gate:
        for turn in conv["turns"]:
            history.append({"role": "user", "content": turn})
            language = main._reply_language(history)
            reply, secs = "", 0.0
            for attempt in range(4):
                try:
                    t0 = time.perf_counter()
                    full = await main._build_full_messages(history, voice=voice)
                    response = await main.chat_client.chat.completions.create(
                        model=model, messages=full, tools=[main.CAPTURE_LEAD_TOOL],
                        extra_body=main.DISABLE_REASONING,
                    )
                    choice = response.choices[0]
                    if choice.finish_reason == "tool_calls":
                        extended = await main._resolve_tool_calls(full, choice.message.model_dump(), "replay")
                        response = await main.chat_client.chat.completions.create(
                            model=model, messages=extended, tools=[main.CAPTURE_LEAD_TOOL],
                            extra_body=main.DISABLE_REASONING,
                        )
                    reply = (response.choices[0].message.content or "").strip()
                    secs = time.perf_counter() - t0
                    break
                except Exception as e:
                    if attempt == 3:
                        reply = f"[ERROR: {type(e).__name__}]"
                    await asyncio.sleep(4 * (attempt + 1))
            history.append({"role": "assistant", "content": reply})
            rows.append({"visitor": turn, "language": language, "reply": reply, "seconds": round(secs, 2)})
    return rows


def score(all_rows: list[list[dict]]) -> dict:
    s = {"replies": 0, "errors": 0, "words": [], "seconds": [], "over_45": 0, "wrong_language": 0,
         "opener": 0, "banned_ta": 0, "not_offered": 0, "exact_repeat": 0, "repeat_question": 0,
         "ends_with_question": 0, "tamil_replies": 0}
    for rows in all_rows:
        seen, questions = set(), set()
        for r in rows:
            reply = r["reply"]
            if reply.startswith("[ERROR"):
                s["errors"] += 1
                continue
            s["replies"] += 1
            words = len(reply.split())
            s["words"].append(words)
            s["seconds"].append(r["seconds"])
            s["over_45"] += words > 45
            tamil_chars = len(TAMIL.findall(reply))
            if r["language"] == "ta":
                s["tamil_replies"] += 1
                s["wrong_language"] += tamil_chars < 10
                s["banned_ta"] += any(b in reply for b in BANNED_TA)
            else:
                s["wrong_language"] += tamil_chars > 0
            s["opener"] += bool(OPENER.search(reply))
            s["not_offered"] += any(w in reply for w in NOT_OFFERED) and not re.search(
                r"don'?t offer|do not offer|இல்ல|not offer|only", reply, re.I)
            s["exact_repeat"] += reply in seen
            seen.add(reply)
            if reply.endswith("?"):
                s["ends_with_question"] += 1
                last = SENTENCE_END.split(reply)[-1]
                s["repeat_question"] += last in questions
                questions.add(last)
    return s


async def run():
    parser = argparse.ArgumentParser(description="Replay saved conversations through two chat models.")
    parser.add_argument("--days", type=int, default=None, help="only logs from the last N days (default: all)")
    parser.add_argument("--models", nargs=2, default=["sarvam-105b", "sarvam-105b-conversations"])
    parser.add_argument("--limit", type=int, default=None, help="replay at most this many conversations")
    args = parser.parse_args()

    conversations = load_conversations(args.days)[: args.limit]
    total_turns = sum(len(c["turns"]) for c in conversations)
    print(f"{len(conversations)} conversations, {total_turns} visitor turns, models: {args.models}", flush=True)
    await main.retriever.warm_up()

    gate = asyncio.Semaphore(2)
    results = {}
    for model in args.models:
        t0 = time.perf_counter()
        results[model] = await asyncio.gather(*(replay(c, model, gate) for c in conversations))
        print(f"  {model}: done in {time.perf_counter() - t0:.0f}s", flush=True)

    out_dir = HERE / "reports"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"model_comparison_{datetime.now().strftime('%Y-%m-%d_%H%M')}.csv"
    a, b = args.models
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["date", "conversation", "source", "turn", "language", "visitor", a, f"{a} words", b, f"{b} words"])
        for ci, conv in enumerate(conversations):
            for ti, (ra, rb) in enumerate(zip(results[a][ci], results[b][ci]), 1):
                w.writerow([conv["date"], conv["id"], conv["source"], ti, ra["language"], ra["visitor"],
                            ra["reply"], len(ra["reply"].split()), rb["reply"], len(rb["reply"].split())])

    print()
    labels = [
        ("replies", "Replies generated"), ("errors", "Failed calls"),
        ("median_words", "Median reply length (words)"), ("over_45", "Replies over 45 words"),
        ("median_seconds", "Median time per reply (s)"), ("wrong_language", "Replies in the wrong language"),
        ("opener", "Stock opener (\"Good question\"...)"), ("exact_repeat", "Exact repeat of an earlier reply"),
        ("ends_with_question", "Replies ending in a question"), ("repeat_question", "Same closing question again"),
        ("tamil_replies", "Tamil replies"), ("banned_ta", "Tamil replies with a banned slang word"),
        ("not_offered", "Named a course the college doesn't offer"),
    ]
    scores = {}
    for model in args.models:
        s = score(results[model])
        s["median_words"] = statistics.median(s["words"]) if s["words"] else 0
        s["median_seconds"] = round(statistics.median(s["seconds"]), 2) if s["seconds"] else 0
        scores[model] = s
    print(f"{'':44s}{a:>16s}{b:>30s}")
    for key, label in labels:
        print(f"{label:44s}{str(scores[a][key]):>16s}{str(scores[b][key]):>30s}")
    print(f"\nLead-capture calls intercepted (nothing written to the Sheet): {_leads_attempted}")
    print(f"Side by side: {out}")


if __name__ == "__main__":
    asyncio.run(run())
