"""Per-topic TTS voice switching + mid-call spoken language switching, both
restored/added 2026-09-29.

History: the original English path (Deepgram's Voice Agent, retired 2026-09-
25 per CLAUDE.md decision #1) supported exactly this — four distinct voices,
one per topic (about-us/admissions/courses/placements), classified from the
visitor's question and applied to the UPCOMING answer via Deepgram's
mid-session `UpdateSpeak` message. That logic lived in the now-deleted
`rag-backend/voice_agent.py` (see git history, commit 848cbad) and was never
carried over to the unified Sarvam pipeline — Tamil never had it either,
since Tamil was always Sarvam-only and this feature was Deepgram-specific.
This module rebuilds it for BOTH languages on Sarvam, plus adds mid-call
language switching, which never existed in either the old or new design.

Mechanism (confirmed live against the installed Pipecat 1.12.0 + Sarvam's
real API, not assumed): Sarvam's realtime STT and streaming TTS both support
a live `config.update` over their already-open per-call WebSocket —
`STTUpdateSettingsFrame`/`TTSUpdateSettingsFrame` reach that mechanism from
inside a Pipecat pipeline without tearing down or reconnecting anything
(`SarvamRealtimeSTTService._update_settings` / `SarvamTTSService._update_
settings`, both in the installed package). `LLMUpdateSettingsFrame` does the
analogous thing for the persona's system_instruction. So a language switch
is three settings-update frames, not a pipeline rebuild.

Classification is a plain keyword heuristic identical in spirit to the old
Deepgram-era `classify_category` (and to widget/index.html's client-side
`classifyCategory`, which does the same thing for the assistant's text to
pick which avatar tile to highlight — kept as a separate, not shared,
implementation since one runs server-side in Python on the VISITOR'S
question and the other runs client-side in JS on the ASSISTANT'S reply).

Everything Tamil-related here (keywords, switch-trigger phrases) is a
first-pass, not verified by a native speaker — same caveat as the VAD
tuning in pipeline.py: needs a real conversation test, not just code review,
before treating it as final.
"""
import asyncio
import re
import time
from dataclasses import dataclass
from typing import Literal

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    LLMUpdateSettingsFrame,
    ManuallySwitchServiceFrame,
    OutputTransportMessageFrame,
    STTUpdateSettingsFrame,
    TranscriptionFrame,
    TTSUpdateSettingsFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.settings import LLMSettings, STTSettings, TTSSettings

from language_detection import detect_language_and_transcript

# Rolling cap on buffered mic audio, and the shortest turn we'll trust for a
# language flip.
MAX_BUFFER_SECONDS = 15
MIN_DETECT_SECONDS = 1.0
# A mid-call flip changes the persona, the voice and the recogniser, so it
# needs a surer detection than the 0.5 used to pick a call's starting language.
SWITCH_MIN_CONFIDENCE = 0.7
# How long an ordinary turn's transcript is held for a detection that was
# started when the visitor stopped speaking. Past this the reply goes ahead
# and a late "different language" result applies from the next turn.
DETECT_GRACE_SECS = 0.25
# The first turn, and any turn whose transcript looks off, waits for the result.
DETECT_MAX_WAIT_SECS = 2.5

Language = Literal["en", "ta"]
Topic = Literal["about-us", "admissions", "courses", "placements"]

# One voice per topic, per language. "about-us" doubles as the default/
# fallback voice (unmatched questions, and the voice a call starts on) —
# same pattern the old Deepgram design used rather than introducing a fifth
# voice. All four names below were verified live against Sarvam's real
# /text-to-speech endpoint for BOTH en-IN and ta-IN (2026-09-29) — Sarvam's
# bulbul:v3 speakers are cross-lingual identities, not English-only or
# Tamil-only, so the same four names work for both languages; only the
# language-appropriate default (priya/kavya) differs, matching what was
# already locked in per language before this change.
#
# Gender assignment matches the widget's own avatar images per topic
# (widget/index.html's topic cards: About Us and Placements show a woman,
# Courses and Admissions show a man) — corrected 2026-09-29 after the
# initial pick didn't match those images (admissions/courses had swapped
# genders from what the UI shows).
TOPIC_VOICES: dict[Language, dict[Topic, str]] = {
    "en": {
        "about-us": "ritu",
        "courses": "aayan",
        "admissions": "sunny",
        "placements": "shreya",
    },
    "ta": {
        "about-us": "ritu",
        "courses": "aayan",
        "admissions": "sunny",
        "placements": "shreya",
    },
}

