# Implementation Plan: ACT AI Voice Assistant (API-Based Path)

**Companion to:** PRD_API_Based_Voice_Assistant
**Author:** Prakash D (Ethax)
**Date:** September 2026

This plan turns the confirmed architecture (Deepgram for English, composed Sarvam for Tamil, one shared RAG backend) into concrete build steps. Code below is illustrative — treat variable names, package names, and exact SDK calls as a starting point to verify against current docs, not copy-paste-final code.

---

## 1. Repo Structure

```
act-ai-assistant/
├── rag-backend/           # FastAPI service — the shared "brain"
│   ├── main.py
│   ├── knowledge_base/    # ACT content: admissions, courses, placements, FAQs
│   ├── retriever.py
│   └── requirements.txt
├── tamil-voice-pipeline/  # Pipecat pipeline for Sarvam (Tamil)
│   ├── pipeline.py
│   └── requirements.txt
├── widget/                # One-page website + ACT AI widget
│   └── index.html
└── README.md
```

## 2. Step 1 — Build the RAG Backend (the shared brain)

This is the most important piece: both the English (Deepgram) and Tamil (Sarvam) paths call this same service, which is what keeps answers consistent.

**2.1 Ingest ACT's content**
- Collect source documents: admissions pages, course catalog, placement stats, FAQs — as plain text/markdown files in `knowledge_base/`.
- Chunk them (e.g., ~500-token chunks with slight overlap) and embed them into a vector store. A lightweight option like Chroma (runs in-process, no separate server) is enough at this scale — no need for a managed vector DB.

**2.2 Expose an OpenAI-compatible endpoint**

Two things matter here beyond basic wiring, and both directly affect whether this feels like a conversation or a search box: **conversation memory** (the LLM needs the full message history, not just the latest question) and **a persona in the system prompt** (without one, most LLMs default to stiff, over-formal answers).

```python
# rag-backend/main.py
from fastapi import FastAPI, Request
import openai  # configured with your Microsoft for Startups / Azure OpenAI key

app = FastAPI()

SYSTEM_PROMPT_TEMPLATE = """You are ACT AI, the friendly voice/chat assistant for
Agni College of Technology. Talk like a helpful senior student showing someone
around campus, not like a brochure: warm, direct, and brief.

Rules:
- Keep answers short and conversational — 2-4 sentences unless the visitor
  clearly wants detail. This matters even more for voice, where long answers
  feel like a monologue rather than a chat.
- Only answer from the context below. If it's not there, say so plainly and
  suggest contacting admissions — don't guess or make up figures.
- It's fine to ask a short clarifying question back if the visitor's question
  is ambiguous (e.g., which department, which year) — that's what makes it
  feel like a conversation instead of a lookup.

Context:
{context}
"""

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages = body["messages"]  # full conversation history, not just the last turn
    latest_user_message = messages[-1]["content"]

    # 1. Retrieve relevant ACT content for the latest question
    context_chunks = retriever.search(latest_user_message, top_k=4)

    # 2. Build the full message list: persona/context system prompt +
    #    the ENTIRE prior conversation, so follow-ups like "what about ECE?"
    #    correctly resolve against what was already discussed
    full_messages = [
        {"role": "system", "content": SYSTEM_PROMPT_TEMPLATE.format(context=context_chunks)},
        *messages,  # preserves every prior user/assistant turn
    ]

    # 3. Call OpenAI (funded via Microsoft for Startups credits — confirm
    #    whether this is a direct OpenAI key or Azure OpenAI Service, since
    #    the client setup differs slightly between the two)
    response = openai.chat.completions.create(
        model="gpt-4o-mini",  # placeholder — pick based on cost/quality testing
        messages=full_messages,
    )

    # 4. Return in OpenAI-compatible format so Deepgram (and your Tamil
    #    pipeline) can call this exactly like any other OpenAI-format model
    return response.model_dump()
```

**Why this matters for both voice and text:** Deepgram's Voice Agent API already sends conversation history via `agent.context.messages` on reconnect, and your Pipecat/LiveKit pipeline for Tamil should be built to accumulate turns the same way — but the *backend* is what actually has to use that history intelligently, and the version above does. Without this, every turn resets to a blank slate regardless of how well the voice plumbing handles turn-taking.

**One more lever worth knowing about:** for voice specifically, the TTS voice you pick (Deepgram's Aura-2 voice options, Sarvam's Bulbul personas) affects "conversational feel" as much as the text does — a flat, low-expressiveness voice will sound robotic even reading a perfectly warm sentence. Worth spending 20 minutes in the Deepgram Playground and Sarvam's voice samples comparing a few options before locking one in, rather than defaulting to the first one.

**2.3 Deploy it somewhere reachable** — a small VM or a platform like Render/Railway works; it doesn't need GPU, just needs to be publicly reachable over HTTPS since Deepgram's servers will call it directly.

## 3. Step 2 — Deepgram Voice Agent Setup (English)

You've already confirmed this works in the Deepgram Playground. For production, configure it programmatically via the Settings message on connection:

```json
{
  "type": "Settings",
  "audio": { "input": { "encoding": "linear16", "sample_rate": 16000 } },
  "agent": {
    "listen": { "provider": { "type": "deepgram", "model": "nova-3" } },
    "think": {
      "provider": { "type": "open_ai" },
      "endpoint": {
        "url": "https://your-rag-backend.example.com/v1/chat/completions",
        "headers": { "Authorization": "Bearer YOUR_BACKEND_AUTH_KEY" }
      },
      "prompt": "You are ACT AI, the assistant for Agni College of Technology."
    },
    "speak": { "provider": { "type": "deepgram", "model": "aura-2-thalia-en" } }
  }
}
```

