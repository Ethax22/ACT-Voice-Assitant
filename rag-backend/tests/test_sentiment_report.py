"""sentiment_report: each saved conversation lands in the right channel, with the caller role."""
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Importing the script loads rag-backend/.env; put the environment back so other tests see theirs.
_env = dict(os.environ)
import sentiment_report as S  # noqa: E402

os.environ.clear()
os.environ.update(_env)


def _save(base, day, name, source, user="hi", meta=None, record=None):
    d = base / day / name
    d.mkdir(parents=True)
    (d / "transcript.json").write_text(json.dumps({"session_id": name, "source": source, "language": "en",
                                                   "messages": [{"role": "user", "content": user}]}), encoding="utf-8")
    if meta is not None:
        (d / "call_meta.json").write_text(json.dumps(meta), encoding="utf-8")
    if record is not None:
        (d / "call_record.json").write_text(json.dumps(record), encoding="utf-8")


def test_channels_and_roles(tmp_path, monkeypatch):
    chat, voice = tmp_path / "rag", tmp_path / "voice"
    monkeypatch.setattr(S, "LOG_DIRS", [chat, voice])
    _save(chat, "2026-10-21", "c1", "chat")
    _save(voice, "2026-10-21", "w1", "voice")
    _save(voice, "2026-10-21", "phone-test-in", "voice", meta={"direction": "inbound", "role": "student"})
    _save(voice, "2026-10-21", "phone-test-out", "voice", meta={"direction": "outbound", "role": "lead"})
    _save(voice, "2026-10-21", "phone-test-old", "voice", record={"caller_role": "parent"})   # before call_meta.json
    _save(voice, "2026-10-21", "phone-test-quiet", "voice", user="  ")                      # caller never spoke
    convs = {c["session_id"]: c for c in S._load_conversations(date(2026, 10, 21), date(2026, 10, 21))}
    assert {k: (c["channel"], c["role"]) for k, c in convs.items()} == {
        "c1": ("website_chat", ""), "w1": ("website_voice", ""), "phone-test-in": ("phone_inbound", "student"),
        "phone-test-out": ("phone_outbound", "lead"), "phone-test-old": ("phone_inbound", "parent")}
    assert len(S._load_conversations(date(2026, 10, 21), date(2026, 10, 21), "phone")) == 3
    assert len(S._load_conversations(date(2026, 10, 21), date(2026, 10, 21), "website")) == 2
