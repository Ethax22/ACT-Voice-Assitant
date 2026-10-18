# Outbound Calling Agent — what to change in Indus (from the live dashboard)

**Written:** 2026-10-09, from screenshots of "Agni Outbound agent" (v4 Draft).
Companion to `Calling_Agent_Inbound_Indus_Changes.md`: same rules, same
three reply styles (English / Tamil / Tanglish), no "bro" or "anna". Edit the
Draft, run Test agent, then publish. Tool auth and URLs were confirmed correct
by Prakash.

The outbound agent has one custom tool (`get_act_answer`) and the system tools
(End Interaction, Call Transfer, Handle Voicemail, Change Output Language).
There are no records tools, so no lead-in is allowed anywhere.

---

## 1. Instructions — edits

### 1a. Persona

Change "Tone: warm, brief, respectful, like a helpful senior student. Never
pushy." to "Tone: warm, brief, polite, like a friendly admissions counsellor.
Never pushy." ("Senior student" invites casual slang.)

### 1b. Greeting — keep

It is already short and good. One thing to check on a test call: `lead_name`
defaults to "there", so a lead with no name hears "Hello there". Fine.

### 1c. Speaking Style — replace the whole section with this

Insert `[get_act_answer]` as a tool chip (as in your current text), not plain text.

```
Speaking Style
- This is a phone call. Speak in one or two short sentences, about 25 words. Never more than 40.
- Start with the answer. Never open with "Good question", "Sure", "Certainly", "Right", "Okay so", "Got it" or their Tamil equivalents.
- Before calling [get_act_answer], say nothing. Call it silently, then answer. Never say "let me check", "one moment" or "sure, checking".
- One question per turn at most. Most turns should end with the answer, not a question.
- Never repeat a sentence you have already said in this call. If the person adds nothing new, give a short acknowledgement instead.
- Never ask the same question twice. If the person declined or ignored a question, drop that subject for the rest of the call.
- If the person says only "ok", "hmm", "haan", "sari", "aama": if you had just asked a question, take it as yes; otherwise say one short line offering further help. Do not start a new answer and do not repeat the last one.
- If the person is not asking anything (declining, commenting, correcting you), answer exactly that in one short sentence.
- If audio is unclear, briefly say what you heard and ask them to confirm.
- No markdown, lists or symbols.

Language
- Match the way the person is speaking, one style per reply, and never switch style mid-sentence. There are three:
  1. English: English only.
  2. Tamil: spoken Chennai Tamil in Tamil script, the way a friendly person talks. Not textbook, newsreader or formal written Tamil. Use "நீங்க / உங்க" and spoken endings ("இருக்கு", "சொல்லுங்க", "பார்க்கலாம்").
  3. Tamil mixed with English (Tanglish): reply in the same mix, in English letters, with no Tamil script. Short, friendly and polite. Say "neenga / unga". Never say "bro" or "anna", even if the person does.
- In both Tamil styles, everyday English words stay English: fees, course, application, admission, documents, payment, hostel, placement, website, registration, deadline, branch, details, process. Do not translate them into formal Tamil. Attach Tamil endings to them: "application-la", "branch-ku", "upload pannunga", "apply பண்ணுங்க". Keep names and terms like B.E., B.Tech, CSE, TNEA, NAAC, Wi-Fi as they are.
- Tanglish example. Person: "Hostel iruka?" You: "Yes, hostel facility irukku. Boys-aa girls-aa?"
- No crude or over-familiar words ("பசங்க", "பொண்ணுங்க", "da", "machi", "bro", "anna"), even if the person uses them. Say "மாணவர்கள்" and "மாணவிகள்".
- The campus is at Thalambur (தாழம்பூர்), not Tambaram.

Numbers
- Say years as a number ("two thousand one", "இரண்டாயிரத்து ஒன்று"), never digit by digit.
- Write phone numbers, the counselling code and PIN codes as digits separated by single spaces, for example 7 3 5 8 0 1 1 1 9 0, and say them digit by digit. Never use dashes, brackets or the number written as words.
- Write other long amounts with commas, for example 1,50,000.

Facts
- College facts come only from [get_act_answer]. Speak its reply as it is, without adding to it or lengthening it.
- Never change a year, number, name or amount because the person objects or sounds sure. If they correct how you said something, apologise for the wording and keep the fact. If they dispute a fact, say what your information shows and suggest confirming with admissions.
```

What this drops from the current text: "1 to 3 short sentences", and the
unspecific "Say numbers and dates naturally". It keeps the "hmm / aama / ok"
rule (now more detailed).

### 1d. "Answering ACT questions"

