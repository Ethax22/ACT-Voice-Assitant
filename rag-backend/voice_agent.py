"""Relays audio + control messages between the browser and Deepgram's Voice
Agent API for the English voice path.

The Deepgram API key never touches the browser: the browser opens a plain
WebSocket to THIS backend (/voice/en), and this module opens the actual
Deepgram connection server-side, forwarding audio both ways. Deepgram's
`think.endpoint` is pointed back at this same backend's /v1/chat/completions,
so English voice answers come from the exact same shared brain as typed chat.
"""
import asyncio
import json
import logging
import os
import re
import time

import websockets
from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("voice_agent")
logging.basicConfig(level=logging.INFO)

DEEPGRAM_AGENT_URL = "wss://agent.deepgram.com/v1/agent/converse"

# Model choices — see docs/Voice_Model_Choices.md. Flux is Deepgram's unified
# listen/speak model line and requires "version": "v2" on both providers —
# confirmed against a real Settings JSON pulled from Prakash's own Deepgram
# console (a prior attempt to drop "version" based on a non-Flux example was
# wrong; that example just wasn't using Flux).
LISTEN_MODEL = "flux-general-en"
# Overridable via env so a different Flux voice can be A/B tested without a
# code change/redeploy — browse other options in the Deepgram Playground's
# Voice Agent Settings → Voice list (naming pattern: flux-{voice}-en), then
# set DEEPGRAM_SPEAK_MODEL to try one. Sienna is also the fallback voice for
# the greeting and any question that doesn't clearly match a category, per
# CATEGORY_VOICES below — keep this in sync with that default if changed.
SPEAK_MODEL = os.environ.get("DEEPGRAM_SPEAK_MODEL", "flux-sienna-en")
MODEL_VERSION = "v2"

# Per-category voice, swapped mid-session via Deepgram's UpdateSpeak message
# (confirmed real: https://developers.deepgram.com/docs/voice-agent-update-speak).
# Per that doc, a voice change takes effect on the agent's NEXT turn — it
# doesn't interrupt a reply already being spoken — so this is triggered right
# after the user's question is transcribed (ConversationText, role "user"),
# before Deepgram generates/speaks the answer to it. "about-us" falls back to
# SPEAK_MODEL (Sienna) rather than having its own distinct voice, since Sienna
# is also the greeting/unmatched-question voice — keeping About Us on the
# same voice as the default avoids introducing a fifth voice.
CATEGORY_VOICES = {
    "admissions": "flux-cole-en",
    "courses": "flux-cliff-en",
    "placements": "flux-haley-en",
}

# Same keyword heuristic as widget/index.html's classifyCategory — kept in
# sync manually since one is Python (classifying the user's question,
# server-side, to pick the upcoming answer's voice) and the other is JS
# (classifying the assistant's reply, client-side, to highlight the matching
# avatar tile). Different inputs, same category set, so duplicating rather
# than sharing is simplest given they run in different languages/processes.
CATEGORY_KEYWORDS = {
    "about-us": ["found", "establish", "accredit", "naac", "campus", "history", "autonomous", "vision", "mission", "agni college", "about act"],
    # Deliberately NOT including degree-name abbreviations like "b.e",
    # "b.tech", "m.e", "m.tech" here — confirmed live that they backfire.
    # Any admissions answer that itemizes UG vs PG process naturally says
    # "For B.E./B.Tech... For M.E./M.Tech...", racking up 4 courses-keyword
    # hits just from naming which degree the admission process applies to,
    # which beat admissions's own keyword count (admission/apply/counsel)
    # and made an admissions answer highlight/voice as "courses" instead.
    # These abbreviations name a degree, not the topic being discussed.
    "courses": ["course", "program", "department", "branch", "curriculum", "semester", "syllabus"],
    "admissions": ["admission", "apply", "application", "counsel", "entrance", "eligib", "fee", "cutoff", "seat", "enroll"],
    "placements": ["placement", "recruit", "company", "companies", "package", "salary", "intern", "hire", "hiring", "offer", "drive"],
}


def classify_category(text: str) -> str | None:
    if not text:
        return None
    lower = text.lower()
    best_key = None
    best_score = 0
    for key, keywords in CATEGORY_KEYWORDS.items():
        # Match at a word BOUNDARY but allow any suffix after the keyword
        # (no trailing \b) — plain `kw in lower` let "mission" (an about-us
        # keyword) silently match inside "admission"/"admissions", so every
        # admissions question also scored a point for about-us and, since
        # about-us is checked first and ties don't overwrite (score >
        # best_score is strict), about-us won the tie and stole the vote on
        # every admissions question. A strict `\bkw\b` fixes that collision
        # but then breaks plurals ("admission" no longer matches
        # "admissions"), since there's a word char (the 's') immediately
        # after, not a boundary. Requiring the boundary only BEFORE the
        # keyword gets both right: "mission" still can't match starting
        # mid-word inside "admission" (no boundary between 'd' and 'm'), but
        # "admission" still matches "admissions", "applying", etc.
        score = sum(
            1 for kw in keywords
            if re.search(r"\b" + re.escape(kw), lower)
        )
        if score > best_score:
            best_score = score
            best_key = key
    return best_key

