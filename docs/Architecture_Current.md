# Agni AI — Current Architecture (as built)

**Written 2026-10-08 from a full read of the code** (`rag-backend/`, `voice-pipeline/`,
`widget/`), not from the planning docs. Where this file and
`Implementation_Plan_API_Based.md` disagree, this file describes what the code does.
Items marked *unverified* are claims in code comments that were not re-tested when this was written.

## 1. Overview

```
              widget/index.html  (static page, vanilla JS)
   typed chat │        voice: mic PCM16 16 kHz        │  pre-connect detect (OFF)    
              │                  │                     │
   POST /widget/chat      WS /voice/en | /voice/ta     POST /detect-language
              │                  │                     │
              ▼                  ▼                     ▼
   ┌────────────────────┐   ┌──────────────────────────────────────┐
   │ rag-backend :8000  │◄──┤ voice-pipeline :8001  (Pipecat)      │
   │ FastAPI            │   │ Sarvam STT → router → LLM step →     │
   │ /v1/chat/completions   │ Sarvam TTS (stock or cloned voice)   │
   └─────────┬──────────┘   └──────────────────────────────────────┘
   Sarvam 105B (chat) · Azure OpenAI (embeddings only) · Google Sheets (leads, knowledge) · local disk (logs)
```

One brain: typed chat, English voice, Tamil voice and the Calling Agent
(`POST /calling-agent/ask`) all run `_build_full_messages` → Sarvam 105B in
`rag-backend/main.py`. The voice pipeline's "LLM" is an `OpenAILLMService`
pointed at rag-backend's `/v1/chat/completions`. No vendor key is ever in browser code.

## 2. rag-backend (FastAPI, Python)

**Stack:** FastAPI + uvicorn; `openai` SDK against `https://api.sarvam.ai/v1`
(model `sarvam-105b`, reasoning disabled via `extra_body={"reasoning_effort": None}`);
numpy cosine search; tiktoken; `AsyncAzureOpenAI` (`text-embedding-3-small`) for
embeddings only; gspread + google-auth; openpyxl + pdfplumber for data files.

| Route | Auth | Notes |
|---|---|---|
| `POST /widget/chat` | none (CORS: POST only) | typed chat, non-streaming, logs transcript |
| `POST /v1/chat/completions` | bearer | OpenAI-compatible; SSE if `stream: true`; voice-pipeline calls this with `X-Session-Id` |
| `POST /calling-agent/ask` | bearer | single-turn; source `phone` |
| `POST /admin/reload-knowledge` | bearer | re-ingest markdown, data files, knowledge sheet |
| `GET /health` | none | |

**Per-turn flow (`_build_full_messages`)**
1. `developer` role → `system` (Sarvam rejects `developer`).
2. Reply style decided in code (`_reply_language`), one of `en` / `ta` / `ta_en`
   (*updated 2026-10-09*): the `reply_language: xx` marker from voice-pipeline's persona
   message if present (then removed), else `language_style.detect_style` on the latest
   user turn that shows a style (Tamil script → `ta`, romanized Tamil words such as
   "evlo", "iruka", "epdi" → `ta_en`, plain English → `en`; a bare "okay" inherits).
   `ta_en` replies are spoken Chennai Tamil/English in English letters (`TANGLISH_STYLE`);
   `ta` replies are spoken Tamil mostly in Tamil script with everyday English words left
   in English (`TAMIL_STYLE`). The old rule that forced pure Tamil script is gone.
3. Query choice: `_retrieval_query` (short turns reuse the last substantive question);
   "ack" turns ("yes", "சரி") search on the assistant's last question; greetings,
   closings and bare acknowledgements skip retrieval entirely.
4. Tamil queries are translated to English first (`_english_search_query`, Sarvam 105B,
   2 s timeout, cached) because the knowledge base is English.
5. `retriever.search` (see below). A retrieval failure answers without context.
6. Prompt = system (persona + rules + context + voice brevity / Tamil style /
   conversation-state block) + full message history + a final system message
   (voice reminder or closing/ack instruction, language rule, facts rule, already-said rule).
7. Generation with the `capture_lead` tool; at most one tool round (`_resolve_tool_calls`:
   phone validated, duplicate-per-session check, row appended to Google Sheets).
   On the streaming path tool-call chunks are swallowed server-side, so Pipecat only
   sees content deltas.

**Retriever (`retriever.py`)** — in-memory numpy index, no vector DB. Markdown under
`knowledge_base/` (curated files + the `website/` mirror of act.edu.in from
`scrape_website.py`), plus `knowledge_base/data/*.csv|xlsx|pdf` and an optional Google
knowledge sheet (`KNOWLEDGE_SHEET_ID`, tabs starting `_` skipped), via `data_loader.py`.
~500-token chunks with 50 overlap; embeddings cached on disk (`.embedding_cache.npz`)
and query embeddings cached in memory; warmed at startup. Search returns the top 4
curated chunks first, then the top 3 website chunks (separate pools so the large
website mirror cannot bury curated answers).