# English keywords ported verbatim from the deleted rag-backend/voice_agent.py
# (git commit 848cbad) — same collisions were already debugged there (see
# its comments on "mission" vs "admission", and B.E./B.Tech backfiring into
# "courses"), no reason to rediscover those bugs.
CATEGORY_KEYWORDS_EN: dict[Topic, list[str]] = {
    "about-us": ["found", "establish", "accredit", "naac", "campus", "history", "autonomous", "vision", "mission", "agni college", "about act"],
    "courses": ["course", "program", "department", "branch", "curriculum", "semester", "syllabus"],
    "admissions": ["admission", "apply", "application", "counsel", "entrance", "eligib", "fee", "cutoff", "seat", "enroll"],
    "placements": ["placement", "recruit", "company", "companies", "package", "salary", "intern", "hire", "hiring", "offer", "drive"],
}

# Tamil keywords — new, first draft (see module docstring). Primarily Tamil
# script, with a handful of common Latin-script/Tanglish spellings since
# Sarvam's realtime STT was observed (real session, 2026-09-29) sometimes
# transliterating Tamil speech into Latin script rather than Tamil script
# (e.g. "Ethume koodaathu" for what was spoken in Tamil).
CATEGORY_KEYWORDS_TA: dict[Topic, list[str]] = {
    # "பற்றி"/"பற்றிய" ("about") deliberately excluded — it's a generic
    # connector that shows up in almost any question phrased "X பற்றி
    # சொல்லுங்க" ("tell me about X"), so it scored about-us a point on
    # EVERY topic and stole ties by being listed first — the exact same
    # collision class the English keywords' own comments warned about
    # ("mission" vs "admission"), caught live via a real test run of this
    # function before shipping it, not left for Prakash to find.
    "about-us": ["கல்லூரி", "நிறுவ", "ஸ்தாபிக்க", "அங்கீகார", "தன்னாட்சி", "வரலாறு", "காம்பஸ்"],
    "courses": ["படிப்பு", "பாடநெறி", "துறை", "பிரிவு", "பாடத்திட்டம்", "செமஸ்டர்", "கோர்ஸ்", "course", "department"],
    "admissions": ["சேர்க்கை", "விண்ணப்ப", "கலந்தாய்வு", "தகுதி", "கட்டணம்", "கட் ஆஃப்", "admission", "seat"],
    "placements": ["வேலைவாய்ப்பு", "பணியமர்த்த", "நிறுவனங்கள்", "சம்பளம்", "பயிற்சி", "placement", "salary"],
}

CATEGORY_KEYWORDS: dict[Language, dict[Topic, list[str]]] = {
    "en": CATEGORY_KEYWORDS_EN,
    "ta": CATEGORY_KEYWORDS_TA,
}


def classify_category(text: str, language: Language) -> Topic | None:
    if not text:
        return None
    lower = text.lower()
    best_key: Topic | None = None
    best_score = 0
    for key, keywords in CATEGORY_KEYWORDS[language].items():
        # Word-boundary-before-only, same fix as the old voice_agent.py: lets
        # "admission" still match "admissions"/"applying" while stopping
        # "mission" (about-us) from matching mid-word inside "admission".
        score = sum(1 for kw in keywords if re.search(r"\b" + re.escape(kw), lower))
        if score > best_score:
            best_score = score
            best_key = key
    return best_key


LANGUAGE_CODES: dict[Language, str] = {"en": "en-IN", "ta": "ta-IN"}

