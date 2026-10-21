# Session handoff: the DIY Calling Agent (written 2026-10-20)

Paste everything below the line into a new session.

---

You are continuing a build with Prakash D (Ethax) for Agni College of Technology (ACT). Read `C:\ACT-Voice-Assistant\CLAUDE.md` first (locked-in decisions), then `docs\Calling_Agent_DIY_Implementation_Plan.md` (the phase list, what was done and checked, the inbound audit) and `docs\Calling_Agent_DIY_Build_Plan.md` (what and why, parity tables with the old Sarvam Indus agent). Do not re-litigate decisions in them.

## What this is

The college phone line is being rebuilt as our own agent (replacing the Sarvam Indus agent, which stays live until the new one passes). Sarvam Saaras listens, `rag-backend` answers (Sarvam 105B), Cartesia speaks; the same voice and brain as the website bot. Phone audio will come from Vobiz. Everything so far is tested in the browser, not on a real phone line.

## Where things are

- `C:\ACT-Voice-Assistant` (git, branch main, last commit `b1f0a11`): `rag-backend/` (shared brain; the phone prompt, forced tool calls, lead capture, `spoken_numbers.py`), `voice-pipeline/` (Pipecat pipeline, turn-taking, router, speech text filter; the phone agent imports its modules, it is not a service the phone needs), `docs/`.
- `C:\ACT-Calling-Agent` (git, remote `https://github.com/Ethax22/ACT-Calling-Agent.git`, local main `9dd271a`; **pushed only up to `1c17eaf`**): `phone-agent/` (server, call pipeline, tools, call control, post-call record, vendored Vobiz serializer, background sound, test page, `live/` manual check scripts, tests), `erp-lookup/` (records and transfer numbers service; its `.env`, `erp.db`, `raw/`, `fake_contacts/` are git-ignored), `start-local.ps1`, `LOCAL_TEST.md`, root `.env` (git-ignored; falls back to `voice-pipeline\.env`).

## State

Done and checked in the browser: Phases 1 to 3 plus the inbound follow-ups: greeting, answers, caller identification (by number), records for student/parent/faculty with date-of-birth check, transfer (simulated: speaks a line, sends the page a message, ends), end call, quiet-caller nudges, 15-minute cap, post-call record to disk and the `Calls` sheet tab, lead capture on website and phone (phone reads the number back and saves on the caller's yes), IST date/time and office hours in the phone prompt (8 AM to 5 PM, Monday to Saturday), phone temperature 0.3, Sarvam recogniser on `auto` language, interruption tuning for the 8 kHz line.

Prakash is testing locally by voice and reporting problems; each round has found real faults. His latest findings were fixed in the last commits (spoken numbers as words, Tamil first turn, abbreviations without spaced letters, barge-in), but **none has been confirmed by ear yet**.

Open items: Prakash's hands-on results; parent and Tamil records by voice; a native Tamil speaker's check of the Tamil nudge/goodbye/time-limit lines; pronunciation of BE/ME/CSE/NBA/Thalambur by ear.

Not started: **Phase 4** Azure deployment (Prakash creates a Central India VM and runs the steps; I write `deploy/AZURE.md`), **Phase 5** real Vobiz line (answer URL `<Stream>`, real transfer via redirect to our `<Dial>` XML, barge-in tuning, 8 kHz vs 16 kHz, voice by ear; the Azure server must be in India), **Phase 6** soak and cut-over (new Vobiz account and number in the college's name, blank `ERP_TEST_CALLER`, real contacts), **Phase 7** outbound (build and test in the browser first; **no real dialling until DLT registration, a 140-series number, consent records and verification are done**: see `docs\Outbound_DLT_Registration_Guide.pdf`, uncommitted).

## Decisions Prakash made (do not change without asking)

Replace Indus; Vobiz account is his for development and a new one for the college at launch (no transfer); Azure now, the college's host later; student data may be spoken by Cartesia; "Output numbers in Indic" dropped; speed stays at the voice default; background sound is built but **switched off** (`PHONE_BACKGROUND=off` in the root `.env`); transfer and barge-in are tuned on the real phone; lead capture must work on both the website chat and the phone.

## How to run and test

- Start: `cd C:\ACT-Calling-Agent; powershell -ExecutionPolicy Bypass -File .\start-local.ps1` (erp-lookup :8100, rag-backend :8000, phone-agent :8002; logs in `C:\ACT-Calling-Agent\logs\`, UTF-16). Test page: `http://127.0.0.1:8002/test`. Caller numbers and what to try are in `LOCAL_TEST.md` (9000000003 student with marks, 9000000001 student with attendance, 9000000002 parent, 9000000004 faculty, anything else unknown).
- Tests: `phone-agent` 24, `rag-backend` 19, `voice-pipeline` 4, each with its own `venv\Scripts\python.exe -m pytest tests -p no:cacheprovider`. Live manual checks: `phone-agent\live\records_dialogs.py` (typed callers, real model), `phone-agent\live\voice_calls.py <end-call|transfer|quiet|cap|records>` (spoken, needs the phone agent with `PHONE_BACKGROUND=off`), `voice-pipeline\scenario_test.py --url ws://127.0.0.1:8002 --path /test/ws --only <name>`.
- To read a bad call: transcript, audio and `call_record.json` are in `C:\ACT-Voice-Assistant\voice-pipeline\logs\<UTC date>\phone-test-<id>\`; `TURN_TAKING_TRACE=1` on the phone agent logs every voice-detection and interruption event.

## Working rules and traps

- Prakash wants every change tested before it is called done, and the plan updated as steps finish. Mark done in the Implementation Plan with what was checked and what was not.
- **Commit only when asked**, on `main`, no new branches, no pushes unless he says. Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. In `ACT-Voice-Assistant` leave out `docs/Calling_Agent_Build_Plan.md` (modified) and the other untracked docs.
- Do not run commands or change code he asked only a question about; when he says "dont run anything", give commands only.
- Test numbers lock for an hour after 3 wrong dates of birth: unlock after tests (`delete from audit where caller like '90000000%' and result in ('verification_failed','locked_out')` in `erp-lookup\erp.db`). Scenario runs must be **one at a time** (the harness reads the newest transcript; overlapping runs mixed up results once).
- Real writes: leads Sheet and `Calls` tab get test rows; tell him to delete them. Never put secrets in chat or commits.
- Tool quirks here: `\\` in a shell command arrives as `\`, so build regexes with `chr(92)` or use the Edit/Write tools; Bash on Windows needs `MSYS_NO_PATHCONV=1` for paths starting with `/`; the permission check sometimes errors transiently, retry once.
- The recogniser's `auto` language sometimes hears a short Tamil sentence in another script (Malayalam seen); the Sarvam model skips tool calls unless code forces them (see `_forced_tool_choice` in `rag-backend/main.py`); changing the voice id re-renders the cached greeting.

Start by asking Prakash what he heard on his latest call, or, if he gives a transcript path, read it before changing anything.