**Other modules:** `conversation_state.py` (derives course/lead-stage facts from the
history; in-memory "lead captured" per session id), `leads.py` (Sheets append, Indian
mobile validation, IST timestamp), `conversation_logger.py` (typed chat transcript to
`logs/<UTC date>/<session>/transcript.json`, overwritten each turn; 90-day prune at
startup), `sentiment_report.py` (manual: `--days` / `--from` / `--to`; Sarvam 105B labels
each saved conversation from both log dirs; HTML + CSV in `reports/`; labels not validated).

## 3. voice-pipeline (Pipecat, Python)

**Stack:** `pipecat-ai[sarvam,openai,websocket]` (1.12.x per code comments), FastAPI +
uvicorn, aiohttp, Silero VAD and smart-turn v3 (local). STT `saaras:v4`
(`SarvamRealtimeSTTService`), TTS `bulbul:v3` (`SarvamTTSService`).

**Server (`server.py`):** `WS /voice/en`, `WS /voice/ta` (origin-checked; one uuid session
per socket), `POST /detect-language` (raw PCM16 body), `GET /health`. Startup: prune logs,
pre-render greeting clips.

**Pipeline (`pipeline.py`, one `build_pipeline(websocket, language, session_id)`):**
```
transport.input (FastAPIWebsocket + RawPCMSerializer, 16 kHz in / 24 kHz out)
 → SarvamRealtimeSTT
 → ConversationRouter
 → user aggregator (local VAD + turn strategies)
 → OpenAILLMService (base_url = rag-backend /v1, X-Session-Id header)
 → TTS (ONE service for the whole call: the cloned About Us voice if configured, else the stock About Us voice)
 → GreetingPlayer → transport.output
 → AudioBufferProcessor → assistant aggregator → TranscriptLogger → LatencyLogger
```

