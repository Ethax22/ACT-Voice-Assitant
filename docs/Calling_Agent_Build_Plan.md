# Calling Agent — Build Plan

> **2026-10-18:** a plan to replace Indus with our own pipeline (Sarvam listening,
> Cartesia voice, Vobiz line) is in `Calling_Agent_DIY_Build_Plan.md`. Approved by
> Prakash on 2026-10-18 and being built (`Calling_Agent_DIY_Implementation_Plan.md`).
> Indus still answers the college's calls until the cut-over (Phase 6 of that plan);
> this file describes that Indus setup.

**Status:** reference-only planning doc. Per `CLAUDE.md`, the Calling Agent is
a separate product built in its **own codebase**, not this one — this file
just holds the plan until that codebase exists, so the reasoning isn't lost.
Move it there when that repo is created. The only link to this repo: once
`rag-backend` is deployed at a stable URL (see
`Implementation_Plan_API_Based.md` phase 12), the Calling Agent points at
that same URL, sharing one brain with the web bot.

**Source:** `Calling Agent - Build Plan.pdf`, amended 2026-09-29 after
confirming the college's existing number is a **regular mobile SIM (Airtel/
BSNL/Jio)**, not a landline/EPABX — the original plan's telephony section
assumed the latter.

**Chosen path for v1 (decided 2026-09-30):** build on **Sarvam Indus**
(their bundled no-code voice agent platform — see §7.1), not the DIY
Pipecat/Exotel stack originally planned. DIY is cheaper per-minute (§7.2–7.3)
but costs real engineering time to build what Indus already provides
out of the box (call-flow builder, tool-calling, turn-taking, transfer,
voicemail handling). DIY is **deferred, not abandoned** — revisit once real
call volume is known and the cost gap (~₹5,000–6,000/month at 40 Calling-
Agent-hours) is worth the build effort. §2 and §4 below now describe the
Indus-based path; the original DIY architecture/phases are kept in §9 for
when that revisit happens.

---

## 1. Scope & goals

A bilingual (English/Tamil) inbound + outbound phone agent riding on ACT's
existing phone number, that treats known ERP contacts (students/parents/
faculty) differently from unknown callers (alumni, industry, prospective
students, general public), forwards to a human when needed, and scores
admission-call seriousness in real time.

**Must-have for launch (v1, inbound-only):**
- Attach to the existing number without porting
- English + Tamil conversation, no talking over the caller
- ERP lookup at call start → branch known vs. unknown caller flow
- Warm transfer to a human at a number you specify
- Full call recording + transcription
- Recording-consent notice at call start

**Phase 2 (outbound + compliance-gated):**
- Outbound calling (admissions follow-up)
- Real-time seriousness/sentiment scoring on admission calls
- TRAI DLT registration completed first — outbound cannot launch without it

**Explicitly out of scope for v1:** self-hosted/GPU models (API-only),
outbound calling (gated on compliance), a proven Tamil-capable sentiment
vendor (none independently verified — build a custom classifier instead).

## 2. Architecture & stack (Sarvam Indus path, chosen for v1)

