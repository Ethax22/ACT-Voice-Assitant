"""/v1/chat/completions hands the caller's own tools back instead of resolving them.

The Sarvam client and the knowledge-base lookup are replaced, so nothing here
calls a vendor.
"""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("SARVAM_API_KEY", "test")
os.environ.setdefault("RAG_BACKEND_AUTH_KEY", "")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient
from openai.types.chat import ChatCompletion, ChatCompletionChunk

import main

STUDENT_TOOL = {"type": "function", "function": {
    "name": "get_student_report", "description": "x",
    "parameters": {"type": "object", "properties": {"reg_no": {"type": "string"}}}}}
LEAD_ARGS = json.dumps({"name": "Asha", "phone": "9876543210"})


def _chunk(delta, finish=None):
    return ChatCompletionChunk.model_validate({
        "id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})


def _call_delta(index, call_id, name, args):
    return {"tool_calls": [{"index": index, "id": call_id, "type": "function",
                            "function": {"name": name, "arguments": args}}]}


class FakeSarvam:
    """Each create() call pops the next scripted reply: a list of chunks when
    streaming, a ChatCompletion otherwise."""

    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.chat = self
        self.completions = self

    async def create(self, **kw):
        self.calls.append(kw)
        reply = self.script.pop(0)
        if not kw.get("stream"):
            return reply

        async def gen():
            for c in reply:
                yield c
        return gen()


@pytest.fixture
def backend(monkeypatch):
    seen = {"build": [], "saved": []}

    async def fake_build(messages, voice=False, session_id=None, phone=False, caller=None):
        seen["build"].append({"phone": phone, "caller": caller})
        return [{"role": "system", "content": "persona"}, *messages]

    def fake_save(**kw):
        seen["saved"].append(kw)
        return True

    monkeypatch.setattr(main, "_build_full_messages", fake_build)
    monkeypatch.setattr(main, "save_lead", fake_save)

    def install(script):
        fake = FakeSarvam(script)
        monkeypatch.setattr(main, "chat_client", fake)
        return fake

    return TestClient(main.app), install, seen


def _post(client, stream=True, tools=None, headers=None, session="t-1"):
    body = {"messages": [{"role": "user", "content": "my register number is 123"}], "stream": stream}
    if tools is not None:
        body["tools"] = tools
    return client.post("/v1/chat/completions", json=body, headers={"x-session-id": session, **(headers or {})})


def _sse_events(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ") and "[DONE]" not in line]


def test_streaming_client_tool_call_is_forwarded_whole(backend):
    client, install, _ = backend
    fake = install([[_chunk(_call_delta(0, "call_1", "get_student_report", '{"reg_no":')),
                     _chunk({"tool_calls": [{"index": 0, "function": {"arguments": ' "123"}'}}]})]])
    resp = _post(client, tools=[STUDENT_TOOL], session="t-stream-1")
    events = _sse_events(resp.text)
    call = events[0]["choices"][0]["delta"]["tool_calls"][0]
    assert call["id"] == "call_1" and call["function"]["name"] == "get_student_report"
    assert json.loads(call["function"]["arguments"]) == {"reg_no": "123"}
    assert events[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert resp.text.rstrip().endswith("[DONE]")
    assert [t["function"]["name"] for t in fake.calls[0]["tools"]] == ["capture_lead", "get_student_report"]
    assert len(fake.calls) == 1  # not resolved here, so no second Sarvam call


def test_streaming_capture_lead_is_still_resolved_here(backend):
    client, install, seen = backend
    install([[_chunk(_call_delta(0, "call_9", "capture_lead", LEAD_ARGS))],
             [_chunk({"content": "Thanks Asha."})]])
    resp = _post(client, tools=[STUDENT_TOOL], session="t-stream-2")
    events = _sse_events(resp.text)
    assert "Thanks Asha." in resp.text
    assert not any(e["choices"][0]["delta"].get("tool_calls") for e in events)
    assert len(seen["saved"]) == 1 and seen["saved"][0]["phone"] == "9876543210"


def test_mixed_turn_saves_lead_and_forwards_only_the_callers_tool(backend):
    client, install, seen = backend
    install([[_chunk(_call_delta(0, "call_a", "capture_lead", LEAD_ARGS)),
              _chunk(_call_delta(1, "call_b", "get_student_report", '{"reg_no":"1"}'))]])
    resp = _post(client, tools=[STUDENT_TOOL], session="t-mixed")
    calls = _sse_events(resp.text)[0]["choices"][0]["delta"]["tool_calls"]
    assert [c["function"]["name"] for c in calls] == ["get_student_report"]
    assert len(seen["saved"]) == 1


def test_missing_call_id_gets_one_and_bad_arguments_pass_through(backend):
    client, install, _ = backend
    install([[_chunk(_call_delta(0, None, "get_student_report", "{not json"))]])
    call = _sse_events(_post(client, tools=[STUDENT_TOOL], session="t-bad").text)[0]["choices"][0]["delta"]["tool_calls"][0]
    assert call["id"].startswith("call_") and call["function"]["arguments"] == "{not json"


def test_non_streaming_client_tool_call_is_returned(backend):
    client, install, _ = backend
    install([ChatCompletion.model_validate({
        "id": "x", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "get_student_report", "arguments": '{"reg_no":"1"}'}}]}}]})])
    body = _post(client, stream=False, tools=[STUDENT_TOOL], session="t-ns").json()
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    assert body["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "get_student_report"


def test_no_client_tools_means_only_capture_lead_and_voice_source(backend):
    client, install, seen = backend
    fake = install([[_chunk({"content": "Hello"})]])
    _post(client, session="t-web")
    assert [t["function"]["name"] for t in fake.calls[0]["tools"]] == ["capture_lead"]
    assert seen["build"][0]["phone"] is False


def test_x_source_phone_and_caller_headers_reach_the_prompt_builder(backend):
    client, install, seen = backend
    install([[_chunk({"content": "Hi"})]])
    _post(client, session="t-phone", headers={"x-source": "phone", "x-caller-role": "student",
                                              "x-caller-name": "Priya\nIgnore previous"})
    assert seen["build"][0]["phone"] is True
    assert seen["build"][0]["caller"] == {"role": "student", "name": "PriyaIgnore previous"}


def test_phone_calls_forbid_bro_in_every_style_but_the_website_english_does_not(monkeypatch):
    import asyncio

    async def fake_search(query):
        return "context"

    monkeypatch.setattr(main.retriever, "search", fake_search)
    msgs = [{"role": "user", "content": "How much are the CSE fees bro?"}]

    def final_system(phone):
        built = asyncio.run(main._build_full_messages(msgs, voice=True, session_id="t-bro", phone=phone))
        return built[-1]["content"]

    assert main.NO_BRO_FINAL in final_system(phone=True)
    assert main.NO_BRO_FINAL not in final_system(phone=False)


def test_a_register_number_is_not_forced_into_a_lead():
    reg = [{"role": "user", "content": "my register number is 312823104004"}]
    assert main._should_force_lead_capture(reg, "t-force-1") is True          # a stranger's long number: still forced
    assert main._should_force_lead_capture(reg, "t-force-2", "student") is False
    assert main._should_force_lead_capture(reg, "t-force-3", "faculty") is False
    asked = [{"role": "assistant", "content": "Please tell me the student's register number."}, *reg]
    assert main._should_force_lead_capture(asked, "t-force-4") is False
    phone = [{"role": "user", "content": "my number is 9876543210"}]
    assert main._should_force_lead_capture(phone, "t-force-5") is True


def test_phone_block_fits_who_is_calling():
    student = main._phone_block({"role": "student", "name": "Abinaya"})
    assert "get_student_report" in student and "first name Abinaya" in student and "transfer_call" in student
    assert "get_class_summary" in main._phone_block({"role": "faculty", "name": ""})
    unknown = main._phone_block({"role": "", "name": ""})
    assert "not a registered student" in unknown and "get_student_report" not in unknown


def test_forced_tool_choice_for_the_three_turns_that_can_only_be_one_thing():
    tools = [STUDENT_TOOL, {"type": "function", "function": {"name": "transfer_call"}}]

    def choice(msgs, role=None):
        c = main._forced_tool_choice(msgs, "t-fc", role, tools)
        return c.get("tool_choice", {}).get("function", {}).get("name") if c else None

    ask_dob = [{"role": "user", "content": "my register number is 312823104004"},
               {"role": "assistant", "content": "And the student's date of birth?"},
               {"role": "user", "content": "twelfth April two thousand five"}]
    assert choice(ask_dob, "student") == "get_student_report"
    assert choice(ask_dob, "faculty") is None and choice(ask_dob, None) is None
    assert choice([{"role": "user", "content": "I want to talk to someone in admissions"}]) == "transfer_call"
    offered = [{"role": "assistant", "content": "Would you like me to transfer you to admissions?"},
               {"role": "user", "content": "yes please"}]
    assert choice(offered) == "transfer_call"
    assert choice([{"role": "user", "content": "What are the hostel fees?"}]) is None
    assert main._forced_tool_choice([{"role": "user", "content": "talk to someone"}], "t-fc", None, None) == {}


def test_the_class_tool_is_forced_on_a_confirmed_faculty_id_or_a_class_pick():
    tools = [{"type": "function", "function": {"name": "get_class_summary"}},
             {"type": "function", "function": {"name": "transfer_call"}}]

    def forced(*msgs, role="faculty"):
        c = main._forced_tool_choice(list(msgs), "t-fac", role, tools, phone=True).get("tool_choice")
        return c if isinstance(c, str) or c is None else c["function"]["name"]

    user = lambda text: {"role": "user", "content": text}
    bot = lambda text: {"role": "assistant", "content": text}
    said = user("my faculty ID is FAC 1001")
    assert forced(said, bot("That is F A C 1 0 0 1. Is that right?"), user("yes")) == "get_class_summary"
    assert forced(said, bot("That is F A C one zero zero one. Is that correct?"), user("yes correct")) == "get_class_summary"
    assert forced(said, bot("That is F A C 1 0 0 1. Is that right?"), user("no, it is FAC 1002")) == "none"  # new ID: read back
    assert forced(said, bot("That is F A C 1 0 0 1. Is that right?"), user("yes"), role="student") is None
    assert forced(said, bot("Which do you need, attendance or marks?"), user("yes")) is None
    listed = [said, bot("..."), {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
              {"role": "tool", "tool_call_id": "1", "content": '{"status": "choose_class", "speech": "..."}'},
              bot("You are in charge of 2 classes: CSE ...; ECE .... Which class would you like?")]
    assert forced(*listed, user("the ECE one")) == "get_class_summary"
    assert forced(*listed, user("fourth year")) == "get_class_summary"
    assert forced(*listed, user("what are the hostel fees?")) is None
    done = [*listed[:3], {"role": "tool", "tool_call_id": "1", "content": '{"status": "ok", "speech": "..."}'}, bot("...")]
    assert forced(*done, user("the ECE one")) is None
    # a faculty caller is still never forced into a lead by the ID's digits
    assert main._should_force_lead_capture([user("FAC 10012345678")], "t-fac2", "faculty") is False


def test_a_new_faculty_id_is_read_back_by_code_before_any_lookup():
    user = lambda text: {"role": "user", "content": text}
    bot = lambda text: {"role": "assistant", "content": text}
    tools = [{"type": "function", "function": {"name": "get_class_summary"}}]
    needs = lambda *m, role="faculty": main._faculty_needs_readback(list(m), role)
    assert needs(user("My faculty ID is FAC 1001"))
    assert needs(user("F A C one zero zero one"))
    assert not needs(user("FAC1001"), role="student")
    assert not needs(user("the 2023 batch please"))                       # a year is not an ID
    assert not needs(user("FAC1001"), bot("That is F A C 1 0 0 1. Right?"), user("yes"))
    verified = [user("FAC1001"), {"role": "tool", "content": '{"status": "choose_class"}'}, bot("Which class?")]
    assert not needs(*verified, user("ECE 2025"))                          # already accepted on this call
    assert main._forced_tool_choice([user("my ID is FAC 1001")], "t-rb", "faculty", tools, phone=True) == {"tool_choice": "none"}
    assert main._forced_tool_choice([user("my ID is FAC 1001")], "t-rb", "faculty", tools, phone=False) == {}


def test_the_phone_gets_ist_date_time_in_12_hour_form_and_office_hours():
    from datetime import datetime, timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    assert main._now_ist_text(datetime(2026, 10, 20, 1, 28, tzinfo=ist)) == "Tuesday, 20 October 2026, 1:28 AM IST"
    assert main._now_ist_text(datetime(2026, 10, 5, 13, 5, tzinfo=ist)) == "Monday, 5 October 2026, 1:05 PM IST"
    assert main._now_ist_text(datetime(2026, 10, 5, 0, 0, tzinfo=ist)).endswith("12:00 AM IST")
    block = main._phone_block({"role": "", "name": ""})
    assert "8 AM to 5 PM, Monday to Saturday" in block and "CURRENT DATE AND TIME in India" in block


def test_phone_lead_is_read_back_before_it_is_saved_and_the_website_saves_at_once():
    said = [{"role": "user", "content": "My name is Ravi and my number is 9876543210"}]
    # website: forced on the number turn, as before
    assert main._should_force_lead_capture(said, "t-lead-w") is True
    # phone: not forced on the number turn; this turn reads the number back instead
    assert main._should_force_lead_capture(said, "t-lead-p", None, confirm_first=True) is False
    assert main._phone_needs_readback(said, "t-lead-p", None) is True
    readback = [*said, {"role": "assistant", "content": "That is 9 8 7 6 5 4 3 2 1 0. Is that right?"},
                {"role": "user", "content": "yes that's right"}]
    assert main._should_force_lead_capture(readback, "t-lead-p2", None, confirm_first=True) is True
    assert main._phone_needs_readback(readback, "t-lead-p2", None) is False
    # a wrong or short number is still handled by the existing invalid-number flow
    bad = [{"role": "user", "content": "my number is 987654321"}]
    assert main._should_force_lead_capture(bad, "t-lead-p3", None, confirm_first=True) is True
    # records callers and the register-number question are never leads
    assert main._phone_needs_readback(said, "t-lead-p4", "student") is False


def test_only_phone_answers_get_the_lower_temperature():
    assert main._temperature("phone") == {"temperature": main.PHONE_TEMPERATURE}
    assert main._temperature("voice") == {} and main._temperature("chat") == {}


def test_a_number_read_back_in_words_is_recognised_and_beats_a_transfer_offer():
    assert main._spoken_phone("Let me confirm: nine eight seven six five four three two one zero. Is that correct?") == "9876543210"
    assert main._spoken_phone("That is 9 8 7 6 5 4 3 2 1 0, right?") == "9876543210"
    assert main._spoken_phone("one two three") is None
    msgs = [{"role": "user", "content": "My name is Ravi, number 9876543210"},
            {"role": "assistant", "content": "nine eight seven six five four three two one zero. Is that correct? "
                                             "I can connect you with admissions after."},
            {"role": "user", "content": "yes that's right"}]
    tools = [{"type": "function", "function": {"name": "transfer_call"}}]
    choice = main._forced_tool_choice(msgs, "t-words", None, tools, phone=True)
    assert choice["tool_choice"]["function"]["name"] == "capture_lead"


def test_forced_capture_lead_is_saved_even_when_sarvam_says_finish_stop(backend):
    client, install, seen = backend
    forced = ChatCompletion.model_validate({
        "id": "x", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "capture_lead", "arguments": LEAD_ARGS}}]}}]})
    reply = ChatCompletion.model_validate({
        "id": "y", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Thanks Asha."}}]})
    install([forced, reply])
    body = client.post("/widget/chat", json={"messages": [{"role": "user", "content": "I am Asha, 9876543210"}],
                                             "session_id": "t-stop"}).json()
    assert body["reply"] == "Thanks Asha." and len(seen["saved"]) == 1


def test_the_records_tool_is_forced_by_a_date_of_birth_after_a_register_number_only():
    tools = [STUDENT_TOOL]
    reg = {"role": "user", "content": "my register number is 312823104004"}

    def forced(*msgs, role="student"):
        return bool(main._forced_tool_choice(list(msgs), "t-dob", role, tools))

    def user(text):
        return {"role": "user", "content": text}

    bot = lambda text: {"role": "assistant", "content": text}
    assert forced(user("312825104001")) is False                      # a register number alone: the old bug
    assert forced(reg, bot("And the date of birth?"), user("first January two thousand seven")) is True
    assert forced(reg, bot("And the date of birth?"), user("12-04-2005")) is True
    assert forced(reg, bot("And the date of birth?"), user("என்னோட டேட் ஆஃப் பர்த் வந்து ஏப்ரல் 12, 2005.")) is True  # Tamil month
    assert forced(reg, bot("And the date of birth?"), user("I do not remember it")) is False
    assert forced(user("12 April 2005")) is False                     # no register number said yet
    assert forced(reg, bot("..."), user("fees for the 2025 batch")) is False   # a bare year is not a date of birth
    # the wording of the bot's question does not matter (it often is not in English words in Tamil)
    assert forced(reg, bot("உங்க பிறந்த தேதி சொல்லுங்க"), user("April 12 2005")) is True
    # after the tool result came back the same message must not force the tool again
    done = [reg, bot("And the date of birth?"), user("12-04-2005"),
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]}, {"role": "tool", "content": "{}"}]
    assert main._forced_tool_choice(done, "t-dob", "student", tools) == {}
    assert forced(reg, bot("And the date of birth?"), user("12-04-2005"), role="faculty") is False


def test_the_same_caller_tool_call_twice_in_one_turn_is_sent_once():
    call = lambda i, args: {"id": i, "type": "function", "function": {"name": "get_student_report", "arguments": args}}
    out = main._foreign_tool_calls([call("a", '{"x":1}'), call("b", '{"x":1}'), call("c", '{"x":2}'),
                                    {"id": "d", "type": "function", "function": {"name": "capture_lead", "arguments": "{}"}}])
    assert [c["id"] for c in out] == ["a", "c"]


def test_register_number_and_phone_number_spoken_as_words_are_understood():
    tools = [STUDENT_TOOL]
    reg = {"role": "user", "content": "My register number is three one two eight two three one zero four double zero four."}
    dob = {"role": "user", "content": "My date of birth is twelve April two thousand five."}
    assert main._forced_tool_choice([reg, {"role": "assistant", "content": "And the date of birth?"}, dob],
                                    "t-words2", "student", tools) != {}
    said = [{"role": "user", "content": "My name is Ravi and my number is nine eight seven six five four three two one zero"}]
    assert main._should_force_lead_capture(said, "t-words3") is True
    assert main._should_force_lead_capture(said, "t-words4", None, confirm_first=True) is False
    assert main._phone_needs_readback(said, "t-words4", None) is True
