# Zimi P0 fixes — validation run (2026-10-09)

**What this is:** the nine inputs from the P0 brief, each played as synthesized speech into a
fresh voice call (calls open on English), through the real pipeline: Sarvam STT → router →
rag-backend → Sarvam 105B → cloned About Us voice. Run with
`python scenario_test.py --url ws://127.0.0.1:8011 --only p0-` against copies of both
services on ports 8010/8011.

**What this is not:** a listening test. The "visitor" is a Sarvam stock voice reading the text,
not a person with a Chennai accent, so the speech-to-text step is harder on Tanglish than
it will be for real people in some places and easier in others. **Nothing here says the voice
sounds less robotic.** That needs a person listening.

## Results (single-turn calls; times in seconds unless marked ms)

"Used by backend" is the style the backend actually applied (`[turn-backend]` line). It can
differ from "requested" because a background language detection may land between the
transcript and the LLM request.

| # | Input | STT transcript | Detected style | Style requested | Used by backend | Reply style (measured on the reply text) | Lang-detect wait | RAG (ms) | Backend first token (ms, incl. RAG) | Pipecat LLM first token | TTS first audio | User stop → first audio (log) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `கட்டணம் எவ்வளவு?` | "How much is the rent?" | EN | EN | **TA** | TA_EN | 0.00 | 3 (cached) | 161 | 0.45 | 1.77 | 2.20 |
| 2 | `fees evlo bro?` | "T. Zevlo Bro." | none → kept EN | EN | EN | EN ("Sorry, I didn't quite catch that…") | 0.00 | 143 | 338 | 0.37 | 1.64 | 2.03 |
| 3 | `B.Tech fees evlo?` | "B.Tech fees evlo?" | TA_EN | TA_EN | TA_EN | TA_EN | 0.00 | 479 | 670 | 0.70 | 1.96 | 2.69 |
| 4 | `What is the B.Tech fee?` | "What is the B.Tech fee?" | EN | EN | EN | EN | 0.00 | 1082 | 1278 | 1.31 | 1.47 | 2.86 |
| 5 | `Anna application epdi panrathu?` | "Hana application how to do it?" | EN | EN | EN | EN | 0.00 | 430 | 656 | 0.69 | 2.55 | 3.33 |
| 6 | `hostel iruka?` | "Hostel eruka." | none → kept EN¹ | EN | EN | EN | 0.00 | 321 | 507 | 0.53 | 1.58 | 2.17 |
| 7 | `seri` | "Seri." | TA_EN | TA_EN | TA_EN | TA_EN | 0.00 | 0 (small talk) | 161 | 0.16 | 2.80 | 3.06 |
| 8 | `okay` | "Okay." | none → kept EN | EN | EN | EN | 0.00 | 0 (small talk) | 144 | 0.17 | 1.39 | 1.61 |
| 9 | `CSE fees evlo bro?` | "CSC fees evlo bro?" | TA_EN | TA_EN | TA_EN | TA_EN | 0.00 | 396 | 580 | 0.61 | 2.01 | 2.67 |

¹ "eruka" was not in the word list at the time; added afterwards (`eruka`, `erukka`, `erukku`).
Row 6's classification is therefore fixed in code but that call was not re-run.

**Selected voice ID in every call:** `svc-acb2b2c9-…` (the About Us cloned voice), unchanged
across turns, topics and language switches.

Harness-measured time from the end of the visitor's clip to the first audio (this includes the
turn-end wait that the log's "user stop" does not): 2.6–4.8 s, median 3.7 s over 18 turns,
against a 0.7 s target. That target is **not** met.

## What the replies looked like

| Input | Reply (as spoken) |
|---|---|
| `B.Tech fees evlo?` | "Exact fees branch-ku branch maarum. Adhukku neenga admission office-la confirm pannanum." |
| `seri` | "Seri, unga doubts edhavadhu irundha sollunga. Admissions, courses, placements, illa campus life pathi edhavadhu kelunga." |
| `CSE fees evlo bro?` | "CSC fees exact-a theriyala bro. Adhu branch-ku branch maarum, so admissions team-kitta confirm pannalaam." |
| `கட்டணம் எவ்வளவு?` (heard as English) | "Hostel rent-… official information எதுவும் என்கிட்ட…" (Tamil with English words) |
| `What is the B.Tech fee?` | "Exact fee amounts aren't listed on our site, so you'll need to confirm with admissions…" |