| Component | Choice | Notes |
|---|---|---|
| **Telephony (inbound)** | **"Rent from Sarvam"** in Indus's "Phone numbers" section (₹59/month launch offer, Vobiz-powered under the hood — confirmed 2026-09-30) + **unconditional call forwarding from the existing mobile SIM** to it — same forwarding mechanism as §3 describes, just pointed at a Sarvam DID instead of an Exotel one. **Use "Rent from Sarvam," not "Bring your own telephony" with Exotel** — see the Human transfer row below for why this specific choice matters. | No porting; the SIM stays active, just forwards to Sarvam's number |
| **Telephony (outbound, Phase 2)** | A **separate**, DLT-registered number, via Indus's **"Outbound campaigns"** feature (batch/event-driven outbound calling, confirmed to exist) | Still needed regardless of Sarvam vs. Exotel — TRAI rules require outbound calls from a registered number series, not a forwarded personal SIM — see §5. **Sarvam does not handle DLT compliance for you** — confirmed via their own docs: the platform supports running the campaigns, but registering as a Principal Entity, templates/headers, and DND/NCPR scrubbing are entirely on the college, same as the DIY path. Outbound-specific concurrency/rate limits (does the 4-channel Starter-tier cap in the row below apply to outbound too, or is it separate?) are **not yet confirmed** — check the "Outbound campaigns" page directly in the account before relying on it at any real volume |
| Orchestration | **Sarvam Indus's no-code builder** (`indus.sarvam.ai`) — Agents, Workflows, Tools, Settings | No self-hosted Pipecat/LiveKit service to run or maintain for v1 |
| STT/TTS/LLM | Sarvam's own managed model, configured via the Instructions tab (persona/prompt) — **not** a direct call to `rag-backend` | See the one-shared-brain workaround below |
| One-shared-brain answers | A custom **API Tool** in the agent's Tools tab, calling `rag-backend`'s `/v1/chat/completions` (or a thin wrapper) for any substantive ACT question — instructed via the persona prompt to always use it rather than answer from its own knowledge | **Blocked on `rag-backend` having a stable public URL** — the current ngrok dev tunnel is not reliable enough for a live phone line; deploy properly first (Implementation Plan phase 12) or at minimum keep the tunnel running continuously for testing |
| ERP caller lookup | A custom **API Tool** pointed at a small lookup endpoint (caller ID → known/unknown + role) | Same defensiveness note as §3: treat missing/bad caller ID as unknown |
| Human transfer | Indus's built-in **"Call forwarding"** toggle — confirmed working 2026-09-30 (Destination type: phone number / SIP URI / auto-detect from a variable; Value type static or dynamic; Forward-to number field) | **Resolved:** the toggle's "Only supported for Vobiz" note refers to Indus's own telephony backend — "Rent from Sarvam" numbers run on Vobiz by definition (confirmed on the Phone Numbers page), so warm transfer works as long as you don't switch to "Bring your own telephony" with a different provider (Exotel, Smartflo, Pulse, InTalk, Twilio are the alternatives listed — none of them get transfer support per this note) |
| Sentiment/seriousness scoring | Not a built-in Indus feature (not seen in Tools/Settings) — likely another custom API Tool, Phase 2 only | No verified Tamil-capable vendor exists either way — build, don't buy |
| Call recording + storage | Indus's "Monitor Calls" gives call logs/transcripts/analytics — **confirm whether raw audio recordings are retained and exportable**, or whether that still needs building separately | Plus consent-notice playback at call start, same as before |
| **Concurrent call capacity** | **Starter tier (the only one live): 4 concurrent calls, 20 CPM (new calls/minute)** — confirmed on the pricing page's "Rate limits" | A 5th simultaneous caller while all 4 channels are busy gets **unconfirmed** treatment (busy tone / voicemail / queued) — ask Sarvam directly before launch. Likely fine for normal traffic but a real risk during **admissions-season peaks** (the source PDF's own estimate: 5,000–12,000 min/month vs. a normal month's 1,500–3,000). Business (30 channels)/Scale (120 channels) aren't live yet; Enterprise (custom concurrency) is sales-only |

## 3. The telephony correction (read this before Phase 1)

The original plan's "attach without porting" language implicitly assumed a
landline/EPABX that Exotel connects to via a SIP trunk (vSIP). That's not
what exists here — it's a plain mobile SIM. The actual mechanism is
**unconditional call forwarding**:

1. Exotel provisions a virtual number (DID) for the agent.
2. You enable unconditional call forwarding on the college's SIM, pointing
   at that DID — via a carrier USSD code (e.g. `*21*<Exotel number>#` to
   enable, `##21#` to disable; exact codes vary slightly by carrier — Airtel/
   BSNL/Jio each have their own).
3. Every call to the real number now rings Exotel's DID instead, which
   routes into your agent.

This is a standard, well-supported pattern (Exotel and comparable providers
all support it) — just a different one than the original plan described.

**Three things this raises, not covered in the original plan:**

1. **Caller-ID (ANI) passthrough is not guaranteed.** The ERP lookup feature
   (§2) depends on Exotel seeing the *original caller's* number, not the
   forwarding number. Indian carriers generally pass the original CLI
   through on forwarded calls, but this varies by carrier/plan and **must be
   tested with a real forwarded call before building the ANI-lookup API**,
   not assumed. Build the lookup defensively: if ANI comes back missing or
   clearly wrong, treat the caller as unknown rather than crashing or
   mis-branching.
