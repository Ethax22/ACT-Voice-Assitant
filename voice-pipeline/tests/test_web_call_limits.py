"""Website voice call limits: calls at once (overall and per address) and starts per hour."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web_call_limits as W  # noqa: E402


def test_session_gate():
    g = W.SessionGate(max_sessions=3, max_per_address=2, per_hour=4)
    assert g.enter("a", now=0) is None and g.enter("a", now=1) is None
    assert g.enter("a", now=2) == "too_many"           # two open from this address
    assert g.enter("b", now=3) is None
    assert g.enter("c", now=4) == "busy"               # three open in all
    g.leave("a"); g.leave("a")
    assert g.enter("a", now=5) is None and g.enter("a", now=6) is None
    g.leave("a"); g.leave("a")
    assert g.enter("a", now=7) == "too_many"           # four starts this hour
    assert g.enter("a", now=3601) is None              # the first start is an hour old
    assert W.SessionGate(0, 0, 0).enter("x") is None   # 0 = off


def test_address_uses_the_entry_nginx_added():
    assert W.client_address({"x-forwarded-for": "1.1.1.1, 9.9.9.9"}, "127.0.0.1") == "9.9.9.9"
    assert W.client_address({}, None) == "unknown"
