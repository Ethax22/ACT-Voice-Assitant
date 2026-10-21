"""Keep spelled-out letters such as "B. E." or "M. E." inside one sentence for the voice.

Pipecat sends the voice one sentence at a time and asks the sentencex library where a sentence
ends, passing the call's language. For Tamil, sentencex ends a sentence after any lone capital
letter with a full stop, so a reply like "B. E. CSE course-ல ..." (or "B.E. CSE ...") reached Cartesia as three
pieces: "B.", "E.", "CSE course-ல ...". Each lone letter was then spoken as a sentence of its
own: stressed, with a falling pitch and a pause of about 0.4 s after it (Prakash's outbound test
call phone-test-5eb88102..., 2026-10-21; the VM log shows "Generating TTS [B.]" then "[E.]").
English is not affected (sentencex does not split there), which is why only Tamil replies
sounded wrong. The speech filter cannot join the letters back: it only ever sees "B." alone.

install() swaps Pipecat's sentence-end check (as used by its text aggregator, which every TTS
service here goes through) for one that skips a boundary sitting right after a lone Latin letter
and looks for the next real one. "Ph.D." and other dotted abbreviations are left to sentencex.
"""
import re

from pipecat.utils import string as _pipecat_string
from pipecat.utils.text import simple_text_aggregator as _simple_aggregator

# Single Latin letters with full stops at the end ("B.", "B.E.", "M. E" -> "E."), not the end of a
# longer word or of "Ph.D.".
_LONE_LETTER_END = re.compile(r"(?:^|[^\w.])(?:[A-Za-z]\. ?)*[A-Za-z]\.$")
_original = _pipecat_string.match_endofsentence


def match_endofsentence(text: str, *, language: str | None = None) -> int:
    """Pipecat's check, but never ending a sentence right after a lone letter ("B." in "B. E.")."""
    offset = 0
    while offset < len(text):
        end = _original(text[offset:], language=language)
        if not end:
            return 0
        stop = offset + end
        if not _LONE_LETTER_END.search(text[:stop].rstrip()):
            return stop
        offset = stop  # an initial, not a sentence end: look further on
    return 0


def install() -> None:
    """Make Pipecat's text aggregator use the check above. Safe to call more than once."""
    _simple_aggregator.match_endofsentence = match_endofsentence
