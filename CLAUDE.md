# ACT Voice Assistant

Building an AI voice + chat assistant for Agni College of Technology's website — a floating widget ("ACT AI") that answers visitor questions about admissions, courses, placements, and campus life, in English and Tamil, by voice or typed text.

**Full build spec:** @docs/Implementation_Plan_API_Based.md
**Architecture rationale (read for the "why" behind any decision below):** docs/PRD_API_Based_Voice_Assistant.md

> `docs/PRD_Self_Hosted_Voice_Assistant.md` is a **rejected alternative**, kept for reference only. We are building the API-based path. Do not build against the self-hosted PRD.

## Locked-in decisions — do not re-litigate these

These were already decided across prior planning sessions. If a build step seems to call one of these into question, flag it to Prakash rather than silently deciding differently.

1. **Compose-it-ourselves, not a bundled vendor agent — both languages now on Sarvam.** Updated 2026-09-25, superseding the original split: both English and Tamil are assembled from Sarvam's Saaras (STT) + Bulbul (TTS) via Pipecat — not Sarvam's bundled Samvaad product. Originally English ran on Deepgram's Voice Agent API (Flux STT/TTS) specifically for its built-in turn-taking/interruption handling, composed via a custom LLM endpoint; that's been dropped in favor of one shared voice vendor across both languages. Known tradeoff, accepted deliberately: the English path loses Deepgram's built-in turn-taking and must now replicate it manually via Pipecat, same as Tamil already does. Working Deepgram code (`rag-backend/voice_agent.py`) predates this change and needs retiring/replacing, not just leaving in place unused.
2. **One shared RAG backend answers all three input paths**: typed chat, English voice, and Tamil voice all call the same FastAPI backend's `/v1/chat/completions` endpoint. Never let a language or input mode fork onto its own separate LLM/knowledge base — that's the one thing this whole architecture is designed to avoid.
3. **The RAG backend must be stateful across a conversation** — pass the full message history on every call, not just the latest turn. A version that only reads `messages[-1]` is a regression, not a simplification.
4. **The system prompt must carry a persona** ("ACT AI" — warm, brief, conversational — see the prompt template in the Implementation Plan). Don't strip this down to a bare "answer from context" instruction; that's what makes it feel robotic.
5. **Language is auto-detected** — updated 2026-09-23, superseding the original v1 decision (explicit EN/Tamil picker, no auto-detect). Typed chat detects Tamil vs. English directly from the input text's script; voice detects language from a short initial audio snippet before routing the session into the Sarvam/Pipecat pipeline configured for that language (see decision #1 — both languages share the same voice vendor now, just different STT/TTS language settings). Keep a manual override in the widget UI as a fallback, but auto-detect is the default path now, not the picker.
6. **The LLM is Sarvam 105B**, not OpenAI. Updated 2026-09-25, superseding the original OpenAI decision (which was funded via Microsoft for Startups credits — those credits go unused under this change; flag to Prakash if that matters before deployment, don't silently absorb the cost shift). Switched after a benchmark/cost comparison: Sarvam 105B scored competitively with or ahead of GPT-4.1-mini and GPT-5-mini on general benchmarks (MMLU, MMLU Pro, GPQA Diamond) and dominated Indic-language benchmarks (~90% win rate vs. frontier English-centric models), at output pricing below gpt-4.1-mini's. Note Sarvam's benchmark numbers are self-reported — validate with the real ACT test-question set (Implementation Plan §6) before treating this as final, same scrutiny any vendor's own numbers deserve. `rag-backend/main.py`'s `AsyncAzureOpenAI` client is now stale and needs replacing with a Sarvam chat-completions client — but **embeddings stay on Azure OpenAI** (decided 2026-09-26, Sarvam has no public embeddings API yet): `retriever.py` has its own separate `AsyncAzureOpenAI` client for embeddings that this migration does not touch. Only `AZURE_OPENAI_DEPLOYMENT` (the chat deployment name) becomes stale; `AZURE_OPENAI_API_KEY`/`ENDPOINT`/`API_VERSION`/`EMBEDDING_DEPLOYMENT` stay.
7. **No vendor API keys (Sarvam) ever touch browser-side JavaScript.** Everything routes through the FastAPI backend, which holds the keys server-side.
8. **Branding:** the widget is "ACT AI." No third-party "Powered by" attribution anywhere in the UI.

## Additional v1 must-haves (added 2026-09-22, from the Website Voice Bot doc)

These weren't in the original Implementation Plan but are confirmed in-scope for v1, not phase-2 nice-to-haves. See `docs/Implementation_Plan_API_Based.md` phases 7–10 for the build detail:

- **Cloned staff voice** for both languages, via Sarvam voice cloning — deferred fast-follow, not a launch blocker (decided 2026-09-26). v1 ships on Sarvam's default stock voices already selected in `voice-pipeline/pipeline.py`; swap in the cloned voice ID whenever staff samples/cloning are done.
- **Admission-intent lead capture**: detect clear admission interest mid-conversation, collect name/phone/email/preferred course, write to **Google Sheets** (decided 2026-09-26, via a service account + `gspread` — simpler than Excel/OneDrive's Graph API/OAuth overhead).
- **Difficult/sensitive-question handling** via pre-approved talking points (e.g. department-vs-placement comparisons) sourced from admissions/faculty, not improvised by the model.
- **Full transcript + audio logging** of every conversation — **local disk, 90-day retention, auto-pruned** (decided 2026-09-26).

## Related product — not built in this repo

There's a separate "Calling Agent" product (a bilingual inbound/outbound phone agent on ACT's existing number, via Exotel) planned alongside this voice assistant. It lives in **its own codebase**, not here. The only link between the two: once `rag-backend` is deployed at a stable URL (Implementation Plan phase 12), the Calling Agent's codebase will call that same URL, so both products stay on one shared brain. Don't add Calling Agent code, telephony integrations, or ERP-caller-lookup logic to this repo.

## Repo structure

```
ACT-Voice-Assistant/
├── CLAUDE.md
├── docs/
│   ├── PRD_API_Based_Voice_Assistant.md
│   ├── PRD_Self_Hosted_Voice_Assistant.md   (reference only — not being built)
│   └── Implementation_Plan_API_Based.md
├── rag-backend/          # FastAPI — the shared "brain," all three paths call this (LLM: Sarvam 105B)
├── voice-pipeline/       # renamed from tamil-voice-pipeline/ once generalized (Implementation
                           # Plan phase 3) — one Pipecat service serving BOTH languages
                           # (Sarvam STT + RAG backend + Sarvam TTS), parameterized by language
└── widget/               # One-page site + ACT AI widget (starting point already exists —
                           # ask Prakash for the current act-ai-widget.html if not yet copied in)
```

## Environment variables (never commit these)

```
SARVAM_API_KEY=            # now the only vendor key — covers LLM (105B), STT (Saaras), TTS (Bulbul)
RAG_BACKEND_AUTH_KEY=      # your own bearer token, checked by rag-backend, used by
                           # the voice pipeline's LLM call and the widget's proxy
```

`AZURE_OPENAI_*` and `DEEPGRAM_API_KEY` (still referenced in `rag-backend/.env.example` and
`rag-backend/.env`) are stale as of the 2026-09-25 switch to Sarvam-for-everything — remove
once the code migration lands, don't leave them alongside the new key as dead config.

## Build order

Follow the Implementation Plan's phases in order — each phase should be working and manually tested before starting the next. Updated 2026-09-25 to reflect the Sarvam-unified pivot and the additional v1 scope (voice cloning, lead capture, sensitive-question handling, logging):

1. **RAG backend** (done) and **typed chat in the widget** (done) — Implementation Plan §0.
2. **Migrate the LLM to Sarvam 105B** (Implementation Plan phase 1) — do this before touching voice, since every later phase's answers depend on it.
3. **Retire the Deepgram English path** (phase 2) and **generalize the voice pipeline to serve both languages** (phase 3, includes the `tamil-voice-pipeline/` → `voice-pipeline/` rename).
4. **Manual turn-taking for English** (phase 4) and **re-verify both languages end-to-end** (phase 5).
5. **Automatic language detection** (phase 6), **voice cloning** (phase 7), **lead capture** (phase 8), **sensitive-question handling** (phase 9), **logging** (phase 10).
6. **Consistency QA** across typed chat + both voice languages (phase 11).
7. **Deploy** — widget to static hosting, backend + voice pipeline to a persistently running host at a stable URL (phase 12).
8. **Avatar** (phase 13, post-launch, deferred).

Full detail, code stubs, open questions needing your decision, and the Definition of Done checklist are in `docs/Implementation_Plan_API_Based.md` — read it before starting Phase 2 above (the LLM migration).

## When something in the docs seems out of date

These documents were written before any code existed — if you discover during the build that something in the PRD or Implementation Plan doesn't hold up (a vendor API changed, a library doesn't exist, a cost estimate was wrong), fix the code correctly and flag the discrepancy to Prakash rather than silently deferring to a stale doc.