- Use a Deepgram SDK (Python/JS) to open the WebSocket, send this Settings message, then stream microphone audio in and play the returned audio out.
- Test with the same question set you'll use for the Tamil path, so you have a baseline before building that side.

## 4. Step 3 — Tamil Pipeline (Sarvam, composed via Pipecat)

Unlike Deepgram, Sarvam's individual STT/TTS are separate APIs, so you assemble the loop yourself:

```python
# tamil-voice-pipeline/pipeline.py  (illustrative — verify against current Pipecat + Sarvam docs)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
# STT, LLM, and TTS services below are illustrative class names —
# confirm exact Pipecat service integrations available for Sarvam at build time;
# if no ready-made Sarvam plugin exists, you'll wrap Sarvam's HTTP/WebSocket
# API in a small custom Pipecat service class.

stt = SarvamSTTService(api_key=SARVAM_API_KEY, language="ta-IN")
llm = OpenAICompatibleLLMService(base_url="https://your-rag-backend.example.com")
tts = SarvamTTSService(api_key=SARVAM_API_KEY, voice="meera", language="ta-IN")

pipeline = Pipeline([stt, llm, tts])
runner = PipelineRunner()
runner.run(pipeline)
```

- The critical point: `llm` here points at the **same** RAG backend URL as the Deepgram config above — this is what keeps English and Tamil answers consistent.
- If Pipecat doesn't have a ready-made Sarvam integration when you build this, LiveKit Agents is the fallback — both are open-source and the wiring pattern is the same.

## 5. Step 4 — The Widget & One-Page Site

The widget file (delivered alongside this plan) supports **two ways to interact**, matching the PRD's goal of supporting both spoken and typed questions:

### 5.1 Typed chat (simplest — build and test this first)

Since your RAG backend already exposes an OpenAI-compatible `/v1/chat/completions` endpoint, typed chat needs no voice vendor at all:

```javascript
// widget/index.html (already stubbed in — replace RAG_BACKEND_URL)
const response = await fetch('https://your-rag-backend.example.com/v1/chat/completions', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    messages: [{ role: 'user', content: userMessage }]
  })
});
const data = await response.json();
// data matches OpenAI's response shape — read data.choices[0].message.content
```

Route this through a small proxy on your own backend rather than calling the RAG backend directly from the browser, so its auth key isn't exposed in client-side JS. This path is worth shipping first — it validates the RAG backend and knowledge base with real visitor questions before you take on the added complexity of voice.

### 5.2 Voice (English via Deepgram, Tamil via composed Sarvam)

- **English:** connect directly to Deepgram's Voice Agent WebSocket from the browser (Deepgram supports browser-based agents), or proxy through your own backend if you want to hide the Deepgram key from the client.
- **Tamil:** since the pipeline runs server-side (Pipecat/LiveKit), the browser connects to *your* server via WebRTC (LiveKit) or a WebSocket relay (Pipecat), which then talks to Sarvam.
- **Never expose Deepgram or Sarvam API keys directly in the browser's JavaScript** — route through a small backend relay that holds the keys server-side, even for the "simple" English path.
- Wire the four category buttons (About Us, Courses, Admissions, Placements) to send a starter message into the conversation rather than just being decorative — e.g., clicking "Placements" opens the voice/chat session with "Tell me about placements at ACT" pre-sent. This works identically whether the visitor is about to type or talk.

## 6. Step 5 — Testing & Consistency QA

Before pilot: run the same ~20-30 question test set (from the PRD) through both the English and Tamil paths and confirm the *substance* of the answers matches — this is the actual proof that the shared RAG backend design is working as intended, not just a nice diagram.

## 7. Step 6 — Deployment

- **Widget/one-page site:** static hosting (Vercel, Netlify, or the college's own web server) — it's a single HTML page, no build step required unless you later migrate it into your Next.js stack.
- **RAG backend + Tamil pipeline:** need a persistently running server (not static hosting) — a small VM or a platform like Render/Railway.
- **Secrets:** Deepgram key, Sarvam key, OpenAI key all live server-side only, loaded from environment variables, never committed to the repo.

## 8. Timeline

| Week | Milestone |
|---|---|
| 1 | RAG backend built and answering typed test questions correctly (no voice yet) |
| 2 | Typed chat wired into the widget end-to-end — this is your first real, testable version |
| 3 | Deepgram English voice path working end-to-end against the RAG backend |
| 4 | Sarvam Tamil voice pipeline working end-to-end against the same RAG backend |
| 5 | Consistency QA across typed chat + both voice languages; widget fully wired |
| 6 | Pilot on a staging page |
| 7 | Launch on the public one-page site |

## 9. Definition of Done (v1)

- [ ] Visitor can type a question and get an answer from the RAG backend
- [ ] Visitor can pick English or Tamil and have a full spoken conversation in either
- [ ] Both languages, and typed chat, answer the same test-question set consistently
- [ ] No API keys exposed in browser-side code
- [ ] Widget matches the approved ACT AI branding (no third-party "Powered by" attribution)
- [ ] Deployed and reachable at a real URL for the pilot
