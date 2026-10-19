# Calling Agent — Implementation Plan (own pipeline, Cartesia voice)

**Written:** 2026-10-19. **Companion to:** `Calling_Agent_DIY_Build_Plan.md` (the what and why; this file is the how and in what order).
**Status (2026-10-20):** Phases 1, 2 and 3 complete, with the gaps listed in their Result notes (phone barge-in not tuned; the parent and Tamil records flows and the real Vobiz transfer not yet tested). Phase 4 not started. Completed steps are marked ✅ in the tables below; the "Result" notes under each phase say what was actually done and checked.
**Commit state:** Phases 1 and 2 are committed locally (`ACT-Voice-Assistant` `545cb04`, `ACT-Calling-Agent` `ad22b4d`; `ACT-Calling-Agent` is pushed only up to `1c17eaf`). Phase 3 is in the working trees, **uncommitted**.

## 0. Decisions made (Prakash, 2026-10-19)

| Topic | Decision |
|---|---|
| Replace Indus | Yes. Indus stays live until the new agent passes its tests, then is paused (not deleted). |
| Code location | `C:\ACT-Calling-Agent`. The GitHub repo for it **already exists** (Prakash, 2026-10-19), but the local folder has had **no `git init`** yet. Step 1.1 connects the two. Contains `erp-lookup/` (exists) and `phone-agent/` (new). |
| Student data and Cartesia | Accepted by the college: records replies may be spoken by Cartesia (Prakash, 2026-10-19). The old risk is closed. |
| Outbound | Built and tested **in the browser** like inbound, **before** compliance and verification are finished. Real-number tests start only after DLT, consent and verification are done (Phase 7). |
| Tool style | Same as Indus: the model calls named tools, our server runs them as HTTP calls to `erp-lookup`. Records tools keep their names and fields (`get_student_report`, `get_class_summary`). Built differently (2026-10-20): caller identification runs automatically before the greeting, not as a model tool; transfer is one tool, `transfer_call` (Indus used `get_transfer_number` then `call_transfer`); `end_call` is unchanged. |
| Vobiz account | Prakash's name for development. For launch: a **new** account and a **new** number in the college's name (no transfer). |
| Hosting | Azure (Central India) for development; the college's own host at launch. Everything address-like lives in `.env`. |
| Greeting | "Welcome to Agni College of Technology, this is Agni AI. This call may be recorded to help us serve you better. How can I help you today?" |
| ERP data | Use the existing fake contacts and marks/attendance until conversation and voice are tested; load real data last. |
| Where the work happens | In this session, with `C:\ACT-Calling-Agent` added as a working directory (one approval). |

## 1. What exists today (read from the code, 2026-10-19)

- `voice-pipeline/pipeline.py` `build_pipeline(websocket, language, session_id)`: **hard-wired to the browser**: `FastAPIWebsocketTransport` with `RawPCMSerializer(16000)`, audio in 16 kHz / out 24 kHz, greeting dict by language, persona by style, `X-Session-Id` header to the LLM. This is the function the phone agent must reuse, so it needs a small refactor first (§3, step 1.2).
- Already reusable as they are: `turn_taking.py` (Silero VAD, smart-turn, barge-in), `conversation_router.py` (EN/TA/Tanglish), `speech_text_filter.py`, `greeting_cache.py`, `conversation_logger.py`, `latency_metrics.py`, `scenario_test.py`.
- Installed in the voice-pipeline venv: `pipecat-ai 1.12.0`, `onnxruntime`, `soxr`, `numpy`, `aiohttp`. **Not installed:** `pipecat-vobiz`. Pipecat 1.12 ships serializers for Twilio, Plivo, Exotel, Telnyx, Vonage and others; check whether Vobiz's stream is Plivo-compatible before relying on the community package (§3, step 1.4).
- `rag-backend/main.py`: `/v1/chat/completions` always sends **only** `CAPTURE_LEAD_TOOL` to Sarvam and **ignores any `tools` the caller sends**; tool-call chunks are swallowed and resolved server-side (`_resolve_tool_calls`). It also **ignores client system messages** except a `reply_language:` line, so caller context (role, first name) cannot travel as a system message. `source` is hard-coded to `"voice"` on this route; `"phone"` is only set by `/calling-agent/ask`. The `phone` behaviours (no "bro"/"anna", spoken brevity) key off that `source`.
- `erp-lookup/api.py`: `/identify`, `/student`, `/class`, `/route`, `/health`. Returns ready-to-speak `speech` text. `ERP_TEST_CALLER` env stands in for the caller number when none is sent (browser tests); it must be blank at go-live.

