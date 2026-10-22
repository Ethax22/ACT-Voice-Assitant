"""Last-step clean-up of reply text before it reaches TTS.

Numbers are spelled out in English words here, in both languages (2026-10-20):
Cartesia read raw digits in Tamil sentences digit by digit ("2024" -> "இரண்டு
பூஜ்ஜியம் இரண்டு நாலு") and a year range like "2024-25" was left untouched. So
"2024" is sent as "twenty twenty-four", "2024-25" as "twenty twenty-four to
twenty-five", "3.9" as "three point nine", "88%" as "eighty-eight percent".
Phone numbers, leading-zero codes and plain 4+ digit codes (1316) are left as
written so they are still read digit by digit.

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
    # Acronyms are written WITHOUT spaces. Spaced letters ("C S E", "B E") were spoken as separate
    # letters with a gap between each (Prakash, 2026-10-20). Measured with the Cartesia voice
    # on the same sentence: spaced "N B A" took 440 ms longer than "NBA", spaced "T N E A" 560 ms
    # longer than "TNEA"; and a recogniser still hears "NBA", "TNEA", "CSE", "ECE" as letters.
    # So CSE, ECE, NBA and TNEA are left as written (no entry here), and the dotted or spaced forms
    # the model sometimes writes are joined up:
    "B.E.": "BE",
    "B.E": "BE",
    "B. E.": "BE",
    "B. E": "BE",
    "M.E.": "ME",
    "M.E": "ME",
    "M. E.": "ME",
    "M. E": "ME",
    "B.Tech.": "BTech",
    "B.Tech": "BTech",
    "B. Tech.": "BTech",
    "B. Tech": "BTech",
    "M.Tech.": "MTech",
    "M.Tech": "MTech",
    "M. Tech.": "MTech",
    "M. Tech": "MTech",
    "Ph.D.": "PhD",
    "Ph.D": "PhD",
    "EEE": "triple E",
    "AI&DS": "AIDS",  # Prakash, 2026-10-22: said as one word, not "AI and DS"
    "AI & DS": "AIDS",
    "HOD": "H O D",  # bare "HOD" was heard back as a word
    # Indus pronunciation entry: Thalambur -> தாழம்பூர். In an English sentence the voice
    # needs Latin letters, so this is a respelling (not yet judged by ear).
    "Thalambur": "Thaazhampoor",
    # The college's name: stress on the first syllable, short "a" as in "up" (Prakash picked
    # option H of 10 by ear, 2026-10-22; the voice said "Ag-NEE"). Cartesia reads <<...>> as IPA.
    "Agni": "<<ˈ|ʌ|g|n|i>>",
    "AGNI": "<<ˈ|ʌ|g|n|i>>",
    # Words built on the name, picked by ear on 2026-10-22: the students (option C) and the
    # event names split as "Agni ..." with the same Agni (option B; the capitals of
    # AGNIHACKATHON were otherwise at risk of being spelled out).
    "Agnians": "<<ˈ|ʌ|g|n|i|ə|n|z>>",
    "Agnian": "<<ˈ|ʌ|g|n|i|ə|n>>",
    "Agnifest": "<<ˈ|ʌ|g|n|i>> Fest",
    "Agniverse": "<<ˈ|ʌ|g|n|i>> Verse",
    "AGNIHACKATHON": "<<ˈ|ʌ|g|n|i>> Hackathon",
}

# Words that need a different spelling in a Tamil sentence than in an English one, picked by ear on
# 2026-10-22/23. The voice turned the "u as in up" vowel into the right அ in Tamil but into "Ugh" in
# English ("Ugnishwar"), so English uses an open "a" as in "father" (round 2, option 1), and
# "Garuda" was right in Tamil as written but wrong in English. A sentence with Tamil letters in it
# counts as Tamil (the filter sees text, not the call's language).
BY_LANGUAGE: dict[str, dict[str, str]] = {
    "ta": {"Agnishwar": "<<ˈ|ʌ|g|n|i|ʃ|w|ə|r>>"},
    "en": {"Agnishwar": "<<ˈ|a|g|n|i|ʃ|w|ə|r>>", "Garuda": "<<ˈ|g|a|r|u|d|ɑː>>"},
}
_TAMIL_LETTERS = re.compile(r"[\u0B80-\u0BFF]")
_BY_LANGUAGE_RE = {
    lang: re.compile(r"(?<![\w.])(" + "|".join(re.escape(k) for k in sorted(words, key=len, reverse=True)) + r")(?!\w)")
    for lang, words in BY_LANGUAGE.items()
}

_PRONUNCIATION_RE = re.compile(
    r"(?<![\w.])("
    + "|".join(re.escape(k) for k in sorted(PRONUNCIATIONS, key=len, reverse=True))
    + r")(?!\w)"
)
# The college's internal exams. erp-lookup writes "Internal Assessment Test 1", but the model
# rewords it as "IA Test-1", "IA TEST-1" or "IAT-1", and the voice blurred "IA" (Prakash,
# 2026-10-21: "doesn't tell IAT-1 clearly"). Spoken in full, as he chose.
_EXAM_NUMBERED = re.compile(r"(?<![\w.])I\.?\s?A\.?\s?T(?:EST|est)?\s*[-–]?\s*(\d+)\b")
_EXAM_BARE = re.compile(r"(?<![\w.])IAT(?![\w-])")
_MARKDOWN = re.compile(r"[*#`~]+|^\s*[-•]\s+", re.MULTILINE)
_URL_SCHEME = re.compile(r"https?://(?:www\.)?")


_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
         "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _below_100(n: int) -> str:
    if n < 20:
        return _ONES[n]
    return _TENS[n // 10] + (f"-{_ONES[n % 10]}" if n % 10 else "")


def _int_words(n: int) -> str:
    parts = []
    if n >= 1000:
        parts.append(f"{_int_words(n // 1000)} thousand")
        n %= 1000
    if n >= 100:
        parts.append(f"{_ONES[n // 100]} hundred")
        n %= 100
    if n:
        parts.append(_below_100(n))
    return " ".join(parts) or "zero"


def _year_words(n: int) -> str:
    """1900-2099 the way people say a year: 2024 -> "twenty twenty-four"."""
    if n < 2000:
        rest = n % 100
        return "nineteen " + (_below_100(rest) if rest else "hundred")
    rest = n - 2000
    if rest == 0:
        return "two thousand"
    if rest < 10:
        return f"two thousand {_ONES[rest]}"
    return f"twenty {_below_100(rest)}" if rest >= 20 else f"twenty {_ONES[rest]}"


# One number, or a range of two ("2024-25", "175-185"), with an optional percent sign.
# Not matched when glued to letters or to a longer number: "B2B", "1st", phone numbers.
_N = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_NUMBER_RE = re.compile(
    rf"(?<![A-Za-z\d.,/])({_N})(?:-({_N}))?(%?)(?!\d|[A-Za-z/]|[.,]\d)"
)


def _one_number(text: str, is_second_of_year_range: bool = False) -> str | None:
    """Words for one numeric token, or None when it should be left as written
    (phone numbers, codes, leading zeros, plain numbers of 4+ digits that are not a year)."""
    if "," in text:
        whole, _, frac = text.replace(",", "").partition(".")
    else:
        whole, _, frac = text.partition(".")
    if len(whole) > 1 and whole.startswith("0"):
        return None
    n = int(whole)
    if frac:
        spoken = f"{_int_words(n)} point " + " ".join(_ONES[int(d)] for d in frac)
    elif "," not in text and len(whole) == 4:
        if not 1900 <= n <= 2099:
            return None
        spoken = _year_words(n)
    elif "," not in text and len(whole) >= 5:
        return None
    else:
        spoken = _int_words(n)
    return spoken


def _numbers(match: re.Match) -> str:
    first, second, percent = match.group(1), match.group(2), match.group(3)
    a = _one_number(first)
    b = _one_number(second) if second else ""
    if a is None or b is None:
        return match.group(0)
    out = a + (f" to {b}" if second else "")
    return out + (" percent" if percent else "")


async def to_speech(text: str, aggregation_type=None) -> str:
    """Pipecat TTS `text_transforms` callable. Registered as a transform, not
    a `text_filters` entry, on purpose: a filter's output also becomes the
    assistant turn in the conversation context, so "C S E" ended up in the
    saved transcript and in the history sent back to the LLM (seen in
    2026-10-07 test transcripts). A transform only changes what is sent to
    the voice."""
    return speech_text(text)


def speech_text(text: str) -> str:
    """What to_speech sends to the voice, callable without await (greeting_cache keys its clips
    on it, so a pronunciation change re-records the greetings)."""
    text = _MARKDOWN.sub(" ", text)
    text = _URL_SCHEME.sub("", text)
    ended_with_period = text.rstrip().endswith(".")
    text = _PRONUNCIATION_RE.sub(lambda m: PRONUNCIATIONS[m.group(1)], text)
    lang = "ta" if _TAMIL_LETTERS.search(text) else "en"
    text = _BY_LANGUAGE_RE[lang].sub(lambda m: BY_LANGUAGE[lang][m.group(1)], text)
    if ended_with_period and not text.rstrip().endswith("."):
        text = text.rstrip() + "."  # "...and M.E." ends the sentence: the entry for "M.E." swallowed its dot
    text = _EXAM_NUMBERED.sub(r"Internal Assessment Test \1", text)
    text = _EXAM_BARE.sub("Internal Assessment Test", text)
    text = _NUMBER_RE.sub(_numbers, text)
    return re.sub(r"[ \t]{2,}", " ", text)