# Mid-call language-switch detection. Deliberately requires a nearby speak/
# switch verb, not just the bare word "tamil"/"english" — ACT's own content
# says "TNEA (Tamil Nadu Engineering Admissions)" and "medium of instruction
# is English" routinely, and a bare keyword match would misfire a switch on
# an ordinary admissions question. First-pass phrasing, needs real-call
# verification (module docstring).
_SWITCH_TO_TA_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        # Negative lookahead excludes "in Tamil Nadu" (TNEA = Tamil Nadu
        # Engineering Admissions, comes up constantly in ACT's own
        # admissions content) — caught live via a real test run before
        # shipping, same as the Tamil keyword fix above.
        r"\bin tamil\b(?!\s*nadu)",
        r"\bspeak(ing)?\s+tamil\b",
        r"\btalk(ing)?\s+(in\s+)?tamil\b",
        r"\bswitch(ed)?\s+to\s+tamil\b",
        r"\btamil\s+please\b",
        r"\btamil[\s-]*la\s+pesu",
        r"தமிழில்\s*பேசு",
        r"தமிழ்ல\s*பேசு",
        r"தமிழ்\s*வேணும்",
    ]
]
_SWITCH_TO_EN_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\bin english\b",
        r"\bspeak(ing)?\s+english\b",
        r"\btalk(ing)?\s+(in\s+)?english\b",
        r"\bswitch(ed)?\s+to\s+english\b",
        r"\benglish\s+please\b",
        r"இங்கிலீஷ்ல?\s*பேசு",
        r"ஆங்கிலத்தில்\s*பேசு",
    ]
]


def detect_language_switch(text: str) -> Language | None:
    if not text:
        return None
    if any(p.search(text) for p in _SWITCH_TO_TA_PATTERNS):
        return "ta"
    if any(p.search(text) for p in _SWITCH_TO_EN_PATTERNS):
        return "en"
    return None


_TAMIL_SCRIPT = re.compile(r"[஀-௿]")
# Function words that turn up in almost any real English sentence of a few
# words. Tamil speech pushed through the English-locked realtime STT comes out
# as transliterated Latin text ("Ethume koodaathu") with none of them.
_ENGLISH_FUNCTION_WORDS = frozenset(
    "a an the is are am was were be do does did can could will would should have has had "
    "i me my you your we our it its they them this that these those there here "
    "what which who when where why how to of in on at for from with about and or but not no "
    "yes if so any some tell want need know please".split()
)
# Below this many words a transcript carries too little to judge ("okay",
# "B.Tech CSE") — never spend a detection call on it. English needs one more:
# three-word noun-phrase answers ("computer science engineering") are common
# and contain no function word either.
_MIN_WORDS_TO_JUDGE = 3
_MIN_WORDS_TO_JUDGE_EN = 4


def _language_suspicion(text: str, session_language: Language) -> str | None:
    """Cheap, local check for "this turn may not be in the session language".

    Returns None (looks fine — the normal case, no detection call), "certain"
    (switch without asking Sarvam), or "uncertain" (worth one batch detection
    call before replying). First-pass heuristic, same caveat as the rest of
    this module: needs real-call verification.
    """
    has_tamil = bool(_TAMIL_SCRIPT.search(text))
    if session_language == "en":
        if has_tamil:
            return "certain"
        words = re.findall(r"[a-z']+", text.lower())
        if len(words) >= _MIN_WORDS_TO_JUDGE_EN and not _ENGLISH_FUNCTION_WORDS.intersection(words):
            return "uncertain"
        return None
    # Tamil session: Tamil-script output is the expected case. An all-Latin
    # transcript is either English speech or Tamil that Sarvam transliterated
    # (observed 2026-09-29), so it needs the real detector to tell apart.
    if not has_tamil and len(text.split()) >= _MIN_WORDS_TO_JUDGE:
        return "uncertain"
    return None


@dataclass
class ConversationRouterCallbacks:
    """Lets pipeline.py supply language-dependent values (persona prompt)
    without this module importing pipeline.py back (would be circular)."""

    persona_prompts: dict[Language, str]


