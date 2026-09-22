# ACT AI — Session Handoff Summary

This documents everything built and debugged in the prior session, for continuing
in a fresh session with full context.

## What ACT AI is

A floating widget ("ACT AI") for Agni College of Technology's website, answering
visitor questions about admissions/courses/placements/campus life via typed chat
or voice (English via Deepgram, Tamil via Sarvam), all backed by one shared RAG
backend so answers stay consistent across every input mode. Full spec in
`CLAUDE.md` and `docs/Implementation_Plan_API_Based.md`.

## Build status by phase

| Phase | Status |
|---|---|
| 1. RAG backend (FastAPI + Azure OpenAI + retrieval) | ✅ Done, confirmed working |
| 2. Typed chat in widget | ✅ Done, confirmed working end-to-end |
| 3. Deepgram English voice | 🟡 Backend confirmed replying (Deepgram's own `AgentAudioDone` fires every turn) but **no audio is actually heard by the user** — unresolved, see "Open issue" below |
| 4. Sarvam Tamil voice | 🟡 Fully working pipeline (STT transcribes, LLM replies, TTS speaks) but voice quality needs a real listening pass on the `kavya` voice pick — sample-rate bug that caused robotic sound is fixed |
| 5. Consistency QA | ⏳ Not started — blocked on voice actually working |
| 6. Deployment | ⏳ Not started |

## Repo layout

```
ACT-Voice-Assistant/
├── CLAUDE.md, docs/ (PRD, Implementation Plan, Voice_Model_Choices.md, this file)
├── rag-backend/       — FastAPI shared brain (port 8000)
├── tamil-voice-pipeline/ — standalone Pipecat/Sarvam service (port 8001)
└── widget/index.html  — the actual widget (navy/gold theme, EN/TA toggle)
```

Both Python services (`rag-backend`, `tamil-voice-pipeline`) have their own
`venv/` — **do not share a global Python environment between them**; that caused
a real `websockets` version collision earlier (see bug list).

## Environment / infra facts to know

- `rag-backend/.env` and `tamil-voice-pipeline/.env` hold real secrets (Azure
  OpenAI, Deepgram, Sarvam keys, plus a **shared** `RAG_BACKEND_AUTH_KEY` you
  must keep identical in both files).
- `RAG_BACKEND_PUBLIC_URL` = an **ngrok tunnel** to `rag-backend` (currently
  `https://buffer-condense-valley.ngrok-free.dev`, a static/reserved free-tier
  domain that doesn't change between ngrok restarts). Both Deepgram's cloud and
  the Tamil pipeline call back into this URL — it must be running and reachable
  or every voice turn fails with `ERR_NGROK_3200`. Verify with
  `curl https://buffer-condense-valley.ngrok-free.dev/health` before debugging
  anything else.
- Azure OpenAI resource: endpoint `https://genie-v1-openai.cognitiveservices.azure.com/`,
  chat deployment `gpt-5.6-sol`, embedding deployment `text-embedding-3-small`,
  **API version must be exactly `2024-12-01-preview`** (Azure 404s on any
  mismatched version string even with correct deployment names).
- Run commands: `uvicorn main:app --reload --port 8000` (rag-backend),
  `uvicorn server:app --reload --port 8001` (tamil-voice-pipeline), widget served
  via `python -m http.server 8899` from the `widget/` folder.

## All bugs found and fixed this session (chronological)

1. **`load_dotenv()` ordering** — was called after `from retriever import
   retriever`, so env vars weren't loaded when the retriever's client was
   constructed. Fixed: `load_dotenv()` now runs first in `main.py`.

2. **Chroma wouldn't build on Windows** (`chroma-hnswlib` needs MSVC Build
   Tools). Replaced with a plain numpy cosine-similarity store in
   `retriever.py` — fine at this knowledge-base's small scale.

3. **Deepgram Settings schema errors** (multiple rounds, now resolved):
   - `agent.listen`/`speak` provider **does** need `"version": "v2"` for Flux
     models (confirmed against a real Settings JSON from Prakash's own
     Deepgram console) — an earlier attempt wrongly removed this based on a
     non-Flux example.
   - `agent.think.provider` needs a `"model"` field even when a custom
     `endpoint` is set — Deepgram's parser rejects the message without one,
     even though the endpoint URL is what's actually called. Fixed with a
     placeholder `"model": "act-ai"` (ignored by our backend).

4. **websockets library API drift** — `voice_agent.py` used the deprecated
   `extra_headers=` kwarg; newer `websockets` versions route that through a
   broken legacy shim (`ImportError: cannot import name 'USER_AGENT'`). Fixed:
   use `additional_headers=` instead, and stopped pinning an old `websockets`
   version (was colliding with what `pipecat-ai` needs anyway).

5. **Synchronous Azure OpenAI client blocking the event loop** — `rag-backend`
   used `AzureOpenAI` (sync) inside `async def` routes. Deepgram fires several
   speculative "think" calls per turn; a blocking client serializes them,
   causing multi-second latency blowups (`SLOW_THINK_REQUEST` →
   `THINK_REQUEST_FAILED`). Fixed: switched to `AsyncAzureOpenAI` everywhere.

6. **Retriever's async client bound to a dead event loop** — my own
   regression: `Retriever.__init__` called `asyncio.run(self._ingest())`,
   which creates and then closes a temporary loop. The `AsyncAzureOpenAI`
   client's underlying `httpx` connections got bound to that closed loop, so
   every real request afterward silently failed. Fixed: ingestion is now
   lazy, triggered by the first real `search()` call on the actual server
   loop, guarded by an `asyncio.Lock`.

7. **Pipecat 1.10 removed `OpenAILLMContext`** in favor of a universal
   `LLMContext` / `LLMContextAggregatorPair` API. Updated
   `tamil-voice-pipeline/pipeline.py` accordingly.

8. **`FastAPIWebsocketTransport(serializer=None)` silently discards every
   incoming message** — Pipecat's source does `if not self._params.serializer:
   continue`. Wrote a custom `raw_pcm_serializer.py` (`RawPCMSerializer`) since
   the browser widget speaks plain PCM16, not a telephony format like Twilio's.

9. **`SarvamTTSService(language=..., voice=...)` — wrong parameter shape.**
   Verified against Pipecat's actual source: only `api_key` is a real direct
   kwarg; `language`/`voice`/`model` must go through
   `settings=SarvamTTSService.Settings(...)`. Flat kwargs were silently
   swallowed by `**kwargs` with **no error** — this is why Sarvam spoke English
   with the default voice (`shubh`) instead of Tamil/`kavya` for a long time.

10. **`saaras:v4` doesn't apply to `SarvamRealtimeSTTService`** — it hardcodes
    `saaras:v3-realtime` internally with no override. Not a bug, just a
    documentation correction (nothing to configure).

11. **Browser WebSocket disconnect handling bug** — `voice_agent.py`'s
    `browser_to_agent()` used low-level `client_ws.receive()`, which returns
    `{"type": "websocket.disconnect"}` on disconnect instead of raising an
    exception (unlike `receive_text()`/`receive_bytes()`). The loop didn't
    check for this and called `.receive()` again, which Starlette forbids
    (`RuntimeError: Cannot call "receive" once a disconnect message has been
    received`). Fixed: explicit check and early return.

12. **ngrok tunnel offline** (`ERR_NGROK_3200`) — infra issue, not code. Was a
    red herring for one round; confirmed it's a **static/reserved** free-tier
    domain so it doesn't change across restarts, just needs the tunnel process
    running.

13. **The big one: streaming was never implemented.** Deepgram's Voice Agent
    and Pipecat's `OpenAILLMService` both send `"stream": true` on `think`/LLM
    calls and require Server-Sent Events back — a single JSON blob (even with
    HTTP 200) is silently unparsable to them, showing up as
    `THINK_REQUEST_FAILED` **despite our own access log showing a successful
    200**. Verified against Deepgram's own reference proxy repo
    (`deepgram-devs/deepgram-voice-agent-client-llm-proxy`). Fixed: `/v1/chat/completions`
    now checks `body.get("stream")` and returns proper `text/event-stream` SSE
    chunks (`data: {...}\n\n` ... `data: [DONE]\n\n`) when true, using Azure's
    own streaming chat completion (which already matches OpenAI's chunk shape,
    no manual reconstruction needed). This fixed both English and Tamil
    getting real LLM replies.

14. **Tamil audio sounded "robotic/stuck"** — root cause: `FastAPIWebsocketParams`
    has **no `sample_rate` field** at all (only `audio_in_sample_rate` /
    `audio_out_sample_rate`); it's a Pydantic model that silently drops unknown
    kwargs. My `sample_rate=16000` had zero effect, so `audio_out` fell back to
    Pipecat's default (24000, matching what Sarvam TTS natively generates),
    while the widget was told to play Tamil audio back at 16000 Hz — playing
    24kHz audio through a 16kHz context sounds deep/robotic. Fixed: explicit
    `audio_in_sample_rate=16000` / `audio_out_sample_rate=24000` in
    `pipeline.py`, and widget's Tamil `playbackSampleRate` corrected to 24000.

15. **AudioContext autoplay suspension (widget)** — the playback `AudioContext`
    was created lazily inside `voiceWs.onmessage`, well outside the direct
    click-handler call stack. Browsers may start such a context "suspended,"
    where `source.start()` plays into silence with **no error at all**. Fixed:
    context now created and `.resume()`d synchronously at the top of
    `startVoiceSession()`, inside the click handler. **This fix did not
    resolve the English silence** — see open issue below.

## OPEN ISSUE — root cause found, fix applied, NOT YET RE-TESTED

**English voice: greeting audio played (confirmed via a
`Forwarding N bytes of audio to browser` logging pass — ~418KB relayed for
the greeting), but every subsequent reply produced `AgentAudioDone` with
ZERO forwarded bytes** — i.e. Deepgram believed it finished speaking but had
nothing to speak, for every real question after the greeting.

**Root cause (confirmed via web research, not guesswork this time):** Azure
OpenAI's streaming API sends an initial SSE chunk with `choices: []` —
content-filter metadata that plain OpenAI's API doesn't send. Our
`_stream_reply_sse()` in `main.py` was relaying that empty chunk verbatim as
the first event of every stream. Deepgram's parser almost certainly reads
that as "no content" and short-circuits straight to `AgentAudioDone`,
discarding whatever real content chunks came after. This exactly explains why
the *greeting* worked (it's sent via Deepgram's own `agent.greeting` field,
never touches our streaming code at all) while every *LLM-generated* reply
after it went silent — same bug, consistently reproduced across many rounds.

**Fix applied in `rag-backend/main.py`'s `_stream_reply_sse()`:** skip any
chunk where `chunk.choices` is empty before yielding it — only forward chunks
that carry real content/finish_reason.

**This fix has NOT been tested yet.** Next session should start by:
1. Restarting `rag-backend`
2. Testing English: greeting, then ask a real question, confirm you hear a
   spoken reply this time
3. If still silent, check the log again for `Forwarding N bytes` lines on the
   *question* turn (not just the greeting) — if bytes are now flowing but
   still inaudible, the bug has moved to the browser after all and the
   AudioContext-suspension work from earlier should be re-examined with this
   new information in hand.

Given this bug also affects the Tamil path (same `_stream_reply_sse` function,
shared by both `voice_agent.py` and `tamil-voice-pipeline`), **this fix may
also explain any remaining Tamil reply issues** — worth confirming Tamil
still works (or works better) after this change too.

**Do not re-litigate the already-fixed items above** — re-verify only if new
evidence contradicts them.

### UPDATE (re-tested, fix confirmed insufficient)

Re-tested with the empty-`choices`-skip fix in place. **Symptom is
unchanged**: log shows the embeddings call, then the `gpt-5.6-sol` chat
completion call (200 OK), then Deepgram's `AgentAudioDone` fires immediately
— with **zero** `Forwarding N bytes of audio to browser` lines in between.
So real content chunks are still not making it through to Deepgram's TTS as
speakable text, or the Azure stream isn't actually carrying real content for
this call at all. The empty-chunk-skip fix is still correct to keep (it was
a real bug) but is not the (or not the only) root cause of the silence.

**Next step in progress:** added temporary diagnostic logging to
`_stream_reply_sse()` in `rag-backend/main.py` — logs every chunk's
`delta.content` and `finish_reason`, and wraps the loop in try/except so any
silently-swallowed exception mid-stream shows up instead of just truncating
the SSE response with no `[DONE]`. **Restart rag-backend and re-run the same
English voice test; the fresh log will show one of:**
- No content chunks logged at all (Azure returned an empty/filtered
  response for this prompt — check `finish_reason` for `content_filter`)
- Content chunks logged but still 0 bytes forwarded (bug is actually on
  Deepgram's parsing side of the SSE, or the `Authorization`/`Content-Type`
  Deepgram sends back to us, or something in how `X-Accel-Buffering`/ngrok
  buffers the stream — needs a raw look at what ngrok/ngrok inspector shows
  went out)
- An exception logged mid-stream (silent truncation — the actual bug, fix
  would be handling whatever's raising)

Remove this diagnostic logging once root cause is confirmed and fixed —
it's intentionally verbose (logs every chunk) and not meant to stay.

### UPDATE 2 — root cause found via diagnostic log, fix applied, NOT YET RE-TESTED

The diagnostic logging above was tested and gave a clean answer. For a real
question, Azure streamed a **complete, correct reply** — 73 chunks, 70 with
real content, ending `finish_reason='stop'` — logged in full in
`rag_backend`'s output. Yet Deepgram still produced `AgentAudioDone` with 0
bytes forwarded, and critically: **Deepgram never even logged a
`ConversationText`/`History` message for the assistant's reply** (it did for
the greeting, which bypasses our endpoint entirely). That's the tell — this
isn't a "Deepgram declined to speak the text" problem, it's "Deepgram never
successfully parsed the text out of our SSE stream in the first place."

**Root cause:** Azure OpenAI's streaming chunks attach an Azure-specific
`content_filter_results` field to **every** content-bearing delta, not just
the empty first metadata chunk (which the earlier fix already skips). This
field isn't part of the standard OpenAI chat-completion-chunk schema.
Forwarding Azure's raw `chunk.model_dump_json()` — which the Implementation
Plan and earlier code assumed was safe ("already matches OpenAI's chunk
shape, no manual reconstruction needed") — sends this extra field on every
chunk. Deepgram's custom-LLM parser is apparently strict enough to silently
discard chunks with unrecognized fields rather than erroring, which explains
a real 200 OK + fully correct content in our own logs while Deepgram acts as
if it received nothing.

**Fix applied in `_stream_reply_sse()` (`rag-backend/main.py`):** instead of
forwarding `chunk.model_dump_json()` verbatim, manually reconstruct a
minimal SSE payload per chunk with only
`id`/`object`/`created`/`model`/`choices[].index`/`choices[].delta.{role,content}`/
`choices[].finish_reason` — no Azure-specific extra fields. The verbose
per-chunk diagnostic logging from Update 1 was removed now that the content
pipeline is confirmed healthy; the try/except-with-logging around the
generator (to catch silent truncation) and the chunk/content-count summary
log were kept since they're cheap and still useful.

**This fix has NOT been tested yet.** Next session should start by:
1. Restarting `rag-backend`
2. Testing English voice: greeting, then a real question — confirm you
   actually **hear** a spoken reply this time (not just see it in logs)
3. If still silent, check whether `ConversationText role=assistant` now
   appears for the reply turn. If yes but still 0 bytes forwarded, the
   problem has moved to Deepgram's speak step itself (worth checking the
   reply text for anything TTS-hostile — the sample answers contain markdown
   bold (`**1316**`) and em dashes, which a plain OpenAI-compatible think
   endpoint wouldn't normally need to sanitize, but worth ruling out). If
   `ConversationText` still never appears, look at the raw SSE bytes on the
   wire (e.g. via ngrok's inspector at `http://127.0.0.1:4040`) to see what
   Deepgram is actually receiving, since the strict-schema theory would then
   need re-examination.
4. Since this is the same shared `_stream_reply_sse()` used by the Tamil
   pipeline, re-verify Tamil voice replies still work end-to-end after this
   change (they should — the payload is standard-compliant, just leaner).

**Do not re-litigate Update 1's already-confirmed facts** (streaming works,
content generation works, retrieval works) — this update only changes what
gets forwarded to Deepgram, not how the reply is generated.

## Working commands for quick restart

```bash
# Terminal 1
cd rag-backend && venv\Scripts\activate && uvicorn main:app --reload --port 8000

# Terminal 2
cd tamil-voice-pipeline && venv\Scripts\activate && uvicorn server:app --reload --port 8001

# Terminal 3 (if not already running)
ngrok http 8000   # only if buffer-condense-valley.ngrok-free.dev isn't already up — check with curl first

# Terminal 4
cd widget && python -m http.server 8899
```
Then open `http://localhost:8899` in a browser.
