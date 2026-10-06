# Session handoff — ERP lookup for the call agent, Indus deployment, voice latency work

**Written:** 2026-10-05. **Author:** Claude (session with Prakash). Read this first in the new session; it replaces re-reading the whole chat.
Related: `CLAUDE.md`, `docs/Implementation_Plan_API_Based.md` (web voice bot), `docs/Calling_Agent_Build_Plan.md` and `docs/Calling_Agent_Indus_Config.md` (call agent).

## 0. How to work with Prakash (learned this session)
- Wants **short, numbered, click-by-click steps**, one thing at a time. Long multi-part messages confused them; they said so twice.
- Works in the **Sarvam Indus console** and sends screenshots. Read each screenshot closely before answering; wait for the next one before assuming a screen.
- Gives you prompts to paste into **Sarvam Genie** (Indus's instruction editor). Write those as one self-contained block.
- Never ask for or echo secrets (API keys, tokens, phone numbers) in chat. Keys live in `.env` files only.
- Prefers decisions backed by live checks, not guesses. Say plainly when something is inferred vs measured.

## 1. Decisions made this session
1. **Web voice bot gets NO ERP data and no aggregate stats.** ERP (marks/attendance) is only for the calling agent, only for current students, their parents, and current faculty.
2. **Calling agent code stays out of this repo** (CLAUDE.md). The lookup service lives in `C:\ACT-Calling-Agent\erp-lookup`.
3. **The call agent runs on Sarvam Indus (managed builder), not a DIY pipeline.** It reaches our code only through API Tools. Keep it that way; a DIY phone pipeline was discussed as a fallback only.
4. **Vobiz KYC was done as "Individual"** (no GSTIN available). The rented number is under an individual's identity. Open question: whether the college wants it under an organisation before launch.
5. **Web voice bot latency/naturalness is handled in code; call agent naturalness is handled with Indus instructions/settings.**

## 2. ERP lookup service (built, tested, working on a real call)
Location: `C:\ACT-Calling-Agent\erp-lookup\` (not a git repo as far as seen; `.gitignore` there excludes `.env`, `erp.db`, `raw/`, `fake_contacts/`).
- `ingest.py` — parses the 4 PDFs in `raw/` into SQLite `erp.db` (students, courses, marks, marks_result, attendance, attendance_summary; plus contacts, verification, audit tables). Idempotent: weekly refresh = drop new PDFs in `raw/`, run `python ingest.py`. Auto-detects "STATEMENT OF MARKS" vs "ATTENDANCE" layouts. Parsed: marks CSE 61 / ECE 64 students (Batch 2025, IA Test-1, Sem 3); attendance CSE 63 / ECE 60 (Batch 2023, 01-09-2026 to 30-10-2026).
- `api.py` (FastAPI, bearer key from `.env`): `POST /identify`, `/student`, `/class`, `/route`, `GET /health`.
  - Caller number must match an **active** contact; missing/null/unknown caller ID => no data.
  - Student/parent: only the reg_no on their own contact row, plus **date of birth** (DD-MM-YYYY) as second factor; 3 failed attempts => 1-hour lockout; every lookup audited (no marks values logged).
  - Faculty: only their own programme/batch/section, summaries only (`low_attendance`, `marks_summary`).
  - Fillers like "none"/"all" for subject are normalised; unknown topic => "both"; `caller_number` may be null.
  - `/route` maps department (reception/admissions/placement) to a number from `.env` (`RECEPTION_PHONE`, `ADMISSIONS_PHONE`, `PLACEMENT_PHONE`), falling back to reception.
  - Test-only override `ERP_TEST_CALLER` in `.env` (makes browser "Test agent" calls behave as a given fake contact). **Must be blank before go-live.**
- `load_contacts.py` — loads `contacts.csv` + `verification.csv` (formats in its docstring); `--fake` loads fake contacts 9000000001–05 (student, parent, student, faculty, inactive).
- `test_api.py` — 18 checks, all passing on fake data. `start.ps1` and `README.md` exist. Course names for the marks PDFs are in `course_names.json` (read by hand from wrapped headers — **verify against the syllabus**).
- `.env` holds `ERP_API_KEY` (user replaced my generated key with their own), the 3 transfer numbers, and the blank `ERP_TEST_CALLER`.
- **Data gap found:** marks PDFs are Batch 2025 (`312825…`), attendance PDFs are Batch 2023 (`312823…`), so **no student has both**. The agent correctly says "could not find marks" for an attendance-only student. Real rollout needs matching batches. Real contacts (phones, parent→child links, DOB, active flag) are still **not provided** — user will supply them in the next phase.

## 3. Sarvam Indus call agent ("Agni Call Agent")
- **Tools** (Tools tab; all POST, Header `Content-Type`=Fixed `application/json`, Auth Bearer using a `Bearer` variable with Include-in-context OFF):
  - `identify_caller` → `/identify`, runs **when the call starts**, saves `role`→`caller_role`, `first_name`→`caller_first_name`.
  - `get_student_report` → `/student`; `get_class_summary` → `/class`; `get_transfer_number` → `/route` (saves `transfer_number`). All "during the conversation".
  - Existing: `get_act_answer` (→ rag-backend `/calling-agent/ask`), `call_transfer`, `end_interaction`.
  - In each tool's body: `caller_number` = **Agent variable → "User identifier"** (confirmed to hold the caller's number on a real call). AI-filled fields use **"Let the agent decide"**, type Text.
  - Gotcha: Content-Type must be **Fixed value**, not "Let the agent decide", or the body arrives as text and returns 422.
  - Tools' Section 3 holds e.g. `Result: @speech` (only available for "during the conversation" tools).
- **Variables:** `caller_role` (default `unknown`), `caller_first_name`, `reg_no`, `date_of_birth`, `topic`, `subject`, `report_speech`, `transfer_number` (default should be the **reception** number; user first set admissions — unconfirmed whether corrected; Include in context ON), `Bearer` (context OFF), plus existing `collegeName`, `admissionsPhone`, `officeHours`, `current_date`.
- **Instructions** were edited via Genie: records section, department routing for transfer (reception/admissions/placement), and finally a consolidated update (natural speaking style, lead-in phrase before tool calls, spaced-digit phone numbers, strict `get_transfer_number` then `call_transfer`). Genie reported it done and validated; not re-read by me.
- **Settings:** Call forwarding ON, Destination=Phone number, Value type=Agent variable, Forward to=`transfer_number` (it switched itself off once earlier — recheck after every commit/version change). Voices: English Ritu, Tamil Pooja (per-language voices on), speed 1.05, pitch 0, temperature "Compliant" (lowest), eagerness Balanced, background "Quiet office". Suggested (not confirmed applied): speed 1.0, temperature one notch up, nudge delays 15–20s (currently 10s, message 2 hangs up).
- **Deployment:** Voice Agents → Deploy → **Inbound calls → Create inbound** (wizard: Agent+committed version, connection "Sarvam Managed (Vobiz)", number, availability, deploy). Number rented from Sarvam: +91 80715 83873. Deployed with committed **v7**, call window 00:00–23:59 all days (narrow before launch). Agent is **locked** once deployed; a new commit needs a new/paused deployment. A later redeploy happened after the transfer-prompt fix; the user's draft is now **v12** — **confirm which version is live**. Drafts can't be deployed.
- **Real-call results:** general answers, Tamil/English switching, ERP lookup, and lead capture all worked (lead appeared with `source=phone`). Transfer history: (1) agent skipped `call_transfer` and claimed a "technical issue" → prompt tightened; (2) `call_transfer` fired, caller heard ringing, destination never rang, call dropped; (3) after the user restarted everything it **worked**. Cause of #2 unknown. **Update (later in session):** (4) transcript showed the correct order (`get_transfer_number` department=reception, then `call_transfer`), but the call went to the **admissions** number = the `transfer_number` variable's default; `/route` itself returns the right reception number (verified locally). **Resolved by Prakash: the cause was the Cloudflare tunnel** (down or address changed), so `get_transfer_number` failed, `transfer_number` kept its default, and Indus dialled the default. My "Indus ignores the variable" theory was wrong. Lessons: a dead tunnel fails silently into the variable's default, so the default must be the safe fallback (reception); and earlier "successful" transfers may have just hit the default, so department routing still needs one confirmed test per department with the tunnel up.

## 4. Phone/web infrastructure in use now (dev only)
- rag-backend: `uvicorn main:app --reload --port 8000` (venv `rag-backend\venv`), exposed by **ngrok** (free plan: only one dev domain/tunnel).
- erp-lookup: `uvicorn api:app --host 0.0.0.0 --port 8100` run from `erp-lookup`, exposed by a **Cloudflare quick tunnel** (`& "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://localhost:8100`). The `trycloudflare.com` address **changes on every cloudflared restart** — then all 4 ERP tool URLs in Indus must be updated. Hosting was deliberately deferred by the user ("not hosting yet").
- voice-pipeline (web bot) on port **8001**; widget static server on 5500; widget calls `ws://localhost:8001/voice/en|ta` and `http://localhost:8000/widget/chat`.
- Lead capture: `rag-backend` `.env` has `GOOGLE_SERVICE_ACCOUNT_JSON` and `LEADS_SHEET_ID` set; the phone path logged "Lead captured". (The Implementation Plan status table still says no sheet connected — stale.)

## 5. Voice naturalness / latency findings
- **Indus call agent:** streaming STT/TTS and bitrate are platform-fixed (phone audio is narrowband; 192 kbps is irrelevant). Biggest controllable delay is `get_act_answer`: ~1.9–2.2 s per answer measured direct to the backend, plus tunnel. Mitigation = spoken lead-in before tool calls (now in instructions), voice/speed/temperature settings, and speeding up the backend.
- **Measurement artifact to remember:** Python clients on Windows add ~2 s when connecting to `localhost` (IPv6 attempt). Use `127.0.0.1`. Real backend first word ≈ 1.7 s over HTTP (retrieval/embedding ≈ 0.3–0.45 s; Sarvam 105B first token ≈ 0.4 s; ~10,000 chars of prompt per turn; `--reload` is not the cause). My earlier "3.4 s first word" claim was this artifact — corrected.
- Not yet measured: web-bot STT and TTS stages, and the end-to-end gap.

## 6. Web voice bot work — Phase 1 done, awaiting data
Changes (uncommitted, in the working tree):
- **New** `voice-pipeline/latency_metrics.py` — `LatencyLogger` logs `[latency] session=… turn=… …` lines: per-service first-byte (STT/LLM/TTS) and "user stopped → bot audio starts". Observes only; swallows its own errors.
- `voice-pipeline/pipeline.py` — imports it, adds `LatencyLogger(session_id)` as the last pipeline stage, and `PipelineParams(allow_interruptions=True, enable_metrics=True)`.
- `voice-pipeline/.env` (gitignored) — `RAG_BACKEND_PUBLIC_URL=http://127.0.0.1:8000` (was the ngrok URL; restore it if the pipeline ever runs on a different host than the backend).
- Verified: logger unit-test on simulated frames, pipeline builds with the new stage, backend reachable on the new URL. **Not verified:** any real voice call.

**Plan from here** (do in order; decide by the measured numbers, not by guess):
1. **Prakash restarts voice-pipeline, makes 2–3 English calls + 1 Tamil, sends the `[latency]` lines.** (pending)
2. Tune `VAD_TUNING["silence_duration_ms"]` 700 → 500 → 400 keeping `threshold` 0.5; check it doesn't cut people off.
3. Tune TTS `min_buffer_size` (120) / `max_chunk_length` (300) lower, one at a time, by ear (Tamil by a native speaker).
4. Backend speed in `rag-backend`: cache repeated questions, trim retrieved context, skip retrieval for greetings/short follow-ups, shorter voice answers; re-run timing, confirm answers unchanged on the Implementation Plan §6 question set.
5. Lead-in phrase when first word > ~1 s; persona wording like the phone agent.
6. Listening test with a Tamil speaker; lock per-language voice/settings (closes pending Phase 4–5).
7. End-to-end re-verify; update the status table in `Implementation_Plan_API_Based.md` honestly.
Do not touch CLAUDE.md locked decisions (shared RAG backend, persona, Sarvam for both languages, no keys in the browser).

## 7. Open items / pending on the user
- Send the `[latency]` lines (web bot Phase 1).
- Real **contacts + DOB** export from the ERP (phone, role, reg_no, parent→child, programme/batch/section, active flag); more departments' PDFs (weekly); matching batches for marks and attendance.
- **Transfer routing:** tunnel problem fixed by Prakash. Still to confirm with tunnel up: set `transfer_number` default to **reception**, then test admissions, placements and reception transfers; each should reach its own number. If any lands on the default, check Monitor → Call logs for a failed `get_transfer_number` or a missing "Variable Changed transfer_number".
- Confirm which Indus version is live; confirm `transfer_number` default = reception; narrow the call window; re-test transfers to all three departments.
- Before go-live: **blank `ERP_TEST_CALLER`**, delete fake contacts and the test row, remove `fake_contacts/`.
- College sign-off: what parents/students may hear by phone; recording/transcript retention (calls and transcripts will contain student data); whose legal entity owns the number.
- **Hosting** (deferred): `erp-lookup` + `rag-backend` need a stable HTTPS host before real callers; then update the 4 ERP tool URLs once and move the college SIM forwarding (USSD) to the Indus number.
- Ask Sarvam support: why the transfer dropped once; whether Indus supports custom/cloned voices (the cloning code in this repo only serves the web bot).
- Docs to refresh later: `Implementation_Plan_API_Based.md` §0 status table (leads sheet now connected; latency work), and add the ERP service/Indus tool setup to `docs/Calling_Agent_Indus_Config.md`.

## 8. Quick-start commands
```powershell
# ERP lookup service
cd C:\ACT-Calling-Agent\erp-lookup
uvicorn api:app --host 0.0.0.0 --port 8100        # key is read from .env
& "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://localhost:8100
python ingest.py                                   # weekly PDF refresh
python load_contacts.py --fake ; python test_api.py

# web voice bot: restart voice-pipeline from C:\ACT-Voice-Assistant\voice-pipeline, then grep logs for "[latency]"
```
