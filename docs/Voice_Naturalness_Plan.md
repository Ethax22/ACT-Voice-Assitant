# Voice Naturalness — Implementation Plan (website voice bot)

**Written:** 2026-10-07 · **Scope:** the website voice bot only (`widget/`,
`voice-pipeline/`, `rag-backend/`). The Calling Agent runs on Sarvam Indus in
its own codebase and is not covered.

**Source:** the "Voice AI Naturalness & Real-Time Conversation Engineering
Upgrade Report", reviewed against this codebase. Task numbers below are the
report's own.

**Rules carried over from the report:** no replacing Sarvam 105B, Saaras or
Bulbul; no vector database; no Pipecat rewrite; no random fillers. Lead
capture, RAG, both languages and logging must keep working after every step.

**Testing approach (Prakash's decision):** build first, test everything on
real calls at the end. One exception is flagged in step C.

## Status at a glance

| Step | Tasks | What | Status |
|---|---|---|---|
| A | 1, 2, 3 | Barge-in reaches the browser; language check off the normal path | Coded, not call-tested |
| B | 4 | Latency logging | Coded, not call-tested |
| C | 5, 6 | Baseline numbers, Bulbul buffer choice | Measured with synthesized speech; by-ear check still needed |
| D | 7 | AudioWorklet mic capture | Coded, browser-tested without a mic |
| E | 11 | Shorter spoken replies | Coded, not tested against the live model |
| F | 12, 12a | Conversation state and memory | Coded, rules tested on samples |
| G | 10 | Backchannels | Coded, tested with synthesized speech |
| H | 8, 9 | Adaptive and semantic end-of-turn | Coded, tested with synthesized speech |
| I | 14, 15 | Text clean-up and pronunciation | First version coded |
| J | 16, 17 | Voice tuning and voice choice | Not started |
| K | 18–22 | Retrieval speed | Main fix done (connection keep-alive); load test not done |
| L | 23, 24 | Scored evaluation against targets | Not started |
| — | 13 | Speech Planner | On hold (not dropped) |

Nothing in steps A–I is committed yet. Steps G and H were built on 2026-10-07;
their sections below describe the original plan — see "Steps G and H as built"
at the end for what was actually done and measured.

---

## Step A — Barge-in and language detection (tasks 1, 2, 3) — coded

**Barge-in.** The server already cancelled the LLM and TTS on interruption,
but it sends audio at up to twice real time, so the browser kept playing what
it had queued.
- `voice-pipeline/raw_pcm_serializer.py`: every Pipecat `InterruptionFrame`
  is now sent to the browser as `{"type": "UserStartedSpeaking"}`.
- `widget/index.html`: the existing handler for that message calls
  `stopPlayback()`, which drops all queued audio.
- Left out on purpose: the browser stopping on its own local speech
  detection. It risks the bot cutting itself off on echo or room noise (the
  false-trigger problem seen on 2026-09-29). Revisit only if the server-driven
  stop measures slower than about 200 ms.

**Language detection.** `voice-pipeline/conversation_router.py` no longer
calls Sarvam's batch detector on every turn. The session language is set once
at call start; after that `_language_suspicion()` decides locally:
- English call + Tamil script in the transcript → switch, no detection call.
- English call + four or more words with no common English word → one
  detection call before replying.
- Tamil call + three or more words with no Tamil script → one detection call.
- Otherwise → straight to the LLM.

Known gap: Tamil speech that the English recogniser turns into real English
words is not caught. The spoken "speak in Tamil" phrases and the manual
toggle still work.

**Done when:** on a real call, interrupting stops the bot's voice within
about 200 ms, and the log shows no `language detection` line on ordinary
turns.

## Step B — Latency logging (task 4) — coded

- `voice-pipeline/latency_metrics.py` logs, per turn: STT / LLM / TTS first
  byte, user stopped → bot audio starts, how long the user spoke, how long
  the bot spoke, and how far into a reply an interruption came.
- `voice-pipeline/conversation_router.py` logs the duration of any language
  detection call that does run.
- `rag-backend/main.py` already logs retrieval time and first-token time.
- `widget/index.html` logs to the browser console how long each reply's first
  audio chunk waited before becoming audible.

Not built: a dashboard. The numbers are `[latency]` lines in two server logs
and the browser console. Build one only if reading the logs proves too slow.

**Done when:** one real call produces every line above for each turn.

## Step C — Baseline and Bulbul buffers (tasks 5, 6) — needs a mic call

1. Restart both services, make one call per language, save the `[latency]`
   lines. That is the baseline every later step is compared against.
2. Repeat with three or four buffer settings via `voice-pipeline/.env`
   (`TTS_MIN_BUFFER_SIZE`, `TTS_MAX_CHUNK_LENGTH`): Sarvam's defaults
   (50 / 150), the current values (120 / 300), and one or two in between.
3. For each: record TTS first byte and user-stopped → bot-audio, and judge
   smoothness by ear. Pick the fastest setting that still sounds natural.

This is the one step that should not wait until the end: steps H and K are
tuned against its numbers. If it has to wait, the buffers stay at 120 / 300
and H and K are built blind.

## Step D — AudioWorklet mic capture (task 7) — coded

`widget/index.html`: `attachMicCapture()` runs a small AudioWorklet that
posts 20 ms blocks (640 bytes at 16 kHz), replacing `ScriptProcessor`'s
4096-sample buffers. `ScriptProcessor` remains as an automatic fallback.
Used for both the start-of-call language snippet and the live call. Echo
cancellation, noise suppression and auto gain are unchanged.

Checked in a browser with a synthetic source: correct chunk size, stops
cleanly, fallback works. Not yet checked with a real microphone.

## Step E — Shorter spoken replies (task 11) — coded

`rag-backend/main.py`, `VOICE_BREVITY_ADDENDUM`: 5–25 words is the norm,
45 the ceiling; one question per reply; contractions; direct answers; no
re-greeting or repeating. Applies to voice only — typed chat keeps its
2–4 sentence guideline.

**Done when:** in the step L calls, most voice replies fall in 5–25 words and
fewer than 10% exceed 45.

## Step F — Conversation state and memory (tasks 12, 12a) — coded

New `rag-backend/conversation_state.py`. No extra LLM call.
- Derived by rules from the full history on every request: who is asking
  (student / parent), level (UG / PG), course of interest, qualification so
  far, and lead stage (none → interested → details given → captured).
- Appended to the system prompt as "known about this visitor — never ask for
  any of it again".
- "Lead already saved" is the one fact the history cannot show, so it is held
  in memory per session (widget `session_id`, or the `X-Session-Id` header
  `voice-pipeline/pipeline.py` now sends). A second `capture_lead` call with
  the same phone on the same call is not written to the Sheet again.

Limits: the rules are English-first (course names said in Tamil script are
not recognised); the lead-saved memory is lost on a backend restart.

**To do:** extend the rules from real transcripts once there are some;
confirm the field list with Prakash.

## Step G — Backchannels (task 10) — half done

**Done:** `rag-backend/main.py` recognises a bare "yeah / okay / hmm / சரி /
ஆமா" and tells the model to treat it as the answer to its last question, or
as "go on" with a one-sentence reply.

**Not done:** a backchannel spoken *while the bot is talking* still interrupts
it, because the interruption fires on voice activity before any words are
known. Plan:
1. In `voice-pipeline/pipeline.py`, give the user aggregator a turn-start
   strategy that needs a minimum number of words before it interrupts while
   the bot is speaking (Pipecat ships `MinWordsUserTurnStartStrategy`).
2. Check on a call that "okay" no longer cuts the bot off but "wait, one
   question" still does, and that step A's stop time does not get worse.

Do this together with step H — both change the same turn-strategy setup.

## Step H — Adaptive and semantic end-of-turn (tasks 8, 9) — not started

Today a turn ends after 700 ms of silence as judged by Sarvam's server-side
VAD. Goal: answer fast when the thought is complete, wait when it is not.

What the installed Pipecat 1.12.0 offers (checked in the package, not yet run):
- `TurnAnalyzerUserTurnStopStrategy` with `LocalSmartTurnAnalyzerV3` — a
  bundled audio model (`smart-turn-v3.2-cpu.onnx`) that predicts whether the
  speaker has finished. Needs the `transformers` package, which is not
  installed in the voice-pipeline venv.
- `LLMTurnCompletionUserTurnStopStrategy` — asks an LLM whether the
  transcript is a complete thought. Adds a model call per pause.

Plan:
1. **Feasibility first.** Confirm (a) whether smart-turn covers Tamil, from
   its current documentation; (b) whether it can run alongside Sarvam's
   realtime STT, which currently supplies the VAD signals — it may need a
   local VAD (Silero) in the pipeline; (c) its CPU cost per call on the
   deployment-size machine.
2. **If it works for both languages:** wire it in behind an env switch, keep
   Sarvam's VAD for speech start, and lower the fallback silence below 700 ms
   since the model now guards against cutting people off.
3. **If Tamil is not covered:** use it for English only and give Tamil a
   cheaper rule — extend the wait when the transcript ends on a trailing
   connective or filler, keep 700 ms otherwise.
4. Compare against step C's baseline: time to first audio, and how often the
   bot answers mid-sentence, in both languages.

Do not simply lower 700 ms globally.

## Step I — Text clean-up and pronunciation (tasks 14, 15) — first version coded

New `voice-pipeline/speech_text_filter.py`, attached to the stock TTS.
- Removes stray markdown symbols and URL prefixes.
- Says college abbreviations properly from an editable table
  (`PRONUNCIATIONS`): B.E., B.Tech, CSE, ECE, EEE ("triple E"), TNEA, and so on.
- Does **not** spell out numbers, dates or currency: Bulbul v3 already does
  that itself and it cannot be turned off, so doing it twice would be worse.

**To do:** listen for mispronounced names, courses and terms on real calls
and add them to the table (needs Prakash's list); check Tamil replies in
particular; confirm whether the cleaned text or the original is what ends up
in the saved transcript; apply the same filter to cloned voices if cloning
goes live.

## Step J — Voice tuning and voice choice (tasks 16, 17) — not started

1. With the step C buffer setting fixed, try Bulbul's `pace` and
   `temperature` (both settable in `voice-pipeline/pipeline.py`).
2. Listening test of the four topic voices in each language, ideally with a
   Tamil speaker — the still-open Phase 5 voice pick from the main
   Implementation Plan. Decide whether one voice per language sounds better
   than four per topic.

Needs a person listening; nothing here can be settled from code.

## Step K — Retrieval speed (tasks 18–22) — after step C

Already in place: query-embedding cache, short turns reusing the previous
query, async throughout, retrieval and first-token timing in the log.
Only act where step C shows retrieval is a real share of the delay:
- skip retrieval for backchannels and pure small talk;
- pre-warm the embedding cache with the common questions (fees, courses,
  admissions, location, contact, eligibility);
- cut the context (4 curated + 3 website chunks) if first-token time tracks
  prompt size;
- a short load test of concurrent calls before deployment.

## Step L — Evaluation (tasks 23, 24) — at the end

1. Write 30–50 call scenarios covering both languages, interruptions, pauses
   mid-sentence, backchannels, a language switch, and an admission enquiry
   through to lead capture.
2. Run them on the current build; score each on the report's rubric
   (turn-taking, interruption, voice quality, response quality,
   understanding, prosody, naturalness, latency), with "lead captured
   correctly" in place of the sales score.
3. Compare the logged numbers with the report's targets: first audio under
   700 ms, browser stop under 200 ms, no language detection on normal turns,
   replies of 5–25 words, under 10% long replies, under 2% repeated
   questions.
4. The sentiment report (`rag-backend/sentiment_report.py`) can be run over
   the same calls as a second view, bearing in mind its labels are unverified.

There is no "before" recording from prior to steps A–I, so the comparison is
against the targets and against step C's baseline, not against the old build.

## On hold — Speech Planner (task 13)

Held because, as a second LLM pass, it adds a model call before every reply
and works against the first-audio target. Steps E and I cover most of its
purpose. Reconsider after step L if replies still sound written.

---

## Order of work from here

1. Step C baseline call (Prakash, mic) — or accept building H and K blind.
2. Steps G (second half) and H together.
3. Step I additions and step J, from listening.
4. Step K, where the numbers justify it.
5. Step L, then commit.

---

## Steps G and H as built (2026-10-07)

**A correction to the plan above.** Before this change the pipeline had no
local voice-activity detector at all: Sarvam's server decided both when a
turn started (any speech it heard) and when it ended (700 ms of silence).
Fixing G and H therefore meant moving both decisions into `voice-pipeline`.

New file `voice-pipeline/turn_taking.py`, wired in from `pipeline.py`:
- A local Silero VAD (ships with Pipecat) detects speech start and stop.
  Sarvam STT keeps its own server-side segmentation but with a 300 ms silence
  window instead of 700 ms, so the final transcript arrives sooner.
  **Corrected later the same day:** the first version switched Sarvam to
  `endpointing="manual"`, which made it drop the first word or two of every
  utterance ("What courses do you offer?" → "What does the offer?"). The
  timing figures in the table below were measured on that faulty version;
  see "Later fixes and the scenario run" for numbers from the corrected one.
- **Start / barge-in (G):** `BargeInUserTurnStartStrategy`. Bot silent → any
  speech starts the turn. Bot talking → the visitor must keep talking for
  about 0.6 s, or say something that is not a bare acknowledgement, or speak a
  second time within a second. A lone "okay" is dropped and the bot carries on.
- **End of turn, English (H):** Pipecat's smart-turn v3 model. Finished →
  answer at once. Not finished → keep listening for up to 1.5 s of silence.
- **End of turn, Tamil:** fixed silence, same 700 ms total as before.
  Smart-turn v3's published list of 23 languages does not include Tamil
  (checked 2026-10-07). A community Tamil fine-tune exists on Hugging Face
  (`santhosh-005/smart-turn-tamil`); it is third-party and untested here.
- `TURN_MODE=sarvam` in `voice-pipeline/.env` restores the old behaviour.
  All timings are env-overridable (see `.env.example`).

**Measured** with a scripted client playing Sarvam-synthesized speech into a
private copy of the service (old vs new, same clips, same day). Small sample,
synthetic voice, no real microphone or room noise:

| | Old | New |
|---|---|---|
| End of speech → turn recognised as over | about 1.6 s | about 0.6 s |
| End of speech → first bot audio at the client | 2.4–3.5 s | 1.3–2.5 s |
| "Okay" said while the bot is talking | bot cut off | bot carries on |
| 1 s pause mid-sentence | split into two turns | kept as one turn |
| Real interruption → bot stops | 0.7 s | 1.1–1.5 s |

**Known costs and open points:**
- Real interruptions stop the bot more slowly than before on the test clip
  ("Wait, actually…", which starts with a short word and a pause). That is
  the price of ignoring "okay". `BARGE_IN_SUSTAIN_SECS=0` trades it back.
- Smart-turn wrongly judged a finished question as unfinished in about 2 of
  10 runs of the same clip; each miss costs the full 1.5 s wait.
- The rest of the delay is now mostly the LLM's first token (0.3–1.7 s in
  these runs) and TTS first audio (0.2–0.9 s) — step K and step C territory.
- Local VAD sensitivity to real room noise and to the bot's own voice coming
  back through speakers is untested; the 2026-09-29 false-trigger problem
  could reappear in a different form.
- The end-of-turn mode is fixed by the language the call starts in.

---

## Steps C and K as done (2026-10-07)

Measured with the same scripted client (five English questions, synthesized
speech, private copies of both services). Not a substitute for a mic test.

**Where a reply's time went (baseline, before the fixes below):**
- Knowledge-base lookup: 385–1,278 ms per turn, almost all of it the Azure
  embedding call. This was the largest single item.
- Sarvam 105B first token: 250–350 ms after the request.
- Bulbul first audio: about 0.2–0.7 s after the first text.

**Step K — fix applied.** The HTTP clients dropped their connection after
5 s idle, so most turns paid for a new TLS handshake. `rag-backend/retriever.py`
(embeddings) and `rag-backend/main.py` (Sarvam chat) now keep connections
open for 300 s. After warm-up the lookup measured 130–290 ms and Sarvam's
first token 180–220 ms. The query cache key now ignores punctuation as well
as case. Not done: skipping retrieval for small talk, pre-warming common
questions, trimming context, load test.

**Step C — Bulbul buffers.** Three settings, ten calls each:

| min_buffer / max_chunk | Bulbul first audio (median) |
|---|---|
| 50 / 150 (Sarvam default) | 0.29 s |
| 80 / 200 | 0.21 s |
| 120 / 300 (current) | 0.23 s |

No meaningful difference in first-audio time, so the setting stays at
120 / 300, which Sarvam documents as the smoother-sounding end. Whether it
actually sounds better still needs a listener.

**End of speech → first bot audio at the client (English, median of 10):**
2.62 s before the keep-alive fix, 1.84 s after.

**Bug found and fixed during this run.** A short noise just after the visitor
finished speaking started a new, empty turn: the reply in progress was thrown
away and the call stalled about 5 s. `BargeInUserTurnStartStrategy` now treats
the bot as busy from the end of the visitor's turn until the bot finishes
speaking, so only sustained speech or real words interrupt in that gap.

**Smart-turn accuracy is the open question.** In 11 of 30 test calls the turn
ended only after the full 1.5 s wait — almost always the same two questions
("Do you have hostel facilities?", "How do I apply for admission?"), which it
judged unfinished every time. When it is right, first audio comes about
0.8–1.4 s after speech ends; when it is wrong, about 2.1–2.7 s. This may be an
artefact of synthesized clips. If it shows up on real voices, either lower
`SMART_TURN_MAX_WAIT_SECS` or set `TURN_STOP_EN=silence`.

---

## Later fixes and the scenario run (2026-10-07, afternoon)

**Scenario script (step L).** `voice-pipeline/scenario_test.py` runs 35
scripted calls (English, Tamil, pauses, acknowledgements, interruptions,
language switches) with a synthesized visitor voice and writes a CSV plus a
summary. First full run, on the corrected turn-taking but before the language
and wording fixes below:

| Measure | Result | Target |
|---|---|---|
| First audio after the visitor stops, English (31 turns) | median 1.62 s, range 1.09–3.69 s | under 0.7 s |
| First audio after the visitor stops, Tamil (11 turns) | median 1.58 s, range 1.20–2.20 s | under 0.7 s |
| Reply length (46 replies) | median 23 words; 32 in the 5–25 range; none over 45 | under 10% long |
| Real interruptions | bot stopped in 4 of 4; median 0.96 s after the visitor began | 0.2 s |
| Mid-sentence pauses | bot stayed quiet in 4 of 4 | — |
| Acknowledgements over the bot | ignored in 1 of 4 (fixed afterwards: 3 of 3 English) | — |

**Fixed after that run:**
- *Acknowledgements.* A partial transcript ("Hm") was starting a turn 60 ms
  before the final "Hmm." arrived. Short partials are now ignored while the
  bot is busy (`turn_taking.py`).
- *Reply language (Prakash's points 1 and 5).* The reply language is now
  decided in code and stated as the last instruction: the call's language for
  voice (`reply_language:` marker from `voice-pipeline`), the script of the
  visitor's latest message for typed chat. In 17 test replies none came back
  in the wrong language.
- *Mid-call language switch.* The text-only check missed switches in both
  directions (Tamil on an English call was transcribed as fluent wrong
  English; English on a Tamil call came back in Tamil letters). Detection now
  runs on every utterance of a second or more, started when the visitor stops
  speaking so it is normally finished before the transcript arrives (measured
  hold: 0.00–0.16 s; first turn of a call waits fully, 0.64 s in one test).
  Both switch scenarios now answer in the new language on the same turn.
- *Repeated openers (point 6).* The prompt used to tell the model to start
  most replies with "Sure / Good question / That's a good one". It now tells
  it to start with the answer. No opener in 17 test replies. (The phone
  agent's own "let me check" is set in Sarvam Indus, outside this repo.)
- *Tamil wording (point 7).* A Tamil style guide (polite spoken Tamil, no
  slang, Tamil words for fees/hostel/admission) is in the prompt and restated
  last. Banned words: 0 of 24 replies, down from 3 of 11. Written without a
  native speaker — needs one to review real output.

**Still open:**
- Tamil answers are sometimes factually wrong where the English answer is
  right — one listed "B.Sc Nursing" and "MBA" among the courses. Looks like
  weaker knowledge-base matching for Tamil questions; not yet investigated.
- Tamil replies still carry some English ("admissions hotline", "package");
  typed-chat Tamil more than voice (about 30% English-letter words vs 6%).
- A spoken "சரி" still interrupted the bot in the test (the synthesized clip
  is long enough to count as sustained speech).
- The synthesized Tamil clips were misheard twice ("இருக்கிறதா" → "இல்ல"),
  which may be the test voice rather than the recogniser.

---

## First real call reviewed (2026-10-07, 21:38)

Prakash's widget call: English "worked great", Tamil was poor. From the saved
transcript (`voice-pipeline/logs/2026-10-07/6fee1bcb…`), the Tamil half showed:

- **Repetition.** Seven replies in a row ended with the same question
  ("நீங்க சேர்க்கை பத்தி தெரிஞ்சுக்க விரும்புறீங்களா?"), including after the
  visitor said they didn't want that; and the same full answer was recited
  five times to "சரிங்க ஓகேங்க" / "நீங்க வச்சிருங்க" (you can hang up).
- **Years read digit by digit.** The visitor corrected the bot: it was saying
  "two zero zero one" for 2001. Confirmed by transcribing Bulbul's audio back.
- **Voice changes between replies.** Each topic has its own voice, so the
  speaker changed several times within the Tamil part of the call.

**Fixed in `rag-backend/main.py`:** acknowledgements and "hang up" phrases are
recognised by their words rather than exact phrases, and a closing gets a short
goodbye; the last instruction now lists the bot's previous reply and the
questions it has already asked, with "never again"; a "yes"/"சரி" is answered
as a reply to the bot's own last question; a failed knowledge-base lookup no
longer fails the whole reply. Replaying the visitor's side of that call:
0 repeated replies (was 6) and no repeated closing question (was 7).

**Fixed in `voice-pipeline/speech_text_filter.py`:** a bare year in a Tamil
sentence is spelled out in Tamil words; codes and phone numbers stay digit by
digit. Confirmed by round trip ("இரண்டாயிரத்து ஒன்றில்").

**Needs a listener:** `voice-pipeline/make_voice_samples.py` writes the same
Tamil sentences in ten Bulbul voices to `voice_samples/ta/index.html`.
Pronunciation and delivery can only be judged by ear; Sarvam's docs give no
per-language voice recommendation. Also to decide: one voice for a whole
Tamil call instead of one per topic.
