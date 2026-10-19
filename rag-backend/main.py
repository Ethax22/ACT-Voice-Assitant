"""Agni AI's shared RAG backend.

Every input path — typed chat, English voice, and Tamil voice — calls this
single chat-completion logic, so all three stay on one brain (same retrieval
+ same persona) by construction. The LLM itself is Sarvam 105B (migrated
from Azure OpenAI 2026-09-25) — embeddings stay on Azure OpenAI, in
retriever.py, unaffected by this.

Two routes expose it:
- POST /v1/chat/completions — OpenAI-compatible shape, bearer-auth required.
  Called by the voice-pipeline service (Tamil now; English once Phase 3
  generalizes that pipeline), which holds RAG_BACKEND_AUTH_KEY server-side.
- POST /widget/chat — simplified shape, no auth required. Called directly by
  the browser widget, which must never hold RAG_BACKEND_AUTH_KEY or any
  vendor key.

The Deepgram-based English voice relay (WS /voice/en, voice_agent.py) was
retired 2026-09-25 (CLAUDE.md decision #1) — English voice moves to the same
Sarvam/Pipecat relay Tamil already uses, generalized in Phase 3.
"""
import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import OrderedDict

from dotenv import load_dotenv

load_dotenv()  # must run before importing retriever, which reads env vars at module load

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import httpx
from openai import AsyncOpenAI

from language_style import detect_style
from retriever import retriever
from leads import normalize_indian_mobile, save_lead
from conversation_logger import log_turn, prune_old_logs
from conversation_state import derive_state, lead_already_captured, mark_lead_captured
from sentiment_report import run_periodically as run_sentiment_reports

app = FastAPI(title="Agni AI RAG Backend")
logger = logging.getLogger("rag_backend")
logging.basicConfig(level=logging.INFO)


@app.on_event("startup")
async def _prune_logs_on_startup():
    """Phase 10: 90-day retention, auto-pruned — a startup check rather than
    a running scheduler, since this only needs to happen about once a day
    and the backend restarts at least that often in practice."""
    prune_old_logs()


# Hours between automatic sentiment reports (rag-backend/reports/); 0 turns
# them off, leaving `python sentiment_report.py` by hand as the only way.
SENTIMENT_REPORT_INTERVAL_HOURS = float(os.environ.get("SENTIMENT_REPORT_INTERVAL_HOURS", "24"))
_sentiment_report_task = None


@app.on_event("startup")
async def _start_sentiment_reports():
    """Runs in the background, after the fact, from saved transcripts — it is
    never in the path of a visitor's reply."""
    global _sentiment_report_task
    if SENTIMENT_REPORT_INTERVAL_HOURS > 0:
        _sentiment_report_task = asyncio.create_task(
            run_sentiment_reports(SENTIMENT_REPORT_INTERVAL_HOURS)
        )


@app.on_event("startup")
async def _warm_retriever():
    """Build the embedding index now, not inside the first visitor's request
    (which would otherwise wait for the whole knowledge base to be embedded)."""
    try:
        await retriever.warm_up()
    except Exception:
        logger.exception("Retriever warm-up failed; it will retry on the first request")

WIDGET_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("WIDGET_ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=WIDGET_ALLOWED_ORIGINS,
    allow_methods=["POST"],
    allow_headers=["Content-Type"],
)

# Sarvam's chat-completions API is OpenAI-SDK-compatible: it accepts
# `Authorization: Bearer <key>` and returns standard chat.completion(.chunk)
# shapes (confirmed against current Sarvam API docs, 2026-09-25) — no Azure-
# style extra fields, so streaming below forwards chunks directly instead of
# reconstructing them.
chat_client = AsyncOpenAI(
    api_key=os.environ["SARVAM_API_KEY"],
    base_url="https://api.sarvam.ai/v1",
    # Keep the connection open between turns (httpx's default drops it after
    # 5 s idle) — same reasoning as retriever.py's embeddings client.
    http_client=httpx.AsyncClient(
        limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=300),
        timeout=httpx.Timeout(60.0, connect=10.0),
    ),
)
CHAT_MODEL = "sarvam-105b"
# sarvam-105b has chain-of-thought reasoning ON by default (reasoning_effort
# "medium"), which burns the completion's token budget on reasoning_content
# before any real answer content — confirmed live: a real request came back
# with finish_reason "length" and content null, reasoning having consumed
# the whole 2048-token default. Sarvam's API disables it on a literal JSON
# `"reasoning_effort": null`, but passing reasoning_effort=None as a normal
# kwarg gets silently dropped by the OpenAI SDK (None there means "field not
# provided", not "send null") — confirmed by comparing a raw curl payload
# against the SDK call. extra_body bypasses that and sends the literal null.
DISABLE_REASONING = {"reasoning_effort": None}
BACKEND_AUTH_KEY = os.environ.get("RAG_BACKEND_AUTH_KEY")

