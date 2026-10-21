# Calling Agent — Build Plan (own pipeline, Cartesia voice)

**Written:** 2026-10-18. **Status:** approved by Prakash on 2026-10-18 — build the DIY way, matching every Indus capability (he is sending the Indus screenshots and settings; they go into a parity table here).
**Replaces, once approved:** the Sarvam Indus path in `Calling_Agent_Build_Plan.md`
§2–§4 (chosen 2026-09-30). That file's §9 "DIY, deferred" is the starting point
here; this plan updates it for what was built and learned since.

## 1. What this is

The phone agent becomes the website voice bot's pipeline answering the college
phone line:

| Job | Today (Sarvam Indus) | Planned |
|---|---|---|
| Phone line | Number rented inside Indus (Vobiz underneath) | Our own Vobiz number, audio streamed to our server |
| Listening | Indus (Sarvam) | Sarvam Saaras v4, 8 kHz phone audio |
| Answers | Indus's model, calling `get_act_answer` one question at a time | `rag-backend` with the full call history, same as the website bot |
| Speaking | Indus voice "Ritu" | Cartesia, the same voice as the website bot |
| Turn-taking, interruptions, language switch | Indus settings | Our `turn_taking.py` and `conversation_router.py` |
| Records (marks, attendance), transfer numbers | `erp-lookup` via Indus API tools | Same `erp-lookup` service, called by our agent |
| Transfer to staff, quiet-caller nudge, call cap, recording | Indus settings | Built by us |

Why: one voice and one brain on web and phone, Tamil/Tanglish handled by the
pipeline we tuned, and lower running cost. Price: three to four weeks of build,
and we own the reliability that Indus provides today.

Cartesia's own phone agents were ruled out on 2026-10-09: they cannot listen in
Tamil, and their numbers are US-only (see the "Cartesia vs Sarvam" doc).

## 2. Where things stand (2026-10-18)

**Already built and reusable**

- `rag-backend` (this repo): answers, lead capture to the Google Sheet (`source=phone`
  works), the three reply styles, phone-length replies, no "bro"/"anna", and
  `POST /calling-agent/ask` (one question at a time, for Indus).
- `voice-pipeline` (this repo): Sarvam listening, Cartesia speaking, turn-taking,
  barge-in rules, EN/TA/Tanglish router, greeting cache, transcript + audio logging
  with 90-day prune, latency logging, scripted scenario tests.
- `C:\ACT-Calling-Agent\erp-lookup` (not a git repo): FastAPI service, `/identify`,
  `/student`, `/class`, `/route`, `/health`; SQLite from marks/attendance PDFs;
  date-of-birth check, 3-try lockout, audit log; 18 tests pass on fake contacts.
- Paste-ready wording for the phone persona, languages and numbers:
  `Calling_Agent_Inbound_Indus_Changes.md`, `Calling_Agent_Outbound_Indus_Changes.md`,
  `Calling_Agent_Outbound_Config.md`. The rules carry over even though Indus goes.
- Pricing and carrier rules: "Cartesia vs Sarvam for Agni AI" doc, tab
  "Indian SIP carrier pricing".

**Working today on Indus (keep it live until the new agent passes)**

- Real inbound calls answered in English and Tamil; ERP lookup; lead capture;
  transfer worked once the tunnel was fixed. Deployed v7, draft v30.

**Not done anywhere yet**