Where the transcript was right, the Tanglish replies read as spoken Chennai Tamil/English,
with Tamil endings on English words, which is what the brief asks for. That is a judgement
from reading the text only.

## Findings you should know about

1. **The first Tamil sentence is exposed.** With the 2.5 s wait gone and calls starting on
   English, Tamil speech to the English-locked STT came back as "How much is the rent?"
   (twice, in two separate calls). The answer is then built from the wrong question. In
   the table's row 1 the background detection landed before the LLM request and the reply came
   back in Tamil, but about the wrong thing (rent). The call is on Tamil from then on. This
   is the trade-off flagged before the change; it is now measured, not assumed.
2. **STT mangles Tanglish from a synthetic voice** ("evlo" → "Zevlo", "Anna … epdi panrathu"
   → "Hana application how to do it?"). Row 5 was therefore classified EN. A real speaker
   may do better or worse; this needs real mic tests, not more of these.
3. **Language-detection wait is 0.00 s on every turn**, as intended. First-token times are
   0.2–1.3 s. **TTS first audio is the largest part: 1.4–2.8 s.** The persistent websocket was
   already the default before this work; keeping it open does not remove Sarvam's
   per-reply start-up for cloned voices (see below).
4. **After the Tamil-locale STT takes over, Tanglish speech arrives in Tamil script**
   (e.g. `ஹாஸ்டல் இருக்கா?`), so it is classed TA, not TA_EN, and answered with Tamil-script
   Tamil plus English words. That is allowed by the new rules but is not the romanized
   Tanglish reply.
5. In the back-to-back call a Tamil-script turn was answered "Sorry, நான் சொன்னது புரியல.",
   and one English turn repeated the previous answer verbatim. The second looks like
   existing behaviour of the backend's answer logic, not something this change touches; not
   investigated.

## P0 #5 — what Sarvam's cloned-voice websocket allows

- **Persistent connection: supported and already in use.** `PersistentClonedVoiceTTSService`
  (built before this work) holds one `wss://api.sarvam.ai/voices/clone/ws` connection for the
  call, sends text sentence by sentence, flushes at the end of a reply, re-sends `config` on a
  settings change. Verified live in the earlier session and again here (9 turns, no reconnects).
- **What changed:** the pipeline used to build one such connection per topic (up to four, one
  per cloned topic voice, switched with a `ServiceSwitcher`). It now builds exactly one, for the
  About Us voice.
- **What persistence does not fix:** Sarvam's per-reply start-up. TTS first audio is still
  ≈1.3–2.8 s per reply. Anything below that needs a different voice (stock Bulbul streams
  faster) or a change on Sarvam's side. Not invented around.

## P0 #6 — streaming and the first chunk

- The path is unchanged: LLM stream → sentence → TTS → browser. Nothing waits for the full
  reply.
- Found: `TTS_MIN_BUFFER_SIZE` was 120 characters in the code defaults and in `.env`. Sarvam
  holds text until that many characters arrive, and Pipecat flushes only at the end of a
  reply, so an 8–15-word first sentence (≈45–90 characters) was waiting for more text. Default
  lowered to 40 (code, `.env.example`, and the local `.env`).
- The reply instruction now asks for a first sentence of 8–15 words. In the replies shown above the first sentences were 5–15 words.
- **Not measured:** I did not A/B 120 against 40 on the same turns, so the gain is unproven
  and the effect on smoothness is unjudged. To compare: set `TTS_MIN_BUFFER_SIZE` and re-run
  this scenario set.

## Open items

- Real-ear test of Tanglish: Bulbul reading romanized Tanglish through the `ta-IN` locale
  versus `en-IN` (`TA_EN_TTS_LANGUAGE`). Not tried.
- Native-speaker review of `style_classifier.py` word lists and the Tanglish/Tamil prompts.
- Real-mic test of the Tamil-first-sentence problem (finding 1). If it is too costly in
  practice, the options are a short wait only on the first turn, or starting calls with the
  pre-connect detection back on (`DETECT_LANGUAGE_BEFORE_CONNECT`).
- The phone agent (`/calling-agent/ask`) shares the Tamil prompts, so its Tamil replies now
  allow English words in English letters. Check how Sarvam Indus speaks those before relying
  on it.
