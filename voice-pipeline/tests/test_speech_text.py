"""Acronyms reach the voice joined up, not as spaced letters (spaced letters were spoken with gaps)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from speech_text_filter import to_speech


def say(text):
    return asyncio.run(to_speech(text))


def test_degree_names_and_acronyms_have_no_spaced_letters():
    assert say("We offer B.E. and M.E.") == "We offer BE and ME."
    assert say("B. E. and M. E. programs") == "BE and ME programs"
    assert say("B.Tech, M.Tech and Ph.D.") == "BTech, MTech and PhD."
    for acronym in ("CSE", "ECE", "NBA", "TNEA"):
        assert say(f"Apply for {acronym} today") == f"Apply for {acronym} today"   # left as written
    assert say("AI&DS and EEE") == "AI and DS and triple E"


def test_a_plain_word_me_is_not_touched():
    assert say("Please help me, ME is a degree") == "Please help me, ME is a degree"
