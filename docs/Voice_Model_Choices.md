# Voice Model Choices (English: Deepgram, Tamil: Sarvam)

Locked in for the v1 build, per Prakash's instruction to pick the best
available model in each category as of September 2026. Revisit only if a
vendor changes pricing/availability or the Implementation Plan is amended.

## English — Deepgram

**Updated 2026-09-13:** switched from `nova-3`/`aura-2-thalia-en` to Deepgram's
**Flux** model line, per Prakash confirming the choice directly in the
Deepgram Playground (Voice Agent Settings → Voice → Flux/Hannah).

| Stage | Model | Why |
|---|---|---|
| STT (listen) | `flux-general-en`, `provider.version: "v2"` | Deepgram's newer unified conversational model, purpose-built for voice agents with low-latency end-of-turn detection — better turn-taking feel than nova-3 for this use case. Flux requires `version: "v2"` on the provider. |
| TTS (speak) | `flux-hannah-en`, `provider.version: "v2"` | Flux TTS voice (American, feminine), confirmed by ear in the Deepgram Playground. Flux TTS models use the `flux-{voice}-{lang}` naming pattern and also require `version: "v2"`. |

**Correction history (2026-09-13, live testing against a real Deepgram
account):**
1. First attempt sent `version: "v2"` — this was actually correct, but got
   flagged as suspect based on a non-Flux GitHub example that didn't need it.
2. Removed `version`, which then broke with `UNPARSABLE_CLIENT_MESSAGE` on
   `agent.listen.provider` — confirming Flux specifically *does* require it.
3. Re-added `version: "v2"` to both providers (confirmed against a real
   Settings JSON pulled directly from Prakash's own Deepgram console) and
   fixed the actual remaining bug: `agent.think.provider` needs a `model`
   field even when a custom `endpoint` is set — Deepgram's parser rejects the
   message without one, even though the endpoint URL is what's actually
   called. Added a placeholder `"model": "act-ai"` (ignored by our backend,
   which always uses its own Azure deployment) purely to satisfy the schema.

Full settings JSON: `docs/deepgram_agent_settings.example.json`. Implemented
in `rag-backend/voice_agent.py`.

If a different voice sounds better in practice once real ACT answers are
tested end-to-end, swap `SPEAK_MODEL` in `voice_agent.py` — it's a one-line
change, no protocol difference between Flux voices.

## Tamil — Sarvam

**Updated 2026-09-13 (Phase 4 build):** `bulbul:v2` is now deprecated — Sarvam's
API rejects requests for it outright — so the earlier choice in this doc is
no longer valid. `saarika:v2.5` is also superseded by newer Saaras versions.
Current picks, per "use the latest Sarvam models as of September 2026":

| Stage | Model | Why |
|---|---|---|
| STT | `saaras:v3-realtime` (fixed) | **Correction (2026-09-13, code review against Pipecat source):** the original plan was `saaras:v4` via `SarvamRealtimeSTTService`, but that class hardcodes its model to `saaras:v3-realtime` internally — there is no supported parameter to override it. `saaras:v4` was never actually applied; nothing to configure here. |
| TTS | `bulbul:v3`, voice `kavya` | `bulbul:v2` is deprecated and no longer answers requests. `bulbul:v3` is the current stable release (39 voices, 11 languages including Tamil). `kavya` is a placeholder pick — verify by ear before locking in, same as the Deepgram voice. |

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

Implemented in `tamil-voice-pipeline/pipeline.py` and served by
`tamil-voice-pipeline/server.py`.

**Known gap (not yet fixed):** the English path plays an immediate greeting
(Deepgram's `agent.greeting` field); the Tamil path has a `GREETING` constant
defined but never wired up — Pipecat needs an explicit initial `TTSSpeakFrame`
(or similar) pushed at pipeline start to replicate this, which hasn't been
implemented yet. Not a "broken" bug, just a UX parity gap between the two
languages worth closing later.