# think.provider.model is required by Deepgram's schema even when a custom
# `endpoint` is set (the endpoint URL is what actually gets called — this
# backend ignores the model name and always uses its own Azure deployment —
# but Deepgram's parser rejects the Settings message if the field is absent).
THINK_MODEL_PLACEHOLDER = "act-ai"

PERSONA_PROMPT = (
    "You are ACT AI, the voice assistant for Agni College of Technology. "
    "Talk like a helpful senior student showing someone around campus: warm, "
    "direct, and brief. Keep answers short since this is a spoken "
    "conversation, not a lookup. Start most replies with a brief, natural "
    "spoken acknowledgment (\"Sure,\" \"Good question,\" \"Got it,\" \"Ah, "
    "that's a good one\") before the answer — vary the phrase each time, "
    "never repeat the same one two turns in a row. Never use markdown or "
    "formatting symbols (no asterisks, bullets, bold) and say digit strings "
    "like phone numbers the way you'd speak them aloud, not as hyphenated "
    "blocks."
)

GREETING = (
    "Hi! I'm ACT AI. Ask me about admissions, courses, placements, or "
    "campus life at Agni College of Technology."
)


def _build_settings_message() -> dict:
    rag_backend_url = os.environ["RAG_BACKEND_PUBLIC_URL"].rstrip("/") + "/v1/chat/completions"
    rag_backend_auth_key = os.environ.get("RAG_BACKEND_AUTH_KEY", "")

    return {
        "type": "Settings",
        "audio": {
            "input": {"encoding": "linear16", "sample_rate": 16000},
            "output": {"encoding": "linear16", "sample_rate": 24000, "container": "none"},
        },
        "agent": {
            "listen": {
                "provider": {
                    "type": "deepgram",
                    "model": LISTEN_MODEL,
                    "version": MODEL_VERSION,
                }
            },
            "think": {
                "provider": {
                    "type": "open_ai",
                    "model": THINK_MODEL_PLACEHOLDER,
                },
                "endpoint": {
                    "url": rag_backend_url,
                    "headers": {"Authorization": f"Bearer {rag_backend_auth_key}"},
                },
                "prompt": PERSONA_PROMPT,
            },
            "speak": {
                "provider": {
                    "type": "deepgram",
                    "model": SPEAK_MODEL,
                    "version": MODEL_VERSION,
                }
            },
            "greeting": GREETING,
        },
    }


