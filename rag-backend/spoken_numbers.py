"""Numbers the caller SAYS, turned into digits so the code can find them.

Speech recognition in English mode writes numbers as words: a register number came back as
"three one two eight two three one zero four double zero four" and a date of birth as "twelve
April two thousand five". The checks that look for a register number, a date or a phone number
(forcing the records tool, lead capture, the guard in the phone agent) only saw digits, so a
correct call failed (2026-10-20).

`normalize_spoken_numbers` is for DETECTION only: never store its output as the transcript.
A copy of this file lives in rag-backend/ and in C:\\ACT-Calling-Agent\\phone-agent/; keep them
the same.
"""
import re

_UNITS = {"zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
          "seven": 7, "eight": 8, "nine": 9}
_TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
          "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
             "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12,
             "thirteenth": 13, "fourteenth": 14, "fifteenth": 15, "sixteenth": 16,
             "seventeenth": 17, "eighteenth": 18, "nineteenth": 19, "twentieth": 20,
             "thirtieth": 30}
_REPEAT = {"double": 2, "triple": 3}
_MULT = {"hundred": 100, "thousand": 1000}
_WORDS = sorted({*_UNITS, *_TEENS, *_TENS, *_ORDINALS, *_MULT, *_REPEAT}, key=len, reverse=True)
_ONE = r"(?:" + "|".join(_WORDS) + r")"
# a run of number words, which may be separated by spaces, commas and a joining "and"
_RUN = re.compile(r"\b" + _ONE + r"(?:(?:[\s,]+(?:and[\s,]+)?)" + _ONE + r")*\b", re.I)


def _digit_string(run: list[str]) -> str | None:
    """A run read digit by digit ("three one two ... double zero four"), or None."""
    out, i = [], 0
    while i < len(run):
        t = run[i]
        if t in _REPEAT and i + 1 < len(run) and run[i + 1] in _UNITS:
            out.append(str(_UNITS[run[i + 1]]) * _REPEAT[t])
            i += 2
        elif t in _UNITS:
            out.append(str(_UNITS[t]))
            i += 1
        else:
            return None
    return "".join(out)


def _cardinal(run: list[str]) -> int:
    """"two thousand five" -> 2005, "twelve" -> 12, "nineteen ninety nine" -> 1999."""
    vals = [t for t in run if t != "and"]
    if len(vals) >= 2 and vals[0] == "nineteen" and (vals[1] in _TENS or vals[1] in _UNITS):
        return 1900 + sum(_TENS.get(t, 0) + _UNITS.get(t, 0) for t in vals[1:])  # a year: nineteen ninety nine
    total = current = 0
    for t in vals:
        if t in _MULT:
            if t == "hundred":
                current = (current or 1) * 100
            else:
                total += (current or 1) * 1000
                current = 0
        else:
            current += _UNITS.get(t, 0) + _TEENS.get(t, 0) + _TENS.get(t, 0) + _ORDINALS.get(t, 0)
    return total + current


def _replace(match: re.Match) -> str:
    run = re.findall(r"[a-z]+", match.group(0).lower())
    digits = _digit_string(run) if len(run) >= 4 else None
    return digits if digits else str(_cardinal(run))


def normalize_spoken_numbers(text: str) -> str:
    """`text` with each run of number words replaced by digits ("1 January 2007"). Everything
    else, including digits already there ("3rd", "12-04-2005"), is left exactly as it was."""
    return _RUN.sub(_replace, text)
