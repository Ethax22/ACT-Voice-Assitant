"""ACT AI's shared RAG backend.

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
import json
import logging
import os

from dotenv import load_dotenv

load_dotenv()  # must run before importing retriever, which reads env vars at module load

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from openai import AsyncOpenAI

from retriever import retriever
from leads import save_lead
from conversation_logger import log_turn, prune_old_logs

app = FastAPI(title="ACT AI RAG Backend")
logger = logging.getLogger("rag_backend")
logging.basicConfig(level=logging.INFO)


@app.on_event("startup")
async def _prune_logs_on_startup():
    """Phase 10: 90-day retention, auto-pruned — a startup check rather than
    a running scheduler, since this only needs to happen about once a day
    and the backend restarts at least that often in practice."""
    prune_old_logs()

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

SYSTEM_PROMPT_TEMPLATE = """You are ACT AI, the friendly voice/chat assistant for
Agni College of Technology. Talk like a helpful senior student showing someone
around campus, not like a brochure: warm, direct, and brief.

Rules:
- Keep answers short and conversational — 2-4 sentences unless the visitor
  clearly wants detail. This matters even more for voice, where long answers
  feel like a monologue rather than a chat.
- Start most replies with a brief, natural spoken acknowledgment ("Sure,"
  "Good question," "Got it," "Ah, that's a good one") before the actual
  answer. Vary the phrase — never repeat the same one two turns in a row.
  This is what makes a reply feel like a live conversation instead of a
  formal lookup response.
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


TAMIL_UNICODE_BLOCK = range(0x0B80, 0x0C00)


def _detect_language(messages: list[dict]) -> str:
    """Best-effort language tag for a captured lead — same script-detection
    idea as the widget's client-side `detectScriptLanguage`, applied
    server-side since rag-backend never receives an explicit language field
    (voice-pipeline's system content is ignored, per pipeline.py)."""
    text = " ".join(m.get("content", "") or "" for m in messages if m.get("role") == "user")
    return "ta" if any(ord(ch) in TAMIL_UNICODE_BLOCK for ch in text) else "en"


async def _resolve_tool_calls(
    messages: list[dict], assistant_message: dict, source: str
) -> list[dict]:
    """Execute every capture_lead call in an assistant tool-call turn and
    return messages extended with the assistant's tool-call turn plus one
    tool-result message per call, ready for a follow-up completion so the
    model can produce its actual spoken/typed confirmation."""
    language = _detect_language(messages)
    extended = [*messages, assistant_message]
    for call in assistant_message.get("tool_calls") or []:
        function = call.get("function") or {}
        result = "Unrecognized tool."
        if function.get("name") == "capture_lead":
            try:
                args = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            saved = save_lead(
                name=args.get("name", ""),
                phone=args.get("phone", ""),
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


async def _build_full_messages(messages: list[dict]) -> list[dict]:
    """Shared brain: retrieve context for the latest turn and prepend the
    ACT AI persona + that context to the FULL conversation history, so
    follow-ups resolve correctly. Used by every input path."""
    messages = _normalize_roles_for_sarvam(messages)
    # Retrieval should key off what the visitor actually asked, not a
    # synthetic developer/system recovery message that may be the literal
    # last entry (see _normalize_roles_for_sarvam above) — search backward
    # for the most recent real user turn instead of assuming messages[-1].
    latest_user_message = next(
        (m["content"] for m in reversed(messages) if m.get("role") == "user"),
        messages[-1]["content"],
    )
    context = await retriever.search(latest_user_message, top_k=4)
    return [
        {"role": "system", "content": SYSTEM_PROMPT_TEMPLATE.format(context=context)},
        *messages,  # preserves every prior user/assistant turn
    ]


async def _generate_reply(messages: list[dict], source: str = "chat"):
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
    full_messages = await _build_full_messages(messages)
    response = await chat_client.chat.completions.create(
        model=CHAT_MODEL,
        messages=full_messages,
        tools=[CAPTURE_LEAD_TOOL],
        extra_body=DISABLE_REASONING,
    )
    choice = response.choices[0]
    if choice.finish_reason == "tool_calls":
        extended_messages = await _resolve_tool_calls(
            full_messages, choice.message.model_dump(), source
        )
        response = await chat_client.chat.completions.create(
            model=CHAT_MODEL,
            messages=extended_messages,
            # Sarvam 400s with "Tool messages found but no tools provided" if
            # `tools` is omitted here — it's required on any call whose
            # message history contains tool/tool_calls entries, not just the
            # call that first offers them.
            tools=[CAPTURE_LEAD_TOOL],
            extra_body=DISABLE_REASONING,
        )
    return response


async def _stream_reply_sse(messages: list[dict], source: str = "voice"):
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
    full_messages = await _build_full_messages(messages)
    current_messages = full_messages

    for _ in range(2):  # at most one tool-resolution round-trip
        stream = await chat_client.chat.completions.create(
            model=CHAT_MODEL,
            messages=current_messages,
            stream=True,
            tools=[CAPTURE_LEAD_TOOL],
            extra_body=DISABLE_REASONING,
        )
        tool_calls: dict[int, dict] = {}
        saw_tool_calls = False
        chunk_count = 0
        content_chunk_count = 0
        try:
            async for chunk in stream:
                chunk_count += 1
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
                    "id": entry["id"],
                    "type": "function",
                    "function": {"name": entry["name"], "arguments": entry["arguments"]},
                }
                for entry in tool_calls.values()
            ],
        }
        current_messages = await _resolve_tool_calls(
            current_messages, assistant_message, source
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

    if body.get("stream"):
        return StreamingResponse(
            _stream_reply_sse(body["messages"], source="voice"),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    response = await _generate_reply(body["messages"], source="voice")
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

    response = await _generate_reply(payload.messages, source="chat")
    reply = response.choices[0].message.content
    if payload.session_id:
        log_turn(payload.session_id, payload.messages, reply)
    return {"reply": reply}


@app.get("/health")
async def health():
    return {"status": "ok"}