async def run_english_voice_session(client_ws: WebSocket):
    deepgram_api_key = os.environ["DEEPGRAM_API_KEY"]

    await client_ws.accept()

    try:
        async with websockets.connect(
            DEEPGRAM_AGENT_URL,
            additional_headers={"Authorization": f"Token {deepgram_api_key}"},
        ) as agent_ws:
            settings_message = _build_settings_message()
            logger.info("Sending Deepgram Settings: %s", json.dumps(settings_message))
            await agent_ws.send(json.dumps(settings_message))

            async def browser_to_agent():
                while True:
                    message = await client_ws.receive()
                    # The low-level receive() returns the raw ASGI message
                    # dict, including {"type": "websocket.disconnect"} on
                    # disconnect — unlike receive_text()/receive_bytes(),
                    # it does NOT raise WebSocketDisconnect for us. Calling
                    # receive() again after that message is an error
                    # ("Cannot call receive once a disconnect message has
                    # been received"), so we must stop here explicitly.
                    if message["type"] == "websocket.disconnect":
                        return
                    data = message.get("bytes")
                    if data is not None:
                        await agent_ws.send(data)
                    else:
                        text = message.get("text")
                        if text is not None:
                            await agent_ws.send(text)

            async def agent_to_browser():
                # Deepgram streams TTS audio as many small chunks per second
                # (roughly one per word/syllable). A synchronous logger.info()
                # call on every single chunk was blocking this loop long
                # enough per chunk (console I/O is slow, especially on
                # Windows) that chunks arrived at the browser later than they
                # finished playing — the browser's gapless Web Audio
                # scheduling (widget/index.html playPcm16Chunk) only stays
                # gapless if chunks arrive faster than they play, so this
                # produced an audible gap after every word. Audio chunks are
                # no longer logged individually; only the running total is
                # logged periodically for diagnostics.
                audio_bytes_forwarded = 0
                audio_chunks_forwarded = 0

                # Real turn-latency measurement, from Deepgram's own event
                # timestamps — replaces guessing at numbers from a video.
                # UserStartedSpeaking marks the start of the user's turn;
                # AgentThinking marks the moment Deepgram has finished
                # listening and is generating a reply (this is where our
                # /v1/chat/completions call + Azure OpenAI TTFB lands); the
                # first audio byte after AgentThinking marks when the user
                # actually starts hearing a response. Logging both gaps
                # separates "how long until we started generating" (VAD/
                # endpointing, not ours to fix) from "how long generation +
                # TTS took" (our RAG backend + Azure OpenAI + Deepgram TTS —
                # the part we can actually optimize).
                turn_started_at = None
                thinking_started_at = None
                awaiting_first_audio = False
                current_voice = SPEAK_MODEL

                async for message in agent_ws:
                    if isinstance(message, (bytes, bytearray)):
                        audio_bytes_forwarded += len(message)
                        audio_chunks_forwarded += 1
                        if awaiting_first_audio:
                            awaiting_first_audio = False
                            now = time.monotonic()
                            if thinking_started_at is not None:
                                logger.info(
                                    "LATENCY: thinking -> first audio byte: %.2fs "
                                    "(our RAG backend + Azure OpenAI + Deepgram TTS)",
                                    now - thinking_started_at,
                                )
                            if turn_started_at is not None:
                                logger.info(
                                    "LATENCY: user started speaking -> first audio byte: "
                                    "%.2fs (total, includes end-of-turn detection)",
                                    now - turn_started_at,
                                )
                        if audio_chunks_forwarded % 50 == 0:
                            logger.info(
                                "Forwarded %d audio chunks (%d bytes total this session)",
                                audio_chunks_forwarded,
                                audio_bytes_forwarded,
                            )
                        await client_ws.send_bytes(message)
                    else:
                        try:
                            parsed = json.loads(message)
                        except (TypeError, ValueError):
                            parsed = None
                        if parsed is not None:
                            msg_type = parsed.get("type")
                            if msg_type == "UserStartedSpeaking":
                                turn_started_at = time.monotonic()
                            elif msg_type == "AgentThinking":
                                thinking_started_at = time.monotonic()
                                awaiting_first_audio = True
                            elif msg_type == "ConversationText" and parsed.get("role") == "user":
                                # Switch voice for the UPCOMING answer, based
                                # on the question that was just asked. Per
                                # Deepgram's docs, UpdateSpeak takes effect on
                                # the agent's next turn — it won't cut off
                                # anything currently playing — so sending it
                                # here, right after the user's question is
                                # transcribed and before AgentThinking even
                                # fires, lands it in time for that answer.
                                #
                                # Only switch on a POSITIVE category match —
                                # if nothing matches, keep the current voice
                                # instead of falling back to SPEAK_MODEL.
                                # Confirmed live: a short follow-up like
                                # "Coding side interest is minimal." (answering
                                # the assistant's own question mid-courses-
                                # conversation) naturally repeats none of the
                                # courses keywords, classified as unmatched,
                                # and yanked the voice back to Sienna away
                                # from Cliff mid-topic. Each turn is
                                # classified with no memory of the ongoing
                                # topic, so an unmatched turn is far more
                                # often "a continuation that didn't repeat
                                # keywords" than "a genuinely new, unrelated
                                # topic" — staying put is the safer default.
                                # Matches the widget's glow classifier, which
                                # already only updates on a truthy category.
                                category = classify_category(parsed.get("content", ""))
                                target_voice = CATEGORY_VOICES.get(category, current_voice)
                                if category and target_voice != current_voice:
                                    await agent_ws.send(json.dumps({
                                        "type": "UpdateSpeak",
                                        "speak": {
                                            "provider": {
                                                "type": "deepgram",
                                                "version": MODEL_VERSION,
                                                "model": target_voice,
                                            }
                                        },
                                    }))
                                    logger.info(
                                        "VOICE: switching to %s (category=%s) for next turn",
                                        target_voice, category or "unmatched",
                                    )
                                    current_voice = target_voice
                        # Log every text/control frame from Deepgram — this is
                        # where Warning/Error events about a bad Settings
                        # message (wrong model name, missing field, etc.) show
                        # up before the connection closes. Text/control frames
                        # are infrequent (not per-audio-chunk), so logging
                        # each one here doesn't reintroduce the stall above.
                        logger.info("Deepgram message: %s", message)
                        await client_ws.send_text(message)

            forward_task = asyncio.create_task(browser_to_agent())
            backward_task = asyncio.create_task(agent_to_browser())
            done, pending = await asyncio.wait(
                [forward_task, backward_task], return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, WebSocketDisconnect):
                    logger.warning("Voice session ended with: %r", exc)
    finally:
        try:
            await client_ws.close()
        except Exception:
            pass
