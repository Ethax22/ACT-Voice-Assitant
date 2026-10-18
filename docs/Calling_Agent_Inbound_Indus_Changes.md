# Inbound Calling Agent — what to change in Indus (from the live dashboard)

**Written:** 2026-10-09, from screenshots of the "Agni Call Agent" inbound
agent (v30 Draft). Supersedes the guesses in
`Calling_Agent_Naturalness_Update.md` §2–§4 for the inbound agent: setting
names below are the real ones. Outbound agent not yet inspected.

Nothing here is applied yet. Edit the **Draft**, run Test agent, then publish.

---

## 1. Instructions — edits

### 1a. Replace the whole "Speaking Style" section with this. Tool names in square
brackets must be inserted as **tool chips** in Indus (as the current text
has them), not typed as plain text. Do the same for `get_act_answer` in the
Facts block below.

```
Speaking Style
- This is a phone call. Speak in one or two short sentences, about 25 words. Never more than 40.
- Start with the answer. Never open with "Good question", "Sure", "Certainly", "Right", "Okay so", "Got it" or their Tamil equivalents.
- Before calling [get_act_answer], say nothing. Call it silently, then answer. Never say "let me check", "one moment" or "sure, checking" for it.
- Before calling [get_student_report] or [get_class_summary], say one very short phrase such as "One moment." Vary it. This is the only place a lead-in is allowed.
- Mention at most three items in one flowing sentence and offer to say more. Never list like a menu.
- One question per turn at most. Most turns should end with the answer, not a question.
- Never repeat a sentence you have already said in this call. If the caller adds nothing new, give a short acknowledgement instead.
- Never ask the same question twice. If the caller declined or ignored a question, drop that subject for the rest of the call.
- If the caller says only "ok", "hmm", "haan", "sari", "aama": if you had just asked a question, take it as yes; otherwise say one short line offering further help. Do not start a new answer and do not repeat the last one.
- If the caller is not asking anything (declining, commenting, correcting you), answer exactly that in one short sentence.
- If audio is unclear, briefly say what you heard and ask them to confirm.
- No markdown, lists or symbols.

Language
- Match the way the caller is speaking, one style per reply, and never switch style mid-sentence. There are three:
  1. English caller: English only.
  2. Caller speaks Tamil: spoken Chennai Tamil in Tamil script, the way a friendly person talks. Not textbook, newsreader or formal written Tamil. Use "நீங்க / உங்க" and spoken endings ("இருக்கு", "சொல்லுங்க", "பார்க்கலாம்").
  3. Caller mixes Tamil and English (Tanglish): reply in the same mix, in English letters, with no Tamil script. Short, friendly and polite. Say "neenga / unga". Never say "bro" or "anna", even if the caller does.
- In both Tamil styles, everyday English words stay English: fees, course, application, admission, documents, payment, hostel, placement, website, registration, deadline, branch, details, process. Do not translate them into formal Tamil (no கட்டணம், விடுதி, சேர்க்கை, வேலைவாய்ப்பு). Attach Tamil endings to them: "application-la", "branch-ku", "upload pannunga", "apply பண்ணுங்க". Keep names and terms like B.E., B.Tech, CSE, TNEA, NAAC, Wi-Fi as they are.
- Tanglish example. Caller: "Hostel iruka?" You: "Yes, hostel facility irukku. Boys-aa girls-aa?"
- No crude or over-familiar words ("பசங்க", "பொண்ணுங்க", "da", "machi", "bro", "anna"), even if the caller uses them. Say "மாணவர்கள்" and "மாணவிகள்".
- The campus is at Thalambur (தாழம்பூர்), not Tambaram.

Numbers
- Say years as a number ("two thousand one", "இரண்டாயிரத்து ஒன்று"), never digit by digit.
- Write phone numbers, the counselling code and PIN codes as digits separated by single spaces, for example 7 3 5 8 0 1 1 1 9 0, and say them digit by digit. Never use dashes, brackets or the number written as words.
- Write other long amounts with commas, for example 1,50,000.

Facts
- College facts come only from get_act_answer. Speak its reply as it is, without adding to it or lengthening it.
- Never change a year, number, name or amount because the caller objects or sounds sure. If they correct how you said something, apologise for the wording and keep the fact. If they dispute a fact, say what your information shows and suggest confirming with admissions.
```

What this removes from the current text: the "say Let me check that / One
moment" rule, the Sure/Right/Okay openers, "1 to 3 sentences", and "mirror how
they speak, including casual mixed speech" (that last one invites slang).
What it keeps: contractions-style warmth comes from the Persona line, which is
unchanged; unclear-audio, phone-number format and comma rule are carried over.

### 1b. "Answering ACT questions" — two bullets

- Change "ALWAYS call `get_act_answer` and speak its reply **in your own
  natural phrasing**" to "…and speak its reply **as it is, without lengthening
  or changing it**." Reason: re-phrasing is where figures and years drift.
- Add: "When you pass the question, also pass `language`: en if the caller is
  speaking English; ta if they speak Tamil; ta_en if they mix Tamil and
  English (Tanglish), even when the transcript shows English letters."
  The backend already supports all three (`LANGUAGE_RULES` in
  `rag-backend/main.py`) and writes the reply in the matching style, the
  same way the website widget does. The Indus agent then only speaks it.

### 1c. "Student, parent and faculty records"