**Turn-taking (`turn_taking.py`, `TURN_MODE=local` default).** Silero VAD runs locally.
Sarvam STT keeps its own segmentation but with a short silence window (300 ms) so the
final transcript is released quickly; it must not be set to manual endpointing (it lost
the first words). Start strategy: when the bot is idle any speech starts the turn; while
the bot is speaking (or a reply is pending, up to 6 s) the visitor must speak ~0.4 s
continuously or say more than a backchannel ("okay", "சரி"…), which is dropped. Stop
strategy: English = smart-turn v3 (max wait 1.5 s); Tamil = plain 0.5 s silence timeout
(smart-turn's language list excludes Tamil; `TURN_STOP_TA=smart` overrides).
`TURN_MODE=sarvam` restores Sarvam server-side turns (silence 700 ms, VAD threshold 0.5).
None of these timings is tuned on a real microphone yet.

**ConversationRouter (`conversation_router.py`)** *(rewritten 2026-10-09)*.
- Never waits. The transcript goes to the LLM as soon as it is final.
- Reply style per turn: `style_classifier.py` (Tamil Unicode, a romanized-Tamil word
  list, English word counts; no network) gives `en` / `ta` / `ta_en`; a bare "okay"
  keeps the call's current style. On a change it pushes the matching persona
  (`PERSONA_PROMPTS` in `pipeline.py`) and the TTS language (`ta_en` speaks with
  `TA_EN_TTS_LANGUAGE`, default `ta-IN`).
- Topic: the keyword classifier (about-us / admissions / courses / placements) only sends
  the browser `{"type":"topic"}` so the avatar tile highlights. It does not change the voice.
- Language switching mid-call: when the local VAD reports end of speech, a background
  batch detection runs on that utterance (≥1 s). The result is applied when it arrives
  (it can still land before the LLM request is built, as it did in the first-turn Tamil
  test) and otherwise from the next turn. Tamil script on an `en` call switches at once
  with no API call. Spoken phrases ("speak Tamil", "in English") also switch. A switch
  sends live STT / persona / TTS-language settings-update frames (no reconnect) and does
  NOT touch the voice. Known cost: Tamil spoken to an English-locked STT can arrive as a
  fluent wrong English sentence, so a Tamil first sentence may be answered from bad text.
- `maybe_auto_switch_language` is unused (no caller).

**Language detection (`language_detection.py`):** Sarvam batch STT `POST /speech-to-text`
(`saaras:v4`, `language_code=unknown`), ~0.6–0.8 s. Realtime STT `auto` was tested and found
unreliable. Used for the widget's start-of-call snippet and for mid-call detection.

**Voices.** One voice per call (*2026-10-09*): the About Us voice. Default: the Sarvam
stock voice `ritu`. Cloned: `CLONED_VOICE_ID_ABOUT_US` (cross-lingual, so it speaks every
style); `CLONED_VOICE_ID_ADMISSIONS|COURSES|PLACEMENTS` are no longer read. By default (`CLONED_VOICE_TRANSPORT=persistent`)
`PersistentClonedVoiceTTSService` keeps ONE websocket open for the call
(`wss://api.sarvam.ai/voices/clone/ws`, built on Pipecat's `SarvamTTSService`), so sentences
of a reply play without gaps. `CLONED_VOICE_TRANSPORT=per_utterance` selects the older
`ClonedVoiceTTSService`, which opens a websocket per sentence and falls back to the
non-streaming `POST /voices/clone` if the websocket fails before any audio. Both get the same
text filter and delivery settings as the stock voice. Cloned voice first
audio is ~1.3-2.8 s per reply even on the persistent connection (2026-10-09 test, 9 turns):
Sarvam's per-utterance start-up is not removed by keeping the websocket open. The stock-voice
buffer default is now `TTS_MIN_BUFFER_SIZE=40` (was 120) so a short first sentence is spoken
at once; the effect on smoothness is not yet judged by ear.
`greeting_cache.py` pre-renders each greeting to disk and plays it instantly through
`GreetingPlayer`; falls back to live TTS and renders for the next call.

**Other:** `speech_text_filter.py` (strips markdown symbols, pronunciation table, applied
as a TTS text transform), `raw_pcm_serializer.py` (binary PCM in/out; JSON for `topic` and
`UserStartedSpeaking` on interruption), `conversation_logger.py` (transcript rewritten
whenever the context grows; one mixed mono `audio.wav`; same layout and 90-day prune),
`latency_metrics.py` (`[latency]` log lines), `scenario_test.py`, `make_voice_samples.py`,
`make_clone_samples.py`, `create_cloned_voice.py`.

## 4. widget (`widget/index.html`, single file, vanilla JS)

- **Typed chat:** `fetch` to `/widget/chat` with the full history and a per-tab
  `session_id`. Widget wording is English only; the backend answers in the language of
  the message. No manual language toggle and no client-side language detection.
- **Voice:** "Let's Talk" → `getUserMedia` (echo cancel / noise suppression on) → opens
  `/voice/en` straight away; the in-call `ConversationRouter` switches to Tamil from the
  visitor's turns. *(Updated 2026-10-08)* The pre-connect step (record ~1.5 s, 5 s timeout,
  POST to `/detect-language`, open `/voice/en` or `/voice/ta`, replay the buffered snippet)
  is still in the widget but switched off by `DETECT_LANGUAGE_BEFORE_CONNECT = false`.
  Mic capture: AudioWorklet (~20 ms blocks), ScriptProcessor fallback, downsampled to
  PCM16 16 kHz. Playback: gapless scheduled 24 kHz chunks; `UserStartedSpeaking` stops
  queued audio (barge-in). The `AudioContext` is created synchronously in the click handler.
- **Topic tiles:** typed replies are classified client-side by keywords; during voice the
  server's `topic` message drives the highlight. The `ConversationText` / `AgentThinking`
  branches are dead code from the Deepgram era.
- **Dev wiring:** URLs are hard-coded to `localhost:8000` / `localhost:8001`; they must
  become configurable at deployment (Phase 12).

## 5. Configuration

`rag-backend/.env`: `SARVAM_API_KEY`, `RAG_BACKEND_AUTH_KEY`, `AZURE_OPENAI_API_KEY`,
`AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_VERSION`, `AZURE_OPENAI_EMBEDDING_DEPLOYMENT`,
`WIDGET_ALLOWED_ORIGINS`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `LEADS_SHEET_ID`, optional
`KNOWLEDGE_SHEET_ID`. `voice-pipeline/.env`: `SARVAM_API_KEY`, `RAG_BACKEND_PUBLIC_URL`,
`RAG_BACKEND_AUTH_KEY`, optional `CLONED_VOICE_ID_*`, `TTS_PACE`, `TTS_TEMPERATURE`,
`TTS_MIN_BUFFER_SIZE`, `TTS_MAX_CHUNK_LENGTH`, `TURN_MODE`, `TURN_STOP_TA`,
`BARGE_IN_SUSTAIN_SECS`, `TURN_SILENCE_TIMEOUT_SECS`, `SMART_TURN_MAX_WAIT_SECS`,
`STT_SEGMENT_SILENCE_MS`. No Deepgram key and no `AZURE_OPENAI_DEPLOYMENT` remain.

## 6. Known gaps (not code defects, verification still owed)

- No real-microphone verification of barge-in feel, turn-taking timings, voice choice, or detection accuracy.
- Tamil keywords, style rules and switch phrases are first-pass, not native-speaker checked.
- `sensitive_topics.md` still holds placeholder text pending admissions wording.
- Sentiment labels not validated against hand labels.
- Not deployed; dev uses localhost / ngrok.
