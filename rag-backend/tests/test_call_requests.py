"""Transfer and hang-up requests in Tamil and Tanglish, and which phone number a lead is saved with
(the visitor's own words win; a wrong-length number saves nothing). Sentences from Prakash's
2026-10-22 test calls."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SARVAM_API_KEY", "test")          # same set-up as test_tool_passthrough.py
os.environ.setdefault("RAG_BACKEND_AUTH_KEY", "")
import main as M  # noqa: E402

TOOLS = [{"type": "function", "function": {"name": n}} for n in ("transfer_call", "end_call")]


def _turn(*pairs):
    return [{"role": r, "content": c} for r, c in pairs]


def test_tamil_transfer_request_forces_the_tool():
    msgs = _turn(("assistant", "உங்க name அப்புறம் phone number சொல்ல முடியுமா?"),
                 ("user", "என்னோட காலு டைரக்டா அட்மிஷனுக்கு டிரான்ஸ்ஃபர் பண்ண முடியுமா?"))
    assert M._forced_tool_choice(msgs, None, "unknown", TOOLS, phone=True) == \
        {"tool_choice": {"type": "function", "function": {"name": "transfer_call"}}}
    for said in ("ரிசப்ஷன் கிட்ட பேசணும்", "admission team-க்கு connect pannunga", "Can you transfer me to reception?"):
        assert M._wants_person(said), said
    assert not M._wants_person("எனக்கு transfer certificate எப்போ கிடைக்கும்?")   # a document, not a call


def test_hang_up_request_forces_end_call():
    for said in ("ஓகே கால் வச்சிருங்க.", "சரி, கால் வச்சிடுங்க", "You can hang up the call.", "ok call vachirunga"):
        msgs = _turn(("assistant", "Anything else?"), ("user", said))
        assert M._forced_tool_choice(msgs, None, "unknown", TOOLS, phone=True) == \
            {"tool_choice": {"type": "function", "function": {"name": "end_call"}}}, said
    assert not M._wants_hang_up("Don't hang up, transfer me to admissions")       # a person wins


def test_wrong_length_number_is_never_saved():
    msgs = _turn(("assistant", "உங்க பேரு அப்புறம் phone number சொல்லுங்க"),
                 ("user", "என்னோட பேர் பிரகாஷ், ஃபோன் நம்பர் வந்து 735801190."))
    assert M._invalid_lead_number(msgs, "unknown") == "735801190"
    assert not M._should_force_lead_capture(msgs, None, "unknown", confirm_first=True)   # phone
    assert not M._should_force_lead_capture(msgs, None, None)                            # website
    assert M._forced_tool_choice(msgs, None, "unknown", TOOLS, phone=True) == {"tool_choice": "none"}
    assert M._lead_phone(msgs, "7358011900") is None          # the model's "fixed" number is not used
    assert M._invalid_lead_number(_turn(("assistant", "What is your register number?"),
                                        ("user", "312823104")), "unknown") is None


def test_the_visitors_own_number_is_saved():
    msgs = _turn(("assistant", "Could you say all ten digits again?"), ("user", "My number is 7358011190."))
    assert M._lead_phone(msgs, "seven three five, eight zero, one one nine zero") == "7358011190"
    readback = _turn(("assistant", "Is it 7 3 5 8 0 1 1 1 9 0?"), ("user", "ஆமா"))
    assert M._lead_phone(readback, "") == "7358011190"
    assert M._lead_phone(_turn(("assistant", "Call 9840012345 for admissions."), ("user", "thanks")), "") is None


def test_interest_gets_one_ask_for_details():
    assert M._should_ask_for_lead(_turn(("user", "I'm really interested in CSE. Tell me more.")), None, None)
    assert M._should_ask_for_lead(_turn(("user", "எனக்கு வந்து சிஎஸ்சியில் இன்ட்ரெஸ்ட் இருக்கு.")), None, None)
    assert not M._should_ask_for_lead(_turn(("user", "I'm not interested, just checking.")), None, None)
    asked = _turn(("user", "How do I apply?"), ("assistant", "Could you share your name and phone number?"),
                  ("user", "I'm interested in ECE too."))
    assert not M._should_ask_for_lead(asked, None, None)                       # asked once already
    assert not M._should_ask_for_lead(_turn(("user", "I'm interested")), None, "student")
