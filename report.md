# Zimi P0 Fix Brief

The implementation audit is complete. Fix **only** the P0 issues below. Do not redesign the entire architecture.

## Goal

Zimi must:

1. Speak natural Chennai Tamil / Tanglish.
2. Stop producing forced pure Tamil.
3. Start responding as quickly as possible.
4. Sound like one consistent human assistant.
5. Preserve the existing Pipecat + Sarvam + cloned voice architecture wherever possible.

---

## P0 #1 — Remove blocking language detection

**Current problem:** `ConversationRouter` waits for batch language detection. First/uncertain turns can wait up to 2.5 seconds.

**Required:**

- Remove this blocking behavior from the normal response path.
- Do **not** call Sarvam batch language detection before every response.
- The user transcript must reach the LLM immediately after finalization.
- Language switching can happen asynchronously.
- Do not wait for background language detection before starting the current reply.

---

## P0 #2 — Add three language styles

**Current:** EN / TA only.

**Implement:**

| Style | Meaning |
|---|---|
| `EN` | English |
| `TA` | Tamil |
| `TA_EN` | Natural Tamil + English code-switching |

These must be treated as Tamil-English conversational speech (`TA_EN`):

- "fees evlo bro?"
- "B.Tech fees evlo?"
- "application epdi panrathu?"
- "hostel iruka?"

**Constraints:**

- Do **not** use another LLM for this. Use a lightweight deterministic classifier.
- The classifier should use:
  - Tamil Unicode detection
  - common Tanglish / romanized Tamil words
  - English/Tamil word ratio
- It must **not** make a network request.

---

## P0 #3 — Change the Tamil LLM prompt

Remove any instruction requiring the entire Tamil answer to be written in Tamil script.

The model **must** be allowed to naturally use English words such as: fees, course, application, admission, documents, payment, hostel, placement, website, registration, deadline, branch, details, process.

**Desired style:** natural spoken Chennai Tamil / Tanglish.

**Not:** literary Tamil, textbook Tamil, formal written Tamil, or newsreader Tamil.

Add these few-shot examples:

| User | Good reply |
|---|---|
| "Bro B.Tech fees evlo?" | "B.Tech fees branch-ku branch maarum bro. Entha branch paakureenga?" |
| "Anna application epdi panrathu?" | "Application online-la pannalaam. Website-la details fill pannitu documents upload pannunga." |
| "Hostel iruka?" | "Yes bro, hostel facility irukku. Boys-aa girls-aa?" |

---

## P0 #4 — One voice per session

- Do **not** change the cloned voice based on topic.
- At the beginning of a session, select **one** Zimi voice.
- Keep the same cloned voice for: about-us, admissions, courses, placements.
- Topic classification may still be used for knowledge/UI, but it must not switch the speaker voice.
- Language switching must also **not** reset the voice.

---

## P0 #5 — Fix cloned TTS connection architecture

**Current implementation:** each sentence opens a new cloned-voice WebSocket. This causes:

- connection overhead
- prosody reset
- robotic sentence boundaries
- sequential synthesis

**Investigate** whether the Sarvam cloned-voice WebSocket can remain persistent for the entire conversation/session.

**If the API supports persistent streaming, implement:**

```
session
  → one cloned TTS WebSocket
  → multiple text chunks / sentences
  → continuous audio stream
```

Do not create a new WebSocket for every sentence.

**If persistent streaming is NOT supported:**

- Do **not** invent an unsupported protocol.
- Report exactly what Sarvam's current API allows.
- Implement the lowest-latency supported approach.

---

## P0 #6 — Preserve streaming

Do not wait for the complete LLM response. Keep:

```
LLM stream
  → first usable spoken chunk
  → TTS
  → browser
```

The first spoken chunk should be short. Prefer 8–15 words for the first conversational response when appropriate.

---

## Validation

After implementation, test these exact inputs:

1. `கட்டணம் எவ்வளவு?`
2. `fees evlo bro?`
3. `B.Tech fees evlo?`
4. `What is the B.Tech fee?`
5. `Anna application epdi panrathu?`
6. `hostel iruka?`
7. `seri`
8. `okay`
9. `CSE fees evlo bro?`

For each, log:

- STT transcript
- detected style: `EN` / `TA` / `TA_EN`
- reply language/style
- language-detection wait time
- RAG time
- LLM first-token time
- TTS first-audio time
- total user-stop → first-audio latency
- selected voice ID

---

## Important notes

- Do not claim the voice is less robotic based only on code. Actual audio testing is required.
- Do not modify unrelated functionality.
- **Before editing, show the exact files/functions that will be modified.**