## 2. Target design

```
Caller ──Vobiz──► phone-agent (WebSocket, 8 kHz μ-law)        Browser test page ──► phone-agent /test/ws
                    │  Pipecat pipeline (shared pieces from voice-pipeline)
                    │  Sarvam STT ─► router ─► LLM service ─► Cartesia TTS (8 kHz)
                    │                              │
                    │            rag-backend /v1/chat/completions   (X-Source: phone, caller context headers)
                    │                              │ tool call not owned by rag-backend is passed back
                    ◄──── tool handlers ───────────┘
                    └── erp-lookup  (/identify /student /class /route)      Vobiz API (transfer, hangup)
```

New repo layout:

```
C:\ACT-Calling-Agent\
  README.md  .gitignore  .env.example
  erp-lookup/                  (exists, unchanged)
  phone-agent/
    server.py                  FastAPI: /vobiz/answer (XML), /vobiz/ws, /test/ws, /test (page), /health
    call_pipeline.py           builds the phone pipeline from shared voice-pipeline parts
    phone_audio.py             8 kHz + μ-law round trip (also used to make the browser test sound like a line)
    tools.py                   tool schemas + handlers (HTTP to erp-lookup)
    call_control.py            end_call, transfer, quiet-caller nudges, 15-minute cap
    vobiz_api.py               hang up / transfer via Vobiz REST
    post_call.py               transcript -> structured call record (Sarvam 105B) -> file + Sheet
    prompts.py                 phone greeting text, nudge text, per-call facts block
    static/test.html           browser test page
    tests/
```

`phone-agent` finds the shared modules through `VOICE_PIPELINE_DIR` (path to `ACT-Voice-Assistant/voice-pipeline`) added to `sys.path`. On Azure both repos are cloned side by side. Turn-taking and language fixes then land in web and phone together.

## 3. Build steps

Order differs from the build plan on purpose: because the agent can be tested in a browser, everything except the real phone line is built first. Only the Vobiz protocol is checked early, by reading docs, so it cannot surprise us late.

Legend: **P** = Prakash does it, **C** = Claude does it, **P+C** = together. "Check" is the gate before the next step.

### Phase 0: In parallel, Prakash only (start now, none of it blocks Phase 1)

| # | Task | Done when |
|---|---|---|
| 0.1 | Create the Vobiz account (your name), complete KYC (Aadhaar + PAN, ~2 min) | Console login works |
| 0.2 | Rent one Indian local number | Number visible in console |
| 0.3 | Create the Azure VM (Central India), open ports 443 (and 22 for you only), point a DNS name or use the VM's public IP | You can SSH in; tell me the address |
| 0.4 | Put keys in `C:\ACT-Calling-Agent\.env` (names in §5) | File exists, never committed |
| ✅ 0.5 | Give me access to `C:\ACT-Calling-Agent` as a working directory in this session, and tell me the GitHub repo URL | Approved in the prompt; URL in chat |
| 0.6 | Answers to the open items in `Calling_Agent_DIY_Build_Plan.md` §4a: the one pronunciation entry in Indus, keep or drop "Output numbers in Indic", background sound yes/no, Sarvam temperature | Written in chat |

### Phase 1: Foundations (C, ~3 to 4 days)

