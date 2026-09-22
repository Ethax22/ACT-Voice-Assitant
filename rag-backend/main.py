"""ACT AI's shared RAG backend.

Every input path — typed chat, Deepgram English voice, and the Sarvam Tamil
pipeline — calls this single chat-completion logic, so all three stay on one
brain (same retrieval + same persona) by construction.

Three routes expose it:
- POST /v1/chat/completions — OpenAI-compatible shape, bearer-auth required.
  Called by Deepgram's custom LLM endpoint and the Tamil Pipecat pipeline,
  both of which hold RAG_BACKEND_AUTH_KEY server-side.
- POST /widget/chat — simplified shape, no auth required. Called directly by
  the browser widget, which must never hold RAG_BACKEND_AUTH_KEY or any
  vendor key.
- WS /voice/en — relays mic/TTS audio between the browser and Deepgram's
  Voice Agent API, so the Deepgram API key stays server-side too. See
  voice_agent.py.
"""
import json
import logging
import os

from dotenv import load_dotenv

load_dotenv()  # must run before importing retriever, which reads env vars at module load

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from openai import AsyncAzureOpenAI

from retriever import retriever
from voice_agent import run_english_voice_session

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

azure_client = AsyncAzureOpenAI(
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
)
CHAT_DEPLOYMENT = os.environ["AZURE_OPENAI_DEPLOYMENT"]
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
    return await azure_client.chat.completions.create(
        model=CHAT_DEPLOYMENT,
        messages=full_messages,
    )


async def _stream_reply_sse(messages: list[dict]):
    """Streaming reply, as Server-Sent Events in OpenAI's chat-completion-
    chunk format. Deepgram's Voice Agent (and Pipecat's OpenAILLMService,
    used by the Tamil pipeline) send `"stream": true` on think/LLM calls and
    require this exact format — a single JSON blob, even with HTTP 200, is
    silently unparsable to them and shows up as THINK_REQUEST_FAILED despite
    our own access log showing a successful 200 response. Confirmed against
    Deepgram's own reference custom-LLM-proxy implementation
    (deepgram-devs/deepgram-voice-agent-client-llm-proxy)."""
    full_messages = await _build_full_messages(messages)
    stream = await azure_client.chat.completions.create(
        model=CHAT_DEPLOYMENT,
        messages=full_messages,
        stream=True,
    )
    chunk_count = 0
    content_chunk_count = 0
    try:
        async for chunk in stream:
            chunk_count += 1
            if not chunk.choices:
                # Azure's initial content-filter-metadata-only chunk (choices: []).
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta_content := (delta.content if delta else None):
                content_chunk_count += 1

            # Re-serialize into a minimal, strictly-standard OpenAI chunk shape
            # instead of forwarding Azure's raw chunk JSON. Azure attaches an
            # extra `content_filter_results` field to every delta (not just
            # the empty first chunk) — confirmed via diagnostic logging that
            # Azure streams a complete, correct answer (70+ real content
            # chunks, finish_reason='stop') yet Deepgram never even emits a
            # ConversationText/History message for it, only AgentAudioDone
            # with 0 bytes — i.e. Deepgram's strict OpenAI-schema parser is
            # failing on the unrecognized field and silently discarding the
            # chunk rather than erroring. Stripping down to only the fields
            # Deepgram's custom-LLM proxy reference expects avoids this.
            delta_out = {}
            if delta and delta.role:
                delta_out["role"] = delta.role
            if delta and delta.content is not None:
                delta_out["content"] = delta.content
            payload = {
                "id": chunk.id,
                "object": "chat.completion.chunk",
                "created": chunk.created,
                "model": chunk.model,
                "choices": [
                    {
                        "index": choice.index,
                        "delta": delta_out,
                        "finish_reason": choice.finish_reason,
                    }
                ],
            }
            yield f"data: {json.dumps(payload)}\n\n"
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
    """OpenAI-compatible endpoint for Deepgram's custom LLM endpoint and the
    Tamil Pipecat pipeline — both call this with the shared bearer token.
    Both request streaming (`"stream": true`); routes to SSE when they do,
    and a single JSON response otherwise (e.g. a non-streaming caller)."""
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


def _origin_allowed(websocket: WebSocket) -> bool:
    if "*" in WIDGET_ALLOWED_ORIGINS:
        return True
    origin = websocket.headers.get("origin", "")
    return origin in WIDGET_ALLOWED_ORIGINS


@app.websocket("/voice/en")
async def voice_en(websocket: WebSocket):
    """English voice path (Phase 3). Same lightweight origin check as the
    widget's CORS config — not a substitute for real auth/rate-limiting in
    production, since each session costs real Deepgram + Azure usage."""
    if not _origin_allowed(websocket):
        await websocket.close(code=4403)
        return
    await run_english_voice_session(websocket)


@app.get("/health")
async def health():
    return {"status": "ok"}
