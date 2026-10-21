"""Sentiment report for the website bot (typed chat + voice) and the phone agent
(inbound and outbound calls), built from the transcripts already saved to disk.

Run after the fact, never during a call — it adds nothing to reply latency:

    python sentiment_report.py              # last 7 days
    python sentiment_report.py --days 30
    python sentiment_report.py --from 2026-10-01 --to 2026-10-07
    python sentiment_report.py --channel phone      # or website; default both

Channels: website chat (rag-backend/logs), website voice and phone calls (both in
voice-pipeline/logs; phone call folders start with "phone-", and their call_meta.json
says inbound or outbound). Phone calls get their own prompt (a caller, not a website
visitor; current students, parents and faculty ringing about records are not admission
leads). The phone agent also judges each call right after it ends (analyse_folder).

The running backend also does this by itself (main.py starts run_periodically()
below): the last 7 days, rebuilt whenever the newest report is a day old.

For each conversation it asks Sarvam 105B (the same model that answers
visitors — no new vendor) for a small structured judgement, then writes an
HTML report plus a CSV of the same rows into rag-backend/reports/.

Each judgement is cached next to its transcript (sentiment.json), keyed by a
hash of the conversation, so re-running only pays for new or changed
conversations, and the cache is deleted with the transcript by the existing
90-day prune. Reports themselves are pruned here on the same 90-day rule.

The labels are a model's reading of a transcript, not a measurement. Nobody
has yet checked them against conversations a person labelled by hand —
least of all the Tamil ones — so treat the report as a pointer to which
conversations to read, not as a score to act on by itself.
"""
import argparse
import asyncio
import csv
import hashlib
import html
import json
import os
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from openai import AsyncOpenAI, RateLimitError

HERE = Path(__file__).parent
LOG_DIRS = [HERE / "logs", HERE.parent / "voice-pipeline" / "logs"]
REPORTS_DIR = HERE / "reports"
RETENTION_DAYS = 90
CHAT_MODEL = "sarvam-105b"
PROMPT_VERSION = "3"  # bump to invalidate every cached website judgement
PHONE_PROMPT_VERSION = "1"  # the same for phone calls
CONCURRENCY = 2

SENTIMENTS = ("positive", "neutral", "negative")
TRENDS = ("improved", "steady", "worsened")
INTEREST_LEVELS = ("high", "medium", "low", "none")
TOPICS = ("admissions", "fees", "courses", "placements", "campus", "about", "other")
PHONE_TOPICS = TOPICS[:-1] + ("records", "other")
CHANNELS = {"website_chat": "Website chat", "website_voice": "Website voice",
            "phone_inbound": "Phone inbound", "phone_outbound": "Phone outbound"}

ANALYSIS_PROMPT = """You review one conversation between a website visitor and "Agni AI",
the assistant of Agni College of Technology. The visitor may write or speak in
English, Tamil, or a mix. Judge the VISITOR's side only.

Reply with one JSON object and nothing else, with exactly these keys:
- "sentiment": "positive", "neutral" or "negative" — the visitor's overall
  feeling by the end. Plain factual questions with no feeling shown are "neutral".
- "trend": "improved", "steady" or "worsened" — how their mood moved from the
  start of the conversation to the end.
- "admission_interest": "high" (wants to apply or be called, or gave contact
  details), "medium" (asks about fees, eligibility or how to apply), "low"
  (general curiosity) or "none".
- "frustrated": true or false — did the visitor show annoyance: repeating
  themselves, complaining, saying the assistant is wrong or not listening?
- "frustration_reason": a short English phrase, or "" if not frustrated.
- "unanswered": visitor questions the assistant FAILED to answer. Include a
  question only if the assistant's reply to it said it doesn't know, doesn't
  have that information, or told the visitor to contact admissions instead of
  answering. If the assistant gave an actual answer, it does not belong here.
  Never include questions the assistant itself asked. Write each in English,
  short. Most conversations have none: use an empty list.
- "topics": a list drawn only from: admissions, fees, courses, placements,
  campus, about, other.
- "summary": one English sentence, at most 25 words, on what the visitor wanted
  and how it went. Do not include names, phone numbers or email addresses.

Base every field only on what is in the transcript. If the visitor said too
little to judge, use "neutral", "steady" and "none"."""