SYSTEM_PROMPT_TEMPLATE = """You are Agni AI, the friendly voice/chat assistant for
Agni College of Technology. Talk like a helpful senior student showing someone
around campus, not like a brochure: warm, direct, and brief.

Rules:
- Keep answers short and conversational — 2-4 sentences unless the visitor
  clearly wants detail. This matters even more for voice, where long answers
  feel like a monologue rather than a chat.
- Answer straight away, starting with the answer itself. Never open with
  praise or a stalling phrase — not "Good question", "That's a good one",
  "Sure", "Certainly", "Let me check", "Let me think", "I'm looking that up",
  or their Tamil equivalents ("நல்ல கேள்வி", "ஒரு நிமிஷம்"). Visitors hear
  the same opener on every answer and it sounds like a recording. A brief
  "Got it" or "Okay" is fine only when the visitor has just told you
  something about themselves, and never in two replies running.
- Only answer from the context below. If it's not there, say so plainly and
  suggest contacting admissions — don't guess or make up figures.
- It's fine to ask a short clarifying question back if the visitor's question
  is ambiguous (e.g., which department, which year) — that's what makes it
  feel like a conversation instead of a lookup.
- Never use markdown or formatting symbols — no asterisks, bullet points,
  headers, or bold text. This response may be read aloud by text-to-speech,
  and a stray "**" gets spoken as literal "asterisk asterisk," which sounds
  broken. Write everything as plain natural sentences.
- Write phone numbers, codes, and other digit strings the way you'd say them
  out loud, not as a hyphenated block — e.g. "oh four four, six seven four,
  zero nine four four, four four" rather than "044-6740944". Hyphens get read
  digit-by-digit in an unnatural, robotic way.
- If a visitor shows clear admission interest (asks about applying, fees,
  eligibility, "how do I join", wants a callback, etc.), warmly ask for their
  name and phone number so admissions can follow up — and their preferred
  course and email if the conversation naturally allows it. Ask
  conversationally, one or two details at a time, not as a form. Once you
  have at least a name and phone number, call the capture_lead tool with
  whatever fields you've collected — don't wait to have all four. Never call
  it on a guess; only once the visitor has actually given you that
  information themselves.
- Some questions are sensitive or easy to answer badly — comparisons between
  departments or against other colleges, fee questions, rankings, negative
  reviews, or doubts about placement numbers. If the context below includes a
  section starting with "Talking point:", that phrasing was written and
  approved by admissions/faculty specifically for this kind of question —
  use it as the core of your answer, close to verbatim, rather than
  improvising your own comparison or opinion. You can still warm it up with a
  natural spoken acknowledgment and trim it for length, but don't soften,
  contradict, or add claims beyond what that talking point says. Some talking
  points aren't finished yet and contain an editor's note in square brackets
  (e.g. "[ADMISSIONS: ...]") — that bracketed text is a note to the college
  staff who will finish writing it, never something to say to the visitor.
  If a talking point is nothing but an unfinished bracketed note with no
  usable answer around it, don't repeat the note at all — just say you don't
  have a confirmed answer for that yet and suggest contacting admissions.

Context:
{context}
"""

CAPTURE_LEAD_TOOL = {
    "type": "function",
    "function": {
        "name": "capture_lead",
        "description": (
            "Record a visitor's admission-interest lead for the admissions "
            "team to follow up. Call only once the visitor has given you at "
            "least their name and phone number in the conversation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Visitor's full name"},
                "phone": {"type": "string", "description": "Visitor's phone number, as given"},
                "email": {"type": "string", "description": "Visitor's email address, if given"},
                "preferred_course": {
                    "type": "string",
                    "description": "Course/department the visitor is interested in, if mentioned",
                },
            },
            "required": ["name", "phone"],
        },
    },
}


# Tools this backend runs itself. Any other tool the model calls belongs to the
# caller (the phone agent declares its ERP tools in the request) and is handed
# back to it instead of being resolved here.
OWNED_TOOLS = {"capture_lead"}


def _foreign_tool_calls(calls: list[dict]) -> list[dict]:
    return [c for c in calls if (c.get("function") or {}).get("name") not in OWNED_TOOLS]


def _tool_call_sse(calls: list[dict]):
    """The caller's tool calls as OpenAI chat-completion chunks: one chunk with
    every call whole, then the finish chunk. Pipecat reads these as a function call."""
    base = {"id": f"chatcmpl-{uuid.uuid4().hex[:24]}", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": CHAT_MODEL}
    delta = {"tool_calls": [{"index": i, "id": c["id"], "type": "function", "function": c["function"]}
                            for i, c in enumerate(calls)]}
    yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]})}\n\n"
    yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]})}\n\n"


_HEADER_TEXT = re.compile(r"[^\w .'-]")


def _caller_from_headers(request: Request) -> dict:
    """Who the phone agent says is calling (X-Caller-Role / X-Caller-Name), cleaned
    because it ends up inside the prompt."""
    return {k: _HEADER_TEXT.sub("", request.headers.get(f"x-caller-{k}", ""))[:40].strip()
            for k in ("role", "name")}


TAMIL_UNICODE_BLOCK = range(0x0B80, 0x0C00)


def _detect_language(messages: list[dict]) -> str:
    """Best-effort language tag for a captured lead — same script-detection
    idea as the widget's client-side `detectScriptLanguage`, applied
    server-side since rag-backend never receives an explicit language field
    (voice-pipeline's system content is ignored, per pipeline.py)."""
    text = " ".join(m.get("content", "") or "" for m in messages if m.get("role") == "user")
    return "ta" if any(ord(ch) in TAMIL_UNICODE_BLOCK for ch in text) else "en"


# A run of 8+ digits, allowing spaces or hyphens between them ("98765 43210",
# "9 8 7 6 5 4 3 2 1 0"). Speech recognition often drops or doubles a digit, so
# this deliberately also matches runs that are NOT a valid mobile number.
_PHONE_RUN = re.compile(r"(?<![\d])(?:\d[\s\-]?){8,}\d(?![\d])")
FORCE_CAPTURE_LEAD = {"type": "function", "function": {"name": "capture_lead"}}


def _should_force_lead_capture(messages: list[dict], session_id: str | None) -> bool:
    """True when the visitor's latest message contains a phone number and this
    conversation has no lead saved for it yet.

    Why code decides instead of the model: Sarvam 105B only sometimes calls
    capture_lead. Measured 2026-10-09 with a name and a valid number in the
    message: it called the tool in 1 of 4 tries in English and 0 of 4 in Tamil,
    and in the other tries said "I've noted your details" with nothing saved
    (a real widget call told a visitor their number was noted; no row reached
    the Sheet). A phone number in the visitor's own words is clear intent, so
    the tool call is required for that turn; the model still extracts the name
    and course from the conversation."""
    latest = next(
        (m.get("content") or "" for m in reversed(messages) if m.get("role") == "user"), ""
    )
    match = _PHONE_RUN.search(latest)
    if not match:
        return False
    phone = normalize_indian_mobile(match.group(0))
    return not (phone and lead_already_captured(session_id, phone))


