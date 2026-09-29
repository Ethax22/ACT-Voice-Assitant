# ACT AI — Session Handoff Summary

**Rewritten 2026-09-27** — the previous version of this file documented the
Deepgram English voice debugging saga (pre-Sarvam-pivot). That path was
retired 2026-09-25 (CLAUDE.md decision #1) and the saga is moot; its content
is preserved in git history (`git log -- docs/SESSION_HANDOFF.md`) if ever
needed, but nothing here carries it forward. This version reflects the
Sarvam-unified build through Phase 6, plus Phase 8 (lead capture), Phase 9
(sensitive-question handling plumbing), Phase 10 (logging), and Phase 7
(voice cloning code) built across this and a follow-up session.

This documents everything built, tested, and still open, for continuing in a
fresh session with full context. **Read `CLAUDE.md` and
`docs/Implementation_Plan_API_Based.md` first** — this file assumes that
context and only adds session-specific detail (what's actually verified vs.
not, exact commands, gotchas hit).

## What ACT AI is

A floating widget ("ACT AI") for Agni College of Technology's website,
answering visitor questions about admissions/courses/placements/campus life
via typed chat or voice (English **and** Tamil, both on Sarvam), all backed
by one shared RAG backend so answers stay consistent across every input
mode. Language auto-detects by default, with a manual override.

## Build status by phase (Phases 1–6 done; 8 built this session; 9 is next code work)

| Phase | Status |
|---|---|
| 1. LLM → Sarvam 105B | ✅ Done, verified live (typed chat + streaming both tested) |
| 2. Retire Deepgram English path | ✅ Done, verified (`/voice/en` on rag-backend now 404s; `voice_agent.py` deleted) |
| 3. Generalize voice pipeline (`voice-pipeline/`) | ✅ Done, verified (both `/voice/en` and `/voice/ta` build and route correctly) |
| 4. English turn-taking/barge-in | 🟡 Code confirmed identical for both languages (shared `build_pipeline()`); tunable knobs pre-wired (`VAD_TUNING` in `pipeline.py`) — **real by-ear mic test not done** |
| 5. Re-verify both languages + greeting | 🟡 Greeting wired up and verified live (real TTS audio confirmed for both languages); found & fixed a real bug (`amelia` isn't a valid Sarvam voice — switched to `priya`); also fixed a `PipelineTask`→`PipelineWorker` deprecation — **native-speaker voice pick (`kavya`/`priya`) not confirmed by ear** |
| 6. Automatic language detection | 🟡 Fully built: typed chat (script detection) verified live in-browser; voice detection (`/detect-language` on voice-pipeline, batch STT `saaras:v4`/`unknown`) verified with synthesized TTS test clips (100% accuracy, ~0.7s) — **not yet tested with a real visitor mic** |
| 7. Voice cloning | 🟡 Code built and verified live (see below) — deferred, not a launch blocker. Needs consented staff voice samples (left to Prakash) before it's actually usable. Also surfaced a real architecture correction the Implementation Plan had wrong — see below. |
| 8. Lead capture | 🟡 Built this session, code-verified live (see below) — **not yet wired to a real Google Sheet** |
| 9. Sensitive-question handling | 🟡 Plumbing built this session, code-verified live (see below) — **content is still draft placeholder text, needs admissions/faculty sign-off** |
| 10. Logging | 🟡 Built this session (see below) — typed chat verified live end-to-end; voice wiring confirmed to link correctly but **not yet run with a real audio call** |
| 11. Consistency QA | ⚪ Not started — **next actionable phase** |
| 12. Deployment | ⚪ Not started (dev-only, via ngrok) |

**The three 🟡 voice-verification items (4-6) all share the same blocker:
they need a human with a real microphone**, not more code. Everything
code-side that could be verified without one has been. If you're picking
this up fresh and have 10 minutes, doing that live test (open the widget,
try "Let's Talk" in both languages, listen to the voices, try interrupting
mid-reply) closes out Phases 4–6 for good. See "What to test live" below.

### Phase 7 (voice cloning) — what was built, 2026-09-27

Prakash asked to start this phase, recording real staff samples himself and
having the code ready for whenever those exist. Before writing any
integration code, I tested the actual mechanism live against Sarvam's real
API instead of trusting the Implementation Plan's assumption or Sarvam's own
docs/playground code sample — this uncovered a real architecture problem:

**The plan's "simple parameter swap" assumption is wrong.** A cloned
voice_id (`svc-{uuid}`, from `POST /voices/create`) is rejected by both the
REST `/text-to-speech` endpoint AND its websocket equivalent
(`wss://api.sarvam.ai/text-to-speech/ws`, what `pipeline.py`'s
`SarvamTTSService` actually uses for stock voices) with a 400/422 "Speaker
'svc-...' is not recognized" error — confirmed with three separate live
tests (REST, then a raw Python websocket client hitting the exact same URL
Pipecat uses). Cloned voices only work through the separate, non-streaming
`POST /voices/clone` endpoint. Also worth flagging: that endpoint's real
response uses the key `audio_b64`, not `audio` — which doesn't match what
Sarvam's own playground UI generates as sample code for that exact endpoint
(screenshotted by Prakash mid-session). Docs and vendor-generated samples
were wrong twice over on this one; only the live API calls were trustworthy.

**What got built to work around this correctly:**
- `voice-pipeline/cloned_voice_tts.py` (new): `ClonedVoiceTTSService`, a
  custom Pipecat `TTSService` subclass modeled on Pipecat's own
  `SarvamHttpTTSService` pattern (non-streaming batch call → decode base64
  WAV → yield one `TTSAudioRawFrame`), but pointed at `/voices/clone` with a
  `voice_id` instead of `/text-to-speech` with a stock `speaker`. Hit one
  more live-only bug while testing it: passing `speech_sample_rate=24000`
  (an explicitly documented-valid value) got a 400 anyway — a genuine API
  quirk. Worked around by omitting the field entirely and relying on
  Sarvam's own default (also 24000), rather than fighting it.
- `voice-pipeline/pipeline.py`: added `CLONED_VOICE_IDS` (from
  `CLONED_VOICE_ID_EN` / `CLONED_VOICE_ID_TA` env vars). `build_pipeline()`
  now swaps in `ClonedVoiceTTSService` for a language only if that language
  has a cloned voice_id set — otherwise behavior is byte-for-byte the same
  as before this phase. The aiohttp session `ClonedVoiceTTSService` needs is
  opened only in that branch and closed via a new `on_client_disconnected`
  handler.
- `voice-pipeline/create_cloned_voice.py` (new): the actual onboarding tool
  for Prakash — `python create_cloned_voice.py <audio_file> <en|ta>` once he
  has a real recording. Wraps `POST /voices/create`, prints the resulting
  `voice_id` and the next step (paste into `.env`, restart the service — no
  code change needed after that).
- **Verified live, start to finish**, using a synthetic stand-in reference
  clip (Sarvam's own stock `priya` voice — no real staff recording exists
  yet, so this only proves the mechanism, not final voice quality):
  1. Generated a 17s synthetic reference clip via the existing stock TTS.
  2. Ran `create_cloned_voice.py` on it — got back a real `voice_id`.
  3. Called `ClonedVoiceTTSService.run_tts()` directly against the live API
     — got back a real ~4s `TTSAudioRawFrame` of actual cloned-voice audio.
  4. Sent the reference clip and the cloned output to Prakash as WAV files
     so he could listen and judge the round-trip himself (I have no
     mic/speakers, same limitation as every other by-ear test in this repo).
  5. Dry-constructed the full pipeline graph with `CLONED_VOICE_ID_EN` set —
     confirmed `ClonedVoiceTTSService` correctly replaces `SarvamTTSService`
     in the linked chain, with everything else (STT, LLM, logging,
     transport) unchanged.
  6. Deleted every test voice created on Sarvam's side afterward — nothing
     left over from this testing.

**Still needed before this phase is actually done** (explicitly Prakash's,
not code work):
- Record consented staff voice samples, both languages.
- Run `create_cloned_voice.py` on each, put the resulting `voice_id`s into
  `voice-pipeline/.env` (`CLONED_VOICE_ID_EN` / `CLONED_VOICE_ID_TA`).
- A real by-ear listening test of the actual staff clone (same category as
  the Phase 4-6/Phase 10 mic-test blocker) — lock the final voice ID per
  language once it sounds right.
- A conscious decision on whether the added per-utterance latency (a full
  request/response round-trip instead of streamed audio) is acceptable for
  whichever language ends up using a cloned voice — this is a real product
  tradeoff the original plan didn't know it was making, not something to
  wave through silently.

### Phase 8 (lead capture) — what was built, 2026-09-27

- `rag-backend/main.py`: added a `capture_lead` OpenAI-style tool
  (`CAPTURE_LEAD_TOOL`), passed on every `chat.completions.create` call
  (`tools=[CAPTURE_LEAD_TOOL]`). The persona prompt now instructs the model to
  ask for name/phone (and course/email if the conversation allows) once
  clear admission intent shows up, then call the tool once it has at least
  name+phone.
  - `_generate_reply` (typed chat, non-streaming): if `finish_reason ==
    "tool_calls"`, resolves the call(s), then re-calls the API once more for
    the model's actual natural-language confirmation. **Gotcha hit**: Sarvam
    400s with `"Tool messages found but no tools provided"` if `tools` is
    omitted on that follow-up call — it's required on *any* call whose
    message history contains tool/tool_calls entries, not just the first
    one. Both calls now pass `tools=`.
  - `_stream_reply_sse` (voice path): accumulates streamed `tool_calls`
    deltas (they arrive fragmented across chunks — id/name/arguments each
    build up incrementally) without forwarding them downstream, since
    Pipecat's `OpenAILLMService` has no `capture_lead` function registered on
    its side and forwarding a raw tool-call chunk would either error or be
    silently ignored there. Once the stream ends, if a tool call was seen,
    resolves it and starts a second stream for the follow-up reply — the
    voice pipeline only ever sees plain content deltas, unchanged from
    pre-Phase-8 behavior.
  - Language tag on the lead comes from `_detect_language()`, a small
    server-side Tamil-Unicode-block check over the conversation's user
    messages — voice-pipeline's system content is ignored by design (see
    `pipeline.py`'s own comment on this), so there's no explicit language
    field arriving from that path to read instead.
- `rag-backend/leads.py` (new): `save_lead(...)` appends a row to a Google
  Sheet via `gspread` + a service account. Deliberately fails soft: if
  `GOOGLE_SERVICE_ACCOUNT_JSON`/`LEADS_SHEET_ID` aren't set, or the Sheet
  can't be opened, it logs the lead and returns `False` instead of raising —
  RAG answers must keep working even with lead capture unconfigured (true in
  this dev environment right now). Writes a header row on first real use if
  the sheet is empty.
- `rag-backend/requirements.txt`: added `gspread==6.1.4`,
  `google-auth==2.37.0` (installed into `rag-backend/venv`, confirmed
  importable).
- `rag-backend/.env` / `.env.example`: added `GOOGLE_SERVICE_ACCOUNT_JSON`
  (path to a service account key file) and `LEADS_SHEET_ID` — both currently
  blank in `.env`, so lead capture is presently a logged no-op.
- **Verified live** (server run locally, port 8000): a typed-chat conversation
  giving name+phone+course triggered `capture_lead`, logged the parsed lead,
  and the model's own next reply naturally confirmed it. A streaming
  `/v1/chat/completions` call (same scenario, `source="voice"`) confirmed
  `tool_calls=True` on the first stream (zero content chunks leaked), then a
  clean second stream with a natural confirmation and a proper `[DONE]`.
- **Not yet done — needs Prakash**: create a Google Cloud service account,
  enable the Sheets API, share a real target Sheet with that service
  account's email (Editor access), and set `GOOGLE_SERVICE_ACCOUNT_JSON` +
  `LEADS_SHEET_ID` in `rag-backend/.env`. Once set, re-run the same live test
  and confirm a row actually lands in the Sheet.
- Not addressed this session (fits later in Phase 8/9, not blocking): no
  explicit test yet for the Tamil voice path's lead capture (the language
  detection and tool-resolution code is language-agnostic, but hasn't been
  exercised with an actual Tamil conversation).

### Phase 9 (sensitive-question handling) — what was built, 2026-09-27

Per the Implementation Plan, this phase needs approved talking points from
admissions/faculty for known tricky questions (department-vs-placement
comparisons, etc.) — that content can't be improvised by the model or by me;
it has to come from the college. Since it wasn't available yet this session,
what got built is the **plumbing**, with clearly marked draft content
Prakash asked for so the structure could be tested end-to-end:

- `rag-backend/knowledge_base/sensitive_topics.md` (new): seven draft
  "Talking point:" Q&A entries covering the common sensitive categories
  (department-vs-placement comparisons, fee comparisons vs other colleges,
  NIRF ranking, comparisons to bigger-name colleges, negative online
  reviews, placement-number skepticism, faculty-student ratio). No code
  change was needed for the retriever to pick it up — `retriever.py` already
  globs every `*.md` in `knowledge_base/`, exactly as the plan said. Every
  unfinished answer has an inline `[ADMISSIONS: ...]` bracketed note marking
  exactly what real wording needs to go there — **this file is not launch
  content, it's a template Prakash/admissions needs to fill in.**
- `rag-backend/main.py`'s `SYSTEM_PROMPT_TEMPLATE`: added a rule telling the
  model to treat any retrieved context section starting with "Talking
  point:" as admissions/faculty-approved phrasing to use close to verbatim,
  rather than improvising its own comparison — that's the actual mechanism
  requested ("prefer these talking points verbatim... over improvising").
  Also added a safety rule so the model never reads the `[ADMISSIONS: ...]`
  editor notes out loud to a visitor while the file is still in draft form —
  if a talking point is nothing but an unfinished bracket with no usable
  answer, it says it doesn't have a confirmed answer and points to
  admissions instead.
- **Verified live** (server run locally): "Is CSE really better than ECE for
  placements?" correctly pulled the CSE-vs-ECE talking point and answered
  from it (no bracket text leaked, no fabricated claims). "What is ACT
  college NIRF ranking?" correctly recognized that talking point is
  unfinished and said so honestly, pointing to the admissions hotline,
  instead of guessing a rank.
- **Not yet done — needs Prakash + admissions/faculty**: replace every
  `[ADMISSIONS: ...]` bracket in `sensitive_topics.md` with the actual
  approved wording (or confirm "redirect to admissions" is the intended
  final answer for a given question) before this phase can be called done.
  The mechanism is proven; the content is not real yet.

### Phase 10 (logging) — what was built, 2026-09-27

Decided: local disk, 90-day retention, auto-pruned (CLAUDE.md/decisions
log). Split into two independent implementations since typed chat and voice
run in different services:

- `rag-backend/conversation_logger.py` (new): `log_turn(session_id,
  messages, reply)` writes `logs/<date>/<session_id>/transcript.json`,
  overwriting with the full message history each call — safe because the
  widget already resends the complete conversation every request
  (CLAUDE.md decision #3), so the newest write is always the authoritative
  transcript and a mid-session crash still leaves the last completed turn on
  disk. `prune_old_logs()` deletes any `logs/<date>/` folder older than 90
  days; wired into `main.py` as a FastAPI `startup` hook rather than a
  running scheduler — good enough for a once-a-day housekeeping task.
  - `main.py`'s `WidgetChatRequest` gained an optional `session_id` field;
    `widget/index.html` now generates one `crypto.randomUUID()` per browser
    tab (`chatSessionId`) and sends it with every `/widget/chat` call so
    turns group into one transcript file per visitor session.
  - **Verified live**: a real request with a `session_id` produced a correct
    `transcript.json`; a synthetic old-dated folder was correctly deleted by
    `prune_old_logs()` while the current day's folder was left alone.
- `voice-pipeline/conversation_logger.py` (new): two pieces.
  - `TranscriptLogger`, a small custom Pipecat `FrameProcessor` placed as the
    *last* stage in `pipeline.py`'s pipeline (after `assistant_aggregator`).
    It doesn't parse frame content itself — it just watches the shared
    `LLMContext.messages` list (the same one `user_aggregator`/
    `assistant_aggregator` already maintain) and re-dumps the whole thing to
    `transcript.json` whenever its length changes, so a transcript persists
    incrementally rather than only at the very end of a call.
  - Audio logging reuses Pipecat's own built-in `AudioBufferProcessor`
    (`pipecat.processors.audio.audio_buffer_processor`) rather than writing
    a custom audio tap — it already handles resampling, mixing, and
    silence-gap filling between the user and bot tracks. **Placement
    matters**: it's inserted right after `transport.output()` (not earlier),
    because (confirmed by reading `pipecat/services/stt_service.py`)
    `STTService.process_frame` forwards `InputAudioRawFrame` downstream
    after transcribing it, so user audio is still visible at that position,
    while `OutputAudioRawFrame` (bot speech) originates at `tts`, immediately
    upstream of it — so that one position sees both tracks. Configured with
    `buffer_size=0, auto_start_recording=True`: recording starts
    automatically at pipeline start and flushes once, on
    `stop_recording()`, which `AudioBufferProcessor` itself already calls
    automatically on `CancelFrame`/`EndFrame` (i.e. whenever the pipeline
    ends) — no manual teardown hook needed. Its `on_audio_data` event
    handler calls `save_session_audio()`, which writes the merged (single
    mixed track, not separate per-speaker tracks — a deliberate v1
    simplification) audio as one `audio.wav` per session.
  - `server.py`: generates one `uuid.uuid4().hex` session id per websocket
    connection (`/voice/en` and `/voice/ta` both go through
    `_run_voice_session`) and passes it into `build_pipeline(...,
    session_id=...)`; also gained the same `prune_old_logs()` startup hook
    as rag-backend.
  - **Verified**: `python -m py_compile` on the changed files, plus a dry
    (no real websocket, no real audio, dummy env vars) call to
    `build_pipeline()` that confirmed the whole graph constructs and links
    without error — logs show `AudioBufferProcessor` and `TranscriptLogger`
    both correctly wired into the chain at the intended positions.
  - **Not yet done — needs a human with a real mic** (same category as
    Phases 4-6): an actual voice call hasn't been run yet to confirm
    `audio.wav` sounds correct and `transcript.json` matches what was really
    said. Do this alongside the Phase 4-6 live test — same session, same
    "open the widget, talk to it" setup.

## Repo layout (current)

```
ACT-Voice-Assistant/
├── CLAUDE.md, docs/ (PRD, Implementation Plan, Voice_Model_Choices.md, this file)
├── rag-backend/       — FastAPI shared brain (port 8000). LLM: Sarvam 105B.
│   ├── main.py         — /v1/chat/completions, /widget/chat, /health
│   └── retriever.py    — embeddings stay on Azure OpenAI (unaffected by the LLM migration)
├── voice-pipeline/    — renamed from tamil-voice-pipeline/ (Phase 3). Sarvam/Pipecat, port 8001.
│   ├── pipeline.py      — build_pipeline(websocket, language="en"|"ta")
│   ├── server.py        — /voice/en, /voice/ta (websocket), /detect-language (POST), /health
│   └── language_detection.py — Phase 6, batch STT-based language auto-detect
└── widget/index.html  — the widget itself (navy/gold theme, EN/TA toggle + auto-detect)
```

Both Python services (`rag-backend`, `voice-pipeline`) have their own
`venv/` — do not share a global Python environment between them.

## Environment / infra facts to know

- `rag-backend/.env` and `voice-pipeline/.env` hold real secrets. As of the
  Sarvam pivot, the **only** vendor key is `SARVAM_API_KEY` (present in both
  files) plus a shared `RAG_BACKEND_AUTH_KEY` (must be identical in both).
  `rag-backend/.env` also still carries `AZURE_OPENAI_*` vars — those are for
  **embeddings only** now (`retriever.py`), not chat. `DEEPGRAM_API_KEY` is
  gone from both files.
- `RAG_BACKEND_PUBLIC_URL` = an ngrok tunnel to `rag-backend`
  (`https://buffer-condense-valley.ngrok-free.dev`, a static/reserved
  free-tier domain — doesn't change across restarts). `voice-pipeline` calls
  back into this URL for every LLM turn on both `/voice/en` and `/voice/ta`.
  Verify with `curl https://buffer-condense-valley.ngrok-free.dev/health`
  before debugging anything voice-related.
- Sarvam model IDs in use: LLM `sarvam-105b` (reasoning disabled via
  `extra_body={"reasoning_effort": None}` — see gotcha below), STT
  `saaras:v3-realtime` (realtime, fixed by Pipecat) and `saaras:v4` (batch,
  used only for language detection), TTS `bulbul:v3` with voice `kavya`
  (Tamil) / `priya` (English) — both placeholders pending a real listening
  test.

## Gotchas hit this build (still relevant, don't re-litigate)

1. **`sarvam-105b` has reasoning ON by default** (`reasoning_effort:
   "medium"`) and will burn the entire token budget on `reasoning_content`
   before any real answer, returning `finish_reason: "length"` and
   `content: null`. Must disable it. **The OpenAI Python SDK silently drops
   `reasoning_effort=None` as a plain kwarg** (treats `None` as "field not
   provided," not "send JSON null") — use `extra_body={"reasoning_effort":
   None}` instead, confirmed necessary by comparing raw curl vs. SDK calls.
2. **Sarvam's streaming chunks are clean, standard OpenAI shapes** — unlike
   Azure's (which needed manual reconstruction to strip
   `content_filter_results`). `_stream_reply_sse()` in `rag-backend/main.py`
   now forwards chunks directly.
3. **Sarvam's own `SarvamTTSSpeakerV3` enum (in the installed `pipecat-ai`
   1.10.0 package) is stale** — `amelia` (from that enum) gets a live 400
   from Sarvam's actual API. Don't trust that enum for picking a voice;
   verify against a live call or current docs.
4. **Sarvam has no dedicated audio Language Identification API** — despite
   what the Implementation Plan originally assumed. Its real LID endpoint
   (`/text-lid`) is text-only. For audio, the realtime STT's own
   `language_code="auto"` is unreliable (Tamil audio came back
   transliterated as English); the **batch** STT endpoint (`POST
   /speech-to-text`, `model: saaras:v4`, `language_code: unknown`) is fast
   (~0.6–0.8s) and accurate — that's what `language_detection.py` uses.
5. **`PipelineTask` is deprecated since Pipecat 1.3.0** in favor of
   `PipelineWorker` — same drop-in signature, `PipelineRunner.run()` accepts
   either. Already migrated in `pipeline.py`/`server.py`.
6. **Windows + git-bash + background uvicorn processes**: `pkill -f
   "uvicorn ..."` does **not** reliably kill them — use `taskkill //PID
   <pid> //F` (find the PID via `netstat -ano | grep <port>`). A stale
   process silently squats on the port and every "restart" after it fails
   with `[Errno 10048]` while your test still (confusingly) succeeds against
   the OLD process.

## What to test live (needs an actual human + mic — I can't do this myself)

1. Start both services (commands below) and open `widget/index.html` for
   real (not the sandboxed preview pane — it blocks real network calls and
   has no mic).
2. Click "Let's Talk" in English — you should hear the greeting immediately
   (Phase 5), then ask a real question and get a spoken reply. Try
   interrupting mid-reply (Phase 4) — does barge-in feel responsive or does
   it cut you off too eagerly / feel laggy? If it's off, tune
   `VAD_TUNING` in `voice-pipeline/pipeline.py` (`threshold`,
   `silence_duration_ms`, `min_speech_duration_ms`, `prefix_padding_ms` —
   all currently `None`, i.e. Sarvam's own defaults).
3. Repeat for Tamil.
4. Listen to both voices (`priya` English, `kavya` Tamil) — do they sound
   right for ACT AI's persona? If not, swap `TTS_VOICES` in `pipeline.py`
   (valid speaker list is in the comment there, or in any 400 error Sarvam
   returns for an invalid one).
5. Test voice auto-detect (Phase 6) for real: click "Let's Talk" *without*
   touching the language toggle first, speak in Tamil — does it correctly
   open the Tamil pipeline? Same for English. Does the ~1.5s detection delay
   feel acceptable, or does it need shortening
   (`DETECTION_SAMPLES` in `widget/index.html`)?
6. Test the manual override: click a language toggle button, then speak the
   *other* language — it should NOT auto-switch (manual choice should win
   for the rest of the session).

Report back what you hear/notice and I'll make the code-side adjustment —
I don't have a mic/speakers, so this step can't be done from my side.

## Working commands for quick restart

```bash
# Terminal 1
cd rag-backend && venv\Scripts\activate && uvicorn main:app --reload --port 8000

# Terminal 2
cd voice-pipeline && venv\Scripts\activate && uvicorn server:app --reload --port 8001

# Terminal 3 (if not already running)
ngrok http 8000   # only if buffer-condense-valley.ngrok-free.dev isn't already up — check with curl first
```
Then open `widget/index.html` directly in a real browser (not this session's
sandboxed preview pane).

## Next actionable phase: 11 (Consistency QA)

Phases 4–6's remaining items, plus Phase 10's voice-side logging, are all the
same human-listening-test blocker — one real voice session closes out all of
them together (barge-in feel, voice pick, real-visitor language detection,
and now confirming audio.wav/transcript.json look right). Phase 8 (lead
capture) and Phase 9 (sensitive-question handling) are both built and
code-verified — Phase 8 needs a real Google Sheet provisioned, and Phase 9
needs admissions/faculty to fill in its draft talking points, before either
is fully "done" end-to-end. Phase 7 (voice cloning) stays deferred per
CLAUDE.md/decision #5 and isn't a launch blocker. The next phase that's
actually new *code* work (once the live mic test happens) is **Phase 11
(consistency QA across typed chat + both voice languages)** — see
`docs/Implementation_Plan_API_Based.md` §12.
