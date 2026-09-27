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

app = FastAPI(title="ACT AI RAG Backend")
logger = logging.getLogger("rag_backend")
logging.basicConfig(level=logging.INFO)

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

Context:
{context}
"""


def _check_auth(request: Request):
    if not BACKEND_AUTH_KEY:
        return
    auth_header = request.headers.get("authorization", "")
    if auth_header != f"Bearer {BACKEND_AUTH_KEY}":
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


async def _build_full_messages(messages: list[dict]) -> list[dict]:
    """Shared brain: retrieve context for the latest turn and prepend the
    ACT AI persona + that context to the FULL conversation history, so
    follow-ups resolve correctly. Used by every input path."""
    latest_user_message = messages[-1]["content"]
    context = await retriever.search(latest_user_message, top_k=4)
    return [
        {"role": "system", "content": SYSTEM_PROMPT_TEMPLATE.format(context=context)},
        *messages,  # preserves every prior user/assistant turn
    ]


async def _generate_reply(messages: list[dict]):
    """Non-streaming reply — used by the widget's typed chat, which just
    wants the final text, not incremental tokens.

    Fully async end-to-end (embeddings + chat completion) — a blocking
    synchronous call here would serialize concurrent requests behind each
    other on the single event loop thread, which is what caused earlier
    multi-second latency blowups under voice-agent load."""
    full_messages = await _build_full_messages(messages)
    return await chat_client.chat.completions.create(
        model=CHAT_MODEL,
        messages=full_messages,
        extra_body=DISABLE_REASONING,
    )


async def _stream_reply_sse(messages: list[dict]):
    """Streaming reply, as Server-Sent Events in OpenAI's chat-completion-
    chunk format, required by the Tamil (and now English) Pipecat pipeline's
    OpenAILLMService. Sarvam's streaming chunks are already clean, standard
    OpenAI shapes (unlike Azure's, which carried an extra
    `content_filter_results` field on every delta and had to be
    reconstructed) — forwarded directly here."""
    full_messages = await _build_full_messages(messages)
    stream = await chat_client.chat.completions.create(
        model=CHAT_MODEL,
        messages=full_messages,
        stream=True,
        extra_body=DISABLE_REASONING,
    )
    chunk_count = 0
    content_chunk_count = 0
    try:
        async for chunk in stream:
            chunk_count += 1
            if chunk.choices and chunk.choices[0].delta.content is not None:
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
            "Stream finished: %d total chunks, %d carried real content",
            chunk_count, content_chunk_count,
        )
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
            _stream_reply_sse(body["messages"]),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    response = await _generate_reply(body["messages"])
    return response.model_dump()


class WidgetChatRequest(BaseModel):
    messages: list[dict]


@app.post("/widget/chat")
async def widget_chat(payload: WidgetChatRequest):
    """Public-facing proxy for the browser widget. No vendor key or backend
    auth key is ever sent to or held by the browser — this route holds them
    server-side and returns just the reply text."""
    if not payload.messages:
        raise HTTPException(status_code=400, detail="messages must not be empty")

    response = await _generate_reply(payload.messages)
    return {"reply": response.choices[0].message.content}


@app.get("/health")
async def health():
    return {"status": "ok"}