async def _resolve_tool_calls(
    messages: list[dict], assistant_message: dict, source: str, session_id: str | None = None
) -> list[dict]:
    """Execute every capture_lead call in an assistant tool-call turn and
    return messages extended with the assistant's tool-call turn plus one
    tool-result message per call, ready for a follow-up completion so the
    model can produce its actual spoken/typed confirmation."""
    language = _detect_language(messages)
    extended = [*messages, assistant_message]
    invalid_phone = None
    for call in assistant_message.get("tool_calls") or []:
        function = call.get("function") or {}
        result = "Unrecognized tool."
        if function.get("name") == "capture_lead":
            try:
                args = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            phone = normalize_indian_mobile(args.get("phone", ""))
            if phone is None:
                logger.warning("capture_lead rejected invalid phone %r", args.get("phone"))
                result = (
                    "Lead NOT saved: that phone number isn't a valid 10-digit "
                    "Indian mobile number (it may have been misheard). Read the "
                    "digits back to the visitor and ask them to repeat the full "
                    "number, then call capture_lead again."
                )
                invalid_phone = args.get("phone", "")
            elif lead_already_captured(session_id, phone):
                result = (
                    "This lead is already recorded for this conversation — nothing "
                    "more to save. Don't ask for their details again."
                )
            else:
                mark_lead_captured(session_id, phone)
                saved = save_lead(
                    name=args.get("name", ""),
                    phone=phone,
                    email=args.get("email"),
                    preferred_course=args.get("preferred_course"),
                    language=language,
                    source=source,
                )
                result = (
                    "Lead recorded for admissions follow-up. Thank the visitor warmly."
                    if saved
                    else "Lead noted (Sheets write failed, but don't mention that to the "
                    "visitor — just thank them warmly as usual)."
                )
        extended.append(
            {"role": "tool", "tool_call_id": call.get("id"), "content": result}
        )
    if invalid_phone is not None:
        # The tool result alone was not enough: in Tamil the model still said
        # it had "noted" a 9-digit number. A system message last is what it
        # follows (same reason the language rules are repeated last).
        extended.append({
            "role": "system",
            "content": (
                f"The phone number the visitor gave ({invalid_phone!r}) is NOT a valid 10-digit "
                "mobile number and nothing was saved. Do not say it was noted, saved or "
                "received. In one short friendly sentence, say you may have misheard and ask "
                "them to say all ten digits again, slowly. Keep the language and style "
                "you were already using."
            ),
        })
    return extended


def _check_auth(request: Request):
    if not BACKEND_AUTH_KEY:
        return
    auth_header = request.headers.get("authorization", "")
    if auth_header != f"Bearer {BACKEND_AUTH_KEY}":
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


# Roles Sarvam's chat-completions API actually accepts (confirmed live via a
# real 400: "Invalid role 'developer'. Must be one of: assistant, system,
# tool, user") — narrower than OpenAI's own role set. Pipecat's
# LLMUserAggregator injects a synthetic `role: "developer"` recovery message
# when a user turn is interrupted or comes back empty (e.g. background
# noise, a turn cut short by barge-in) — harmless on OpenAI itself, but it
# 400s every request built from it on Sarvam, which is what made the voice
# pipeline go silent mid-conversation instead of just that one turn: the
# request never even reaches the model, so no reply (and no error surfaced
# to the visitor) comes back. "developer" is semantically a system-level
# instruction anyway, so remapping it is a correct translation, not a hack.
_SARVAM_ALLOWED_ROLES = {"assistant", "system", "tool", "user"}


def _normalize_roles_for_sarvam(messages: list[dict]) -> list[dict]:
    return [
        {**m, "role": "system"} if m.get("role") not in _SARVAM_ALLOWED_ROLES else m
        for m in messages
    ]


def _retrieval_query(user_turns: list[str]) -> str:
    """Pick what to search the knowledge base with. A very short turn ("yes",
    "okay", "சரி", "interested in undergraduate") carries almost no topic, so
    searching on it retrieves noise — and costs a fresh embedding call. Fall
    back to the most recent substantive user turn, whose embedding is already
    cached from the turn that asked it."""
    latest = user_turns[-1]
    if len(latest.split()) > 3:
        return latest
    for earlier in reversed(user_turns[:-1]):
        if len(earlier.split()) > 3:
            return earlier
    return latest


# Replies that will be heard, not read. The phone agent (source "phone",
# Sarvam Indus calling /calling-agent/ask) was left on the typed-chat style
# until 2026-10-07 and so got 2-4 sentence answers to read out.
_SPOKEN_SOURCES = ("voice", "phone")

VOICE_BREVITY_ADDENDUM = """

SPOKEN-CALL STYLE (overrides the 2-4 sentence guideline above): this reply
will be heard aloud, on a live call.
- Most replies should be 35 to 55 words: two or three short sentences. 70 words
  is the hard ceiling, for when the visitor asked for detail. A bare
  acknowledgement ("okay", "hmm") still gets one short sentence.
- Open with the direct answer in a short sentence, then add the one or two
  facts from the context that best help them decide or act (a number, a name,
  a next step). A one-line answer that leaves out the useful detail is too thin.
- Ask at most one question per reply, and only if you haven't already asked
  for something.
- Use contractions and everyday words ("we've got", "you'll need"), never
  brochure language ("we provide a comprehensive range of").
- If the context holds a list (courses, facilities, recruiters, numbers),
  name up to 4 items and offer to go on.
- Don't greet again, don't restate the question, don't recite the context,
  and don't repeat anything you've already told them."""

# Repeated as the LAST message on voice calls. The style rules above sit in
# front of several thousand characters of retrieved context and were being
# ignored: measured 2026-10-07 on ten questions, median reply 42 words and
# 4 of 10 over the 45-word ceiling. Restating the limit right before the
# model answers is what brings it down.
VOICE_FINAL_REMINDER = (
    "You are speaking on a live voice call. Answer the visitor's last message "
    "in two or three short sentences, about 40 to 50 words in total. Start with "
    "the answer, not with a filler or praise, and make that first sentence short "
    "(8 to 15 words) so it can be spoken at once. Then give the one or two facts "
    "from the context that help most, such as a number, a name or the next step; "
    "do not stop at a bare one-line answer. Do not list more than four items."
)