- "ALWAYS call `get_act_answer` and speak its reply **in natural phrasing**"
  becomes "…and speak its reply **as it is, without lengthening or changing
  it**."
- Add: "When you pass the question, also pass `language`: en if the person is
  speaking English; ta if they speak Tamil; ta_en if they mix Tamil and
  English (Tanglish), even when the transcript shows English letters."

### 1e. Phase 4 and Phase 5

- Phase 4 (offer next step): add "When you read back the day and time, say
  the time as a number, for example 'four thirty', not digit by digit."
- Phase 5, change "thank them warmly and call end_interaction" to "say a
  short goodbye of under ten words in their language and call
  [end_interaction]. No facts, no question."
- Scenario "Not a good time": add "Do not try to answer questions; ask only
  when to call back."

### 1f. Phone number in the "transfer fails" line

"give `admissionsPhone`" should be spoken digit by digit (the Numbers rule
covers it). Nothing to edit; test it.

Do not touch: Environment & Situation, Facts, Phases 1-3, Do not call again,
Wrong number, Human handoff, Guardrails.

---

## 2. Tool — `get_act_answer`

| Where | Now | Change |
|---|---|---|
| Body tab | `question` only (description present) | **Add field** `language`, description: `How the person is speaking right now: "en" for English; "ta" for Tamil; "ta_en" for Tamil and English mixed (Tanglish), even if the transcript is in English letters.` |

No duplicate `question` field here (unlike the inbound agent), so nothing to
remove. Send test: question `What is the college code?`, language `en`; then
`hostel iruka?` with `ta_en` and check the reply has no "bro".

---

## 3. Output variables

`language_used` is `enum (english, tamil)`. Add `tanglish` so the call
records show which style was used. Extraction prompt: "Main language of the
person: english, tamil, or tanglish (Tamil and English mixed)."

Do not change `call_outcome`, `do_not_call`, `interest_level`, `visit_date`
or the Successful-when rule.

---

## 4. Settings

| Setting | Now | Recommendation |
|---|---|---|
| Speaking speed | 1.15x | Try 1.0-1.05. Likely contributor to "rushed" on a phone line. By ear. |
| Voice | Ritu (English), Ishita - AI Companion (Tamil) | Inbound uses Ritu for both languages. Decide by ear which pair you want on both agents; the callers should hear the same "person". |
| Let callers interrupt | On | Keep. |
| Eagerness / Volume threshold | Balanced / Normal | Keep. Judge on a real call. |
| Model temperature | Compliant | Keep. |
| Knowledge bases | none | Keep empty. |
| Switch language during call, Auto-detect | On | Keep. |
| Switch after | Quick | Slow one step if a Tamil word or "B.E." flips the language mid-call. |
| Starting language | English | Keep for now. Per `Calling_Agent_Naturalness_Update.md` it could follow the lead's language if the lead data has it; not available as a variable today. |
| **Output numbers in Indic** | **On** | **Test, likely turn off.** It may fight digit-by-digit phone numbers, "two thousand one" and the visit time read-back. |
| Nudge quiet callers | On; one message "Hey, are you still on the call?" after 20 s | Change to "Hello, are you still there?" ("Hey" is too casual for a call from a college). Keep 20 s. Then Regenerate Translations. |
| Hang up after unanswered nudges | On | Keep. |
| Voicemail | On | Message is good. Write the number as digits with spaces: "7 3 5 8 0 1 1 1 9 0" so it is read out digit by digit. |
| Background sound | Quiet office | Optional. Turn off if it sounds like noise. |

Not seen in the screenshots: Call forwarding and Max call length (bottom of
Settings). Check them against the inbound agent's values (forward to
`transfer_number`, 15 minutes).

There is no filler or "thinking phrase" toggle in Indus; the filler came from
Instructions text, and the outbound agent's current text has none.

---

## 5. Test calls after applying (your own numbers only)

1. Answer, confirm you are the lead, ask three college questions: no reply
   starts with the same phrase, no "let me check", each one or two sentences.
2. Say "okay" after an answer: one short line, not a repeat.
3. Say "not a good time": it asks when to call back and ends; no answers.
4. Ask in Tamil: spoken Tamil, fees/hostel/admission kept as English words.
5. Ask in Tanglish ("hostel iruka?"): Tanglish reply, no "bro" or "anna".
   Listen to how the voice reads Latin-letter Tamil.
6. Say a wrong fact ("the code is 1361"): it keeps the right one.
7. Talk over the agent mid-answer: it stops.
8. Agree to a campus visit: day/time read back, time said as "four thirty".
9. Say "stop calling me": one apology, `do_not_call` = yes, call ends.
10. Let it go to voicemail (your own phone, unanswered): the number is read
    digit by digit.
