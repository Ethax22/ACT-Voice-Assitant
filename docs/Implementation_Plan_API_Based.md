# Implementation Plan: ACT AI Voice Assistant (Sarvam-Unified Path)

**Companion to:** PRD_API_Based_Voice_Assistant
**Author:** Prakash D (Ethax)
**Date:** September 2026 — **rewritten 2026-09-25** to reflect the pivot to a single
shared vendor (Sarvam) for both languages' voice AND the LLM, replacing the
original Deepgram(English)/Sarvam(Tamil)/Azure-OpenAI(LLM) split. See
`CLAUDE.md`'s locked-in decisions #1 and #6 for why. This version also folds
in the additional v1 scope from the Website Voice Bot doc (voice cloning,
lead capture, sensitive-question handling, logging) that wasn't in the
original plan, and reflects the actual current build state instead of
assuming a blank slate.

Code below is illustrative — treat variable names, package names, and exact
SDK calls as a starting point to verify against current docs, not
copy-paste-final code.

---

## 0. Current build status (read this before starting any phase)

| Area | Status |
|---|---|
| RAG backend core (FastAPI, retrieval, persona, streaming) | ✅ Done — `rag-backend/main.py`, `retriever.py` |
| Typed chat in widget | ✅ Done — `/widget/chat`, now with script-based language auto-detect (Phase 6) |
| LLM | ✅ Migrated to Sarvam 105B (Phase 1, 2026-09-25) — reasoning disabled via `extra_body`, see Phase 1 notes |
| Embeddings | ✅ Staying on Azure OpenAI (`text-embedding-3-small`) — separate from the LLM, kept as-is since Sarvam has no public embeddings API yet |
| English voice (Deepgram) | ✅ Retired (Phase 2, 2026-09-25) — `voice_agent.py` deleted, `/voice/en` moved to `voice-pipeline/` |
| English + Tamil voice (`voice-pipeline/`, Sarvam/Pipecat) | 🟡 Built and code-verified (Phases 3-6: generalized pipeline, greeting wired up, voice auto-detect) — still needs a real human listening/mic test (Phase 4 barge-in feel, Phase 5 voice pick), not yet done |
| Automatic language detection | 🟡 Built (Phase 6, 2026-09-25) — typed chat fully verified; voice detection verified with synthesized test clips only, not yet a real visitor mic |
| Voice cloning, lead capture, sensitive-question handling, logging | ⚪ Not started (Phases 7-10) |
| Deployment | ⚪ Not started (dev-only, via ngrok) |

Do not re-litigate anything marked ✅ or the historical bug fixes logged in
`docs/SESSION_HANDOFF.md` — that document's Deepgram-specific "OPEN ISSUE"
thread is now moot (Phase 2 retires that code rather than fixing it), but its
other fixes (streaming SSE format, sample-rate bug, async client fixes) are
still correct and still apply to the unified pipeline.

## 1. Repo structure (target state after Phase 3's rename)

```
ACT-Voice-Assistant/
├── CLAUDE.md
├── docs/
├── rag-backend/           # FastAPI — shared brain. LLM: Sarvam 105B (Phase 1).
│   ├── main.py
│   ├── retriever.py       # embeddings stay on Azure OpenAI — unchanged by the LLM migration
│   ├── leads.py           # new, Phase 8 — writes to Google Sheets
│   └── knowledge_base/
├── voice-pipeline/        # renamed from tamil-voice-pipeline/ (Phase 3) — one
│   │                        Pipecat service serving BOTH languages, parameterized
│   ├── pipeline.py        # by language instead of hardcoded to Tamil
│   ├── server.py          # exposes /voice/en AND /voice/ta
│   └── raw_pcm_serializer.py
└── widget/
    └── index.html
```

`rag-backend/voice_agent.py` (the Deepgram relay) is deleted in Phase 2, not
kept around unused.

## 2. Phase 1 — Migrate the LLM to Sarvam 105B

**Why now, first:** every other phase's answers depend on this; no point
re-verifying voice against a model that's about to change.

- Replace `AsyncAzureOpenAI` in `rag-backend/main.py` with a Sarvam
  chat-completions client (Sarvam's API is OpenAI-compatible per their docs —
  verify the exact base URL and payload shape against current Sarvam API
  reference before assuming a drop-in swap).
- `CHAT_DEPLOYMENT` becomes a fixed Sarvam model identifier for the 105B
  model (confirm exact model string in Sarvam's docs — naming may differ from
  "105B" in the API).
