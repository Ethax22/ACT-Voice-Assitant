# Implementation Plan: Agni AI Voice Assistant (Sarvam-Unified Path)

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

> **`docs/Architecture_Current.md` (2026-10-08) is the as-built description of
> all three components.** Where this plan's later sections describe an older
> design, that file wins.

| Area | Status |
|---|---|
| RAG backend core (FastAPI, retrieval, persona, streaming) | ✅ Done — `rag-backend/main.py`, `retriever.py` |
| Typed chat in widget | ✅ Done — `/widget/chat`, now with script-based language auto-detect (Phase 6) |
| LLM | ✅ Migrated to Sarvam 105B (Phase 1, 2026-09-25) — reasoning disabled via `extra_body`, see Phase 1 notes |
| Embeddings | ✅ Staying on Azure OpenAI (`text-embedding-3-small`) — separate from the LLM, kept as-is since Sarvam has no public embeddings API yet |
| English voice (Deepgram) | ✅ Retired (Phase 2, 2026-09-25) — `voice_agent.py` deleted, `/voice/en` moved to `voice-pipeline/` |
| English + Tamil voice (`voice-pipeline/`, Sarvam/Pipecat) | 🟡 Built and code-verified (Phases 3-6: generalized pipeline, greeting wired up, voice auto-detect; pre-connect detection switched off 2026-10-08, in-call switching only) — still needs a real human listening/mic test (Phase 4 barge-in feel, Phase 5 voice pick), not yet done |
| Speaking voice (changed 2026-10-18) | 🟡 **Cartesia Sonic 3.6 only** — Sarvam Bulbul and the Sarvam cloned voice removed (CLAUDE.md decision #1). Listening (Saaras) and answers (Sarvam 105B) unchanged. Scripted calls: voice starts 0.06-0.20 s after the reply text (Sarvam clone was 1.4-2.8 s); end of speech to first audio median ~2.1 s, so the 0.7 s target is still missed. No backup voice. Not yet judged by ear or on a real mic. See `docs/Architecture_Current.md` §3. |
| Automatic language detection | 🟡 Built (Phase 6, 2026-09-25) — typed chat fully verified; voice detection verified with synthesized test clips only, not yet a real visitor mic |
| Voice cloning (Phase 7) | 🟡 **Superseded 2026-10-18:** the Sarvam cloning code (`cloned_voice_tts.py`, `create_cloned_voice.py`) was removed with the switch to Cartesia. A staff clone is now a Cartesia instant clone whose voice id goes in `CARTESIA_VOICE_ID`; no code needed. Still deferred, not a launch blocker; still needs a consented staff recording. §8 below is the Sarvam history. |
| Lead capture (Phase 8) | ✅ Built and wired to Google Sheets (updated 2026-10-06). `capture_lead` triggers on name+phone in typed chat and the voice/streaming path (no tool-call chunks leak to the voice pipeline), phone numbers are validated, and `rag-backend/.env` has `GOOGLE_SERVICE_ACCOUNT_JSON` + `LEADS_SHEET_ID` set. A phone call through the Calling Agent also produced a lead with `source=phone`, so the Sheet write works. Not yet confirmed from a real website-widget visitor (only dev tests). **2026-10-09:** a real widget call showed the model saying "noted" without calling `capture_lead` (measured 1 of 4 calls in English, 0 of 4 in Tamil), so nothing reached the Sheet. `main.py` now forces the tool call (`tool_choice`) on any turn whose message contains a phone number (`_should_force_lead_capture`); an invalid number (e.g. 9 digits from speech recognition) saves nothing and the bot asks for it again. Checked with the Sheet write mocked, 4 of 4 valid numbers saved in en / ta / ta_en; not yet re-checked on a real call. |
| Sensitive-question handling (Phase 9) | 🟡 Plumbing built and verified live (`rag-backend/knowledge_base/sensitive_topics.md` retrieved automatically, system prompt prefers "Talking point:" sections near-verbatim, correctly avoids leaking unfinished-content editor notes and avoids fabricating numbers it doesn't have) — **content itself is still draft placeholder text**, not the real admissions/faculty-approved wording CLAUDE.md requires. Needs Prakash/admissions to fill in each `[ADMISSIONS: ...]` bracket in that file before launch. |
| Logging (Phase 10) | 🟡 Built. Typed chat: verified live end-to-end (`rag-backend/conversation_logger.py`, transcript written and 90-day pruning tested with a synthetic old folder). Voice: `voice-pipeline/conversation_logger.py`'s `TranscriptLogger` + Pipecat's `AudioBufferProcessor` are wired into the pipeline and confirmed to link correctly in a dry (no real audio) pipeline-construction test — **not yet confirmed with a real voice call**, since that needs actual audio flowing through, same category of human-verification gap as Phases 4-6. |
| Sentiment report (added 2026-10-07) | 🟡 Built — `rag-backend/sentiment_report.py` reads saved typed-chat and voice transcripts, has Sarvam 105B label each conversation, and writes an HTML + CSV report to `rag-backend/reports/`. Run once on 48 dev-test conversations. Labels are **not validated** against hand-labelled conversations (English or Tamil), and the "unanswered questions" field is noisy. **2026-10-09:** now automatic — `rag-backend` rebuilds the last-7-days report every 24 h while it is running (`SENTIMENT_REPORT_INTERVAL_HOURS`, 0 = off); running it by hand still works. See §11a. |
| Reply styles + mid-call language switch (reworked 2026-10-09) | 🟡 `voice-pipeline/conversation_router.py` no longer waits for language detection (it was up to 2.5 s on the first turn), classifies each turn locally as EN / TA / TA_EN (`style_classifier.py`, Tamil + English mixed in English letters), and keeps ONE voice (About Us) for the whole call — topic only lights the browser tile. Tamil/Tanglish prompts rewritten (`main.py` `LANGUAGE_RULES`, `TAMIL_STYLE`, `TANGLISH_STYLE`). Checked with synthesized speech only: results in `docs/Zimi_P0_Validation.md`; **no real-ear listening test yet**, Tanglish word list and prompts unreviewed by a native speaker. |
| Voice latency work | 🟡 `voice-pipeline/latency_metrics.py` logs per-turn `[latency]` lines (STT/LLM/TTS first byte, user-stopped → bot-audio). Backend first word measured ≈ 1.7 s over HTTP (retrieval ≈ 0.3–0.45 s, Sarvam 105B first token ≈ 0.4 s); a retrieval-query cache was added. **No real voice-call numbers yet** — tuning of VAD silence, TTS buffers and answer length waits on them (see `docs/Session_Handoff_ERP_CallAgent_VoiceLatency.md` §6). |
| Calling Agent (separate product, not built in this repo) | 🟡 Runs on Sarvam Indus, not DIY. Reaches this repo only via `POST /calling-agent/ask` (shared brain + lead capture). Real inbound call tested: answers, Tamil/English, ERP lookup (separate service, fake contacts), lead capture, transfer. Not launch-ready (hosting, real ERP data, transfer-routing retest, consent/retention sign-off); outbound and sentiment not started. See `docs/Calling_Agent_Build_Plan.md` and the 2026-10-05 handoff. |
| Deployment | ⚪ Not started (dev-only: ngrok for rag-backend, Cloudflare quick tunnel for the ERP service, voice-pipeline on localhost:8001) |

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

- **Typed chat:** *(updated 2026-10-08)* the widget no longer detects language
  or switches its UI — its wording is English only. `rag-backend/main.py`
  (`_reply_language`) picks the reply language from the Unicode script of the
  visitor's latest long-enough message (Tamil block → Tamil) and enforces it
  in the final system message.
- **Voice — pre-connect detection switched off (2026-10-08):** the widget's
  `DETECT_LANGUAGE_BEFORE_CONNECT` flag is `false`, so every voice call opens
  `/voice/en` immediately and the in-call router (below) switches to Tamil on
  the visitor's first Tamil turn. Reason: it added ~2 s before connecting, and
  it records before the greeting plays, so a visitor waiting to be greeted gave
  it silence and it fell back to English anyway. Trade-off: a Tamil-first
  visitor hears an English greeting and a slower first turn. The code and
  `/detect-language` route are kept; set the flag to `true` to restore it. The
  description below is how it worked when on.
- **Voice (pre-connect, when enabled):** `voice-pipeline/language_detection.py` + a new `POST
  /detect-language` route in `voice-pipeline/server.py` (not on rag-backend —
  it's Sarvam-specific and voice-pipeline already holds `SARVAM_API_KEY`).
  The widget buffers ~1.5s of mic audio into the same PCM16 format it already
  uses for the realtime pipeline, POSTs it there, and opens `/voice/en` or
  `/voice/ta` based on the response — falling back to the current language on
  a low-confidence or failed detection rather than forcing a guess. The
  buffered snippet is flushed into the real pipeline connection once it opens
  (in `widget/index.html`'s `startVoiceSession`), so the visitor doesn't have
  to repeat whatever they said during detection.
- *(Updated 2026-10-08)* The manual EN/Tamil override was removed from the
  widget with the English-only wording change; there is currently no manual
  override (CLAUDE.md decision #5 asks for one — flag to Prakash before launch).
- **Mid-call switching** lives in `voice-pipeline/conversation_router.py`: a
  background batch detection per utterance (≥1 s), a Tamil-script shortcut, and
  spoken phrases ("speak Tamil"); needs 0.7 confidence. See
  `Architecture_Current.md` §3.
- Not yet done: a real by-ear/by-hand test of detection latency and accuracy
  with an actual visitor mic, as opposed to synthesized test clips — that's
  the same category of human-verification gap as Phase 4's turn-taking test
  and Phase 5's voice pick.

## 8. Phase 7 — Voice cloning (both languages) — code built 2026-09-27, still not a launch blocker

> **2026-10-18: this section is history.** The bot now speaks with Cartesia, and the
> Sarvam cloning code described below was deleted. A clone is a Cartesia voice id in
> `CARTESIA_VOICE_ID`.

Decided 2026-09-26: v1 launches on Sarvam's **default stock voices** (already
selected in `pipeline.py`, e.g. `kavya`) — cloning is a fast-follow
improvement done whenever staff samples are ready, not a gate on Phases 8–13.
Don't block lead capture, logging, QA, or deployment on this phase landing
first.

**Correction (2026-09-27, verified live against the real Sarvam API, not
assumed from docs):** this section originally assumed cloning would be "a
simple parameter swap" into `pipeline.py`'s per-language `TTS_VOICE`
setting. That's **wrong** — confirmed by direct testing: a cloned voice_id
(`svc-{uuid}`, from `POST /voices/create`) is rejected with a 400/422 "not
recognized" error by both the REST `/text-to-speech` endpoint and its
websocket equivalent (`wss://api.sarvam.ai/text-to-speech/ws`) — the exact
streaming TTS mechanism `pipeline.py`'s `SarvamTTSService` uses for stock
voices. Cloned voices only work through the separate, non-streaming `POST
/voices/clone` endpoint, which returns one complete base64 WAV per call
(under the key `audio_b64` — confirmed live; note this doesn't even match
the `audio` key Sarvam's own playground-generated code sample shows, so that
sample wasn't trustworthy either). See `voice-pipeline/cloned_voice_tts.py`'s
module docstring for the full account.

**What got built instead** (real code, not just a plan revision):
- `voice-pipeline/cloned_voice_tts.py`: `ClonedVoiceTTSService`, a custom
  Pipecat `TTSService` subclass. **Updated 2026-10-08:** it streams over
  `wss://api.sarvam.ai/voices/clone/ws` (Sarvam changelog 2026-10-01) and
  yields audio chunks as they arrive; the original non-streaming
  `POST /voices/clone` call (one full WAV per utterance) is kept only as a
  fallback if the websocket fails before any audio. Measured first audio is
  still ≈1.7 s, which is why greetings are pre-rendered
  (`greeting_cache.py`) rather than synthesized live.
- `voice-pipeline/pipeline.py`: added `CLONED_VOICE_IDS` (read from
  `CLONED_VOICE_ID_EN` / `CLONED_VOICE_ID_TA` env vars). If set for a
  language, `build_pipeline()` swaps in `ClonedVoiceTTSService` for that
  language only — unset (the default) keeps the exact previous stock-voice
  behavior unchanged. An aiohttp session opened for the cloned path is
  closed on `on_client_disconnected`.
- `voice-pipeline/create_cloned_voice.py`: a one-off CLI script — run
  `python create_cloned_voice.py <audio_file> <en|ta>` once a consented
  staff sample is recorded, and it prints the resulting `voice_id` to paste
  into `.env`. No further code change or redeploy needed after that.
- **Verified live** end-to-end using a synthetic stand-in reference clip
  (Sarvam's own stock `priya` voice, since no real staff recording exists
  yet): `create_cloned_voice.py` successfully created a cloned voice,
  `ClonedVoiceTTSService.run_tts()` successfully generated real audio from
  it, and a full pipeline dry-construction test confirmed `pipeline.py`
  correctly swaps in `ClonedVoiceTTSService` in place of `SarvamTTSService`
  when a `CLONED_VOICE_ID_*` env var is set. All test voices created during
  this verification were deleted from Sarvam afterward.

**Still needed before this phase is actually "done"**:
- Record consented staff voice samples, both languages (explicitly left to
  Prakash, not attempted here).
- Run `create_cloned_voice.py` on each real recording, put the resulting
  `voice_id`s in `voice-pipeline/.env`.
- A/B test the real clone by ear (native-speaker listening test, same
  category as Phases 4–6's pending mic test) — lock the final voice ID per
  language.
- Decide whether the added per-utterance latency is acceptable for launch,
  given it's no longer a drop-in swap — flagged here for that decision, not
  decided silently.

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

## 11a. Sentiment report (website bot) — added 2026-10-07

Decided 2026-10-07: the website bot gets a sentiment report. This supersedes
the Website Voice Bot doc, which listed sentiment as out of scope for the web
bot. What stays out of scope here is **real-time** scoring during a call —
that remains the Calling Agent's requirement, in its own codebase.

- `rag-backend/sentiment_report.py` — a script run by hand
  (`python sentiment_report.py --days 7`, or `--from`/`--to`). It reads the
  Phase 10 transcripts from both `rag-backend/logs/` and
  `voice-pipeline/logs/`, so it adds nothing to reply latency.
- One Sarvam 105B call per conversation (same vendor as everything else)
  returns: sentiment (positive/neutral/negative), mood change
  (improved/steady/worsened), admission interest (high/medium/low/none),
  frustration + reason, questions the bot failed to answer, topics, and a
  one-sentence summary. Phone numbers and emails are masked in the output.
- Each result is cached as `sentiment.json` beside its transcript, keyed by a
  hash of the conversation, so re-runs only pay for new conversations and
  the cache is removed by the existing 90-day log prune.
- Output: `rag-backend/reports/sentiment_<from>_to_<to>.html` and `.csv`
  (git-ignored — visitor data; pruned after 90 days on each run).

**Still needed before the numbers can be relied on:**
- Check the labels against a set of conversations labelled by hand, Tamil
  included — same requirement the Calling Agent plan sets for its own
  sentiment scoring.
- The "unanswered questions" field varied a lot with prompt wording during
  the build (7 to 28 of 48 conversations) and sometimes listed questions the
  bot did answer. Treat it as a hint until tuned against real data.
- Decide whether it should be shown anywhere other than the local report file.

**Phone calls (added 2026-10-21):** the report also covers the Calling Agent's
phone calls, in four channels: website chat, website voice, phone inbound, phone
outbound (`--channel website|phone|all`). Phone calls have their own prompt; the
website prompt is unchanged. Before this the report took phone calls in by mistake
and counted them as website voice. The phone agent judges each call right after it
ends and shows the results on its test page (Analytics → Sentiment). Details:
`docs/Calling_Agent_DIY_Implementation_Plan.md`.

**Automatic run (added 2026-10-09):** `main.py` starts
`sentiment_report.run_periodically()` as a background task at startup. Whenever
the newest report in `rag-backend/reports/` is older than
`SENTIMENT_REPORT_INTERVAL_HOURS` (default 24; 0 turns it off) it rebuilds the
last 7 days. It only runs while the backend is running, and it is still a
post-conversation job — nothing is scored during a call.

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

- [ ] LLM and listening run on Sarvam, speaking on Cartesia (2026-10-18); no Azure chat/Deepgram code or keys remain
- [ ] Visitor can type a question and get an answer from the RAG backend
- [ ] Visitor gets a full spoken conversation in either language, auto-detected,
      with a manual override available
- [ ] Both languages and typed chat answer the same test-question set consistently
- [ ] Voice launches on a Cartesia library voice (a staff clone is a fast-follow, not a launch gate — see Phase 7)
- [ ] Admission-intent visitors get captured as structured leads in Google Sheets
- [ ] Known sensitive questions answered via approved talking points
- [ ] Every conversation logged to local disk (transcript + audio), auto-pruned after 90 days
- [ ] No API keys exposed in browser-side code
- [ ] Widget matches Agni AI branding, no third-party "Powered by" attribution
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
6. **Sentiment (decided 2026-10-07):** the website bot gets a
   post-conversation sentiment report built from saved transcripts (§11a);
   real-time sentiment scoring stays with the Calling Agent.