| # | Task | Check |
|---|---|---|
| ✅ 1.1 | `git init` in `C:\ACT-Calling-Agent`, add the existing GitHub repo as `origin` (**needs the repo URL from P**), fetch first to see whether it already holds files (a README or licence) and merge with that, not over it. `.gitignore` covering `.env`, `erp.db`, `raw/`, `fake_contacts/`, `__pycache__`, logs; first commit of `erp-lookup/`; push only when P says so | `git status` clean; `git ls-files` shows no data files; nothing from the remote overwritten |
| ✅ 1.2 | **Refactor `voice-pipeline/pipeline.py`** so the browser-specific parts become a profile: serializer, input/output sample rates, greeting text per language, persona prompts, extra LLM headers (`X-Source`, caller context), extra tools, extra pipeline processors, log folder prefix. `WEB_PROFILE` reproduces today's values exactly. No behaviour change for the website | `scenario_test.py` results unchanged; a browser call to `/voice/en` behaves as before |
| ✅ 1.3 | `rag-backend` change (see §4): accept client `tools`, pass non-owned tool calls back, read `X-Source` and caller-context headers | New unit tests in §4 pass; website chat and web voice unchanged |
| ✅ 1.4 | **Vobiz protocol check (no number needed):** read Vobiz's stream docs; compare with Pipecat's bundled `PlivoFrameSerializer`; decide between bundled Plivo serializer, `pipecat-vobiz` 0.0.3, or vendoring `pipecat-vobiz` (one file). Write the finding into `phone-agent/README.md` | Decision recorded with the doc section it rests on |
| ✅ 1.5 | Scaffold `phone-agent/` (layout above), `/health`, config loading, `.env.example` | `GET /health` answers locally |

**Result of Phase 1 (done 2026-10-19):**

- **1.1** The GitHub repo was empty. `git init`, `origin` set, branch `main`, root `.gitignore` (`.env`, `*.db`, `raw/`, `fake_contacts/`, `venv/`, logs), first commit of `erp-lookup` pushed. A check of tracked files found no `.env`, `erp.db`, PDFs or contacts.
- **1.2** `PipelineProfile` added to `voice-pipeline/pipeline.py`; `build_pipeline(ws, language, session_id, profile=None)`. Fields: `serializer`, `audio_in_rate`, `audio_out_rate`, `tts_rate`, `greetings`, `llm_headers`, `tools`, `function_handlers`. No profile = the old website values. `greeting_cache.ensure_rendered` takes an optional `text`. **Not added, on purpose:** persona override (the phone persona lives in `rag-backend`, step 3.2), extra processors and a log prefix (the phone just passes a `phone-` session id; processors arrive with 3.3 if needed). **Checked:** 2 new tests (`voice-pipeline/tests/test_profile.py`: default rates/serializer unchanged, override applied); website scenarios run live against local servers: `en-courses`, `en-followup-memory`, `en-hostel`, `en-ack-1`, `en-pause-1`, `ta-courses` all answered in range with no errors in the log (first audio 1.2 to 3.1 s; the 3.1 s was a cold first call). **Not checked:** the full scenario set, and that `llm_headers`/`tools`/`function_handlers` reach the LLM (verified in steps 2.2 and 3.1).
- **1.3** `/v1/chat/completions` now takes the caller's `tools`, `X-Source: phone` (anything else = `voice`) and `X-Caller-Role` / `X-Caller-Name` (cleaned of odd characters before reaching the prompt, then added as one line for phone calls). `capture_lead` is still resolved in the backend; any other tool call goes back to the caller as a normal OpenAI tool call in both streaming and non-streaming replies. If one turn holds both, the lead is saved and only the caller's call is returned. The streaming path now also passes `phone=` (it never did before). **Checked:** 7 new tests (`rag-backend/tests/test_tool_passthrough.py`, vendor calls faked); one live Sarvam call confirmed it returns a call to a caller-defined tool alongside `capture_lead` and accepts the tool result in the next turn. **Not checked:** that Pipecat's `OpenAILLMService` runs a handler from the chunks we send (first real test in 3.1).
  **Risk found, to fix in Phase 3:** `_should_force_lead_capture` fires on any 8+ digit run. A 12-digit register number spoken during the records flow would force a bogus `capture_lead` call. Step 3.2 must switch forcing off when the caller is identified as student/parent/faculty or the assistant just asked for a register number.