PHONE_PROMPT = """You review one phone call handled by "Agni AI", the voice assistant of
Agni College of Technology. The first line says whether the college was called
(inbound) or called a person who had enquired about admissions (outbound), and who
the caller is if the number was recognised (student, parent, faculty, lead). The
caller may speak English, Tamil, or a mix; speech recognition makes mistakes, so
odd words are usually mishearings, not the caller's mood. Judge the CALLER's side only.

Reply with one JSON object and nothing else, with exactly these keys:
- "sentiment": "positive", "neutral" or "negative" — the caller's overall feeling
  by the end. Plain factual questions with no feeling shown are "neutral".
- "trend": "improved", "steady" or "worsened" — how their mood moved from the
  start of the call to the end.
- "admission_interest": "high" (wants to apply, agreed to a campus visit or a
  callback, or gave contact details for admission), "medium" (asks about fees,
  eligibility or how to apply), "low" (general curiosity) or "none". Current
  students, parents and faculty asking about marks or attendance are "none"
  unless they ask about admission.
- "frustrated": true or false — did the caller show annoyance: repeating
  themselves, complaining, saying the assistant is wrong or not listening,
  asking for a person because the assistant failed them?
- "frustration_reason": a short English phrase, or "" if not frustrated.
- "unanswered": caller questions the assistant FAILED to answer. Include a
  question only if the assistant's reply said it doesn't know, doesn't have that
  information, or passed the caller to a person instead of answering. If the
  assistant gave an actual answer, it does not belong here. Never include
  questions the assistant itself asked. Write each in English, short. Most calls
  have none: use an empty list.
- "topics": a list drawn only from: admissions, fees, courses, placements,
  campus, about, records (marks, attendance, fees due of a current student), other.
- "summary": one English sentence, at most 25 words, on what the caller wanted
  and how it went. Do not include names, phone numbers, register numbers, dates
  of birth or email addresses.

Base every field only on what is in the transcript. If the caller said too
little to judge, use "neutral", "steady" and "none"."""

_PHONE = re.compile(r"(?:\+?91[\s-]?)?[6-9](?:[\s-]?\d){9}")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _mask(text: str) -> str:
    return _EMAIL.sub("[email]", _PHONE.sub("[phone]", text))