# The reply style is decided in code and stated last, where the model weighs
# it most. Left to the model it drifted: replies mixed English and Tamil, or
# answered in the language of the retrieved context instead of the visitor's
# (reported by Prakash 2026-10-07; also flagged by the sentiment report on a
# real test call).
#
# Three styles (2026-10-09): "en", "ta" (visitor wrote Tamil script) and
# "ta_en" (Tamil and English mixed, usually typed or transcribed in English
# letters: "fees evlo bro?"). The old "ta" rule forced every word into Tamil
# script and banned English words, which is not how anyone speaks and is what
# made replies sound like a newsreader.
LANGUAGE_RULES = {
    "en": (
        "Reply in English only. Do not use Tamil words or Tamil script anywhere in "
        "the reply, even if the visitor mixed in a Tamil word or the context contains Tamil."
    ),
    "ta": (
        "Reply in spoken Chennai Tamil, written mostly in Tamil script, the way a "
        "friendly person talks - not textbook, not newsreader, not formal written Tamil. "
        "Everyday English words stay in English (fees, course, application, admission, "
        "documents, payment, hostel, placement, website, registration, deadline, branch, "
        "details, process) - do not translate them into formal Tamil. "
        "Use English names, degree names and acronyms as they are (B.Tech, CSE, NAAC, TNEA, LPA). "
        "The context is mostly in English: say what you take from it in natural spoken Tamil "
        "instead of copying English sentences. Don't switch into a full English sentence partway through."
    ),
    "ta_en": (
        "Reply in natural spoken Chennai Tamil mixed with English (Tanglish), written in "
        "English letters, the way the visitor just wrote. Not literary, textbook, formal or "
        "newsreader Tamil. English words stay English (fees, course, application, admission, "
        "documents, payment, hostel, placement, website, registration, deadline, branch, "
        "details, process), with Tamil endings attached the way people say them "
        "(\"application-la\", \"branch-ku\", \"upload pannunga\"). "
        "Do not write Tamil script in this reply."
    ),
}

# Added to the system prompt for Tamil and Tamil-English replies. First pass,
# written without a native speaker's review - it needs one before launch
# (Voice Naturalness Plan, steps I/J).
TAMIL_STYLE = """

Tamil style - you are replying in spoken Chennai Tamil:
- Talk like a helpful person at a college front office: everyday spoken Tamil,
  short and friendly, "இருக்கு", "சொல்லுங்க", "பார்க்கலாம்", and the respectful
  "நீங்க / உங்க". Spoken endings (படிக்கிறாங்க, வேணும், புரிஞ்சுது), never
  written ones (உள்ளது, ஆகும், வழங்கப்படுகிறது).
- Mix English the way Chennai does today. Everyday English words stay English;
  do not swap them for formal Tamil (கட்டணம், விடுதி, சேர்க்கை, வேலைவாய்ப்பு,
  விண்ணப்பம்). English verbs get Tamil endings: "apply பண்ணுங்க",
  "contact பண்ணலாம்".
- Never crude or over-familiar words: no "ஆம்பளை", "பொம்பளை", "பசங்க",
  "பொண்ணுங்க". Say "மாணவர்கள்" and "மாணவிகள்" in Tamil script. Never "bro",
  "anna", "da" or "machi" either, even when the visitor says them.
- The campus is at Thalambur (தாழம்பூர்), never Tambaram (தாம்பரம்), a different place.
- Say numbers and amounts the way they are spoken in Tamil."""

TANGLISH_STYLE = """

Tanglish style - the visitor is mixing Tamil and English in English letters:
- Reply in the same mix, in English letters. Short, spoken, friendly - the way a
  helpful senior student answers a junior, not the way a form is worded.
- Never say "bro", "anna", "da" or "machi", even when the visitor does
  (Prakash, 2026-10-13). Stay polite: "neenga", "unga".
- Keep English words English and attach Tamil endings to them: "Application
  online-la pannalaam", "documents upload pannunga", "branch-ku branch maarum".
- Ask one short follow-up question when it moves things along.
- The campus is at Thalambur, never Tambaram, which is a different place.
- No crude words and no Tamil script in this reply.

Examples of the tone (the facts must still come from the context):
Visitor: "Bro B.Tech fees evlo?"
You: "B.Tech fees branch-ku branch maarum. Neenga entha branch paakureenga?"
Visitor: "Anna application epdi panrathu?"
You: "Application online-la pannalaam. Website-la details fill pannitu documents upload pannunga."
Visitor: "Hostel iruka?"
You: "Yes, hostel facility irukku. Boys-aa girls-aa?"
"""

# Which extra style block each reply style gets.
STYLE_BLOCKS = {"ta": TAMIL_STYLE, "ta_en": TANGLISH_STYLE}

_REPLY_LANGUAGE_MARKER = re.compile(r"reply_language:\s*(ta_en|en|ta)\b")


def _reply_language(messages: list[dict]) -> str:
    """Which single reply style this reply must be in: "en", "ta" or "ta_en".

    Voice: voice-pipeline puts a `reply_language: xx` marker in its persona
    message - the style it classified for this turn from the transcript, which
    it can also adjust for what it knows about the audio (Tamil speech is
    sometimes transcribed in Latin letters). Typed chat and phone: the style of
    the visitor's most recent turn that shows one (language_style.py); a bare
    "ok" or a number inherits the style of the turns before it."""
    for m in messages:
        if m.get("role") == "system":
            marker = _REPLY_LANGUAGE_MARKER.search(m.get("content") or "")
            if marker:
                return marker.group(1)
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        style = detect_style(m.get("content") or "")
        if style:
            return style.lower()
    return "en"


# Both Sarvam models gave in on a fact when pushed. On a real call the visitor
# objected to how the bot SAID a year ("you say zero zero — say two thousand")
# and, replaying it 2026-10-07, both replied that CSE started in 2000 where the
# knowledge base says 2001.
FACTS_RULE = (
    " Facts come only from the context: years, numbers, names, amounts and codes do not "
    "change because the visitor objects, corrects you or sounds sure. If they correct how "
    "you SAID something, apologise for the wording and restate the same fact unchanged. "
    "If they dispute a fact, say what your information shows and suggest confirming with "
    "admissions. Never agree to a different figure just to be agreeable."
)

