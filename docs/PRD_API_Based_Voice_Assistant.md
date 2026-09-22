# PRD: Agni College of Technology — AI Voice Assistant (API-Based Path)

**Author:** Prakash D (Ethax)
**Status:** Draft for review
**Version:** 0.2 — architecture confirmed for Deepgram (English) + composed Sarvam (Tamil)
**Date:** September 2026

> **Note on figures in this document:** Deepgram and Sarvam pricing below are current as of my last search but both can change without much notice — confirm exact current rates in your dashboards before finalizing a budget.

---

## 1. Problem Statement

Agni College of Technology's website has no conversational interface. Prospective students, parents, and current students must navigate static pages to find information on admissions, courses, placements, and campus life. A peer institution (Sona College of Technology) already runs a third-party voice widget on their site. ACT has asked for an equivalent capability, built and owned in-house.

## 2. Goals

- Let website visitors ask spoken (and typed) questions about ACT and get accurate, sourced answers in real time.
- Ship a working pilot quickly, leaning on managed voice infrastructure rather than building STT/TTS ops expertise from scratch.
- Support English and Tamil.
- Make use of existing credits (Deepgram: $1,190; Sarvam: Rs. 25,000) to minimize near-term cost.
- Keep answers **consistent across both languages** by routing both paths through the same knowledge base and the same LLM.

## 3. Non-Goals (Out of Scope for v1)

- Outbound calling — website widget only.
- Transactional actions (payments, enrollment) — informational Q&A only.
- Telephony/IVR integration.

## 4. Users & Core Use Cases

Same as the self-hosted PRD — prospective students, parents, current students asking admissions, course, placement, and campus-life questions.

## 5. Confirmed Architecture

**Decision:** compose both language paths yourself rather than using either vendor's fully bundled/hosted agent product, so that one shared RAG backend answers both languages consistently. This was chosen over the faster "bundled Samvaad API" alternative specifically to avoid the English and Tamil paths drifting apart in content and quality over time.

```
Visitor (browser)
   │
   ▼
Website widget (frontend — Next.js)
   │
   ▼
Language selection (visitor picks English or Tamil — explicit selection recommended for v1
                     over auto-detect, to reduce engineering risk)
   │
   ├── English ─────────────────────────────────────────────┐
   │                                                          ▼
   │                                    Deepgram Voice Agent API
   │                                    (single WebSocket connection)
   │                                    ├── Listen: Deepgram Nova-3 STT
   │                                    ├── Think: CUSTOM LLM ENDPOINT
   │                                    │     → your own FastAPI RAG backend,
   │                                    │       exposed in OpenAI-compatible
   │                                    │       chat-completions format
   │                                    │     (confirmed supported: Deepgram's
   │                                    │      "think" step accepts a Custom
   │                                    │      Model URL + OpenAI-format API)
   │                                    └── Speak: Deepgram Aura-2 TTS
   │                                          (or BYO TTS if a specific voice
   │                                           matters more than cost)
   │
   └── Tamil ──────────────────────────────────────────────┐
                                        ▼
                          Pipecat or LiveKit Agents (self-composed)
                          ├── STT: Sarvam Saarika (saaras:v3)
                          ├── LLM: SAME custom RAG backend as English path
                          └── TTS: Sarvam Bulbul v3

                    Both paths call the same RAG backend/knowledge base
                    (admissions, courses, placements, FAQs) — this is what
                    keeps English and Tamil answers consistent.
```

**Why this works:** Deepgram's Voice Agent API supports a "Custom" LLM provider — you give it a Custom Model URL, an OpenAI-compatible API format, and an auth header, and it calls your own backend for the "think" step instead of a Deepgram-managed model. This means your English path's brain and your Tamil path's brain can be the literal same backend, even though the two paths use different STT/TTS vendors.

## 6. Vendor Details & Cost Estimate

### Deepgram (English: STT + orchestration + TTS)

- **Product:** Voice Agent API, single WebSocket connection, custom LLM endpoint mode.
- **Pricing tiers (verify current rate in your dashboard):**
  - Fully managed (Deepgram STT + Deepgram LLM + Deepgram TTS): ~$0.07–0.08/min
  - Custom/BYO LLM only (your case, Deepgram STT + your LLM + Deepgram TTS): ~$0.06–0.07/min
  - BYO LLM + BYO TTS: ~$0.04–0.05/min
- **Your credit:** $1,190. At the BYO-LLM tier (~$0.065/min), that's roughly **18,300 minutes (~305 hours)**. These are estimates from published tier figures, not a guarantee — actual billing depends on which exact tier your account lands in.

### Sarvam AI (Tamil: STT + TTS, composed via Pipecat/LiveKit)