class ConversationRouter(FrameProcessor):
    """Sits between STT and the user-turn aggregator. On each finalized user
    transcript: either handles a spoken language-switch request (updates
    STT/TTS/LLM settings live, no reconnect), or classifies the topic and
    switches to that topic's voice for the upcoming answer. Passes every
    frame through unchanged either way — this only ever adds side-effect
    frames, never consumes or blocks the transcript itself, so the
    user-aggregator downstream still sees every TranscriptionFrame exactly
    as before.

    `cloned_services_by_topic`, added 2026-09-29: maps a topic to its own
    dedicated `ClonedVoiceTTSService` instance, for topics that have a
    cloned voice configured (pipeline.py builds these; empty dict if none
    are). A topic present here gets a `ManuallySwitchServiceFrame` to make
    that instance active in the `ServiceSwitcher` pipeline.py wraps around
    it and the stock `SarvamTTSService`; a topic absent from it instead gets
    a plain voice-only `TTSUpdateSettingsFrame` targeted at `stock_tts`,
    exactly as before this feature existed. When `cloned_services_by_topic`
    is empty (the common case today — no staff recordings exist yet), this
    class's behavior is unchanged from before per-topic cloning existed:
    every topic just gets an untargeted voice update on the single `tts` in
    the pipeline."""

    def __init__(
        self,
        initial_language: Language,
        callbacks: ConversationRouterCallbacks,
        stock_tts,
        cloned_services_by_topic: dict[Topic, "FrameProcessor"] | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._language: Language = initial_language
        self._callbacks = callbacks
        self._stock_tts = stock_tts
        self._cloned_services_by_topic = cloned_services_by_topic or {}
        self._audio_buf = bytearray()
        self._audio_rate = 16000
        self._bytes_seen = 0
        # Value of _bytes_seen where this utterance began. A plain offset into
        # _audio_buf would go stale: the buffer drops audio from the front
        # once it is full, which it always is after the first few seconds.
        self._speech_from: int | None = None
        self._detect_task: asyncio.Task | None = None
        self._user_turns = 0

    @property
    def language(self) -> Language:
        return self._language

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, InputAudioRawFrame):
            # STT forwards its input audio downstream; keep the current
            # utterance's tail so the transcript handler can language-detect
            # it BEFORE the reply is generated.
            self._audio_buf.extend(frame.audio)
            self._bytes_seen += len(frame.audio)
            self._audio_rate = frame.sample_rate
            del self._audio_buf[: -MAX_BUFFER_SECONDS * self._audio_rate * 2]
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            if self._speech_from is None:
                # Half a second of lead-in, so the first word is in the clip.
                self._speech_from = self._bytes_seen - self._audio_rate
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._start_background_detection()
        elif isinstance(frame, TranscriptionFrame) and frame.text and frame.text.strip():
            await self._detect_and_switch(frame)
            await self._handle_transcript(frame.text)

        await self.push_frame(frame, direction)

    async def _detect_and_switch(self, frame: TranscriptionFrame):
        """2026-10-05: run language detection on this turn's audio BEFORE the
        transcript is released to the LLM. Previously detection ran after the
        turn (AudioBufferProcessor.on_user_turn_audio), so the reply to the
        first Tamil utterance was still generated under the English persona
        and the call only flipped on the NEXT turn. Realtime STT is also
        locked to the call language, so Tamil speech on an `en` call arrived
        as transliterated English; on a switch the batch transcript (spoken
        language, not forced) replaces it.

        2026-10-07, morning: stopped running on every turn — the batch call
        was ~0.6-0.8s sitting directly in front of the reply — and ran only
        when the transcript looked wrong for the session language
        (`_language_suspicion`).

        2026-10-07, afternoon: that text check alone missed real switches in
        both directions in scripted calls. Tamil spoken on an English call
        came back as a fluent, wrong English sentence ("How to apply for
        citizenship?"), and English spoken on a Tamil call came back spelled
        out in Tamil letters — neither looks "off". So detection is back on
        every utterance of a second or more, but started early and mostly off
        the reply path: it begins when the local VAD says the visitor stopped
        (`_start_background_detection`), while Sarvam is still finalizing the
        transcript, and an ordinary turn waits at most DETECT_GRACE_SECS for
        it. The first turn of a call and suspicious transcripts wait for the
        answer, because the widget's start-of-call snippet is often silence
        and the visitor's first sentence is then the first real evidence."""
        audio, rate = self._utterance_audio(), self._audio_rate
        self._audio_buf.clear()
        self._speech_from = None
        task, self._detect_task = self._detect_task, None
        self._user_turns += 1

        suspicion = _language_suspicion(frame.text, self._language)
        if suspicion == "certain":
            # Tamil script out of an English-locked STT: no API call needed.
            if task:
                await self.cancel_task(task)
            await self._switch_language("ta")
            return
        if task is None:
            # No local VAD in the pipeline (TURN_MODE=sarvam): only pay for a
            # detection when the transcript looks off.
            if suspicion is None or len(audio) < MIN_DETECT_SECONDS * rate * 2:
                return
            task = self.create_task(self._detect(audio, rate))

        must_wait = suspicion == "uncertain" or self._user_turns == 1
        t0 = time.monotonic()
        done, _ = await asyncio.wait(
            {task}, timeout=DETECT_MAX_WAIT_SECS if must_wait else DETECT_GRACE_SECS
        )
        if not done:
            task.add_done_callback(self._apply_late_detection)
            return
        detected, transcript = task.result()
        logger.info(
            f"[latency] language detection held the transcript {time.monotonic() - t0:.2f}s "
            f"-> {detected!r} (session {self._language!r})"
        )
        if detected and detected != self._language:
            await self._switch_language(detected)
            if transcript:
                frame.text = transcript

    def _utterance_audio(self) -> bytes:
        if self._speech_from is None:
            return bytes(self._audio_buf)
        wanted = self._bytes_seen - self._speech_from
        return bytes(self._audio_buf[-wanted:]) if wanted > 0 else b""

    def _start_background_detection(self):
        """Called when the local VAD reports the visitor stopped speaking."""
        audio, rate = self._utterance_audio(), self._audio_rate
        # Very short turns ("ok", "yes") are too ambiguous to flip a call on.
        if len(audio) < MIN_DETECT_SECONDS * rate * 2:
            return
        if self._detect_task and not self._detect_task.done():
            self._detect_task.cancel()  # superseded by a longer clip of the same utterance
        self._detect_task = self.create_task(self._detect(audio, rate))

    async def _detect(self, audio: bytes, rate: int) -> tuple[str | None, str | None]:
        try:
            return await detect_language_and_transcript(
                audio, rate, min_confidence=SWITCH_MIN_CONFIDENCE
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never let detection break the turn
            logger.warning(f"ConversationRouter: language detection failed: {e}")
            return None, None

    def _apply_late_detection(self, task: asyncio.Task):
        """A detection that finished after its turn was already answered."""
        if task.cancelled() or task.exception():
            return
        detected, _ = task.result()
        if detected and detected != self._language:
            logger.info(
                f"ConversationRouter: late detection says {detected!r} — switching for the next turn"
            )
            self.create_task(self._switch_language(detected))

    async def _activate_topic_voice(self, category: Topic):
        """Make the right TTS instance active/configured for `category`,
        under the CURRENT language. Handles both the no-clones case (a
        single stock `tts`, untargeted settings update — unchanged behavior)
        and the switcher case (multiple instances, explicit activation)."""
        cloned_service = self._cloned_services_by_topic.get(category)
        if cloned_service is not None:
            # Its voice_id and (per the live language update below) language
            # are already correct — just make it the active member.
            await self.push_frame(
                ManuallySwitchServiceFrame(service=cloned_service),
                FrameDirection.DOWNSTREAM,
            )
            logger.debug(f"ConversationRouter: topic={category!r} -> cloned voice ({self._language})")
            await self._notify_client_topic(category)
            return

        voice = TOPIC_VOICES[self._language][category]
        if self._cloned_services_by_topic:
            # A switcher exists (at least one OTHER topic is cloned) — this
            # topic isn't, so make sure the stock service is the active
            # member before/alongside updating its voice.
            await self.push_frame(
                ManuallySwitchServiceFrame(service=self._stock_tts),
                FrameDirection.DOWNSTREAM,
            )
        await self.push_frame(
            TTSUpdateSettingsFrame(
                service=self._stock_tts if self._cloned_services_by_topic else None,
                delta=TTSSettings(voice=voice),
            ),
            FrameDirection.DOWNSTREAM,
        )
        logger.debug(f"ConversationRouter: topic={category!r} -> stock voice={voice!r} ({self._language})")
        await self._notify_client_topic(category)

    async def _notify_client_topic(self, category: Topic):
        """Tells the browser which topic the upcoming reply is about, so
        widget/index.html's setActiveTopic/setSpeakingTopic can drive the
        avatar "speaking" glow — dead in voice mode until this was added
        (module import comment / raw_pcm_serializer.py's docstring explain
        why: no JSON ever reached the browser from the Sarvam pipeline
        before this). Travels through llm and tts like any other downstream
        frame (both forward frames they don't specifically handle) before
        reaching transport.output(), where RawPCMSerializer turns it into a
        JSON text WebSocket message."""
        await self.push_frame(
            OutputTransportMessageFrame(message={"type": "topic", "topic": category}),
            FrameDirection.DOWNSTREAM,
        )

    async def _handle_transcript(self, text: str):
        switch_target = detect_language_switch(text)
        if switch_target and switch_target != self._language:
            await self._switch_language(switch_target)
            return  # meta-command, not a real question — skip topic voice pick

        category = classify_category(text, self._language) or "about-us"
        await self._activate_topic_voice(category)

    async def maybe_auto_switch_language(self, detected: Language | None):
        """2026-09-29: called from pipeline.py, NOT from process_frame — the
        caller re-runs Sarvam's batch language detector (the same one
        language_detection.py already uses once, before a call even opens,
        because realtime STT can't reliably tell en from ta on its own —
        confirmed live, see that module's docstring) on each finished user
        turn's audio, via AudioBufferProcessor's on_user_turn_audio event.
        This is the automatic replacement for requiring a spoken "switch to
        Tamil" — the visitor just starts talking in the other language and
        it catches up within roughly one turn (detection + a real API call
        takes a beat; it can't be instant mid-sentence). `detected` is
        already None-if-low-confidence (language_detection.py's own
        MIN_CONFIDENCE gate), so this only switches on a real signal, not a
        coin flip."""
        if detected and detected != self._language:
            await self._switch_language(detected)

    async def _switch_language(self, target: Language):
        logger.info(f"ConversationRouter: switching language {self._language!r} -> {target!r}")
        self._language = target

        # STT is upstream of this processor in the pipeline (transport.input
        # -> stt -> [this router] -> user_aggregator -> ...), so reaching it
        # means pushing upstream; TTS and the LLM are downstream.
        await self.push_frame(
            STTUpdateSettingsFrame(delta=STTSettings(language=LANGUAGE_CODES[target])),
            FrameDirection.UPSTREAM,
        )
        # Untargeted + reach_inactive_services=True so EVERY TTS member
        # (stock and every cloned instance, active or not) picks up the new
        # language — a cloned voice is cross-lingual (module docstring in
        # cloned_voice_tts.py) so it stays valid across the switch, it just
        # needs telling which language to speak next. `voice=` here only
        # matters to the stock service (cloned instances ignore `voice`,
        # their identity is fixed per instance) — harmless no-op for them.
        await self.push_frame(
            TTSUpdateSettingsFrame(
                delta=TTSSettings(
                    language=LANGUAGE_CODES[target],
                    voice=TOPIC_VOICES[target]["about-us"],
                ),
                reach_inactive_services=bool(self._cloned_services_by_topic),
            ),
            FrameDirection.DOWNSTREAM,
        )
        # Reset to the default/about-us voice for the new language — mirrors
        # a fresh call's starting state, and matters when a switcher exists:
        # whichever topic was active before the switch stays active
        # otherwise, even though its voice (if stock) or identity (if
        # cloned) may no longer make sense as a "default."
        await self._activate_topic_voice("about-us")
        await self.push_frame(
            LLMUpdateSettingsFrame(
                delta=LLMSettings(system_instruction=self._callbacks.persona_prompts[target])
            ),
            FrameDirection.DOWNSTREAM,
        )