# Bare acknowledgements. On their own these carry no new request — they either
# answer the assistant's last question or just mean "go on".
_BACKCHANNELS = frozenset({
    "yeah", "yes", "yep", "yup", "ya", "ok", "okay", "k", "right", "alright",
    "hmm", "hm", "mm", "mhm", "uh huh", "sure", "fine", "i see", "got it",
    "oh", "oh okay", "ok ok", "okay okay", "cool", "nice", "correct",
    "சரி", "ஆமா", "ஆமாம்", "ம்ம்", "ஓகே", "ஓ", "அப்படியா", "சரி சரி", "sari", "aama", "seri",
})

BACKCHANNEL_ADDENDUM = """

The visitor's last turn was only a short acknowledgement. If your previous
reply ended with a question, treat it as their answer to that question.
Otherwise it just means "go on": reply with one short sentence — the next
useful detail or a brief offer of what you can tell them next. Do not start
a new long answer and do not repeat what you just said."""


# Greetings and sign-offs: nothing in the knowledge base helps answer these.
_SMALL_TALK = frozenset({
    "hi", "hello", "hey", "hi there", "hello there", "good morning", "good afternoon",
    "good evening", "thanks", "thank you", "thank you so much", "thanks a lot", "bye",
    "goodbye", "ok thanks", "okay thanks", "ok thank you", "okay thank you", "ok bye",
    "வணக்கம்", "நன்றி", "ரொம்ப நன்றி", "vanakkam", "nandri",
})
NO_LOOKUP_CONTEXT = (
    "(No college information was looked up for this turn: the visitor has only "
    "greeted, thanked or acknowledged you. Reply naturally in one short sentence "
    "and, if it fits, offer to help with admissions, courses, placements or campus life.)"
)


def _normalize_short(text: str) -> str:
    # Strip listed punctuation rather than [^\w\s]: \w doesn't cover Tamil
    # vowel signs, so that would mangle "சரி".
    return re.sub(r"[.,!?;:\"'…]", "", text or "").strip().lower()


def _is_small_talk(text: str) -> bool:
    normalized = _normalize_short(text)
    return normalized in _SMALL_TALK or normalized in _BACKCHANNELS


def _is_backchannel(text: str) -> bool:
    return _turn_kind(text) == "ack"


# Word-level vocabularies for turns that carry no question. The exact-phrase
# sets above missed ordinary variations — in a real Tamil call (2026-10-07)
# "சரிங்க ஓகேங்க", "நீங்க வச்சிருங்க" and "சரி ஓகேங்க வச்சிருங்க" each got the
# previous answer recited again, five times running.
_ACK_WORDS = frozenset({
    "yeah", "yes", "yep", "yup", "ya", "ok", "okay", "k", "right", "alright", "hmm", "hm",
    "mm", "mhm", "sure", "fine", "oh", "cool", "nice", "correct", "great", "good", "i", "see",
    "got", "it", "that", "is", "that's", "interesting", "understood", "actually", "pretty", "really",
    "சரி", "சரிங்க", "ஓகே", "ஓகேங்க", "ஆமா", "ஆமாம்", "ஆமாங்க", "ம்ம்", "ஓ", "ஆ", "அப்படியா",
    "நல்லது", "பரவாயில்ல", "பரவாயில்லை", "sari", "seri", "aama",
})
_CLOSING_WORDS = frozenset({
    "bye", "goodbye", "thanks", "thank", "enough", "நன்றி", "தேங்க்ஸ்", "போதும்",
    "புரிஞ்சுச்சு", "புரிஞ்சது", "புரிந்தது", "nandri",
})
# Tamil for "you can hang up now" in its many spellings: வச்சிருங்க,
# வச்சுருங்க, வச்சிடுங்க, வைக்கிறேன், வெச்சிடறேன் ...
_CLOSING_PREFIXES = ("வச்ச", "வெச்ச", "வைக்க", "வைச்ச")
_FILLER_WORDS = frozenset({
    "you", "so", "much", "a", "lot", "very", "all", "for", "now", "then", "the", "info", "information",
    "நீங்க", "நீங்கள்", "அது", "எனக்கு", "எனக்குப்", "ரொம்ப", "அவ்வளவுதான்", "அவ்ளோதான்",
})


def _turn_kind(text: str) -> str | None:
    """"closing" (the visitor is wrapping up), "ack" (a bare acknowledgement),
    or None for anything that says something."""
    words = _normalize_short(text).split()
    if not words or len(words) > 8:
        return None
    closing = False
    for word in words:
        if word in _CLOSING_WORDS or word.startswith(_CLOSING_PREFIXES):
            closing = True
        elif word not in _ACK_WORDS and word not in _FILLER_WORDS:
            return None
    if closing:
        return "closing"
    return "ack" if any(w in _ACK_WORDS for w in words) else None


# These go in the LAST message, in place of the usual voice reminder. Placed
# mid-prompt they were ignored: a replay of the real call still answered
# "நீங்க வச்சிருங்க" (you can hang up) with the campus address.
CLOSING_FINAL = (
    "The visitor is ending the conversation. Reply with ONLY a short, warm goodbye of under "
    "ten words: thank them and say they are welcome back any time. No college facts, no "
    "contact details, no question."
)
ACK_FINAL = (
    " The visitor's last message was only an acknowledgement. If your previous reply asked a "
    "question, treat it as a yes to that question and answer that. Otherwise reply with one "
    "short line offering further help, with no new facts."
)

_SENTENCE_END = re.compile(r"(?<=[.?!])\s+")


def _already_said_rule(messages: list[dict]) -> str:
    """Tells the model, last thing before it answers, what it must not say
    again. The same real call showed both habits this targets: every reply
    ended with the same question ("நீங்க சேர்க்கை பத்தி தெரிஞ்சுக்க
    விரும்புறீங்களா?") even after the visitor said they didn't want that, and
    whole replies were recited again when the visitor added nothing new."""
    replies = [
        m["content"].strip() for m in messages
        if m.get("role") == "assistant" and isinstance(m.get("content"), str) and m["content"].strip()
    ]
    if not replies:
        return ""
    questions: list[str] = []
    for reply in replies:
        for sentence in _SENTENCE_END.split(reply):
            sentence = sentence.strip()
            if sentence.endswith("?") and sentence not in questions:
                questions.append(sentence)
    rule = (
        " Do not repeat yourself. Your previous reply was: \u00ab" + replies[-1][:400] + "\u00bb. "
        "Say none of that again, even reworded, unless the visitor explicitly asks you to repeat it "
        "or to say it in another language. If their message adds nothing new, give a short "
        "acknowledgement instead of more facts."
    )
    if questions:
        rule += (
            " You have already asked: " + " ".join("\u00ab" + q[:160] + "\u00bb" for q in questions[-4:])
            + ". Never ask any of these again, in any wording. If the visitor declined or ignored a "
            "question, drop that subject completely."
        )
    rule += (
        " Most replies should end with the answer, not a question: ask one only when you "
        "cannot help without the visitor's answer. If the visitor's message is not a question "
        "or request — they are declining something, commenting, or correcting how you spoke — "
        "respond to exactly that in one short sentence (apologise briefly if they corrected you) "
        "and do not add college facts."
    )
    return rule


