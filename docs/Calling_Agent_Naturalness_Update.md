# Calling Agent — Naturalness Update (inbound and outbound, Sarvam Indus)

**Written:** 2026-10-07. Carries the lessons from the website voice bot's
naturalness work (`docs/Voice_Naturalness_Plan.md`) over to the two phone
agents on Sarvam Indus.

The phone agents are configured in the Indus dashboard, not in this repo. So
this is mostly paste-ready text and a settings checklist for Prakash to apply
there. It was written from this repo's planning docs — the live Indus
configuration was not inspected, and setting names below are the ones those
docs use. Check each against the dashboard.

---

## 1. Already carried over — nothing to do in Indus

Both agents get their college answers from `rag-backend`
(`POST /calling-agent/ask`), so these changes reach phone calls as soon as
`rag-backend` is restarted:

| Change | Effect on a phone call |
|---|---|
| Spoken-length answers | Tool replies are now about 15 words (max 35 in testing), not 2–4 sentences. This was **new on 2026-10-07**: the phone path had been left on typed-chat style. |
| No stock openers | Tool replies no longer start with "Good question / Sure / That's a good one". |
| Facts don't bend | The backend won't change a year, code or figure because the caller pushes back. |
| Tamil questions find the right page | Tamil questions are translated for the knowledge-base lookup; before, Tamil callers were answered from unrelated pages. |
| One language per reply | English question → English only; Tamil → Tamil only, in polite spoken Tamil with no slang. |
| Lookup failure isn't fatal | A network blip no longer makes the tool call fail outright. |

## 2. One tool change in Indus (optional but recommended)

`get_act_answer` currently sends only `question`. The backend now also
accepts `language` (`"en"` or `"ta"`). If Indus exposes the call's current
language as a variable, add it to the tool's request body:

```json
{ "question": "<self-contained question>", "language": "<en or ta>" }
```

Without it the reply language follows the script of the question — wrong
whenever the agent passes a Tamil caller's question to the tool in English.

## 3. Instructions — replace "Speaking Style" in BOTH agents with this

```
Speaking Style
- This is a phone call. Speak in one or two short sentences, about 25 words. Never more than 40.
- Start with the answer. Never open with "Good question", "Sure", "Certainly", "That's a good one", or their Tamil equivalents.
- Do not say "let me check", "one moment", "I am searching" or "let me think" before using a tool. Use the tool silently and then answer.
- One question per turn at most, and most turns should end with the answer, not a question.
- Never repeat a sentence you have already said in this call. If the caller adds nothing new, give a short acknowledgement instead.
- Never ask the same question twice. If the caller declined or ignored a question, drop that subject for the rest of the call.
- If the caller says only "ok", "hmm", "sari", "aama": if you had just asked a question, take it as yes; otherwise say one short line offering further help. Do not start a new answer.
- If the caller is wrapping up ("ok thanks", "nandri", "vachidunga", "that's all"): say a short goodbye of under ten words and end the call. No facts, no question.
- If the caller is not asking anything (declining, commenting, correcting you): respond to exactly that in one short sentence.
- No markdown, lists or symbols.

Language
- Reply only in the caller's language. English caller: English only. Tamil caller: Tamil only, from the first word to the last. Do not switch mid-sentence.
- In Tamil, use polite everyday spoken Tamil with "neenga / unga". Say "maanavargal" and "maanavigal" for boys and girls. No slang ("da", "machi", "pasanga", "ponnunga").
- In Tamil, use the Tamil word where everyone does: kattanam (fees), viduthi (hostel), serkkai (admission), padippu (course), velaivaaippu (placement). Keep English only for names and terms like B.Tech, CSE, TNEA, Wi-Fi.
- The campus is at Thalambur (தாழம்பூர்), not Tambaram.

Numbers
- Say years as a number ("two thousand one", "இரண்டாயிரத்து ஒன்று"), never digit by digit.
- Say phone numbers, the counselling code and PIN codes digit by digit.

Facts
- Facts come only from get_act_answer. Speak its reply without adding to it.
- Never change a year, number, name or amount because the caller objects or sounds sure. If they correct how you said something, apologise for the wording and keep the fact. If they dispute a fact, say what your information shows and suggest confirming with admissions.
```

Write the Tamil words in the block above in Tamil script if the Indus
Instructions box accepts it; they are in English letters here only so the
block survives copy-paste.

### Inbound agent — also change

- The current draft (`Calling_Agent_Build_Plan.md` §4.1) says "Keep replies
  short — 2-4 sentences". Delete that line; the block above replaces it.
- In "Objective", change "speak its response" to "speak its response as it
  is, without lengthening it" — the tool reply is already phone-length.

### Outbound agent — also change

In `Calling_Agent_Outbound_Config.md` §3:
- Replace the whole "Speaking Style" section with the block above (it
  already says "under 40 words" and "one question at a time"; the block adds
  the no-repeat, no-filler, closing and facts rules).
- Keep 'Treat "hmm", "aama", "ok" as listening' — the block's
  acknowledgement rule says the same in more detail.
- In "Closing", keep `end_interaction`, but the goodbye line must be in the
  caller's language, not always "Nandri, have a great day".

## 4. Settings checklist in Indus (both agents)

| Setting | Set to | Why |
|---|---|---|
| Let callers interrupt | On | Barge-in. On the website bot this was the single biggest "feels robotic" item. |
| Eagerness to respond | Start in the middle; lower it if the agent talks over thinking pauses, raise it if replies feel slow | Indus's equivalent of end-of-turn tuning. It can only be judged on a real phone call. |
| Auto-detected language switch | On | So a caller who switches language is followed. |
| Starting language | English (inbound); the lead's language if known (outbound) | — |
| Model temperature | Low | Fewer invented details. |
| Filler / "thinking" phrase while a tool runs | Off, or the shortest option, if Indus has such a setting | This is the likely source of "let me check" on every question. Not confirmed — look for it in the tool or agent settings. |
| Nudge quiet callers | On, 10 s | Unchanged. |
| Voice | Choose by ear, per language | Same as the website bot: needs a listener, ideally a Tamil speaker. |

## 5. What cannot be copied from the website bot

These were code changes in `voice-pipeline/`, which the phone agents do not
use. On Indus they are whatever the platform provides:

- The custom interruption rule (ignore a bare "okay" while the agent is
  speaking, stop on sustained speech).
- The end-of-turn model for English.
- Per-utterance language detection.
- The text clean-up before speech (abbreviation and year pronunciation) —
  the "Numbers" lines in §3 are the prompt-only substitute.
- Per-topic voices.
- Latency logging and the scripted scenario test.

If Indus's turn-taking proves the weak point on real calls, the fallback in
`Calling_Agent_Build_Plan.md` §9 (the deferred DIY path on Pipecat) is where
all of the above would apply directly.

## 6. Test calls after applying (own numbers only)

1. Ask three questions in a row → no reply starts with the same phrase; no
   "let me check".
2. Say "okay" after an answer → one short line, not a repeat.
3. Decline to give a phone number → the agent does not ask again.
4. Ask in Tamil → the whole reply is Tamil; no "pasanga"; "2001" is said as
   a number.
5. Tell it a wrong fact ("the code is 1361") → it keeps 1316.
6. Talk over the agent mid-answer → it stops.
7. Say "sari, vachidunga" → a short goodbye and the call ends.