- **1.4** Vobiz protocol read from its docs and the `pipecat-vobiz` source; findings in `phone-agent/README.md`. **Decision: vendor the serializer** (`phone-agent/vobiz_serializer.py`, adapted for Pipecat 1.12). The pip package targets Pipecat 1.8 (never calls the base-class init) and Pipecat's Plivo serializer ignores the `start` event and hangs up through Plivo's API. Transfer is done by redirecting the call to our own XML (`<Dial>`), and hang-up is a socket `stop` plus a REST `DELETE`. **Still unverified until Phase 5:** exact JSON for `clearAudio`/`stop`, whether `streamId` is mandatory, the answer-URL content type.
- **1.5** `phone-agent/` has `server.py` (`/health`), `config.py` (loads `.env`, adds `VOICE_PIPELINE_DIR` to the path), `prompts.py` (your greeting), `requirements.txt`, `.env.example`, a `venv` (Pipecat 1.12.0, git-ignored). **Checked:** 4 tests pass (`/health`, audio in, audio out as mu-law 8 kHz, `start` event and barge-in `clearAudio`).

### Phase 2: Phone pipeline, tested in the browser (C writes, P tests, ~1 week)

| # | Task | Check |
|---|---|---|
| ✅ 2.1 | `phone_audio.py` + `/test/ws` + `static/test.html`: the page sends mic audio, the server squeezes it to 8 kHz μ-law and back (so it sounds like a phone line), and plays the reply the same way. A `?caller=9000000001` URL parameter stands in for caller ID | You can hold a call from the browser; audio is clearly narrowband |
| ✅ 2.2 | `call_pipeline.py`: phone profile (8 kHz in, 8 kHz out for Cartesia, Sarvam STT at 8 kHz or resampled to 16 kHz: **decide by testing both**, keep the one with fewer misheard words), greeting clip with the new wording pre-rendered through `greeting_cache.py` (new text, so a new cache entry), `X-Source: phone` | Greeting plays with no live TTS delay; a question gets a spoken answer in the same words the website bot would give |
| ✅ 2.3 | Run the 12 questions from the "Test questions" tab through the browser phone test in English, Tamil, Tanglish | Substance matches typed chat; no "bro"/"anna"; replies phone-length |
| ✅ (measured, not tuned) 2.4 | Phone turn-taking pass (VAD threshold, smart-turn wait, barge-in) with `phone_audio` noise/narrowband in the loop. Tune in `turn_taking.py` only through env-driven values so web is not affected | Checks 13 to 18 of the test list pass by browser; remaining tuning is marked "needs real phone" |
| ✅ 2.5 | Logging to a phone folder (existing logger, `phone-` session prefix), 90-day prune applies | Transcript and audio file appear per test call |

**Result of Phase 2 (done 2026-10-20):**