- **Products used:** Saarika (STT), Bulbul v3 (TTS) — called directly as APIs, not through the bundled Samvaad agent product.
- **Confirmed pricing (Sarvam's public pricing page):**
  - Speech-to-Text: Rs. 30 per hour of audio
  - Text-to-Speech (Bulbul v3): Rs. 30 per 10,000 characters
  - Chat completion (Sarvam-30B, if used as a fallback/secondary LLM): Rs. 2.5 / Rs. 1.5 / Rs. 10 per 1M tokens (input / cached input / output) — not needed in this architecture since the LLM step uses your own RAG backend, not Sarvam's.
- **Your credit:** Rs. 25,000. Rough estimate: that alone covers **~833 hours of STT** or **~8.3 million characters of TTS** (or a mix of both) — a large runway for a pilot and early production use. Track actual burn rate once real traffic starts rather than relying on this estimate.

## 7. Data & Privacy

Voice/text queries are processed on Deepgram's (US-based) and Sarvam's (India-based) servers. Worth flagging explicitly to college administration — not a blocker, but a disclosure item. Check both vendors' current data retention terms before launch, especially if any query could include personally identifiable admissions information.

## 8. Milestones

| Phase | Deliverable |
|---|---|
| 0. Discovery | Stand up the RAG backend as an OpenAI-compatible endpoint; scope which college content becomes the knowledge base |
| 1. English prototype | Deepgram Voice Agent API wired to the custom LLM endpoint, English only, tested end-to-end |
| 2. Tamil path | Pipecat/LiveKit pipeline with Sarvam Saarika + same RAG backend + Bulbul, tested against the same question set as English |
| 3. Consistency check | Ask the same set of test questions in both languages; verify answers match in substance |
| 4. Pilot | Widget live on a staging page, both languages tested |
| 5. Launch | Widget live on the public site |

## 9. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Deepgram credits ($1,190) or Sarvam credits (Rs. 25,000) run out and no one renews the budget after you graduate. | Track burn rate from day one of the pilot; get a college budget line approved before either credit runs low. |
| Your RAG backend becomes a single point of failure for both languages (if it goes down, both English and Tamil break). | This is a deliberate trade-off for consistency — make sure the RAG backend itself is reliably hosted and monitored, since it's now load-bearing for the whole assistant, not just a nice-to-have. |
| Custom LLM endpoint adds latency Deepgram's managed LLMs wouldn't have (your backend is an extra network hop). | Benchmark end-to-end latency early in Phase 1; optimize the RAG backend's response time specifically, since it's now on the critical path for every turn. |
| Two different orchestration stacks (Deepgram's built-in loop vs. your own Pipecat/LiveKit loop) means two codebases to maintain instead of one. | Accepted trade-off for this PRD version — document both clearly so a future maintainer (see below) isn't confused by the asymmetry. |
| You graduate before this ships or matures. | **Resolved:** Prakash will continue owning and updating this after graduation. Residual risk: this is single-person dependency with no institutional backup — worth documenting both pipelines (Deepgram config and Pipecat/LiveKit code) clearly regardless, in case that changes. |

## 10. Success Metrics (proposed — confirm with college stakeholders)

- Answer accuracy on a shared test set of ACT-specific questions, asked in both English and Tamil — the key metric proving the "one brain, two languages" design works.
- p95 end-to-end latency, per language path.
- Cost per conversation, per vendor (track from day one to validate the credit runway estimates above).
- Uptime, largely inherited from vendor SLAs.

## 11. Cost Summary

- Deepgram: covered by $1,190 credit for an estimated several months of moderate traffic (Section 6 estimate).
- Sarvam: covered by Rs. 25,000 credit, likely comfortable runway for STT+TTS given the per-unit rates above.
- RAG backend hosting: your own infrastructure cost (likely minimal — a small server/API deployment, not GPU-dependent). The LLM itself is OpenAI, funded via Microsoft for Startups credits — confirm the exact credit balance and whether it's billed as direct OpenAI API usage or Azure OpenAI Service usage.
- Engineering time: two orchestration paths to build (Deepgram's native loop + a Pipecat/LiveKit pipeline for Sarvam) — more than a single bundled platform, less than full self-hosting.

## 12. Open Questions

1. ~~Which LLM actually powers the RAG backend's "think" step~~ — **Resolved:** OpenAI, funded via Microsoft for Startups credits. (Confirm whether this is a direct OpenAI API key or Azure OpenAI Service under the Microsoft for Startups benefit, since that affects which endpoint/region your RAG backend calls and its latency profile.)
2. ~~Explicit language-selection UX vs. auto-detect~~ — **Resolved:** visitor picks the language explicitly.
3. ~~Which college content sources form the shared RAG knowledge base, and who keeps them updated~~ — **Resolved:** Agni College of Technology's own content (admissions, courses, placements, FAQs); Prakash will keep it updated, both now and after graduation.
4. ~~Who owns the RAG backend's uptime after graduation~~ — **Resolved:** Prakash will continue owning this after graduation.
