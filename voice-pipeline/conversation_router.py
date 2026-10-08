"""Mid-call spoken language switching, reply-style selection, and topic
classification for the UI.

**2026-10-09 changes (P0 fixes from the Zimi audit):**
- Nothing here waits for language detection any more. The transcript goes
  to the LLM as soon as it is final; the batch detector runs in the
  background and a different result switches the call from the NEXT turn.
- Each turn is classified locally (style_classifier.py, no network) as
  en / ta / ta_en and the persona + TTS language follow it.
- One voice for the whole call. Topic classification still runs and still
  tells the browser which topic tile to light, but it no longer changes the
  speaker, and a language switch no longer resets the voice.
The history below describes the per-topic voice design this replaced.

Per-topic TTS voice switching + mid-call spoken language switching, both
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
import os
import re
from dataclasses import dataclass
from typing import Literal

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    LLMUpdateSettingsFrame,
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
from style_classifier import detect_style

# Rolling cap on buffered mic audio, and the shortest turn we'll trust for a
# language flip.
MAX_BUFFER_SECONDS = 15
MIN_DETECT_SECONDS = 1.0
# A mid-call flip changes the persona, the voice and the recogniser, so it
# needs a surer detection than the 0.5 used to pick a call's starting language.
SWITCH_MIN_CONFIDENCE = 0.7

# Language = what the call's speech recogniser is set to. Style = how the next
# reply is worded: "ta_en" is Tamil mixed with English, written in English
# letters, and is neither of the other two.
Language = Literal["en", "ta"]
Style = Literal["en", "ta", "ta_en"]
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

# TTS language per reply style. Tanglish is Latin-letter text that Tamil-locale
# TTS has to read; whether en-IN or ta-IN says it better is a question for a
# listening test, so it is switchable without a code change.
STYLE_TTS_LANGUAGE: dict[Style, str] = {
    "en": "en-IN",
    "ta": "ta-IN",
    "ta_en": os.environ.get("TA_EN_TTS_LANGUAGE", "ta-IN"),
}

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
    """Lets pipeline.py supply style-dependent values (persona prompt)
    without this module importing pipeline.py back (would be circular)."""

    persona_prompts: dict[Style, str]


class ConversationRouter(FrameProcessor):
    """Sits between STT and the user-turn aggregator. On each finalized user
    transcript it (1) classifies the reply style locally and, if it changed,
    updates the LLM persona and TTS language live, (2) handles a spoken
    language-switch request, and (3) classifies the topic so the browser can
    light the right tile. It never waits for anything and never blocks the
    transcript: every frame is passed through, so the user-aggregator
    downstream sees every TranscriptionFrame exactly as before.

    The speaker's voice is fixed for the whole call (pipeline.py builds one TTS
    service); this class never changes it."""

    def __init__(
        self,
        initial_language: Language,
        callbacks: ConversationRouterCallbacks,
        trace=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._language: Language = initial_language
        self._style: Style = initial_language
        self._tts_language = STYLE_TTS_LANGUAGE[self._style]
        self._callbacks = callbacks
        self._trace = trace
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
            await self._apply_style(frame.text)
            await self._handle_transcript(frame.text)

        await self.push_frame(frame, direction)

    async def _detect_and_switch(self, frame: TranscriptionFrame):
        """Language detection for this turn, WITHOUT holding the transcript.

        History: 2026-10-05 this ran before the transcript reached the LLM so
        the reply to a first Tamil sentence was already in Tamil; 2026-10-07 it
        waited up to 0.25 s for an ordinary turn and up to 2.5 s for the first
        turn or an odd-looking one. 2026-10-09: no waiting at all. The first
        sentence of a call is exactly where that 2.5 s was being paid, and the
        visitor feels every second of it.

        What is still instant: a Tamil-script transcript on an English call is
        switched on the spot (a local check, no API call). What is not: the
        batch detector started when the local VAD said the visitor stopped
        (`_start_background_detection`) runs on its own and, if it disagrees
        with the call's language, switches it from the next turn
        (`_apply_late_detection`).

        Known cost: realtime STT is locked to the call language, so Tamil spoken
        on an English-start call can arrive as a fluent but wrong English
        sentence, which no text check can spot. That first reply is then
        generated from the wrong text, and the call corrects itself only on the
        following turn."""
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
        task.add_done_callback(self._apply_late_detection)

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

    async def _apply_style(self, text: str):
        """Pick this turn's reply style from the transcript, locally and
        instantly, and push the persona / TTS language if it changed."""
        previous = self._style
        evidence = detect_style(text)
        style: Style = (evidence or previous).lower()  # no evidence: keep the call's style
        if self._trace is not None:
            self._trace.begin_turn(
                transcript=text,
                detected_style=evidence or "none (kept previous)",
                reply_style=style.upper(),
                stt_language=LANGUAGE_CODES[self._language],
            )
        if style != previous:
            logger.info(f"ConversationRouter: reply style {previous!r} -> {style!r} ({text[:60]!r})")
            await self._set_style(style)

    async def _set_style(self, style: Style):
        self._style = style
        await self.push_frame(
            LLMUpdateSettingsFrame(
                delta=LLMSettings(system_instruction=self._callbacks.persona_prompts[style])
            ),
            FrameDirection.DOWNSTREAM,
        )
        tts_language = STYLE_TTS_LANGUAGE[style]
        if tts_language != self._tts_language:
            self._tts_language = tts_language
            # Language only: the voice is fixed for the call and is never in
            # this delta.
            await self.push_frame(
                TTSUpdateSettingsFrame(delta=TTSSettings(language=tts_language)),
                FrameDirection.DOWNSTREAM,
            )

    async def _handle_transcript(self, text: str):
        switch_target = detect_language_switch(text)
        if switch_target and switch_target != self._language:
            await self._switch_language(switch_target)
            return  # meta-command, not a real question - skip topic pick

        # The topic only tells the browser which tile to light; it does not
        # touch the voice. A stray "I" / "okay" has no topic: leave the tile.
        category = classify_category(text, self._language)
        if category is not None:
            await self._notify_client_topic(category)

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
        # The reply style follows the language the call was just found to be
        # in: Tamil from an English style, English from either Tamil style.
        # A Tanglish call that is detected as Tamil stays Tanglish.
        if target == "ta" and self._style == "en":
            await self._set_style("ta")
        elif target == "en" and self._style != "en":
            await self._set_style("en")
        # The speaker's voice is NOT touched here: one voice per call.