- Leave "Before calling a records tool, say a short lead-in as described in
  Speaking Style" as it is: the lead-in rule now lives in Speaking Style (1a)
  and applies only to the records tools. Delete both lines if you want zero
  filler; decide after a test call.

### 1d. "Human handoff"

- Change "Say one short sentence such as: Sure, connecting you to the
  admissions team now." to "Say one short sentence such as: Connecting you to
  the admissions team now." (no "Sure" opener.)

### 1e. "Closing" — replace with

```
Closing
- If the caller is wrapping up ("ok thanks", "that's all", "நன்றி", "வச்சிடுறேன்"), say a short goodbye of under ten words in their language and call end_interaction. No facts, no question.
- Otherwise, after answering, do not ask "anything else?" every time. Ask it at most once in the whole call.
```

Do not touch: Persona, Environment, Objective ("speak its reply" is fine; add
"as it is"), Facts/variables, lead capture, records rules, handoff flow,
sensitive topics, guardrails.

---

## 2. Tools — `get_act_answer`

| Where | Now | Change |
|---|---|---|
| **Params** tab | `question` (with description) | Remove it, **if** the Send test in step 3 still works. The backend reads a JSON body, not the query string. |
| **Body** tab | `question`, description box **empty** ("Describe this value for the agent") | Paste the description that is currently on the Params entry: "The caller's question, rephrased as a complete, standalone sentence — include any context from earlier in the call." |
| **Body** tab | no `language` | **Add field** `language`, description: `How the caller is speaking right now: "en" for English; "ta" for Tamil; "ta_en" for Tamil and English mixed (Tanglish), even if the transcript is in English letters.` |

Why: `question` is defined twice (Params and Body); the Body copy, which is the
one the backend reads, has no description, so the model has no guidance for
it. `POST /calling-agent/ask` already accepts `language`
(`rag-backend/main.py:982`).

Send test: question `What is the college code?`, language `en`; then a Tamil
question with `ta`.

Other tools (`get_student_report`, `get_class_summary`, `get_transfer_number`)
have sensible descriptions and body fields. No change.

Tool auth and URLs were checked by Prakash (2026-10-09) and are set
correctly; nothing to change there.

---

## 3. Settings tab

| Setting | Now | Recommendation |
|---|---|---|
| Let callers interrupt | On | Keep. |
| Eagerness to respond | Balanced | Keep for now. Raise if replies feel slow, lower if it talks over pauses. Judge on a real call. |
| Volume threshold | Normal | Keep. |
| Model temperature | Compliant (low) | Keep. |
| Knowledge bases | none | Keep empty. All college facts must come from `get_act_answer`. |
| Switch language during call | On | Keep. |
| Auto-detected language switch | On | Keep. |
| Switch after | Quick | Try one step slower if a short Tamil word or an English term like "B.E." flips the language mid-call. Test first. |
| Starting language | English | Keep. |
| **Output numbers in Indic** | **On** ("500" → "paanch sau") | **Test, likely turn off.** It rewrites numbers into spoken words and may fight the digit-by-digit phone number and "two thousand one" rules above. Make test calls with a phone number and a year before deciding. |
| **Speaking speed** | **1.15x** | Try 1.0–1.05. 1.15x is a likely contributor to "rushed/robotic" on a phone line. By ear. |
| Voice / Per-language voices | Ritu for both English and Tamil | By ear, ideally with a Tamil speaker. Not decidable from here. |
| Pitch | 0.00 | Keep. |
| Pronunciation dictionary | none | Optional later: "B.E.", "TNEA", "ACT" if they are mispronounced. |
| Nudge quiet callers | On; message 1 after 10 s, message 2 (goodbye) after 10 s more | Keep. Message 1 is fine; it is a different sentence from the greeting. |
| Hang up after unanswered nudges | On | Keep. |
| Voicemail | On, message set | Keep. |
| Call forwarding | On, `transfer_number` variable | Keep. Platform note says "Only supported for Vobiz…": confirm that is your telephony provider. |
| Max call length | 15 min | Keep. |
| Background sound | Quiet office | Optional. Turn off if it sounds like noise on the line. |

Not present in the Indus UI (so the plan's checklist item does not apply):
a "filler / thinking phrase" setting. The filler is coming from the
Instructions text (section 1a), not a toggle.

---

## 4. Test calls after applying (own numbers only)

1. Three college questions in a row: no reply starts with the same phrase,
   no "let me check", each about one or two sentences.
2. Say "okay" after an answer: one short line, not a repeat.
3. Decline to give a phone number: it does not ask again.
4. Ask in Tamil: spoken Tamil, with fees / hostel / admission kept as
   English words, no "pasanga"; "2001" said as a number; same facts as the
   English answer.
4b. Ask in Tanglish ("hostel iruka bro?"): the reply is Tanglish, one or two
   short sentences. **Listen to how the voice reads Latin-letter Tamil**
   ("irukku", "pannunga"). If it sounds wrong, the fix is on the Indus
   voice/language side, not in the text; tell me what you hear.
5. Say a wrong fact ("the code is 1361"): it keeps the right one.
6. Talk over the agent mid-answer: it stops.
7. Say "sari, vachidunga": short goodbye, call ends.
8. Give a phone number for a lead: read back digit by digit (checks the
   "Output numbers in Indic" decision).
9. Ask for your marks as a registered student: at most one short lead-in.
