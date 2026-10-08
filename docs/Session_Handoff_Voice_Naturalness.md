# Session handoff — voice naturalness work (written 2026-10-08)

Paste the block below as the first message of a new session.

```
You are continuing work on the Agni AI website voice bot in C:\ACT-Voice-Assistant
(read CLAUDE.md first; then docs/Voice_Naturalness_Plan.md, which is the full
record of the last session, including measurements and corrections).

IMMEDIATE TASK
Prakash listened to a real Tamil call (voice-pipeline/logs/2026-10-08/2002b553…,
transcript + audio.wav) and says the bot's Tamil is now "too pure, like ancient
Tamil" — not how people talk today. He will give you a REFERENCE AUDIO FILE of
how he wants the bot to sound. Ask for it if he hasn't attached it. Then:
1. Transcribe the reference (Sarvam batch STT: POST /speech-to-text,
   model saaras:v4, language_code ta-IN; mode "verbatim" keeps spoken forms)
   and study its register: how much English is mixed in, which words stay
   English, verb endings, fillers, pace.
2. Rewrite the Tamil style rules in rag-backend/main.py to match it. The
   current rules are the likely cause of the "pure Tamil" problem — they tell
   the model to prefer Tamil words (கட்டணம், விடுதி, சேர்க்கை, வேலைவாய்ப்பு,
   விண்ணப்பம்) and NOT to write English words in Tamil letters (ஹாஸ்டல்,
   ஃபீஸ், அட்மிஷன்). Everyday Chennai Tamil uses exactly those English words.
   Look at TAMIL_STYLE and LANGUAGE_RULES["ta"] (both state the rule; the
   second is in the final system message and is the one the model obeys).
3. Keep what Prakash already approved: no crude slang (ஆம்பளை, பொம்பளை,
   பசங்க, பொண்ணுங்க → மாணவர்கள் / மாணவிகள்), respectful நீங்க/உங்க, one
   language per reply, no stock openers, replies of about 25 words.
4. If the reference also implies a different voice or pace, use
   voice-pipeline/make_voice_samples.py to generate samples for him to pick
   from (TTS_PACE / TTS_TEMPERATURE in voice-pipeline/.env set pace and
   delivery). He wants ONE VOICE PER TOPIC kept (ritu / aayan / sunny /
   shreya) — do not collapse to a single voice.
5. Test with the scripts below, show him before/after sample replies in Tamil,
   and get his sign-off. You cannot judge how Tamil sounds; he can.

STATE OF THE REPO
- Branch main. NOTHING from the last session is committed. Do not create
  branches without asking. Commit only when Prakash asks.
- Modified: .gitignore, CLAUDE.md, docs/Implementation_Plan_API_Based.md,
  rag-backend/main.py, rag-backend/retriever.py, voice-pipeline/.env.example,
  conversation_router.py, language_detection.py, latency_metrics.py,
  pipeline.py, raw_pcm_serializer.py, widget/index.html.
- New: rag-backend/conversation_state.py, sentiment_report.py,
  compare_models.py; voice-pipeline/turn_taking.py, speech_text_filter.py,
  scenario_test.py, make_voice_samples.py; docs/Voice_Naturalness_Plan.md,
  docs/Calling_Agent_Naturalness_Update.md, this file.
- Prakash runs rag-backend on :8000 and voice-pipeline on :8001 himself.
  Never kill them. For tests start private copies on other ports (8010/8011,
  RAG_BACKEND_PUBLIC_URL=http://127.0.0.1:8010) and stop them afterwards.
  He must restart his own services to pick up changes.

WHAT WAS BUILT (details in Voice_Naturalness_Plan.md)
- Turn-taking (voice-pipeline/turn_taking.py): local Silero VAD; a bare
  "okay" no longer interrupts the bot; English end-of-turn uses Pipecat's
  smart-turn model, Tamil uses a fixed 700 ms. Sarvam STT must stay on
  endpointing="vad" — "manual" clipped the first words of every utterance.
  TURN_MODE=sarvam restores the old behaviour.
- Browser stops playing on interruption; AudioWorklet mic capture.
- Language: reply language decided in code (voice: reply_language marker
  from voice-pipeline; chat: script of the latest message). Mid-call switch
  detection runs per utterance, started at VAD stop so it adds ~0 s.
- Backend prompt: spoken replies ~20 words, no stock openers, anti-repeat
  (previous reply + already-asked questions listed in the final message),
  closings get a short goodbye, facts don't change when the visitor pushes
  back, conversation state (who is asking, course, lead stage).
- Tamil questions are translated to an English search query before the
  knowledge-base lookup (Tamil used to retrieve unrelated pages).
- Years in Tamil sentences are spelled out before TTS (Bulbul read "2001"
  digit by digit).
- Connection keep-alive on both API clients; lookup failure no longer fatal.
- Sentiment report for the website bot (rag-backend/sentiment_report.py).
- Phone agents (Sarvam Indus, outside this repo): backend answers for
  source "phone" are now spoken-length; paste-ready Indus instructions are
  in docs/Calling_Agent_Naturalness_Update.md — NOT yet applied in Indus.

OPEN PROBLEMS, IN ORDER
1. Tamil register too formal (the immediate task above).
2. From the 2026-10-08 morning calls: a stray fragment at call start ("It's
   a.") is answered as a question; and on "I want" the bot replied "I want to
   know about the B.Tech admissions…", finishing the visitor's sentence.
   Prakash has not yet said to fix these — ask.
3. First audio is ~1.6 s after the visitor stops (target 0.7 s).
4. Smart-turn judged some finished English questions unfinished (1.5 s wait).
5. The bot said CSE started in 2001 with 60 students — the knowledge base
   gives that for ECE. Real CSE figures needed from Prakash.
6. Sensitive-topic talking points are still placeholder text.
7. Nothing has had a native-speaker review; scripted tests used a
   synthesized voice.

DECISIONS ALREADY MADE — do not reopen
- Stay on bulbul:v3. bulbul:v4-flash exists but the account has no beta
  access, has no conversational or female Tamil voices, and Pipecat 1.12.0
  doesn't support it.
- Stay on sarvam-105b. sarvam-105b-conversations was compared on all 172
  saved conversations and came out level; it asks more follow-up questions.
- Keep the 0.6 s sustained-speech rule for interrupting the bot.
- Per-topic voices stay. Speech Planner (second LLM pass) is on hold.
- Sentiment report is in scope for the website bot (CLAUDE.md updated).

TEST TOOLS
- voice-pipeline/scenario_test.py --url ws://127.0.0.1:8011 [--only ta-]
  35 scripted calls; CSV in voice-pipeline/scenario_results/.
- rag-backend/compare_models.py — replays every saved conversation; lead
  capture is stubbed so nothing is written to the admissions Sheet.
- For prompt changes, call main._generate_reply(messages, source="voice")
  directly from the rag-backend venv; prefix messages with
  {"role":"system","content":"p\nreply_language: ta"} to simulate a Tamil call.
- To hear what Bulbul actually says: synthesize, then transcribe back with
  batch STT in mode "verbatim".

WORKING STYLE
- Verify vendor behaviour live against the API and the installed package,
  not from memory or docs. Several doc-based assumptions were wrong.
- Flag locked decisions rather than changing them silently.
- Say plainly what was and wasn't tested; scripted tests are not a mic test.
```
