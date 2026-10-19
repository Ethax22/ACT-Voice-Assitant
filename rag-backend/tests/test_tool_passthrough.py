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