- Stable hosting (dev uses ngrok and a Cloudflare quick tunnel).
- Real ERP contacts, and marks/attendance from the same batches.
- College sign-off: recording consent, retention, what parents may hear, whose
  name the number is in (current Indus number is under an individual's KYC).
- Department transfer routing tested with the tunnel up.
- Outbound calling (needs DLT) and call sentiment.

## 3. Technical facts this plan rests on (checked 2026-10-18)

- **Vobiz → Pipecat:** Vobiz streams call audio over a WebSocket (`<Stream>` in its
  answer XML), 8 kHz μ-law by default. The adapter is `pipecat-vobiz` 0.0.3 on PyPI
  (`VobizFrameSerializer`, community package, needs `pipecat-ai>=0.0.80`), used with
  the same `FastAPIWebsocketTransport` the website pipeline already uses. It converts
  μ-law and resamples. Small (one file): vendor it if it goes stale.
- **Sarvam listening at 8 kHz:** the streaming endpoint accepts `sample_rate=8000`
  as a connection setting; Pipecat 1.12's Sarvam STT allows 8000 and 16000. We can
  also resample phone audio to 16 kHz. Pick by test.
- **Cartesia speaking:** asked for 8 kHz PCM, sent to the serializer for μ-law.
- **Quiet callers:** Pipecat 1.12 has a user-idle controller for nudges.
- **Calls must be handled in India.** Vobiz and Plivo both state that call audio
  for Indian numbers must stay on servers in India; Plivo fails calls whose endpoint
  is abroad. So the phone agent must run on an Indian server, also for testing
  (ngrok's default endpoint may be abroad; check its India region first).
- **Vobiz India rates** (rate card Prakash shared 2026-10-09, before tax): number
  ₹500/month + ₹100 setup; WebSocket-streamed calls ₹0.44/min; 3 simultaneous calls
  included, 10 more ₹4,990/month; recording ₹0.10/min.

## 4. Decisions needed before building

1. **Approve replacing Indus** with our own agent (reverses 2026-09-30 / 2026-10-05).
2. **Where the code lives.** Recommended: make `C:\ACT-Calling-Agent` a git repo
   with `erp-lookup/` and a new `phone-agent/`, as CLAUDE.md asks (no telephony or
   ERP code in this repo). The phone agent imports the shared voice modules from
   `ACT-Voice-Assistant/voice-pipeline` rather than copying them, so turn-taking and
   language fixes land in both at once.
3. **How the agent calls the records tools.** Recommended: the phone agent declares
   `identify_caller`, `get_student_report`, `get_class_summary`,
   `get_transfer_number` and `end_call` as tools in its request; `rag-backend` passes
   tools it does not own back to the caller instead of swallowing them, and the
   phone agent runs them against `erp-lookup`. Keeps ERP logic out of this repo and
   keeps one brain. Needs a small `rag-backend` change (§5 step 6).
4. **Server.** One Indian-region VM for `rag-backend`, `voice-pipeline`,
   `phone-agent`, `erp-lookup`. Azure Central India fits the existing Azure account;
   AWS Mumbai also works.
5. **Vobiz account and KYC entity:** the college's (GST) or an individual's.
6. **Wording sign-offs:** greeting with AI and recording notice, what records may be
   read to parents, retention period for call audio (web uses 90 days).

## 4a. Indus parity — inbound (from `Indus_Inbound_Agent_Capture.md`)

Every inbound capability Indus gives us today, and how the DIY agent provides it.
"Exists" = already in `rag-backend` / `voice-pipeline`; "Build" = new work; the
phase letter points to §5.

| Indus capability | DIY equivalent | Status |
|---|---|---|
| Greeting with recording notice | Pre-rendered greeting clip (`greeting_cache.py`), phone wording | Exists, new text — A |
| Instructions: persona, speaking style, language, numbers, facts, guardrails, closing | `rag-backend` persona + style rules (`main.py`), plus a `source=phone` block carrying the phone-only rules (records, transfer, closing, nudges) | Mostly exists — B/C |
| Facts variables (collegeName, admissionsPhone, officeHours, current_date) | Phone prompt block filled from config at call start | Build — B |
| `get_act_answer` (one question at a time) | Replaced: every turn goes to `rag-backend` with the full call history | Exists — B |
| Lead capture via `get_act_answer` sentence | `capture_lead` tool already in `rag-backend`, forced on phone-number turns | Exists — B |
| `identify_caller` on call start → `caller_role`, `caller_first_name` | Phone agent calls `erp-lookup` `/identify` with Vobiz caller ID before the greeting; role goes into the prompt block | Build — C |
| `get_student_report` (reg_no, DOB, topic, subject) | Tool declared by the phone agent, run against `/student`, `speech` returned to the model | Build — C |
| `get_class_summary` (topic) | Same, `/class` | Build — C |
| `get_transfer_number` + `call_transfer` | Phone agent calls `/route`, then Vobiz transfer; reception as the safe default | Build — C |
| End Interaction | `end_call` tool → hang up after the goodbye | Build — C |
| Change Output Language / auto language switch (Quick), EN + TA allowed, start English | `conversation_router.py` (EN / TA / Tanglish), starts English | Exists — B |
| Handle Voicemail + voicemail message | Inbound only: not needed (a caller can't be voicemail). Keep for outbound | Outbound |
| Query Knowledge Base (off) | Not used | — |
| Let callers interrupt | `turn_taking.py` barge-in rules | Exists, tune on phone — B |
| Eagerness to respond: Balanced | Smart-turn / silence timeout settings | Exists, tune on phone — B |
| Volume threshold: Normal | Silero VAD threshold | Exists, tune on phone — B |
| Voice Ritu (EN + TA), speed 1.10, pitch 0, volume 1.0 | Cartesia voice from `CARTESIA_VOICE_ID`, `CARTESIA_SPEED` / `CARTESIA_VOLUME` | Exists — B |
| Pronunciation dictionary (1 entry) | `speech_text_filter.py` PRONUNCIATIONS | Exists; entry unknown |
| Output numbers in Indic: On | Not copied: our phone-number and year rules come from the prompt and the text filter. Decide after test calls | Decide |
| Model temperature: Compliant | Sarvam 105B temperature in `rag-backend` | Check value — B |
| Background sound: Quiet office | Optional low ambience mixed into the output | Optional — C |
| Nudge quiet callers (10 s, 10 s) + hang up | Pipecat user-idle controller, same two messages, in the caller's language | Build — C |
| Max call length 15 min | Call timer → goodbye and hang up | Build — C |
| Call forwarding to `transfer_number` (Vobiz) | Vobiz transfer API | Build — C |
| Post-call outputs: admission_interest, call_disposition, call_summary, caller_intent, caller_type, escalation_reason, language_used, lead_course, lead_name, lead_phone; success = lead_captured | Post-call extraction with Sarvam 105B over the saved transcript (same pattern as `sentiment_report.py`), written beside the transcript and to a calls sheet | Build — C |
| Monitor Calls (logs, transcripts) | Existing transcript + audio logger, phone folder; 90-day prune | Exists — C |
| Automated tests | `scenario_test.py` adapted to a phone WebSocket, plus the 24-check list | Build — D |
| Versions / drafts / deploy | Git commits + a deploy step | Process |

Fixes to carry over rather than copy: speak `get_act_answer`-style answers "as it is"
(Indus's instructions contradict themselves), default transfer to reception (Indus
defaults to admissions), and start speaking speed at 1.0 rather than 1.10.

## 4b. Indus parity — outbound (from `Indus_Outbound_Agent_Capture.md`)

Built after inbound, once DLT registration allows outbound calls. Everything inbound
already has (answers, languages, voice, turn-taking, nudges, call cap, logging,
post-call extraction) is reused; this lists only what outbound adds.

| Indus capability | DIY equivalent | Status |
|---|---|---|
| Placing calls to a lead list (Indus outbound campaigns) | Vobiz outbound call API, driven by a small dialler reading the leads Google Sheet; calling window 9 AM–7 PM, no Sundays; retry rules | Build — later |
| Per-lead input variables (lead_name, lead_course, enquiry_source, enquiry_date) | Passed from the dialler into the call's prompt block; same defaults ("there", "engineering", "our website", "recently") | Build — later |
| Greeting with name, course and recording notice | Rendered per call (cannot be pre-cached: it contains the name) | Build — later |
| Five phases + scenario branches (confirm person, interest, questions, next step, close; not a good time, wrong number, do not call, handoff) | Outbound prompt block in `rag-backend` for `source=outbound` | Build — later |
| Handle Voicemail + voicemail message | Voicemail detection (greeting phrases / answering-machine detection from Vobiz if offered), then speak the message and hang up | Build — later |
| Transfer to `admissionsPhone` | Vobiz transfer to the admissions number | Build — later |
| Single nudge after 20 s, then hang up | Same idle controller, outbound settings | Build — later |
| Output variables: call_outcome (success = visit_booked), call_summary, callback_time, do_not_call, interest_level, language_used, visit_date | Post-call extraction; results written back to the lead's row; do_not_call = yes blocks future dials | Build — later |
| Voice Ritu (EN) / Ishita (TA), speed 1.15 | One Cartesia voice for both, as inbound | Exists |

Before any real outbound call: DLT registration, consent check, and the 2026-10-09
outbound wording fixes (they were never applied to Indus v4).

## 5. Build steps

Each step ends with a check. Effort figures are estimates.

### Phase A — Foundations (about 1 week)

1. **Repo and hosting.** Make `C:\ACT-Calling-Agent` a git repo (keep `.env`,
   `erp.db`, `raw/`, `fake_contacts/` ignored). Rent the Indian VM; deploy
   `rag-backend` and `erp-lookup` with fixed HTTPS addresses. *Check:* health URLs
   answer from the VM; Indus tools pointed at them still work.
2. **Vobiz test number.** Open the account, rent one local number, point its answer
   URL at the VM. *Check:* a call reaches our server and we see the caller's number.
3. **Audio loop.** `phone-agent` service: Vobiz `<Stream>` → `VobizFrameSerializer`
   → greeting clip → hang up. *Check:* a real call hears the greeting cleanly.

### Phase B — Conversation (about 1 week)

4. **Listen, answer, speak.** Reuse `build_pipeline`'s pieces with the phone audio
   path: Sarvam STT at 8 kHz (or resampled 16 kHz), router, `rag-backend`
   `/v1/chat/completions` with full history and `source=phone`, Cartesia at 8 kHz.
   *Check:* the 12 questions in the "Test questions" tab answered by phone in
   English, Tamil and Tanglish.
5. **Phone turn-taking.** Tune VAD, smart-turn and barge-in on real phone audio
   (narrowband, line noise). *Check:* checks 13 to 18 of the test list pass by phone.

### Phase C — Phone features (about 1 to 1.5 weeks)

6. **Records tools.** `rag-backend`: return caller-defined tool calls to the caller
   (step 4.3). `phone-agent`: run them against `erp-lookup`; caller ID from Vobiz
   goes into `/identify` at call start. Port the records rules from
   `Calling_Agent_Inbound_Indus_Changes.md` (DOB check, never read records unverified).
   *Check:* fake student, parent, faculty and unknown callers each behave correctly.
7. **Transfer.** `get_transfer_number` then Vobiz's transfer API; reception as the
   safe default. *Check:* reception, admissions and placement each ring their own number.
8. **Call housekeeping.** Quiet-caller nudge then goodbye, 15-minute cap, end-call,
   recording and transcript logging (existing logger, phone folder), consent line in
   the greeting. *Check:* checks 19 to 24 of the test list pass.

### Phase D — Launch (a few days)

9. **Soak test.** Ten or more real test calls across all three styles; compare with
   Indus on the same questions; read the `[latency]` lines.
10. **Cut-over.** Before go-live: blank `ERP_TEST_CALLER`, load real contacts, remove
    fake ones. Forward the college SIM to the Vobiz number by USSD. Pause the Indus
    deployment; do not delete it. *Check:* first week of real calls reviewed.

### Later

- **Outbound follow-up:** DLT registration as Principal Entity, a 140/160-series or
  landline-series number by purpose, consent, calling hours (9 AM to 7 PM, no Sundays
  per the 2026-10-06 decision). Port `Calling_Agent_Outbound_Config.md`.
- **Call sentiment:** start with the existing post-call `sentiment_report.py` over
  phone transcripts; real-time scoring is a separate build.

## 6. Cost per month (2,400 call minutes, before tax)

| Item | Per month |
|---|---|
| Vobiz streamed calls (₹0.44/min) + number | ₹1,556 |
| Sarvam listening (₹0.50/min) | ₹1,200 |
| Sarvam 105B answers | ₹120 to ₹312 |
| Cartesia speaking (648K to 1.08M characters) | ₹2,430 to ₹4,040 at Startup-plan rates (plan shared with the website bot) |
| Share of the Indian VM | ₹500 to ₹1,000 |

Indus today: ₹4.90/min = ₹11,760/month. The own-agent route is roughly ₹5,800 to
₹8,100/month at this volume. Watch two limits: Vobiz includes 3 simultaneous
calls (Indus Starter allows 4), and Cartesia's Startup plan allows 5 simultaneous
voices across web and phone together.

## 7. Risks

- **Phone turn-taking is ours.** Our timings are not yet tuned on a real microphone,
  let alone a phone line; this is the step most likely to take longer.
- **Community adapter.** `pipecat-vobiz` is 0.0.3 from an individual; be ready to
  vendor and fix it.
- **Student data leaves India as text** when Cartesia speaks a records reply. An
  India deployment of Cartesia needs an Enterprise account. College sign-off needed.
- **Three vendors on every call** (Vobiz, Sarvam, Cartesia) plus our server.
- **SIM forwarding and caller ID:** forwarded calls must still carry the original
  caller's number; the Indus tests suggest they do, recheck on Vobiz.
