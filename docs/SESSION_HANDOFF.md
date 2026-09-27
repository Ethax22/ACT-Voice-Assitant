# ACT AI — Session Handoff Summary

**Rewritten 2026-09-27** — the previous version of this file documented the
Deepgram English voice debugging saga (pre-Sarvam-pivot). That path was
retired 2026-09-25 (CLAUDE.md decision #1) and the saga is moot; its content
is preserved in git history (`git log -- docs/SESSION_HANDOFF.md`) if ever
needed, but nothing here carries it forward. This version reflects the
Sarvam-unified build through Phase 6.

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

## Build status by phase (Phases 1–6 done; 7 is next)

| Phase | Status |
|---|---|
| 1. LLM → Sarvam 105B | ✅ Done, verified live (typed chat + streaming both tested) |
| 2. Retire Deepgram English path | ✅ Done, verified (`/voice/en` on rag-backend now 404s; `voice_agent.py` deleted) |
| 3. Generalize voice pipeline (`voice-pipeline/`) | ✅ Done, verified (both `/voice/en` and `/voice/ta` build and route correctly) |
| 4. English turn-taking/barge-in | 🟡 Code confirmed identical for both languages (shared `build_pipeline()`); tunable knobs pre-wired (`VAD_TUNING` in `pipeline.py`) — **real by-ear mic test not done** |
| 5. Re-verify both languages + greeting | 🟡 Greeting wired up and verified live (real TTS audio confirmed for both languages); found & fixed a real bug (`amelia` isn't a valid Sarvam voice — switched to `priya`); also fixed a `PipelineTask`→`PipelineWorker` deprecation — **native-speaker voice pick (`kavya`/`priya`) not confirmed by ear** |
| 6. Automatic language detection | 🟡 Fully built: typed chat (script detection) verified live in-browser; voice detection (`/detect-language` on voice-pipeline, batch STT `saaras:v4`/`unknown`) verified with synthesized TTS test clips (100% accuracy, ~0.7s) — **not yet tested with a real visitor mic** |
| 7. Voice cloning | ⚪ Not started — deferred, not a launch blocker |
| 8. Lead capture | ⚪ Not started — **next actionable phase** |
| 9. Sensitive-question handling | ⚪ Not started |
| 10. Logging | ⚪ Not started |
| 11. Consistency QA | ⚪ Not started |
| 12. Deployment | ⚪ Not started (dev-only, via ngrok) |

**The three 🟡 items all share the same blocker: they need a human with a
real microphone**, not more code. Everything code-side that could be
verified without one has been. If you're picking this up fresh and have 10
minutes, doing that live test (open the widget, try "Let's Talk" in both
languages, listen to the voices, try interrupting mid-reply) closes out
Phases 4–6 for good. See "What to test live" below.

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

## Next actionable phase: 8 (Lead capture)

Phases 4–6's remaining items are all the human-listening-test blocker above.
The next phase that's actually *code* work is **Phase 8 (lead capture)** —
Phase 7 (voice cloning) is explicitly deferred per CLAUDE.md/decision #5 in
the Implementation Plan and isn't a launch blocker. Phase 8 needs a Google
Cloud service account + Sheets API key before any code — see
`docs/Implementation_Plan_API_Based.md` §9 for the full spec.