- **2.1** `phone-agent/phone_audio.py` (`BrowserPhoneSerializer`: browser audio is squeezed to 8 kHz mu-law and back in both directions), `/test` page (`static/test.html`, mic via AudioWorklet, plays 8 kHz, drops queued audio on barge-in) and `/test/ws`. The test routes switch off with `ENABLE_TEST_PAGE=0`. **Not checked by a human:** nobody has yet held a call from the browser page with a real mic and headphones; only the scripted caller has used the socket. **You should try it:** start the services (below), open `http://127.0.0.1:8002/test`.
- **2.2** `call_pipeline.py` builds the phone profile (8 kHz in/out, `X-Source: phone`, your greeting). The greeting is pre-rendered and cached (7.8 s). **8 kHz vs 16 kHz into Sarvam:** 27 shared English turns, word error 14.7% (8 kHz) vs 11.7% (16 kHz), exact matches 21 of 27 both; first audio median 1.45 s vs 1.72 s. A near tie on a small sample with a synthetic caller, so **8 kHz stays the default** (`PHONE_STT_RATE`); recheck on a real phone in 5.3. The caller number (`?caller=`) is accepted but unused until 3.1. `config.py` falls back to `voice-pipeline/.env` for any key missing from the root `.env`.
- **2.3** Used `scenario_test.py`'s 35 scripted scenarios (English, Tamil, mid-call switches) plus its 10 Tanglish ones, run through the phone line (`scenario_test.py --url ws://127.0.0.1:8002 --path /test/ws`, new `--path` option). The "Test questions" tab from the vendor-comparison doc was not available to me. Results: no errors in the server log; first audio median 1.55 s English, 1.70 s Tamil; no reply over 70 words; "okay" ignored while the bot talks 4/4; pauses respected 4/4; Tamil and Tanglish replies in the right style. **Bug found and fixed:** two Tanglish turns heard as English got "Hey bro" replies. The no-"bro" rule now applies to every reply on a phone call (`rag-backend/main.py`), with a test; the two scenarios re-run clean.
- **2.4 Measured, not tuned.** Barge-in on the phone line vs the website (same three `en-interrupt` scenarios): website 3/3 stopped, median 0.84 s; phone 2/3 stopped, 1.46 s (earlier phone runs: 3/4 at 1.0 s, 2/3 at 1.23 s). The miss is always the "Wait, actually tell me about placements instead" clip, whose words Sarvam never transcribed on the phone line. Both lines miss the 0.2 s target. A synthetic caller with no echo cannot show whether to change `BARGE_IN_SUSTAIN_SECS` or the VAD, so no timing was changed; **to tune on a real phone in step 5.3.**
- **2.5** Each phone test call writes `transcript.json` and `audio.wav` under `voice-pipeline/logs/<date>/phone-test-<id>/` (the website's log folder, told apart by the `phone-` prefix; 54 sessions after the runs). The 90-day prune runs at phone-agent startup.
- **Tests:** phone-agent 9, rag-backend 8, voice-pipeline 2, all passing.
- **To run locally:** `rag-backend` on 8000 (`uvicorn main:app --port 8000`), phone agent on 8002 (`cd phone-agent; venv\Scripts\python -m uvicorn server:app --port 8002`).

### Phase 3: Records, transfer, end call (C, ~1 week)

| # | Task | Check |
|---|---|---|
| ✅ 3.1 | `tools.py`: declare the five tools to the LLM context; handlers call `erp-lookup` with `ERP_API_KEY`; return the `speech` text unchanged. `identify_caller` also runs **before the greeting** from the caller number and puts role and first name into the LLM request headers | Fake student, parent, faculty and unknown callers each behave per `Calling_Agent_Inbound_Indus_Changes.md` rules |
| ✅ 3.2 | Phone prompt block in `rag-backend` for `source=phone` (records rules: ask register number and date of birth, convert to DD-MM-YYYY, read `speech` as it is, never read records unverified, offer a transfer on any non-`ok` status; closing; one "anything else?" at most). Port wording from the Indus changes doc, and apply the three carry-over fixes (answers spoken as they are, default transfer = reception, start speed 1.0) | Same checks as 3.1 plus wrong-DOB x3 gives the lockout message |
| ✅ 3.3 | `call_control.py`: quiet-caller nudge at 10 s and 10 s more then goodbye and hang up (Pipecat `UserIdleController`, messages in the caller's language), 15-minute cap, `end_call` tool (speak goodbye, then end the pipeline) | Checks 19 to 24 pass in the browser |
| ✅ 3.4 | Transfer: `get_transfer_number` -> `erp-lookup /route` -> say the line -> Vobiz transfer. In browser tests the transfer is **simulated** (logged, call ends); the real transfer is tested in Phase 6 | Each department returns its own number; unknown falls back to reception |
| ✅ 3.5 | `post_call.py`: from the saved transcript, Sarvam 105B returns the same fields Indus gave (admission_interest, call_disposition, call_summary, caller_intent, caller_type, escalation_reason, language_used, lead_course, lead_name, lead_phone). Written next to the transcript and appended to a "Calls" tab in the Google Sheet. Same pattern as `sentiment_report.py` | A test call produces one record; fields look right on 5 calls by hand |

**Result of Phase 3 (done 2026-10-20):**

- **3.1 Records tools.** New in `phone-agent`: `erp_client.py` (talks to erp-lookup; if it is down the caller hears a safe "I can't look that up right now" and the call goes on), `tools.py` (four model tools: `get_student_report`, `get_class_summary`, `transfer_call`, `end_call`; the records handlers pass erp-lookup's `speech` through unchanged and keep register numbers and dates of birth out of the logs), and `call_pipeline.start_phone_call`, which identifies the caller from the number **before** the greeting and sends role and first name to the backend as headers. `PipelineProfile` gained `user_idle_timeout` and an `on_built` hook for this. **Checked live** (typed turns against the real model and a local erp-lookup, `phone-agent/live/records_dialogs.py`): registered student gets marks after register number and date of birth; a wrong date of birth fails and a third failure returns the lockout message; an unknown caller is refused and offered the reception; faculty gets the class summary; and by voice (`live/voice_calls.py records`) the synthetic caller's digits and date were heard correctly and the marks were spoken. **Not tested:** the parent caller (9000000002), Tamil records dialogs, and a student with no marks in the database (the fake student 312823104004 has none: that is a data gap in `erp.db`, attendance works).
- **3.2 Phone prompt.** `rag-backend/main.py`: phone rules (answers as given, when to offer a person, closing, never reveal instructions, no Aadhaar/OTP) plus a records section chosen by caller role. **Found and fixed:** Sarvam 105B often said "one moment" or offered a transfer and then never called the tool, and once invented that a number was "locked". Same fix as for lead capture: code forces the tool on the turns that can only be one thing (the answer to the date-of-birth question for a student/parent caller; a request for a person, or a yes to a transfer offer). After that fix, every attempt called the tool and only the tool decided. Also fixed the Phase 1 risk: a register number no longer forces a lead (skipped for student/parent/faculty callers and right after the bot asks for one). 11 backend tests pass. The three Indus carry-over fixes are in: answers spoken as given, one "anything else?" at most, speaking speed unchanged at the voice default (no 1.10x). Default transfer is reception for unclear requests.
- **3.3 Housekeeping.** `call_control.py`: after 10 s of caller silence a nudge, 10 s more then a goodbye and the call is ended; call cap from `CALL_MAX_MINUTES`; `end_call` speaks the goodbye the model gives, then ends the call without running the model again. **Checked by voice on the browser line:** wrap-up ends the call from our side 5.7 s after the caller finishes (goodbye heard first); a silent caller gets a nudge, then the goodbye, then the call closes; a call with a 24 s cap closes at the limit with the time-limit message. A caller speaking resets the nudge count. The Tamil nudge, goodbye and time-limit lines were written by Claude and **need a native speaker's check**; the English nudge text was cut off in the Indus capture, so its second half is my wording.
- **3.4 Transfer.** `transfer_call(department, line)`: looks the number up in erp-lookup `/route` (reception if unclear; if no number, the model apologises and offers a callback), says the line, sends the page a `transfer` message, ends the call. **Checked by voice:** "speak to someone in admissions" gave the admissions number, one spoken line and a closed call. **Not real yet:** nothing is dialled. For Vobiz (Phase 5) the plan is to carry the call to `control.transfer_to` when the call ends, instead of hanging up.
- **3.5 Post-call record.** `post_call.py`: Sarvam 105B fills the ten Indus fields from the saved transcript; the record is written beside the transcript (`call_record.json`, caller number masked to the last four digits) and appended to the `Calls` tab (created with a header on first use). Four test calls produced sensible records. **One row was written to the real Calls sheet** to prove the write: session `phone-test-b1b2e…`, delete it if you do not want it. Labels are model judgements, not checked against hand-labelled calls.
- **Tests:** phone-agent 19, rag-backend 11, voice-pipeline 2, all passing. **Notes for you:** test caller 9000000001 was locked out for an hour by the lockout test (use 9000000003 for student flows); `live/` scripts are manual checks, not part of pytest.

### Phase 4: Azure deployment (P runs, C writes the steps, ~2 days)

| # | Task | Check |
|---|---|---|
| 4.1 | C writes `deploy/AZURE.md`: install Python, clone both repos side by side, venvs, `.env`, systemd services (or a process manager) for `rag-backend`, `voice-pipeline`, `phone-agent`, `erp-lookup`, nginx + HTTPS certificate, firewall | P can follow it start to finish |
| 4.2 | P deploys; C helps with logs | The four `/health` URLs answer over HTTPS from outside |
| 4.3 | Browser test page now served from Azure | A call from your laptop to the Azure agent works |

### Phase 5: Connect the real line (P+C, ~3 to 5 days)

| # | Task | Check |
|---|---|---|
| 5.1 | Point the Vobiz number's answer URL at `https://<azure>/vobiz/answer`; `<Stream>` to `/vobiz/ws` | A real call reaches the server and the log shows the caller's number |
| 5.2 | Audio loop: greeting clip heard cleanly on a real handset | You hear the greeting |
| 5.3 | Full conversation on a real handset; re-tune turn-taking on real audio; compare 8 kHz vs 16 kHz STT again | Checks 13 to 24 pass by real phone |
| 5.4 | Real transfer to reception, admissions, placement (use your own numbers as the targets first) | Each rings the right phone |
| 5.5 | Media-anchoring check: Vobiz fails calls whose server is outside India, so the Azure region must be India | No `violates_media_anchoring` hangups |

### Phase 6: Soak and cut-over (P+C, ~1 week)

| # | Task | Check |
|---|---|---|
| 6.1 | 10+ real test calls across English, Tamil, Tanglish; read the `[latency]` lines; compare with Indus on the same questions | Written comparison in `docs/` |
| 6.2 | College sign-offs: greeting wording, retention of call audio (student data via Cartesia is already accepted) | Written approval |
| 6.3 | Go-live checklist: new Vobiz account and number in the college's name, blank `ERP_TEST_CALLER`, load real contacts, remove fake ones, move services to the college host, forward the college SIM to the new number | Checklist all ticked |
| 6.4 | Pause Indus; review the first week of real calls | Review notes |

### Phase 7: Outbound (C builds and P tests in the browser first; real numbers only after compliance)

Reuses everything inbound has (answers, languages, voice, turn-taking, nudges, call cap, logging, post-call extraction). Adds only what `Calling_Agent_DIY_Build_Plan.md` §4b lists. Same browser-first order as inbound.

| # | Task | Check |
|---|---|---|
| 7.1 | Outbound profile in `phone-agent`: `/test/outbound?lead=...` in the browser page plays the lead's side. The greeting is rendered per call (it contains the name, so it cannot be pre-cached): lead name, course, enquiry source, recording notice; defaults "there", "engineering", "our website", "recently" | You hear a personalised opening in the browser |
| 7.2 | Outbound prompt block in `rag-backend` for `source=outbound`: five phases (confirm person, interest, questions, next step, close) and the branches (not a good time, wrong number, do not call, handoff). Apply the 2026-10-09 outbound wording fixes that were never applied to Indus v4 | Scripted scenario tests for each branch pass |
| 7.3 | Voicemail handling (speak the message, hang up), single nudge after 20 s then hang up, transfer to admissions | Simulated in the browser |
| 7.4 | Post-call extraction for outbound (call_outcome, call_summary, callback_time, do_not_call, interest_level, language_used, visit_date) written back to the lead's row; `do_not_call = yes` blocks future dials | Fields written for 5 test calls |
| 7.5 | Dialler: reads the leads Sheet, calling window 9 AM to 7 PM, no Sundays, retry rules, DND and consent checks before every dial. **Dials nothing real**: a dry-run mode logs who would be called | Dry run lists the right leads at the right times |
| 7.6 | **Real-number tests: only after** DLT, a 140-series number, consent records and verification are done (see `Outbound_DLT_Registration_Guide.pdf`) | P confirms compliance is complete in writing before the first real dial |

### Later
Sentiment over phone transcripts, the "Output numbers in Indic" decision, optional background sound.

## 4. The `rag-backend` change in detail (step 1.3)

Today a client-sent tool is dropped. Needed:

1. `/v1/chat/completions` reads the request's `tools`. It sends Sarvam **its own** `capture_lead` **plus** the client's tools.
2. In both the streaming and non-streaming paths, a tool call to `capture_lead` is resolved server-side as now. A tool call to any other name is **not** resolved: it is sent to the caller as a normal OpenAI tool-call response, and the turn ends. Pipecat runs the handler and calls again with the result in the history.
3. `_build_full_messages` must accept `assistant` messages carrying `tool_calls` and `tool` messages in the incoming history without dropping them.
4. `X-Source` header (`phone` only if the shared bearer key matches, default `voice`) sets `source`. Caller context headers (`X-Caller-Role`, `X-Caller-Name`) feed the phone prompt block. They are headers, not system messages, because the backend ignores client system content on purpose.
5. Edge cases to test: a turn that returns both a `capture_lead` and a client tool call; a client tool call with malformed arguments; a client that sends no `tools` (web voice, unchanged).

Tests to write first: web voice path unchanged; phone path forwards `get_student_report`; `capture_lead` still saves; unknown tool name from the model does not crash.

## 5. Environment variables (names only)

`C:\ACT-Calling-Agent\.env` (never committed):

```
SARVAM_API_KEY=  CARTESIA_API_KEY=  CARTESIA_VOICE_ID=  RAG_BACKEND_AUTH_KEY=
RAG_BACKEND_PUBLIC_URL=            # the rag-backend URL (Azure)
ERP_URL=  ERP_API_KEY=             # erp-lookup
VOICE_PIPELINE_DIR=                # path to ACT-Voice-Assistant/voice-pipeline
VOBIZ_AUTH_ID=  VOBIZ_AUTH_TOKEN=  # check exact names in Vobiz console
PHONE_PUBLIC_URL=                  # https address Vobiz calls
GOOGLE_SERVICE_ACCOUNT_JSON=  CALLS_SHEET_ID=
RECEPTION_PHONE=  ADMISSIONS_PHONE=  PLACEMENT_PHONE=
ERP_TEST_CALLER=                   # test only; blank at go-live
CALL_MAX_MINUTES=15
```

## 6. Risks and unknowns (carry these, do not hide them)

- **Vobiz stream protocol and the transfer API are read from docs only.** Step 1.4 is done and the serializer is already vendored; the real behaviour (clearAudio/stop JSON, transfer) is first tested in Phase 5.
- **The refactor of `pipeline.py` touches the live website bot.** Step 1.2 is gated by `scenario_test.py` and a manual web call before anything else builds on it.
- **Phone turn-taking cannot be proven in the browser.** The browser test adds narrowband and μ-law, not carrier delay or line noise. Phase 5 is where it is really judged.
- ~~Student data is spoken by Cartesia~~: accepted by the college (2026-10-19). Closed.
- **Outbound legal exposure.** Building and browser-testing outbound carries none. The first real dial does: calls without consent or on the wrong number series bring suspension or blacklisting. Phase 7.6 is gated for that reason.
- **Sarvam's STT at 8 kHz is untested here.** Step 2.2 settles it by comparing both options on the same audio.
- **Concurrency limits:** Vobiz includes 3 simultaneous calls; Cartesia's Startup plan allows 5 voices across web and phone together.
- **Estimates:** the build plan says 3 to 4 weeks. The phases above sum to roughly that, with Phase 2.4 and Phase 5.3 the most likely to overrun.

## 7. Suggested first session once you approve

Prakash gives access to `C:\ACT-Calling-Agent` and the GitHub repo URL, then do steps 1.1 (git init, connect to the existing remote, first commit), 1.5 (scaffold) and 1.4 (Vobiz doc check). Those three change nothing in this repo. Then 1.3 and 1.2 in this repo, each behind its tests.