_SEARCH_QUERY_PROMPT = (
    "Translate the user's message into a short English search query. It may be Tamil, "
    "or Tamil written in English letters, or a mix. Output only the English query."
)
_search_query_cache: OrderedDict[str, str] = OrderedDict()


async def _english_search_query(query: str) -> str:
    """English version of a Tamil question, used ONLY to search the knowledge
    base — the visitor's own words still go to the model unchanged.

    The knowledge base is in English and the embedding model
    (text-embedding-3-small) barely connects Tamil text to it: measured
    2026-10-07, a Tamil question scored ~0.1 against the right curated chunk
    where the same question in English scored ~0.4, and what came back
    instead was syllabus pages that happen to contain Tamil. Every Tamil
    answer was being written from unrelated context, which is how a reply
    came to list courses the college doesn't offer.

    Uses Sarvam 105B rather than Sarvam's translate API: tested the same
    day, ~160-310 ms against ~700-1,000 ms, and it handles Tamil typed in
    English letters, which the translate API got wrong. Falls back to the
    original text on any failure."""
    key = _normalize_short(query)
    cached = _search_query_cache.get(key)
    if cached:
        _search_query_cache.move_to_end(key)
        return cached
    try:
        response = await asyncio.wait_for(
            chat_client.chat.completions.create(
                model=CHAT_MODEL,
                messages=[
                    {"role": "system", "content": _SEARCH_QUERY_PROMPT},
                    {"role": "user", "content": query},
                ],
                temperature=0,
                max_tokens=60,
                extra_body=DISABLE_REASONING,
            ),
            timeout=2.0,
        )
        english = (response.choices[0].message.content or "").strip()
    except Exception:
        logger.warning("Search-query translation failed; searching on the original text")
        return query
    if not english:
        return query
    _search_query_cache[key] = english
    if len(_search_query_cache) > 512:
        _search_query_cache.popitem(last=False)
    return english


# The phone agent is a front desk, not a senior student: no "bro"/"anna" even
# when the caller uses them (Prakash, 2026-10-09). Since 2026-10-13 the same rule
# applies to the website bot (TANGLISH_STYLE / TAMIL_STYLE); kept as a reminder.
PHONE_TANGLISH_NO_BRO = (
    "\nThis is a phone call: never say \"bro\" or \"anna\", even if the caller does. "
    "Say \"neenga / unga\" and stay polite."
)


# Repeated in the final system message because that is the one the model obeys:
# with the rule only in the style block, 2 of 16 test replies still said "bro".
NO_BRO_FINAL = (
    " Do not use the words \"bro\", \"anna\", \"da\" or \"machi\" anywhere in this reply, "
    "even though the visitor did."
)


def _caller_line(caller: dict | None) -> str:
    """One line about the caller, from the phone agent's records lookup. The
    full phone/records rules arrive with Phase 3 step 3.2."""
    if not caller or not caller.get("role"):
        return ""
    name = f", first name {caller['name']}" if caller.get("name") else ""
    return f"\nThe caller was identified from their phone number: role {caller['role']}{name}."


async def _build_full_messages(
    messages: list[dict], voice: bool = False, session_id: str | None = None,
    phone: bool = False, caller: dict | None = None,
) -> list[dict]:
    """Shared brain: retrieve context for the latest turn and prepend the
    Agni AI persona + that context to the FULL conversation history, so
    follow-ups resolve correctly. Used by every input path."""
    messages = _normalize_roles_for_sarvam(messages)
    language = _reply_language(messages)
    # voice-pipeline's persona message has done its job (carrying the call's
    # language); this backend supplies the one real persona itself.
    messages = [
        m for m in messages
        if not (m.get("role") == "system" and _REPLY_LANGUAGE_MARKER.search(m.get("content") or ""))
    ]
    # Retrieval should key off what the visitor actually asked, not a
    # synthetic developer/system recovery message that may be the literal
    # last entry (see _normalize_roles_for_sarvam above) — search backward
    # for the most recent real user turn instead of assuming messages[-1].
    user_turns = [m["content"] for m in messages if m.get("role") == "user"]
    query = _retrieval_query(user_turns) if user_turns else messages[-1]["content"]
    kind = _turn_kind(user_turns[-1]) if user_turns else None
    if kind == "ack":
        # "Yes"/"சரி" answers the assistant's last question, so that question
        # — not some earlier visitor turn — is what the reply is about.
        last_reply = next(
            (m.get("content") or "" for m in reversed(messages) if m.get("role") == "assistant"), ""
        )
        asked = [x.strip() for x in _SENTENCE_END.split(last_reply) if x.strip().endswith("?")]
        if asked:
            query = asked[-1]
    t0 = time.perf_counter()
    if kind == "closing" or (user_turns and _is_small_talk(query)):
        # Only reached when there is no earlier substantive turn to reuse
        # (_retrieval_query falls back to one when there is): searching on
        # "hello" costs an embedding call and returns noise.
        context = NO_LOOKUP_CONTEXT
        logger.info("[timing] retrieval skipped (small talk)")
    else:
        if language in ("ta", "ta_en") or any(ord(ch) in TAMIL_UNICODE_BLOCK for ch in query):
            query = await _english_search_query(query)
            logger.info("[timing] search-query translation %.0f ms",
                        (time.perf_counter() - t0) * 1000)
        try:
            context = await retriever.search(query)
        except Exception:
            # A network blip to the embeddings API used to fail the whole
            # request, which on a voice call is dead silence. Answer without
            # context instead: the prompt then has the model say it can't
            # confirm and point to admissions.
            logger.exception("Knowledge-base lookup failed; answering without context")
            context = "No matching context found."
        logger.info("[timing] retrieval %.0f ms (query %d chars)",
                    (time.perf_counter() - t0) * 1000, len(query))
    # One line per turn that voice-pipeline's [turn] line can be matched with
    # (same session id prefix, same order).
    logger.info("[turn-backend] session=%s reply_style=%s rag_ms=%.0f",
                (session_id or "-")[:8], language, (time.perf_counter() - t0) * 1000)
    return [
        {
            "role": "system",
            "content": SYSTEM_PROMPT_TEMPLATE.format(context=context)
            + (VOICE_BREVITY_ADDENDUM if voice else "")
            + (BACKCHANNEL_ADDENDUM if kind == "ack" else "")
            + STYLE_BLOCKS.get(language, "")
            + (PHONE_TANGLISH_NO_BRO if phone and language == "ta_en" else "")
            + (_caller_line(caller) if phone else "")
            + derive_state(messages, session_id).prompt_block(),
        },
        *messages,  # preserves every prior user/assistant turn
        {
            "role": "system",
            "content": (
                CLOSING_FINAL + " " if kind == "closing"
                else VOICE_FINAL_REMINDER + " " if voice else ""
            )
            + LANGUAGE_RULES[language]
            + (NO_BRO_FINAL if language in ("ta", "ta_en") else "")
            + (ACK_FINAL if kind == "ack" else "")
            + ("" if kind == "closing" else FACTS_RULE + _already_said_rule(messages)),
        },
    ]