- **Only remove `AZURE_OPENAI_DEPLOYMENT`** (the chat deployment name) from
  `rag-backend/.env.example` and `.env` — add `SARVAM_API_KEY` for the new
  chat client (likely already present for voice). **Keep**
  `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_VERSION`,
  and `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` — decided 2026-09-26, embeddings
  stay on Azure OpenAI (see §0), so `retriever.py`'s own `AsyncAzureOpenAI`
  client (separate from `main.py`'s, which is what's being replaced here)
  keeps using them unchanged.
- **Validate the streaming fix still applies or is unnecessary**: the whole
  reason `_stream_reply_sse()` in `main.py` manually reconstructs a minimal
  SSE payload is that Azure attaches non-standard `content_filter_results`
  fields to every chunk. Sarvam's streaming format may not have this problem
  at all — test with real streaming calls before assuming the workaround is
  still needed; simplify back to forwarding chunks directly if Sarvam's shape
  is already clean.
- Re-run the existing test-question set through `/widget/chat` (typed path,
  no voice involved) to sanity-check answer quality before touching voice at
  all — this isolates "did the LLM swap work" from "did voice break."
- Per CLAUDE.md: validate Sarvam 105B's self-reported benchmark edge with
  real ACT questions (Phase 10 formalizes this further) — don't treat the
  vendor's numbers as final on faith.

## 3. Phase 2 — Retire the Deepgram English path

- Delete `rag-backend/voice_agent.py` and the `/voice/en` websocket route in
  `main.py` (`voice_en()`, `_origin_allowed` can stay if still used
  elsewhere).
- Remove `DEEPGRAM_API_KEY` from env files.
- Remove Deepgram-specific docs content from `docs/Voice_Model_Choices.md`'s
  English section (keep as historical record in git history, not as live
  guidance — replace with a pointer to the new unified Sarvam voice section
  from Phase 4).
- Widget (`widget/index.html`): remove the code path that opens a WebSocket
  to `/voice/en` on `rag-backend`; English voice now goes through the same
  kind of relay Tamil already uses (Phase 3).

## 4. Phase 3 — Generalize the voice pipeline to serve both languages

Rename `tamil-voice-pipeline/` → `voice-pipeline/` (CLAUDE.md explicitly
calls this out as worth doing once the pipeline stops being Tamil-only).

- Parameterize `pipeline.py`'s `build_pipeline()` by a `language: Literal["en", "ta"]`
  argument (or similar), driving:
  - `SarvamRealtimeSTTService`'s `language_code` (`en-IN` vs `ta-IN` — confirm
    Sarvam's exact English locale code)
  - `SarvamTTSService`'s `voice`/`language` settings (a different Bulbul voice
    per language, at least until Phase 7's cloning picks final voices)
  - The `PERSONA_PROMPT`/`GREETING` constants (currently Tamil-only text)
- `server.py`: expose both `/voice/en` and `/voice/ta` websocket routes,
  each calling `build_pipeline(websocket, language=...)`.
- Widget: point its English voice button/flow at this service's `/voice/en`
  instead of `rag-backend`'s now-deleted relay.
- Keep `RawPCMSerializer` and the sample-rate settings (`audio_in_sample_rate=16000`,
  `audio_out_sample_rate=24000`) — those were fixed for genuine technical
  reasons unrelated to language and apply to both.

## 5. Phase 4 — Manual turn-taking/interruption for English

Deepgram's Voice Agent handled end-of-turn detection and barge-in
automatically; Pipecat doesn't give you that for free. This is the accepted
tradeoff from CLAUDE.md decision #1.

- Confirm what Pipecat mechanism the Tamil path already relies on for
  turn-taking (VAD-based endpointing, `allow_interruptions=True` on
  `PipelineParams` — already set in `pipeline.py`) and verify by ear that it
  feels acceptable for English too, or needs tuning (VAD sensitivity,
  endpointing silence threshold) per-language.
- This phase is mostly **testing and tuning**, not new code — the mechanism
  is already built for Tamil; the work is verifying it holds up for English
  speech patterns and adjusting parameters if barge-in feels laggy or trigger-happy.

## 6. Phase 5 — Re-verify both languages end-to-end

- Full restart-and-test pass on the unified `voice-pipeline` service for
  both `/voice/en` and `/voice/ta`: greeting plays, real questions get
  spoken replies, follow-up questions correctly use conversation history.
- Wire up the greeting for whichever language doesn't have one yet (the
  Tamil `GREETING` constant was previously defined but never spoken — same
  fix needed for English's greeting under the new pipeline).
