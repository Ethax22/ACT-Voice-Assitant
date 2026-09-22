# ACT Voice Assistant

Building an AI voice + chat assistant for Agni College of Technology's website — a floating widget ("ACT AI") that answers visitor questions about admissions, courses, placements, and campus life, in English and Tamil, by voice or typed text.

**Full build spec:** @docs/Implementation_Plan_API_Based.md
**Architecture rationale (read for the "why" behind any decision below):** docs/PRD_API_Based_Voice_Assistant.md

> `docs/PRD_Self_Hosted_Voice_Assistant.md` is a **rejected alternative**, kept for reference only. We are building the API-based path. Do not build against the self-hosted PRD.

## Locked-in decisions — do not re-litigate these

These were already decided across prior planning sessions. If a build step seems to call one of these into question, flag it to Prakash rather than silently deciding differently.

1. **Compose-it-ourselves, not a bundled vendor agent.** English uses Deepgram's Voice Agent API with a **custom LLM endpoint** (not Deepgram's managed LLM). Tamil is assembled from Sarvam's Saarika (STT) + Bulbul (TTS) via Pipecat or LiveKit Agents — not Sarvam's bundled Samvaad product. This was chosen specifically so both languages share one brain.
2. **One shared RAG backend answers all three input paths**: typed chat, English voice, and Tamil voice all call the same FastAPI backend's `/v1/chat/completions` endpoint. Never let a language or input mode fork onto its own separate LLM/knowledge base — that's the one thing this whole architecture is designed to avoid.
3. **The RAG backend must be stateful across a conversation** — pass the full message history on every call, not just the latest turn. A version that only reads `messages[-1]` is a regression, not a simplification.
4. **The system prompt must carry a persona** ("ACT AI" — warm, brief, conversational — see the prompt template in the Implementation Plan). Don't strip this down to a bare "answer from context" instruction; that's what makes it feel robotic.
5. **Language selection is explicit** (the visitor picks English or Tamil) — no auto-detect in v1.
6. **The LLM is OpenAI**, funded via Microsoft for Startups credits. Confirm with Prakash whether the actual key is a direct OpenAI key or Azure OpenAI Service before hardcoding a client setup — they behave slightly differently.
7. **No vendor API keys (Deepgram, Sarvam, OpenAI) ever touch browser-side JavaScript.** Everything routes through the FastAPI backend, which holds the keys server-side.
8. **Branding:** the widget is "ACT AI." No third-party "Powered by" attribution anywhere in the UI.

## Repo structure

```
ACT-Voice-Assistant/
├── CLAUDE.md
├── docs/
│   ├── PRD_API_Based_Voice_Assistant.md
│   ├── PRD_Self_Hosted_Voice_Assistant.md   (reference only — not being built)
│   └── Implementation_Plan_API_Based.md
├── rag-backend/          # FastAPI — the shared "brain," all three paths call this
├── tamil-voice-pipeline/ # Pipecat/LiveKit pipeline: Sarvam STT + RAG backend + Sarvam TTS
└── widget/               # One-page site + ACT AI widget (starting point already exists —
                           # ask Prakash for the current act-ai-widget.html if not yet copied in)
```

## Environment variables (never commit these)

```
OPENAI_API_KEY=            # or AZURE_OPENAI_* — confirm which with Prakash
DEEPGRAM_API_KEY=
SARVAM_API_KEY=
RAG_BACKEND_AUTH_KEY=      # your own bearer token, checked by rag-backend, used by
                           # both Deepgram's custom endpoint call and the widget's proxy
```

## Build order

Follow the Implementation Plan's phases in order — each phase should be working and manually tested before starting the next:

1. **RAG backend** — ingest ACT content, build retrieval, expose `/v1/chat/completions` with conversation history + persona system prompt (Implementation Plan §2).
2. **Typed chat wired into the widget** — this is the first real end-to-end version; validates the backend before adding voice complexity (Implementation Plan §5.1).
3. **Deepgram English voice path**, custom LLM endpoint pointed at the RAG backend (Implementation Plan §3).
4. **Sarvam Tamil voice pipeline** via Pipecat/LiveKit, same RAG backend (Implementation Plan §4).
5. **Consistency QA** — run the same test-question set through typed chat, English voice, and Tamil voice; answers should match in substance (Implementation Plan §6).
6. **Deploy** — widget to static hosting, backend + Tamil pipeline to a persistently running host (Implementation Plan §7).

Full detail, code stubs, and the Definition of Done checklist are in `docs/Implementation_Plan_API_Based.md` — read it before starting Phase 1.

## When something in the docs seems out of date

These documents were written before any code existed — if you discover during the build that something in the PRD or Implementation Plan doesn't hold up (a vendor API changed, a library doesn't exist, a cost estimate was wrong), fix the code correctly and flag the discrepancy to Prakash rather than silently deferring to a stale doc.