async def _generate_reply(
    messages: list[dict], source: str = "chat", session_id: str | None = None,
    client_tools: list[dict] | None = None, caller: dict | None = None,
):
    """Non-streaming reply — used by the widget's typed chat, which just
    wants the final text, not incremental tokens.

    Fully async end-to-end (embeddings + chat completion) — a blocking
    synchronous call here would serialize concurrent requests behind each
    other on the single event loop thread, which is what caused earlier
    multi-second latency blowups under voice-agent load.

    Phase 8: passes the capture_lead tool so the model can record an
    admission-intent lead mid-conversation. A tool-call turn is resolved
    (lead saved, tool result appended) and re-submitted for the model's
    actual reply before returning — the caller never sees a tool_calls
    finish_reason, only a normal assistant message."""
    full_messages = await _build_full_messages(
        messages, voice=(source in _SPOKEN_SOURCES), session_id=session_id,
        phone=(source == "phone"), caller=caller,
    )
    all_tools = [CAPTURE_LEAD_TOOL, *(client_tools or [])]
    response = await chat_client.chat.completions.create(
        model=CHAT_MODEL,
        messages=full_messages,
        tools=all_tools,
        **({"tool_choice": FORCE_CAPTURE_LEAD} if _should_force_lead_capture(messages, session_id) else {}),
        extra_body=DISABLE_REASONING,
    )
    choice = response.choices[0]
    if choice.finish_reason == "tool_calls":
        calls = choice.message.model_dump().get("tool_calls") or []
        foreign = _foreign_tool_calls(calls)
        if foreign:
            # The caller's tool: save any lead in the same turn, then hand the
            # caller's calls back unresolved (it runs them and calls again).
            owned = [c for c in calls if c not in foreign]
            if owned:
                await _resolve_tool_calls(full_messages, {"role": "assistant", "content": None, "tool_calls": owned},
                                          source, session_id)
            choice.message.tool_calls = [c for c in choice.message.tool_calls if c.function.name not in OWNED_TOOLS]
            return response
        extended_messages = await _resolve_tool_calls(
            full_messages, choice.message.model_dump(), source, session_id
        )
        response = await chat_client.chat.completions.create(
            model=CHAT_MODEL,
            messages=extended_messages,
            # Sarvam 400s with "Tool messages found but no tools provided" if
            # `tools` is omitted here — it's required on any call whose
            # message history contains tool/tool_calls entries, not just the
            # call that first offers them.
            tools=all_tools,
            extra_body=DISABLE_REASONING,
        )
    return response


