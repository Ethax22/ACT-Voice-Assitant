# Voice Model Choices (Sarvam, both languages)

**Superseded 2026-09-25 — see CLAUDE.md decision #1.** Both languages now run
on Sarvam (Saaras STT + Bulbul TTS) via Pipecat. The Deepgram/Flux English
path this doc originally described was retired the same day (code deleted:
`rag-backend/voice_agent.py`, `/voice/en` route in `main.py`). Its
correction-history notes (Flux's `version: "v2"` requirement, the
`agent.think.provider.model` placeholder-field bug, etc.) are preserved in
git history if Deepgram is ever reconsidered, not carried forward here since
none of it applies to the Sarvam path.

Locked in for the v1 build, per Prakash's instruction to pick the best
available model in each category as of September 2026. Revisit only if a
vendor changes pricing/availability or the Implementation Plan is amended.

## Both languages — Sarvam via voice-pipeline/

**Updated 2026-09-25 (Phase 3 build):** `tamil-voice-pipeline/` generalized into
`voice-pipeline/`, serving both `/voice/en` and `/voice/ta` from one
parameterized `pipeline.py` (see CLAUDE.md decision #1). Same model choices as
the original Tamil build apply to both languages now — nothing language-
specific changed at the model level, only `language_code`/`voice` settings:

| Stage | Model | Why |
|---|---|---|
| STT | `saaras:v3-realtime` (fixed), `language_code` per language (`en-IN`/`ta-IN`) | **Correction (2026-09-13, code review against Pipecat source):** the original plan was `saaras:v4` via `SarvamRealtimeSTTService`, but that class hardcodes its model to `saaras:v3-realtime` internally — there is no supported parameter to override the model. `saaras:v4` was never actually applied; nothing to configure there. The *language*, however, is a separate, freely configurable setting (`SarvamRealtimeSTTSettings.language_code`) — confirmed by reading Pipecat 1.10.0's actual installed source at Phase 3 build time — which is what `pipeline.py` now parameterizes per language. |
| TTS | `bulbul:v3`, voice `kavya` (Tamil) / `priya` (English) | `bulbul:v2` is deprecated and no longer answers requests. Both voice picks are placeholders — verify by ear against real ACT answers before locking in (Phase 5's native-speaker listening test, still needed for both languages). |

**Bug found and fixed (2026-09-25, live test during Phase 5):** the original English
pick, `amelia`, came from Pipecat 1.10.0's own `SarvamTTSSpeakerV3` enum — but
Sarvam's live API rejected it outright: `400: Speaker 'amelia' is not
recognized`. The installed package's enum is stale relative to Sarvam's
actual voice list: it's also missing several speakers Sarvam has added since
(`anand`, `tanya`, `tarun`, `shruti`, `kavitha`, and others), which only
showed up in the live 400 error's own "Available speakers" list. Switched to
`priya`, confirmed live. **Don't trust `SarvamTTSSpeakerV3` as the source of
truth for valid voices** — verify against a live call or current Sarvam docs
instead, same scrutiny as any other vendor claim.

**Bug found and fixed (2026-09-13, static code review against Pipecat
source):** `SarvamTTSService`'s `language=` and `voice=` were being passed as
flat constructor kwargs. That's wrong — `SarvamTTSService.__init__` only
accepts `api_key` directly, plus a couple of deprecated legacy args (`model`,
`voice_id`); anything else, including `language=`/`voice=`, is silently
absorbed by `**kwargs` and has **no effect** — no error, no warning. This is
why live testing showed Sarvam speaking **English** with the **default
voice** (`shubh`) regardless of what was configured. Fixed by passing
`settings=SarvamTTSService.Settings(model=..., voice=..., language=...)`
instead — confirmed against Pipecat's actual `SarvamTTSSettings` dataclass
fields, not just its docs.

Also moved the persona prompt from an initial `"system"` message in
`LLMContext` (deprecated since Pipecat 1.9, logged a `DeprecationWarning` in
testing) to `system_instruction` on `OpenAILLMService.Settings` — functionally
inconsequential here since our backend always injects its own persona +
retrieved context server-side and ignores whatever system content the client
sends, but keeps this off Pipecat's deprecated path.

Implemented in `voice-pipeline/pipeline.py` and served by
`voice-pipeline/server.py`.

**Greeting wired up (2026-09-25, Phase 5):** both languages now speak
`GREETINGS[language]` immediately on WebSocket connect, via a
`transport.event_handler("on_client_connected")` that queues a
`TTSSpeakFrame` straight to TTS — no LLM round-trip, since it's a fixed line
we already know verbatim (and running RAG retrieval against something like
"introduce yourself" would be an odd query anyway). Replicates what
Deepgram's `agent.greeting` field used to do automatically for English before
its retirement; Tamil's `GREETING` constant existed since the original build
but was never wired up until now. Closed for both languages at once, since
they share one pipeline.

**Deprecation fixed alongside (2026-09-25, Phase 5):** `PipelineTask` is
deprecated since Pipecat 1.3.0 in favor of `PipelineWorker` (confirmed
against the installed 1.10.0 source, which decorates `PipelineTask` with
`@deprecated`) — same category as the earlier `LLMContext` fix above.
`pipeline.py`/`server.py` now construct and run a `PipelineWorker` instead;
`PipelineRunner.run()` accepts it identically.

**Known gap (Phase 4 — turn-taking, requires a human listening test, not yet
done):** English has no confirmed-by-ear turn-taking/barge-in tuning yet —
Tamil's VAD-based endpointing and `allow_interruptions=True` (already in
`PipelineParams`) apply to English too by construction (same pipeline code),
but whether the same VAD sensitivity feels right for English speech patterns
hasn't actually been verified live. Deepgram's old Voice Agent handled this
automatically for English; Pipecat doesn't, which is the accepted tradeoff in
CLAUDE.md decision #1. Tunable knobs (`threshold`, `silence_duration_ms`,
`min_speech_duration_ms`, `prefix_padding_ms`) are pre-wired as `VAD_TUNING`
in `pipeline.py`, currently all `None` (Sarvam's own defaults) pending that
test.

**Known gap (Phase 5 — native-speaker voice pick, requires a human listening
test, not yet done):** `kavya` (Tamil) and `amelia` (English) are placeholder
`bulbul:v3` voices, not yet confirmed by ear against real ACT answers.
