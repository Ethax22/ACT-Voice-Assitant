# PRD: Agni College of Technology — AI Voice Assistant (Self-Hosted, Open-Weight Path)

**Author:** Prakash D (Ethax)
**Status:** Draft for review
**Version:** 0.1
**Date:** September 2026

> **Note on figures in this document:** model sizes, VRAM estimates, and license terms below are current as of my last search but change frequently — verify each model's license and hardware requirement against its official model card before committing engineering time to it.

---

## 1. Problem Statement

Agni College of Technology's website currently has no conversational interface. Prospective students, parents, and current students must navigate static pages to find information on admissions, courses, placements, and campus life. A peer institution (Sona College of Technology) already runs a third-party voice widget on their site. ACT has asked for an equivalent capability, built and owned in-house.

## 2. Goals

- Let website visitors ask spoken (and typed) questions about ACT and get accurate, sourced answers in real time.
- Keep all student/visitor data and voice processing inside the college's own infrastructure — no student query data sent to third-party servers.
- Build something the college can run indefinitely without a recurring per-minute or per-seat vendor bill.
- Support English as the first-priority language, with Tamil as a nice-to-have for v1.

## 3. Non-Goals (Out of Scope for v1)

- Outbound calling (admissions follow-up calls, fee reminders) — this PRD covers the website widget only.
- Transactional actions (form submission, payment, enrollment) — v1 is informational Q&A only.
- Languages beyond English and Tamil, unless a later phase confirms demand.

## 4. Users & Core Use Cases

| User | Example query |
|---|---|
| Prospective student / parent | "What is the fee structure for CSE?" |
| Current student | "When is the next placement drive?" |
| Visitor | "Where is the admissions office?" |

## 5. Proposed Architecture

```
Visitor (browser)
   │  voice/text
   ▼
Website widget (frontend)
   │  WebSocket/WebRTC audio
   ▼
Voice orchestration layer (Pipecat or LiveKit Agents — open source, self-hosted)
   ├── STT: AI4Bharat indic-conformer-600m (Tamil + English)
   ├── LLM: open-weight 7–8B model (Llama 3.1 8B or Qwen2.5 7B) via vLLM/Ollama
   │        └── RAG layer over ACT's own content (admissions pages, course catalog,
   │            placement stats, FAQs) — this is the part that makes answers accurate
   │            and specific to ACT rather than generic
   └── TTS: AI4Bharat Indic Parler-TTS (gated — apply for HF access early) or
            Kokoro/Piper for a lighter English-only fallback voice
   ▼
Response streamed back to widget
```

All inference runs on the college's own GPU server — nothing above the orchestration layer touches a third-party API.

## 6. Hardware & Infrastructure

**Open item — confirm before finalizing:** exact GPU model and VRAM on the college server. The architecture above needs roughly the following, based on current published figures:

| Component | Approx. VRAM |
|---|---|
| LLM (7–8B, quantized) | ~6–16 GB |
| STT (indic-conformer-600m) | ~1–2 GB |
| TTS (Indic Parler-TTS, ~938M params) | ~2–4 GB |
| **Total, running concurrently** | **~10–22 GB** (rough estimate, not benchmarked) |

If the server has a single GPU with 24GB+ VRAM (e.g., an RTX 4090-class card or a datacenter card like an A10/L4/A100), all three components likely fit together. If VRAM is tighter, the LLM is the first candidate to shrink (Phi-4 14B at ~8GB, or a smaller quantization) or the components can be scheduled rather than run all at once for low-traffic periods.

Other infra needs:
- Persistent storage for the RAG knowledge base (vector database — e.g., a self-hosted instance of a lightweight vector store).
- Reverse proxy + TLS for the public-facing widget endpoint.
- Monitoring for GPU utilization and uptime (this is genuinely new operational surface for the college — see Risks).

## 7. Model Options Considered