async def _stream_reply_sse(
    messages: list[dict], source: str = "voice", session_id: str | None = None,
    client_tools: list[dict] | None = None, caller: dict | None = None,
):
    """Streaming reply, as Server-Sent Events in OpenAI's chat-completion-
    chunk format, required by the Tamil (and now English) Pipecat pipeline's
    OpenAILLMService. Sarvam's streaming chunks are already clean, standard
    OpenAI shapes (unlike Azure's, which carried an extra
    `content_filter_results` field on every delta and had to be
    reconstructed) — forwarded directly here.

    Phase 8: also passes the capture_lead tool. voice-pipeline's
    OpenAILLMService has no capture_lead function registered on its side, so
    tool-call chunks are never forwarded downstream — they're accumulated
    here, resolved (lead saved, tool result appended), and a follow-up stream
    is started for the model's actual spoken reply. Pipecat only ever sees
    plain content deltas, exactly as before Phase 8."""
    t_start = time.perf_counter()
    full_messages = await _build_full_messages(
        messages, voice=(source in _SPOKEN_SOURCES), session_id=session_id,
        phone=(source == "phone"), caller=caller,
    )
    all_tools = [CAPTURE_LEAD_TOOL, *(client_tools or [])]
    current_messages = full_messages

    for round_no in range(2):  # at most one tool-resolution round-trip
        t_round = time.perf_counter()
        stream = await chat_client.chat.completions.create(
            model=CHAT_MODEL,
            messages=current_messages,
            stream=True,
            tools=all_tools,
            # First round only: the follow-up round (after the tool result) must
            # be free to answer in words.
            **({"tool_choice": FORCE_CAPTURE_LEAD}
               if round_no == 0 and _should_force_lead_capture(messages, session_id) else {}),
            extra_body=DISABLE_REASONING,
        )
        tool_calls: dict[int, dict] = {}
        saw_tool_calls = False
        chunk_count = 0
        content_chunk_count = 0
        try:
            async for chunk in stream:
                chunk_count += 1
                if chunk_count == 1:
                    logger.info("[timing] round %d first chunk %.0f ms after request start "
                                "(%.0f ms after Sarvam call)", round_no + 1,
                                (time.perf_counter() - t_start) * 1000,
                                (time.perf_counter() - t_round) * 1000)
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.tool_calls:
                    saw_tool_calls = True
                    for tc in delta.tool_calls:
                        entry = tool_calls.setdefault(
                            tc.index, {"id": None, "name": "", "arguments": ""}
                        )
                        if tc.id:
                            entry["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                entry["name"] += tc.function.name
                            if tc.function.arguments:
                                entry["arguments"] += tc.function.arguments
                    continue
                if delta and delta.content is not None:
                    content_chunk_count += 1
                    if content_chunk_count == 1:
                        logger.info("[timing] round %d first content %.0f ms after request start",
                                    round_no + 1, (time.perf_counter() - t_start) * 1000)
                        logger.info("[turn-backend] session=%s round=%d llm_first_token_ms=%.0f (from request start, includes retrieval)",
                                    (session_id or "-")[:8], round_no + 1,
                                    (time.perf_counter() - t_start) * 1000)
                    yield f"data: {chunk.model_dump_json()}\n\n"
        except Exception:
            logger.exception(
                "Stream raised after %d chunks (%d with content) — this would silently "
                "truncate the SSE response with no [DONE]",
                chunk_count, content_chunk_count,
            )
            raise
        finally:
            logger.info(
                "Stream finished: %d total chunks, %d carried real content, tool_calls=%s",
                chunk_count, content_chunk_count, saw_tool_calls,
            )

        if not saw_tool_calls:
            yield "data: [DONE]\n\n"
            return

        assistant_message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": entry["id"] or f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {"name": entry["name"], "arguments": entry["arguments"]},
                }
                for entry in tool_calls.values()
            ],
        }
        foreign = _foreign_tool_calls(assistant_message["tool_calls"])
        if foreign:
            # The caller's tool: save any lead in the same turn, then send the
            # caller's calls on unresolved (it runs them and calls again).
            owned = [c for c in assistant_message["tool_calls"] if c not in foreign]
            if owned:
                await _resolve_tool_calls(
                    current_messages, {**assistant_message, "tool_calls": owned}, source, session_id
                )
            for chunk in _tool_call_sse(foreign):
                yield chunk
            yield "data: [DONE]\n\n"
            return
        current_messages = await _resolve_tool_calls(
            current_messages, assistant_message, source, session_id
        )
        # loop: stream the model's follow-up reply now that the tool result is in context

    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI-compatible endpoint for the voice-pipeline service (Tamil now,
    English once Phase 3 lands), called with the shared bearer token. Streams
    when the caller sends `"stream": true`, otherwise returns a single JSON
    response."""
    _check_auth(request)
    body = await request.json()
    # voice-pipeline sends its per-call id so conversation_state can remember
    # that a lead was already saved on this call.
    session_id = request.headers.get("x-session-id")
    # The phone agent says so with X-Source; anything else is the website voice.
    source = "phone" if request.headers.get("x-source") == "phone" else "voice"
    client_tools = body.get("tools") or None
    caller = _caller_from_headers(request)

    if body.get("stream"):
        return StreamingResponse(
            _stream_reply_sse(body["messages"], source=source, session_id=session_id,
                              client_tools=client_tools, caller=caller),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    response = await _generate_reply(body["messages"], source=source, session_id=session_id,
                                     client_tools=client_tools, caller=caller)
    return response.model_dump()


class WidgetChatRequest(BaseModel):
    messages: list[dict]
    session_id: str | None = None


@app.post("/widget/chat")
async def widget_chat(payload: WidgetChatRequest):
    """Public-facing proxy for the browser widget. No vendor key or backend
    auth key is ever sent to or held by the browser — this route holds them
    server-side and returns just the reply text."""
    if not payload.messages:
        raise HTTPException(status_code=400, detail="messages must not be empty")

    response = await _generate_reply(
        payload.messages, source="chat", session_id=payload.session_id
    )
    reply = response.choices[0].message.content
    if payload.session_id:
        log_turn(payload.session_id, payload.messages, reply)
    return {"reply": reply}


class CallingAgentAskRequest(BaseModel):
    question: str
    # Optional: the caller's reply style ("en", "ta" or "ta_en" for Tanglish),
    # if the Indus tool is set up to send it. Without it the style follows the script of the
    # question, which is wrong when Indus passes a Tamil caller's question in
    # English.
    language: str | None = None


@app.post("/calling-agent/ask")
async def calling_agent_ask(payload: CallingAgentAskRequest, request: Request):
    """Sarvam Indus Tool endpoint for the Calling Agent (see
    docs/Calling_Agent_Build_Plan.md §4.1's `get_act_answer` tool).
    Bearer-auth required, same key as /v1/chat/completions.

    Deliberately single-turn, with no call-ID/session tracking: Indus's own
    agent already holds the full call's conversation memory and is
    instructed to always pass a fully self-contained question (pronouns/
    references resolved) rather than relying on this endpoint to remember
    prior turns — see the persona Instructions draft in the build plan for
    the exact wording. This keeps the endpoint simple while the actual
    answer still comes from the same retrieval + persona + capture_lead
    logic every other input path uses (_generate_reply), so phone answers
    stay consistent with typed chat and voice."""
    _check_auth(request)
    if not payload.question.strip():
        raise HTTPException(status_code=400, detail="question must not be empty")
    messages = [{"role": "user", "content": payload.question}]
    if payload.language in LANGUAGE_RULES:
        messages.insert(0, {"role": "system", "content": f"reply_language: {payload.language}"})
    response = await _generate_reply(
        messages, source="phone"
    )
    return {"reply": response.choices[0].message.content}


@app.post("/admin/reload-knowledge")
async def reload_knowledge(request: Request):
    """Re-ingest markdown, knowledge_base/data/ files and the knowledge Google
    Sheet after admissions edits them — no redeploy needed."""
    _check_auth(request)
    return {"chunks": await retriever.reload()}


@app.get("/health")
async def health():
    return {"status": "ok"}
