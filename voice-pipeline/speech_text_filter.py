"""Last-step clean-up of reply text before it reaches TTS.

Deliberately small. Bulbul v3 already normalises numbers, dates and currency
itself (Pipecat's TTS_MODEL_CONFIGS marks bulbul:v3 as
`preprocessing_always_enabled`), so this does NOT spell out numbers — doing
it twice, or doing it in English on a Tamil reply, would make things worse.

What it does handle is what the vendor can't know:
- formatting symbols the LLM occasionally emits despite the prompt rule
  ("**" read aloud as "asterisk asterisk"),
- how this college's own abbreviations should be said (PRONUNCIATIONS below).

PRONUNCIATIONS is the place to add any name, course or acronym that is heard
being mispronounced on a real call — the entries here are a starting set, not
verified by ear.
"""
import re

# Written form -> how it should be spoken. Matched as whole tokens,
# case-sensitively, in both languages.
PRONUNCIATIONS: dict[str, str] = {
    "B.E.": "B E",
    "B.E": "B E",
    "B.Tech.": "B Tech",
    "B.Tech": "B Tech",
    "M.E.": "M E",
    "M.E": "M E",
    "M.Tech.": "M Tech",
    "M.Tech": "M Tech",
    "Ph.D.": "P H D",
    "Ph.D": "P H D",
    "CSE": "C S E",
    "ECE": "E C E",
    "EEE": "triple E",
    "AI&DS": "A I and D S",
    "TNEA": "T N E A",
    "NBA": "N B A",
    "HOD": "H O D",
    # Indus pronunciation entry: Thalambur -> தாழம்பூர். In an English sentence the voice
    # needs Latin letters, so this is a respelling (not yet judged by ear).
    "Thalambur": "Thaazhampoor",
}

_PRONUNCIATION_RE = re.compile(
    r"(?<![\w.])("
    + "|".join(re.escape(k) for k in sorted(PRONUNCIATIONS, key=len, reverse=True))
    + r")(?!\w)"
)
_MARKDOWN = re.compile(r"[*#`~]+|^\s*[-•]\s+", re.MULTILINE)
_URL_SCHEME = re.compile(r"https?://(?:www\.)?")


_TAMIL_SCRIPT = re.compile(r"[஀-௿]")
# A year standing alone: not part of a longer number, a phone number, a
# decimal or a range like 2024-25 (Bulbul already reads those as numbers).
_YEAR = re.compile(r"(?<![\d.,-])(19|20)(\d\d)(?![\d.,]|-\d)")
_TA_UNITS = ["", "ஒன்று", "இரண்டு", "மூன்று", "நான்கு", "ஐந்து", "ஆறு", "ஏழு", "எட்டு", "ஒன்பது"]
_TA_TEENS = ["பத்து", "பதினொன்று", "பன்னிரண்டு", "பதின்மூன்று", "பதினான்கு", "பதினைந்து",
             "பதினாறு", "பதினேழு", "பதினெட்டு", "பத்தொன்பது"]
_TA_TENS = ["", "", "இருபது", "முப்பது", "நாற்பது", "ஐம்பது", "அறுபது", "எழுபது", "எண்பது", "தொண்ணூறு"]
_TA_TENS_JOINING = ["", "", "இருபத்து", "முப்பத்து", "நாற்பத்து", "ஐம்பத்து", "அறுபத்து",
                    "எழுபத்து", "எண்பத்து", "தொண்ணூற்று"]


def _tamil_year(match: re.Match) -> str:
    """1900-2099 as spoken Tamil, e.g. 2001 -> இரண்டாயிரத்து ஒன்று.

    In Tamil sentences Bulbul reads a bare four-digit year digit by digit
    ("2001-ல" came out as "இரண்டு பூஜ்ஜியம் பூஜ்ஜியம் ஒன்றில்" — confirmed
    2026-10-07 by transcribing its audio back, after a visitor corrected the
    bot on a real call). Codes and phone numbers SHOULD be read digit by
    digit, so only years are spelled out."""
    century, rest = match.group(1), int(match.group(2))
    if century == "19":
        head = "ஆயிரத்து தொள்ளாயிரத்து" if rest else "ஆயிரத்து தொள்ளாயிரம்"
    else:
        head = "இரண்டாயிரத்து" if rest else "இரண்டாயிரம்"
    if rest == 0:
        return head
    if rest < 10:
        tail = _TA_UNITS[rest]
    elif rest < 20:
        tail = _TA_TEENS[rest - 10]
    elif rest % 10 == 0:
        tail = _TA_TENS[rest // 10]
    else:
        tail = f"{_TA_TENS_JOINING[rest // 10]} {_TA_UNITS[rest % 10]}"
    return f"{head} {tail}"


async def to_speech(text: str, aggregation_type=None) -> str:
    """Pipecat TTS `text_transforms` callable. Registered as a transform, not
    a `text_filters` entry, on purpose: a filter's output also becomes the
    assistant turn in the conversation context, so "C S E" ended up in the
    saved transcript and in the history sent back to the LLM (seen in
    2026-10-07 test transcripts). A transform only changes what is sent to
    the voice."""
    text = _MARKDOWN.sub(" ", text)
    text = _URL_SCHEME.sub("", text)
    text = _PRONUNCIATION_RE.sub(lambda m: PRONUNCIATIONS[m.group(1)], text)
    if _TAMIL_SCRIPT.search(text):
        text = _YEAR.sub(_tamil_year, text)
    return re.sub(r"[ \t]{2,}", " ", text)