2. **Personal-SIM terms of service.** Most personal/prepaid or standard
   postpaid plans prohibit call-center-style automated call handling.
   Confirm the SIM's plan type (personal vs. registered business plan) with
   the carrier before going live — unusual call-volume patterns on a
   personal-tariff SIM risk being flagged or throttled independent of
   anything you build.
3. **Outbound needs a different number anyway.** TRAI's DLT/commercial-
   calling rules require outbound calls to originate from a number tied to
   your registered Principal Entity, not a forwarded personal mobile number.
   Practical setup: the forwarded SIM number stays the public-facing number
   for **inbound**; a separate Exotel-provisioned, DLT-registered number
   handles **outbound** admissions follow-up (Phase 2). Plan for two numbers
   from the start, not one.

## 4. Phased build plan (Sarvam Indus path)

| Phase | Milestone | Key tasks |
|---|---|---|
| 0. Remaining blocker | Know it works before going live (not before building/testing) | Get `rag-backend` reachable at a stable public URL for actual launch — the existing ngrok dev tunnel is fine for building and testing the Tool integration now, but not reliable enough for real callers. (Warm transfer's provider question is resolved — see §2: use "Rent from Sarvam," not Exotel BYO telephony.) |
| 1. Create the agent | A blank Indus agent exists, scoped to ACT | `Agents → Create from scratch`; write the Instructions (persona/greeting/objective) — draft in §4.1 below |
| 2. Wire the shared brain | Agent answers ACT questions via `rag-backend`, not its own knowledge | Add the `get_act_answer` API Tool (§4.1); test in the builder's chat/call preview before going further |
| 3. Set up telephony | Calls to the existing number reach the agent | Rent a number in Indus's "Phone numbers" section; enable unconditional call forwarding on the college SIM to that number (same USSD mechanism as the original Exotel plan, §3); confirm with a real test call |
| 4. ERP caller lookup | Known vs. unknown branching works | Add a `check_caller_erp_status` API Tool against the college ERP; test with real student/parent/faculty numbers; fall back to "unknown" on missing/bad caller ID (§3's caller-ID risk still applies) |
| 5. Bilingual conversation tuning | Turn-taking and language switching feel right | Configure Settings: Languages allowed (add Tamil if not already), Starting language, Auto-detected language switch, "Let callers interrupt," Eagerness to respond — test on real phone audio, not just the builder's preview |
| 6. Human transfer | Warm transfer works reliably (pending Phase 0a) | Configure the "Call forwarding" toggle with department transfer numbers; test hand-off |
| 7. Recording + consent | Every call recorded, consent played | Confirm what "Monitor Calls" retains (transcripts vs. raw audio); add a recording-consent + AI-disclosure line to the greeting/Instructions if not automatic |
| 8. Inbound launch (v1) | Live on the existing number, inbound only | Soft launch; monitor call quality and lookup accuracy via Indus's Agent analytics |
| 9. DLT registration & compliance | Legal clearance for outbound | Register as Principal Entity on the DLT platform; register templates/headers; provision the dedicated outbound number (Sarvam or otherwise) |
| 10. Outbound + sentiment (Phase 2) | Admissions follow-up calls live | Use Indus's "Outbound campaigns" once DLT clears; build a custom sentiment-scoring API Tool; validate against real labeled Tamil calls before trusting it |

### 4.1 Draft content to paste into Indus

**Instructions tab (persona), starting point — edit before using:**
> Persona: You are Agni AI, the voice assistant for Agni College of
> Technology, answering phone calls. Talk like a helpful senior student:
> warm, direct, brief — this is a phone call, not a lookup.
>
> Environment & Situation: Channel is voice telephony, inbound. Callers are
> students, parents, faculty, alumni, or prospective students asking about
> admissions, courses, placements, or campus life, or needing a human.
>
> Objective: For ANY question about ACT (courses, admissions, placements,
> campus life, fees, policies), always call the `get_act_answer` tool and
> speak its response — never answer from your own knowledge, so answers
> stay identical to the website assistant. Only use your own judgment for
> call-flow matters (greeting, clarifying which department, offering a
> human transfer).
>
> Speaking Style: Keep replies short — 2-4 sentences unless the caller
> clearly wants detail. Never use markdown or formatting symbols; say
> phone numbers and digit strings the way you'd speak them aloud.

**Tools to add:**
- `get_act_answer` — API Tool, POST to `rag-backend`'s `/v1/chat/completions`
  (or a thin wrapper endpoint if the request/response shape needs adapting
  to what Indus's API Tool expects), carrying the bearer auth key server-
  side in the tool's configured headers, never exposed to the caller.
- `check_caller_erp_status` — API Tool, POST to the ERP lookup endpoint
  (Phase 4, blocked on `CLAUDE.md`/§6's open question: does the ERP expose
  an API at all).

**Settings to check/set:** Languages allowed → include Tamil; Starting
language → English (or leave auto-detect to decide, consistent with
`CLAUDE.md` decision #5); Let callers interrupt → on; Nudge quiet callers →
on, with an ACT-appropriate message; Voicemail message → drafted once the
greeting/persona is finalized; Max call length → sized to a real ACT call
(15 min default seen in the builder may be enough, revisit after real usage).

## 5. Compliance workstream (runs in parallel, not after)

- **TRAI DLT registration** — register as Principal Entity, register call
  templates/headers, choose the right number series (service vs.
  promotional) before any outbound calling
- **DND/NCPR scrubbing** — outbound numbers checked against the do-not-call
  registry
- **Implied consent for follow-up calls** — confirm with counsel that a
  recent admissions enquiry counts as implied consent for a callback
- **AI-disclosure line** — agent states at call start it's an AI assistant
- **Call-recording consent notice** — played at the start of every call
- **Data residency check** — confirm whatever STT/TTS/LLM vendor mix you
  land on for calls touching Indian numbers is acceptable
- **Recording retention policy** — decide and document (the web bot settled
  on local disk / 90 days; consider the same here for consistency)
- **Personal-SIM ToS check** (new, from §3) — confirm commercial/automated
  call handling is permitted on the inbound SIM's plan, independent of DLT
  (which governs outbound specifically)

## 6. Open items only Prakash can provide/decide

- Which carrier (Airtel/BSNL/Jio) and plan type the existing SIM is on, and
  whether its ToS permits this use
- Whether the college ERP exposes any API, or whether ANI-lookup needs raw
  database access
- Transfer numbers per department (admissions, faculty, etc.)
- Legal sign-off on the implied-consent reasoning for outbound follow-up
  calls
- Whether to standardize STT/TTS on Sarvam-only here too, matching the web
  bot's pivot (§2)

## 7. Cost estimates (updated 2026-09-29, with real vendor numbers)

The original PDF's ₹4.4–11.3/min inbound estimate is superseded below —
several real, vendor-confirmed numbers are now available (not marketing-page
guesses), gathered by pulling up each vendor's actual pricing UI.

### 7.1 Build-or-buy: DIY vs. Sarvam's bundled agent platform

Sarvam has its own end-to-end no-code voice agent product — **Indus (aka
Samvaad/Voice Agents)**, at `indus.sarvam.ai` — a real alternative to hand-
building this on Pipecat. It bundles telephony + STT + LLM + TTS + a call-
flow builder + Tools (custom API/HTTP calls the agent can invoke mid-call,
which is what would satisfy the ERP-lookup requirement) + built-in turn-
taking, barge-in tuning, mid-call language auto-detection, voicemail
detection, and "nudge quiet caller" handling — all confirmed live in the
product UI, not from marketing copy.

**Important limitation, confirmed by going through the Agents builder
directly:** there is no field to point the agent's actual LLM at an external
endpoint. Its "Knowledge Bases" are Sarvam's own separate document store —
using it as-is means a **second, separate brain** from `rag-backend`,
conflicting with the one-shared-brain principle (`CLAUDE.md` decision #2).
**Workaround:** add a custom **API Tool** (the builder supports this
natively) that calls `rag-backend`'s `/v1/chat/completions` for any
substantive ACT question, with the agent's own instructions telling it to
always use that tool rather than answer from its own knowledge — this keeps
Sarvam's model as call-flow orchestration only, while the actual answers
still come from the shared backend.

Also unresolved: whether **warm transfer** (a "Call forwarding" toggle in
Settings) works with an Exotel-forwarded SIM number, or only with specific
named telephony partners — the settings text was cut off mid-sentence
("Only supported for Vobiz...") and needs confirming before committing to
this platform for v1.

### 7.2 Confirmed pricing

**Sarvam Indus, Starter tier (pay-as-you-go, the only tier live today —
Business/Scale are "coming soon"):**

| Item | Rate |
|---|---|
| Voice AI (STT+LLM+TTS+orchestration) | ₹4.50/min |
| Telephony rental (calls on a Sarvam number) | ₹0.40/min |
| Call forwarding to a human (warm transfer leg) | ₹0.40/min (phone) / ₹0.30/min (SIP) |
| Phone number rental | ₹59/month (launch offer, ₹159/month standard) |
| **All-in rate, no transfer** | **≈ ₹4.90/min** |

**Raw Sarvam API rates** (if building the Calling Agent's brain yourself,
same pattern as the web bot):

| Item | Rate |
|---|---|
| STT (real-time/streaming/batch) | ₹30/hour = ₹0.50/min |
| TTS | ₹3.00 per 1,000 characters |
| LLM (Sarvam 105B) | ₹29.28/1M input tokens, ₹73.20/1M output tokens |

**Telephony, DIY (Exotel or Plivo SIP trunk):** ₹0.90–1.40/min — Exotel
tends to run ~10–25% more than Plivo on pure per-minute, per a third-party
vendor-pricing teardown; get real quotes from both, this is not a vendor
quote. Exotel's own **AI & Automation** product (their bundled voicebot,
the actual competitor to Sarvam Indus) has **no public pricing at all** —
it's a sales-only "Get in Touch" form, so it cannot be compared numerically
yet. Exotel's Business Phone System plans (₹9,999–49,499, credit-based) are
telephony-only and don't include an AI agent.

**DIY Calling Agent, all-in per minute** (telephony + STT + TTS + LLM),
assuming the agent speaks ~30–50% of call time:

| | Low | High |
|---|---|---|
| Telephony | ₹0.90 | ₹1.40 |
| STT | ₹0.50 | ₹0.50 |
| TTS (30–50% speaking time × ~900 chars/min × ₹0.003/char) | ₹0.81 | ₹1.35 |
| LLM (rough, ~1,350 input + 75 output tokens/turn) | ₹0.05 | ₹0.13 |
| **Total** | **₹2.30/min** | **₹3.34/min** |

**DIY web voice bot, all-in per minute** (same STT/TTS/LLM rates, no
telephony since it's a browser connection, not a phone call):

| | Low | High |
|---|---|---|
| STT | ₹0.50 | ₹0.50 |
| TTS | ₹0.81 | ₹1.35 |
| LLM | ₹0.05 | ₹0.13 |
| **Total** | **₹1.40/min** | **₹1.94/min** |

### 7.3 Blended monthly estimate — 100 hours/month, 60% web bot / 40% Calling Agent

6,000 minutes total → 3,600 min web bot, 2,400 min Calling Agent.

| Scenario | Web bot (DIY) | Calling Agent | **Total/month** |
|---|---|---|---|
| **A — both DIY** | ₹5,040–6,984 | ₹5,520–8,016 (DIY) | **₹10,560–15,000** (midpoint ≈ ₹12,780) |
| **B — web bot DIY, Calling Agent on Sarvam Indus** | ₹5,040–6,984 | ₹11,819 (flat, confirmed) | **₹16,859–18,803** (midpoint ≈ ₹17,831) |

Going bundled on the Calling Agent alone costs **~₹5,000–6,000/month more**
than DIY at this volume — the price of Indus's no-code builder, ERP-tool
integration, and built-in call-handling features (turn-taking, transfer,
voicemail, language auto-detect) versus building all of that yourself.

**Caveats on every DIY number above:** TTS/LLM figures depend on assumed
speaking-time percentage and average call/turn patterns — not measured.
Telephony is a third-party rate-card estimate, not a quote from Exotel or
Plivo directly. None of the DIY totals include hosting (~₹500–1,000/month
for a small VM) or engineering time — Sarvam Indus's ₹4.90/min is the only
fully confirmed, quote-grade number in this section.

**Still needed before locking a decision:** a real quote from Exotel's AI
& Automation sales team (per §6, no self-serve pricing exists), the ₹-per-
credit conversion on Exotel's Business Phone System plans, and confirmation
of whether Sarvam Indus's warm-transfer feature works with an
Exotel-forwarded SIM number (§7.1).

## 8. Sentiment/seriousness scoring — still not built anywhere

Neither the Sarvam Indus path (§2) nor the original DIY plan (§9) has this
resolved beyond "build a custom classifier" — no vendor, bundled or DIY, has
a verified Tamil-capable sentiment tool. Treat this as unstarted regardless
of which path v1 launches on; it's Phase 2/outbound-gated either way (§4
step 10), so it doesn't block the inbound v1 launch chosen here.

## 9. Original DIY architecture & phases (deferred, not abandoned)

Kept for the cost-optimization revisit flagged in the "Chosen path" note at
the top of this doc — once real call volume is known and the ~₹5,000–6,000/
month gap (§7.3) is worth the engineering effort, this is the path to build.

### 9.1 Architecture & stack (DIY)

| Component | Choice | Notes |
|---|---|---|
| Telephony (inbound) | Exotel virtual number (DID) + unconditional call forwarding from the existing mobile SIM (§3) | No porting; the SIM stays active, just forwards to Exotel |
| Telephony (outbound, Phase 2) | A separate, Exotel-provisioned, DLT-registered number | Same TRAI constraint as the Indus path — see §5 |
| Orchestration | DIY (Pipecat/LiveKit, self-hosted) | You own the full orchestration; telephony/compliance glue with Exotel is your own build |
| STT/TTS | Sarvam Saaras/Bulbul, matching the web bot's Sarvam-unified pivot (`CLAUDE.md` decision #1) | Raw API rates, see §7.2 |
| LLM/RAG | Same shared RAG backend as the web bot (Sarvam 105B), called directly — no Tool-based workaround needed since you own the orchestration | Once deployed at a stable URL |
| ERP caller lookup | Custom-built | Small API: caller ID (ANI) → known/unknown + role — build defensively, see §3's caller-ID risk |
| Human transfer | Exotel call-bridging | Needs a maintained list of staff/department transfer numbers |
| Sentiment/seriousness scoring | Custom LLM classifier (Phase 2 only) | No verified Tamil-capable vendor exists — build, don't buy |
| Call recording + storage | To be built | Plus consent-notice playback at call start |

### 9.2 Phased build plan (DIY)

| Phase | Milestone | Key tasks |
|---|---|---|
| 0. Validate the telephony approach | Know the forwarding setup will actually work before building on it | Confirm SIM carrier + plan type with the carrier (commercial/automated call handling permitted?); confirm with Exotel support that forwarding-based attach is supported for this carrier; do a real test call through a forwarded line and check Exotel's logs for original-caller-ID passthrough |
| 1. Set up forwarding | Calls to the existing number reach Exotel | Provision the Exotel DID; enable unconditional call forwarding on the SIM via the carrier's USSD code; confirm with a real test call |
| 2. Attach agent to the number (inbound) | Calls route to the AI agent | Set up Exotel Voicebot WebSocket applet on the DID; connect to your self-hosted Pipecat/LiveKit orchestration |
| 3. ERP caller lookup | Known vs. unknown branching works | Build the ANI-lookup API against the college ERP; test with real student/parent/faculty numbers; fall back to "unknown" on missing/bad ANI (§3) |
| 4. Bilingual conversation + no-interrupt | Core conversation quality bar met | Reuse the shared Sarvam stack the web bot uses; test turn-taking specifically on phone audio (different quality profile than a browser mic) |
| 5. Human transfer | Warm transfer works reliably | Configure staff/department transfer numbers; test hand-off briefing |
| 6. Recording + consent | Every call recorded, consent played | Build storage; add recording-consent notice at call start; add AI-disclosure line |
| 7. Inbound launch (v1) | Live on the existing number, inbound only | Soft launch; monitor call quality and lookup accuracy |
| 8. DLT registration & compliance | Legal clearance for outbound, and provisions the separate outbound number from §3 | Register as Principal Entity on the DLT platform; register templates/headers; provision the dedicated outbound number |
| 9. Outbound + sentiment (Phase 2) | Admissions follow-up calls live | Enable outbound via the DLT-registered number (not the forwarded SIM); build the custom LLM-based seriousness classifier; validate against real labeled Tamil calls before trusting it |