def _load_one(path: Path, day: date) -> dict | None:
    """One saved conversation (its transcript.json path), or None if there is nothing to judge."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    messages = [
        m for m in data.get("messages", [])
        if isinstance(m.get("content"), str) and m["content"].strip()
    ]
    user_turns = [m for m in messages if m.get("role") == "user"]
    if not user_turns:
        return None  # greeting only — nothing to judge
    role = ""
    if path.parent.name.startswith("phone-"):
        try:
            meta = json.loads(path.with_name("call_meta.json").read_text(encoding="utf-8"))
        except Exception:
            meta = {}  # calls before 2026-10-20 have no call_meta.json: all were inbound
        channel = "phone_outbound" if meta.get("direction") == "outbound" else "phone_inbound"
        role = meta.get("role") or ""
        if not role:
            try:
                role = json.loads(path.with_name("call_record.json").read_text(encoding="utf-8")).get("caller_role") or ""
            except Exception:
                pass
    else:
        channel = "website_chat" if data.get("source", "chat") == "chat" else "website_voice"
    return {
        "path": path,
        "date": day.isoformat(),
        "session_id": data.get("session_id", path.parent.name),
        "source": data.get("source", "chat"),
        "channel": channel,
        "role": role,
        "language": data.get("language") or _script_language(user_turns),
        "messages": messages,
        "user_turns": len(user_turns),
        # Pipecat adds a "developer" note each time a reply was cut
        # off by the visitor speaking — a free barge-in count.
        "interruptions": sum(1 for m in messages if m.get("role") == "developer"),
    }


def _load_conversations(start: date, end: date, channel: str = "all") -> list[dict]:
    """channel: "all", "website" or "phone"."""
    conversations = []
    for logs_dir in LOG_DIRS:
        if not logs_dir.exists():
            continue
        for date_dir in sorted(logs_dir.iterdir()):
            try:
                day = datetime.strptime(date_dir.name, "%Y-%m-%d").date()
            except ValueError:
                continue
            if not (start <= day <= end):
                continue
            for path in date_dir.glob("*/transcript.json"):
                conv = _load_one(path, day)
                if conv and channel in ("all", conv["channel"].split("_")[0]):
                    conversations.append(conv)
    return conversations


def _script_language(user_turns: list[dict]) -> str:
    text = " ".join(m["content"] for m in user_turns)
    return "ta" if re.search(r"[஀-௿]", text) else "en"


def _transcript_text(messages: list[dict], speaker: str = "Visitor") -> str:
    lines = []
    for m in messages:
        if m.get("role") == "user":
            lines.append(f"{speaker}: {m['content'].strip()}")
        elif m.get("role") == "assistant":
            lines.append(f"Agni AI: {m['content'].strip()}")
    return "\n".join(lines)


def _clean(raw: dict, topics: tuple = TOPICS) -> dict:
    """Force the model's answer into the allowed values — anything unexpected
    becomes the neutral default rather than a made-up category in the report."""
    def pick(key, allowed, default):
        value = str(raw.get(key, "")).strip().lower()
        return value if value in allowed else default

    def as_list(key):
        value = raw.get(key)
        return [str(v).strip() for v in value if str(v).strip()] if isinstance(value, list) else []

    return {
        "sentiment": pick("sentiment", SENTIMENTS, "neutral"),
        "trend": pick("trend", TRENDS, "steady"),
        "admission_interest": pick("admission_interest", INTEREST_LEVELS, "none"),
        "frustrated": raw.get("frustrated") is True,
        "frustration_reason": _mask(str(raw.get("frustration_reason") or "").strip()),
        "unanswered": [_mask(q) for q in as_list("unanswered")][:5],
        "topics": [t.lower() for t in as_list("topics") if t.lower() in topics] or ["other"],
        "summary": _mask(str(raw.get("summary") or "").strip()),
    }


async def _analyse(client: AsyncOpenAI, conv: dict, gate: asyncio.Semaphore) -> dict | None:
    if conv["channel"].startswith("phone"):
        who = {"student": "a student", "parent": "a parent", "faculty": "a faculty member",
               "lead": "a lead (enquired about admissions)"}.get(conv["role"], "not recognised")
        kind = "outbound (the college called this person)" if conv["channel"] == "phone_outbound" else "inbound"
        transcript = f"Call: {kind}. Caller: {who}.\n" + _transcript_text(conv["messages"], "Caller")
        key = hashlib.sha1(f"phone{PHONE_PROMPT_VERSION}\n{transcript}".encode()).hexdigest()
        prompt, topics, prompt_id = PHONE_PROMPT, PHONE_TOPICS, f"phone{PHONE_PROMPT_VERSION}"
    else:
        # Unchanged since prompt version 3, so website judgements cached before phone calls
        # were split out stay valid.
        transcript = _transcript_text(conv["messages"])
        key = hashlib.sha1(f"{PROMPT_VERSION}\n{transcript}".encode()).hexdigest()
        prompt, topics, prompt_id = ANALYSIS_PROMPT, TOPICS, f"website{PROMPT_VERSION}"
    cache_path = conv["path"].with_name("sentiment.json")
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("key") == key:
            if cached.get("prompt") != prompt_id:  # cached before the prompt tag existed
                cached["prompt"] = prompt_id
                cache_path.write_text(json.dumps(cached, ensure_ascii=False, indent=2), encoding="utf-8")
            return cached["result"]
    except Exception:
        pass

    result = None
    async with gate:
        # The model occasionally returns malformed JSON (one retry clears most),
        # and Sarvam rate-limits bursts (wait, then retry).
        for attempt in range(4):
            try:
                response = await client.chat.completions.create(
                    model=CHAT_MODEL,
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": transcript[:12000]},
                    ],
                    temperature=0,
                    extra_body={"reasoning_effort": None},  # see main.py's DISABLE_REASONING
                )
                content = response.choices[0].message.content or ""
                result = _clean(json.loads(re.search(r"\{.*\}", content, re.S).group()), topics)
                break
            except RateLimitError as e:
                if attempt == 3:
                    print(f"  could not analyse {conv['session_id'][:8]}: {e}")
                await asyncio.sleep(5 * (attempt + 1))
            except Exception as e:
                if attempt >= 1:
                    print(f"  could not analyse {conv['session_id'][:8]}: {e}")
                    break
    if result is None:
        return None
    try:
        cache_path.write_text(
            json.dumps({"key": key, "prompt": prompt_id, "result": result}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
    return result


def _bar_rows(counter: Counter, order: tuple | None, total: int) -> str:
    keys = [k for k in (order or [k for k, _ in counter.most_common()]) if counter.get(k)]
    if not keys:
        return '<p class="muted">None.</p>'
    rows = []
    for k in keys:
        n = counter[k]
        pct = 100 * n / total if total else 0
        rows.append(
            f'<div class="bar"><span class="lbl">{html.escape(str(k))}</span>'
            f'<span class="track"><span class="fill {html.escape(str(k))}" style="width:{pct:.0f}%"></span></span>'
            f'<span class="num">{n} ({pct:.0f}%)</span></div>'
        )
    return "\n".join(rows)


def _channel_table(rows: list[dict]) -> str:
    """One row per channel, so website and phone can be compared at a glance."""
    out = []
    for key, label in CHANNELS.items():
        rs = [r for r in rows if r["channel"] == key]
        if not rs:
            continue
        n = len(rs)

        def pct(k):
            return f"{k} <span class=muted>({100 * k / n:.0f}%)</span>"

        out.append(
            f"<tr><td>{label}</td><td>{n}</td>"
            f"<td>{pct(sum(r['sentiment'] == 'positive' for r in rs))}</td>"
            f"<td>{pct(sum(r['sentiment'] == 'negative' for r in rs))}</td>"
            f"<td>{pct(sum(r['frustrated'] for r in rs))}</td>"
            f"<td>{pct(sum(r['admission_interest'] == 'high' for r in rs))}</td>"
            f"<td>{pct(sum(bool(r['unanswered']) for r in rs))}</td></tr>"
        )
    return ("<div class=\"scroll\"><table><tr><th>Channel</th><th>Analysed</th><th>Positive</th><th>Negative</th>"
            "<th>Frustrated</th><th>High admission interest</th><th>Unanswered question</th></tr>"
            + "".join(out) + "</table></div>")


def _who(r: dict) -> str:
    label = CHANNELS.get(r["channel"], r["channel"])
    return f"{label} / {r['language']}" + (f" / {r['role']}" if r.get("role") and r["role"] != "unknown" else "")


def _write_report(rows: list[dict], failed: int, start: date, end: date, channel: str = "all") -> tuple[Path, Path]:
    REPORTS_DIR.mkdir(exist_ok=True)
    stem = f"sentiment_{start.isoformat()}_to_{end.isoformat()}" + ("" if channel == "all" else f"_{channel}")
    csv_path, html_path = REPORTS_DIR / f"{stem}.csv", REPORTS_DIR / f"{stem}.html"

    columns = ["date", "session_id", "channel", "role", "source", "language", "user_turns", "interruptions",
               "sentiment", "trend", "admission_interest", "frustrated",
               "frustration_reason", "topics", "unanswered", "summary"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for r in rows:
            writer.writerow({
                **{c: r[c] for c in columns},
                "topics": "; ".join(r["topics"]),
                "unanswered": "; ".join(r["unanswered"]),
            })

    total = len(rows)
    sentiment = Counter(r["sentiment"] for r in rows)
    interest = Counter(r["admission_interest"] for r in rows)
    trend = Counter(r["trend"] for r in rows)
    topics = Counter(t for r in rows for t in r["topics"])
    by_channel = Counter(f"{CHANNELS.get(r['channel'], r['channel'])} / {r['language']}" for r in rows)
    frustrated = [r for r in rows if r["frustrated"]]
    unanswered = Counter(q for r in rows for q in r["unanswered"])

    scope = {"all": "website chat and voice, phone calls in and out", "website": "website chat and voice",
             "phone": "phone calls, inbound and outbound"}.get(channel, channel)

    def tile(label, value):
        return f'<div class="tile"><div class="v">{value}</div><div class="k">{label}</div></div>'

    order = {"negative": 0, "neutral": 1, "positive": 2}
    table_rows = "\n".join(
        f"<tr><td>{r['date']}</td><td>{html.escape(_who(r))}</td>"
        f"<td><span class=\"pill {r['sentiment']}\">{r['sentiment']}</span></td>"
        f"<td>{r['trend']}</td><td>{r['admission_interest']}</td>"
        f"<td>{'yes — ' + html.escape(r['frustration_reason']) if r['frustrated'] else ''}</td>"
        f"<td>{html.escape(r['summary'])}</td><td class=\"muted\">{html.escape(r['session_id'][:8])}</td></tr>"
        for r in sorted(rows, key=lambda r: (not r["frustrated"], order[r["sentiment"]], r["date"]))
    )
    unanswered_html = (
        "<ul>" + "".join(
            f"<li>{html.escape(q)}{f' <span class=muted>×{n}</span>' if n > 1 else ''}</li>"
            for q, n in unanswered.most_common(40)
        ) + "</ul>"
    ) if unanswered else '<p class="muted">None recorded.</p>'

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agni AI sentiment report</title>
<style>
  :root {{ --bg:#fff; --fg:#1c1c1c; --muted:#6b6b6b; --line:#e4e4e4; --card:#f6f6f4;
           --pos:#2e7d4f; --neu:#8a8a8a; --neg:#c0392b; --acc:#b4531a; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#161616; --fg:#ececec; --muted:#a0a0a0;
           --line:#333; --card:#202020; --pos:#5bbf85; --neu:#9a9a9a; --neg:#e8705f; --acc:#e08a4f; }} }}
  body {{ background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif;
         max-width:1080px; margin:0 auto; padding:24px 16px 48px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }} h2 {{ font-size:16px; margin:32px 0 10px; }}
  .muted {{ color:var(--muted); }} .note {{ background:var(--card); padding:10px 14px; border-radius:8px; }}
  .tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:18px 0; }}
  .tile {{ background:var(--card); border-radius:8px; padding:12px 14px; }}
  .tile .v {{ font-size:24px; font-weight:600; }} .tile .k {{ color:var(--muted); font-size:13px; }}
  .cols {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:8px 32px; }}
  .bar {{ display:grid; grid-template-columns:110px 1fr 84px; gap:8px; align-items:center; margin:5px 0; }}
  .track {{ background:var(--card); border-radius:4px; height:12px; overflow:hidden; display:block; }}
  .fill {{ display:block; height:100%; background:var(--acc); }}
  .fill.positive {{ background:var(--pos); }} .fill.neutral {{ background:var(--neu); }} .fill.negative {{ background:var(--neg); }}
  .num {{ color:var(--muted); font-size:13px; text-align:right; }}
  .scroll {{ overflow-x:auto; }} table {{ border-collapse:collapse; width:100%; min-width:760px; }}
  th, td {{ text-align:left; padding:7px 8px; border-bottom:1px solid var(--line); vertical-align:top; font-size:14px; }}
  th {{ color:var(--muted); font-weight:500; }}
  .pill {{ padding:1px 8px; border-radius:10px; color:#fff; font-size:12px; }}
  .pill.positive {{ background:var(--pos); }} .pill.neutral {{ background:var(--neu); }} .pill.negative {{ background:var(--neg); }}
</style></head><body>
<h1>Agni AI — sentiment report</h1>
<div class="muted">{start.isoformat()} to {end.isoformat()} · {scope} ·
generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</div>

<div class="tiles">
  {tile("conversations analysed", total)}
  {tile("positive", sentiment["positive"])}
  {tile("negative", sentiment["negative"])}
  {tile("showed frustration", len(frustrated))}
  {tile("high admission interest", interest["high"])}
  {tile("with an unanswered question", sum(1 for r in rows if r["unanswered"]))}
</div>
<p class="note">These labels are Sarvam 105B's reading of each transcript and have not been checked
against hand-labelled conversations, in English or Tamil. Use them to decide which conversations
to read, not as a final score.{f" {failed} conversation(s) could not be analysed and are left out." if failed else ""}</p>

<h2>By channel</h2>
{_channel_table(rows)}

<div class="cols">
  <div><h2>Overall sentiment</h2>{_bar_rows(sentiment, SENTIMENTS, total)}</div>
  <div><h2>Mood from start to end</h2>{_bar_rows(trend, TRENDS, total)}</div>
  <div><h2>Admission interest</h2>{_bar_rows(interest, INTEREST_LEVELS, total)}</div>
  <div><h2>Topics raised</h2>{_bar_rows(topics, None, total)}</div>
  <div><h2>Channel and language</h2>{_bar_rows(by_channel, None, total)}</div>
</div>

<h2>Questions the assistant could not answer <span class="muted">(all channels)</span></h2>
{unanswered_html}

<h2>Conversations <span class="muted">(frustrated and negative first)</span></h2>
<div class="scroll"><table>
<tr><th>Date</th><th>Channel</th><th>Sentiment</th><th>Mood</th><th>Admission interest</th>
<th>Frustration</th><th>Summary</th><th>Session</th></tr>
{table_rows}
</table></div>
</body></html>"""
    html_path.write_text(page, encoding="utf-8")
    return html_path, csv_path