**LLM candidates:**
- Llama 3.1/3.3 8B (Llama Community License)
- Qwen 2.5/3 7–8B (Apache 2.0-leaning, verify exact terms)
- Phi-4 14B (MIT) — most hardware-friendly if VRAM is constrained
- Gemma 3 27B — higher quality ceiling, needs ~16GB alone

**STT candidates:**
- AI4Bharat indic-conformer-600m — recommended default given Tamil requirement
- OpenAI Whisper large-v3 / faster-whisper — better for English-heavy traffic, weaker on Tamil specifically

**TTS candidates:**
- AI4Bharat Indic Parler-TTS — recommended default for Tamil, but is gated on Hugging Face (apply for access as an early task, not late)
- Kokoro (82M, Apache 2.0) — very lightweight, strong English quality, Tamil support unconfirmed
- Piper — fallback if GPU headroom is tighter than expected

**Orchestration:** Pipecat or LiveKit Agents (both open-source; pick based on which the team finds easier to prototype with — no strong technical reason to prefer one for this scale).

## 8. Data & Privacy

This is the strongest argument for the self-hosted path: no student query, voice recording, or transcript needs to leave ACT's own network. Worth stating explicitly to college administration as the core value proposition versus the API path, especially for anything touching personal/admission data.

## 9. Milestones (indicative — adjust to your actual semester calendar)

| Phase | Deliverable |
|---|---|
| 0. Discovery | Confirm GPU spec with IT; apply for AI4Bharat gated model access; scope which college content becomes the RAG knowledge base |
| 1. Text-only prototype | RAG pipeline answering typed questions correctly, no voice yet |
| 2. Voice pipeline | STT + TTS wired in via Pipecat/LiveKit, tested end-to-end for latency |
| 3. Pilot | Widget live on a staging/test page, limited internal testing |
| 4. Launch | Widget live on the public site |

## 10. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| **You graduate before this ships or matures.** Self-hosted infra needs someone to patch, monitor, and restart it. | **Resolved:** Prakash will continue owning and maintaining this after graduation. Residual risk: single-person dependency with no institutional backup — write deployment/runbook documentation as you go, not at the end, in case that changes. |
| GPU is shared with other academic/research workloads and gets contended. | Confirm with IT whether the GPU is dedicated to this project or shared, and what happens under load conflicts. |
| Open-weight Tamil TTS quality lags commercial APIs. | Budget time for a listening-quality evaluation early (Phase 1) rather than discovering this at launch. |
| AI4Bharat gated model approval is delayed. | Apply in Phase 0; have Whisper/Kokoro as an English-only fallback path if Tamil access is delayed. |
| Concurrent users during peak periods (admission season) exceed what one GPU can serve smoothly. | Load-test before launch; have a queueing/waiting-message fallback for overload. |

## 11. Success Metrics (proposed — confirm with college stakeholders)

- Answer accuracy on a test set of ~50 real ACT-specific questions (target: define acceptable threshold with a faculty reviewer).
- p95 end-to-end response latency (STT start → TTS audio start).
- Uptime during business hours.
- Visitor engagement (widget opens, completed conversations) — needs a baseline, since this is a new feature.

## 12. Cost Summary

- No recurring third-party API fees.
- Hardware is already available (per your confirmation), so no new capex.
- Real ongoing cost is engineering/maintenance time — budget this explicitly rather than treating it as free.

## 13. Open Questions

1. Exact GPU model and VRAM on the college server — not yet known, Prakash will confirm with IT.
2. ~~Who owns this system after you graduate~~ — **Resolved:** Prakash will manage it, both now and after graduation.
3. ~~Which college content sources form the RAG knowledge base~~ — **Resolved:** Agni College of Technology's own content (admissions, courses, placements, FAQs).
4. ~~Confirmed priority: is Tamil support a hard requirement or a nice-to-have for v1~~ — **Resolved:** English is first priority; Tamil is a nice-to-have.