- Do the native-speaker listening test on Bulbul voice options for **both**
  languages now (previously only flagged for Tamil) — pick default voices
  per language before moving on.

## 7. Phase 6 — Automatic language detection ✅ Built 2026-09-25

Simpler now than under the old split-vendor design, since one STT vendor
(Sarvam) covers both languages.

**Correction (2026-09-25, verified live against current Sarvam docs and real
API calls):** the "dedicated Language Identification API" this section
originally assumed turned out to be **text-only** (`/text-lid`, 1000-char
limit) — there is no standalone audio LID endpoint. Two real options for
audio were tested by round-tripping synthesized English/Tamil clips through
each:
- `SarvamRealtimeSTTService`'s own `language_code="auto"` (the realtime
  streaming STT the pipeline already uses) — **unreliable**: Tamil audio came
  back transcribed as English (transliterated), not detected as Tamil.
- Sarvam's **batch** STT endpoint (`POST /speech-to-text`, `model:
  saaras:v4`, `language_code: unknown`) — fast (~0.6-0.8s) and accurate (both
  test clips detected at 1.0 confidence). This is what got built.

- **Typed chat:** detects Tamil vs. English directly from the input text's
  Unicode script client-side in `widget/index.html` (`detectScriptLanguage`,
  checking for the Tamil Unicode block), before calling `/widget/chat` — no
  vendor call needed. Auto-switches the UI (labels, placeholder, topic
  cards) unless the visitor has used the manual toggle.
- **Voice:** `voice-pipeline/language_detection.py` + a new `POST
  /detect-language` route in `voice-pipeline/server.py` (not on rag-backend —
  it's Sarvam-specific and voice-pipeline already holds `SARVAM_API_KEY`).
  The widget buffers ~1.5s of mic audio into the same PCM16 format it already
  uses for the realtime pipeline, POSTs it there, and opens `/voice/en` or
  `/voice/ta` based on the response — falling back to the current language on
  a low-confidence or failed detection rather than forcing a guess. The
  buffered snippet is flushed into the real pipeline connection once it opens
  (in `widget/index.html`'s `startVoiceSession`), so the visitor doesn't have
  to repeat whatever they said during detection.
- Manual EN/Tamil override kept in the widget UI (`languageManuallySet` flag)
  — clicking the toggle stops auto-detect from overriding that choice for the
  rest of the session, for both typed chat and voice.
- Not yet done: a real by-ear/by-hand test of detection latency and accuracy
  with an actual visitor mic, as opposed to synthesized test clips — that's
  the same category of human-verification gap as Phase 4's turn-taking test
  and Phase 5's voice pick.

## 8. Phase 7 — Voice cloning (both languages) — deferred, not a launch blocker

Decided 2026-09-26: v1 launches on Sarvam's **default stock voices** (already
selected in `pipeline.py`, e.g. `kavya`) — cloning is a fast-follow
improvement done whenever staff samples are ready, not a gate on Phases 8–13.
Don't block lead capture, logging, QA, or deployment on this phase landing
first.

- Record consented staff voice samples, both languages.
- Clone via Sarvam's voice cloning (single vendor now, per decision #1 — no
  need for a separate ElevenLabs comparison unless quality specifically
  disappoints).
- A/B test the clone by ear; lock the final voice ID.
- Swap into `pipeline.py`'s per-language `TTS_VOICE`/settings from Phase 3 —
  already designed as a simple parameter swap, so this stays a drop-in
  change whenever it happens.

## 9. Phase 8 — Lead capture flow

- Add intent-detection to the shared prompt/logic in `rag-backend/main.py`'s
  `_build_full_messages`: on clear admission interest, the persona should
  conversationally ask for name/phone/email/preferred course, then the
  backend parses a structured marker out of the reply (or a dedicated
  function/tool-call turn, if Sarvam 105B supports function calling — verify
  and prefer that over marker-parsing if available, it's more reliable).
- New module `rag-backend/leads.py` persists captured leads to **Google
  Sheets** (decided 2026-09-26, over Excel/OneDrive — simpler server-to-server
  auth via a service account, no interactive OAuth consent flow, well-
  supported Python library (`gspread`)). Needs: a Google Cloud service
  account with a Sheets API key, sharing the target sheet with that service
  account's email, and the sheet ID in `rag-backend/.env`.
- Keep the capture logic itself (intent detection, slot-filling) decoupled
  from the `gspread`-specific write call, so swapping destinations later
  stays a small, isolated change if ever needed.

## 10. Phase 9 — Difficult/sensitive-question handling

- Get approved talking points from admissions/faculty for known tricky
  questions (e.g. department-vs-placement comparisons).
- Add as a new file in `rag-backend/knowledge_base/` (e.g.
  `sensitive_topics.md`) — the existing retriever picks it up automatically,
  no retrieval code changes needed.
- Consider tightening the system prompt to explicitly prefer these talking
  points verbatim (or close to it) over improvising when a retrieved chunk
  matches a sensitive-topic question, since the whole point is controlled
  phrasing, not just "context is available."

## 11. Phase 10 — Transcript + audio logging

Decided 2026-09-26: **local disk, 90-day retention.**

- Add persistence to both voice paths (`voice-pipeline/pipeline.py` — hook
  into Pipecat's frame pipeline for transcripts/audio) and to `/widget/chat`,
  writing to a local directory on the deployed host (e.g.
  `rag-backend/logs/<date>/<session-id>/` for transcript JSON + audio file).
- Add a simple cleanup routine (scheduled task or a startup check) that
  deletes anything older than 90 days — don't rely on manually remembering
  to prune.
- Revisit storage backend if disk usage on the deployed host becomes a
  problem (Phase 12 picks the actual host, which will have a real disk-size
  constraint to check against) — local disk is the right starting point at
  current expected volume, not necessarily the permanent answer.

## 12. Phase 11 — Consistency QA

- Run the same ~20–30 question test set (from the PRD) through typed chat,
  English voice, and Tamil voice; confirm substance matches — the real proof
  the shared-brain architecture works, especially important now that the LLM
  itself just changed vendors.
- Specifically re-test any question that previously exposed
  Sarvam-vs-frontier-model quality gaps, if any showed up in Phase 1's
  sanity check.

## 13. Phase 12 — Deployment

- **Widget:** static hosting (Vercel/Netlify/college's own server).
- **`rag-backend` + `voice-pipeline`:** persistent host (small VM,
  Render/Railway) — replaces the current ngrok dev tunnel with a real stable
  HTTPS URL.
- This stable URL is also what the separate Calling Agent codebase will
  point at once it exists, since both products share this one RAG backend.
- Secrets: only `SARVAM_API_KEY` and `RAG_BACKEND_AUTH_KEY` now (plus
  whatever Phase 10/embeddings decision adds) — confirm nothing Deepgram/Azure
  related lingers in deployed env config.

## 14. Phase 13 — Avatar (Phase 2, post-launch, deferred)

- Finalize avatar artwork (confirm original/licensed, per the Website Voice
  Bot doc's own flag).
- Integrate LemonSlice fed by the cloned TTS voice output from Phase 7.
- Test gesture triggers.
- Explicitly post-launch — don't pull forward ahead of Phases 1–12.

## 15. Definition of Done (v1)

- [ ] LLM and voice both run on Sarvam; no Azure/Deepgram code or keys remain
- [ ] Visitor can type a question and get an answer from the RAG backend
- [ ] Visitor gets a full spoken conversation in either language, auto-detected,
      with a manual override available
- [ ] Both languages and typed chat answer the same test-question set consistently
- [ ] Voice launches on Sarvam's default voices (cloning is a fast-follow, not a launch gate — see Phase 7)
- [ ] Admission-intent visitors get captured as structured leads in Google Sheets
- [ ] Known sensitive questions answered via approved talking points
- [ ] Every conversation logged to local disk (transcript + audio), auto-pruned after 90 days
- [ ] No API keys exposed in browser-side code
- [ ] Widget matches ACT AI branding, no third-party "Powered by" attribution
- [ ] Deployed and reachable at a real, stable URL

## 16. Decisions log (resolved 2026-09-26 — no open questions remain)

1. **Embeddings vendor:** stay on Azure OpenAI (`text-embedding-3-small`) —
   Sarvam has no public embeddings API as of this writing; only `main.py`'s
   chat client migrates to Sarvam, `retriever.py`'s embeddings client is
   unaffected (§0, §2).
2. **Unused Microsoft for Startups credits:** not a concern — Sarvam startup
   credits are separately available and cover the LLM cost shift.
3. **Lead-capture destination:** Google Sheets, via a service account +
   `gspread`, over Excel/OneDrive — simpler server-to-server auth (§9).
4. **Logging:** local disk, 90-day retention, auto-pruned (§11).
5. **Voice IDs:** launch v1 on Sarvam's current default voices already in
   `pipeline.py`; voice cloning (Phase 7) is a deferred fast-follow, not a
   launch blocker (§8).