def _prune_old_reports() -> None:
    if not REPORTS_DIR.exists():
        return
    cutoff = datetime.now().timestamp() - RETENTION_DAYS * 86400
    for path in REPORTS_DIR.glob("sentiment_*"):
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Sentiment report for the website bot.")
    parser.add_argument("--days", type=int, default=7, help="how many days back, ending today (default 7)")
    parser.add_argument("--from", dest="start", help="start date, YYYY-MM-DD")
    parser.add_argument("--to", dest="end", help="end date, YYYY-MM-DD (default today)")
    parser.add_argument("--channel", choices=("all", "website", "phone"), default="all",
                        help="website, phone, or all (default)")
    args = parser.parse_args()

    # Log folders are named by UTC date (see conversation_logger.py).
    end = date.fromisoformat(args.end) if args.end else datetime.now(timezone.utc).date()
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=args.days - 1)

    await build_report(start, end, args.channel)


async def build_report(start: date, end: date, channel: str = "all") -> Path | None:
    """Analyse every conversation in the range and write the report. Returns
    the HTML path, or None if there was nothing to write."""
    conversations = _load_conversations(start, end, channel)
    if not conversations:
        print(f"No conversations with a visitor turn between {start} and {end}.")
        return None
    print(f"Analysing {len(conversations)} conversation(s) from {start} to {end}...")

    client = AsyncOpenAI(api_key=os.environ["SARVAM_API_KEY"], base_url="https://api.sarvam.ai/v1")
    gate = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*(_analyse(client, c, gate) for c in conversations))

    rows = [
        {**{k: c[k] for k in ("date", "session_id", "channel", "role", "source", "language", "user_turns",
                              "interruptions")}, **r}
        for c, r in zip(conversations, results) if r
    ]
    failed = len(conversations) - len(rows)
    if not rows:
        print("Every analysis call failed — no report written.")
        return None
    _prune_old_reports()
    html_path, csv_path = _write_report(rows, failed, start, end, channel)
    print(f"Report: {html_path}\nCSV:    {csv_path}")
    if failed:
        print(f"{failed} conversation(s) could not be analysed and were left out.")
    return html_path


