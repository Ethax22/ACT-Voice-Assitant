# Calling Agent — Session Handoff (2026-09-30)

Read this first in a new session before touching the Calling Agent. Full
background/rationale lives in `docs/Calling_Agent_Build_Plan.md` — this file
is just "where things stand right now and what's next."

## What's actually working right now

- **`rag-backend/main.py` has a `POST /calling-agent/ask` endpoint**
  (bearer-auth required, same `RAG_BACKEND_AUTH_KEY` as everything else).
  Takes `{"question": "..."}`, returns `{"reply": "..."}`. Deliberately
  single-turn/stateless — see the endpoint's docstring for why (Indus's own
  agent holds conversation memory for the call; this endpoint just answers
  one self-contained question at a time using the same retrieval + persona
  + `capture_lead` logic every other input path uses).
- **A Sarvam Indus agent exists** (`indus.sarvam.ai`, agent named
  "Conversation Agent 7660") with a working **`get_act_answer`** Tool wired
  to that endpoint — **confirmed live**, via Indus's own "Send" test button,
  returning real answers pulled from the actual ACT knowledge base (tested
  with "What courses does ACT offer in computer science?" → got back a real,
  correctly-phrased answer about CS/ME offerings).
- The Tool's exact working configuration (**reproduce this if the tool ever
  needs rebuilding**):
  - Method `POST` → `https://buffer-condense-valley.ngrok-free.dev/calling-agent/ask`
    (static ngrok domain — same URL every restart, just needs the tunnel
    process running: `ngrok http 8000`)
  - **Params tab:** one parameter, `question` (string)
  - **Auth tab** (not Headers — this was the bug that cost the most time,
    see "Lessons learned" below): Auth Type = **Bearer**, Token = the real
    `RAG_BACKEND_AUTH_KEY` value as a **fixed value**
  - **Body tab:** one field named `question`, gear icon set to **"Let the
    agent decide"** (this is what lets Indus's model generate the actual
    question text from the conversation)
  - Step ③ (map response to agent): needs `reply` inserted via `@reply` —
    **confirm this got saved** before ending the last session; if the tool
    doesn't speak answers back in a real test call, check this first.

## Lessons learned (useful if building more Tools, e.g. `check_availability`-
style ones for ERP lookup or lead capture)

1. **Indus's "Headers" tab does NOT carry authentication for the actual
   request** — a manually-typed `Authorization: Bearer ...` header sitting
   in the Headers tab is silently ignored. Auth must go in the dedicated
   **Auth tab** (Auth Type dropdown → Bearer → Token field). Confirmed by
   adding temporary debug logging to `rag-backend` (since removed) that
   showed the header arriving as an empty string when only set via Headers.
2. **The `@` insert-shortcut only works in step ③** (mapping API response
   fields back to the agent) — it does nothing in the Body/Headers request-
   construction fields, despite looking like it should.
3. **Body field values are set via a gear-icon dropdown** (Let the agent
   decide / Agent variable / Fixed value), not by typing a reference
   directly into the value box.
4. **"Params" tab ≠ the tool's callable arguments in the traditional
   sense** — declaring `question` there also appears to add it as a literal
   URL query string parameter on the actual HTTP request (harmless here
   since the endpoint only reads the JSON body, but worth knowing).

## What's NOT done yet

From `docs/Calling_Agent_Build_Plan.md` §4's phase list:
- **Phase 1 (Instructions/persona)** — the agent's actual persona prompt
  hasn't been finalized/pasted in yet; a draft exists in the build plan's
  §4.1, needs review and the `get_act_answer` usage instruction folded in
  (tell the model to always call this tool for ACT questions rather than
  answer from its own knowledge/Knowledge Base).
- **Phase 3 (telephony)** — no phone number set up yet. Plan: "Rent from
  Sarvam" (Vobiz-powered, confirmed compatible with warm transfer — see
  build plan §2), then forward the existing college SIM to it via carrier
  USSD code. **Waiting on Prakash for the SIM carrier/plan-type check and
  the transfer destination number** (a different number than the public
  SIM — see build plan's "the loop problem" discussion for why).
- **Phase 4 (ERP caller lookup)** — not started; needs a
  `check_caller_erp_status` Tool once there's an ERP endpoint to call
  (open question: does the ERP even expose an API — build plan §6).
- **Phases 5–10** (bilingual tuning, human transfer, recording/consent,
  launch, DLT, outbound) — not started.

## Immediate next step for a new session

Either:
(a) finalize and paste in the Instructions/persona (Phase 1), or
(b) wait on Prakash's telephony inputs (carrier check, transfer number) to
    unblock Phase 3.

Both can happen in parallel — (a) doesn't depend on (b).
