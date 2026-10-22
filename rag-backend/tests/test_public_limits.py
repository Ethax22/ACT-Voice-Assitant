"""Public typed-chat limits: per-address rate, message length, history trimming, address source."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import public_limits as L  # noqa: E402


def test_rate_per_minute_and_hour():
    r = L.RateLimiter(per_minute=3, per_hour=5)
    assert [r.allow("a", now=t) for t in (0, 1, 2, 3)] == [True, True, True, False]   # 4th within a minute
    assert r.allow("b", now=3)                                                         # other visitors unaffected
    assert r.allow("a", now=61) and r.allow("a", now=62)                               # minute passed: 5 this hour
    assert not r.allow("a", now=200)                                                   # hour cap
    assert r.allow("a", now=3601)                                                      # first hit an hour old
    assert all(L.RateLimiter(0, 0).allow("a", now=t) for t in range(100))              # 0 = off


def test_length_and_trim(monkeypatch):
    monkeypatch.setattr(L, "MAX_MESSAGE_CHARS", 10)
    assert L.too_long([{"role": "user", "content": "x" * 11}])
    assert not L.too_long([{"role": "user", "content": "x" * 11}, {"role": "assistant", "content": "ok"},
                           {"role": "user", "content": "short"}])                      # only the newest counts
    monkeypatch.setattr(L, "MAX_MESSAGES", 4)
    msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(i)} for i in range(9)]
    kept = L.trim_history(msgs)
    assert [m["content"] for m in kept] == ["6", "7", "8"] and kept[0]["role"] == "user"
    assert L.trim_history(msgs[:3]) == msgs[:3]


def test_address_uses_the_entry_nginx_added():
    assert L.client_address({"x-forwarded-for": "1.1.1.1, 9.9.9.9"}, "127.0.0.1") == "9.9.9.9"
    assert L.client_address({}, "10.0.0.5") == "10.0.0.5"