async def analyse_folder(folder: Path) -> dict | None:
    """Judge one saved conversation now (the phone agent calls this right after each call)
    and cache it beside the transcript. None if there is nothing to judge or it failed."""
    path = Path(folder) / "transcript.json"
    try:
        day = datetime.strptime(path.parent.parent.name, "%Y-%m-%d").date()
    except ValueError:
        day = datetime.now(timezone.utc).date()
    conv = _load_one(path, day)
    if not conv:
        return None
    client = AsyncOpenAI(api_key=os.environ["SARVAM_API_KEY"], base_url="https://api.sarvam.ai/v1")
    return await _analyse(client, conv, asyncio.Semaphore(1))


def cached_result(folder: Path) -> dict | None:
    """The judgement cached beside a phone call by the current phone prompt, if any (a phone
    call judged before phone calls had their own prompt does not count)."""
    try:
        cached = json.loads((Path(folder) / "sentiment.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    return cached["result"] if cached.get("prompt") == f"phone{PHONE_PROMPT_VERSION}" else None


async def run_periodically(interval_hours: float, days: int = 7) -> None:
    """Keep the report current without anyone running this script: rebuild the
    last `days` days whenever the newest report is older than `interval_hours`.
    Started by main.py at backend startup. Going by the newest report's age
    (not time since startup) means a backend that restarts often neither
    re-runs on every restart nor skips its run forever."""
    interval = interval_hours * 3600
    while True:
        reports = list(REPORTS_DIR.glob("sentiment_*.html")) if REPORTS_DIR.exists() else []
        age = datetime.now().timestamp() - max((p.stat().st_mtime for p in reports), default=0)
        if age < interval:
            await asyncio.sleep(interval - age)
            continue
        end = datetime.now(timezone.utc).date()
        try:
            await build_report(end - timedelta(days=days - 1), end)
        except Exception as e:
            print(f"Automatic sentiment report failed: {e}")
        # Also the retry delay when nothing was written (no conversations, or
        # Sarvam unreachable) — the report's age doesn't advance in that case.
        await asyncio.sleep(min(interval, 3600))


if __name__ == "__main__":
    asyncio.run(main())
