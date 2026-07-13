import json
import time

from agentbar.adapters.base import looks_like_quota, parse_reset_hint
from agentbar.adapters.claude import ClaudeAdapter
from agentbar.adapters.codex import CodexAdapter
from agentbar.config import Settings


def _claude(tmp_path):
    return ClaudeAdapter(Settings(state_dir=tmp_path))


def _codex(tmp_path):
    return CodexAdapter(Settings(state_dir=tmp_path))


# ---------- quota patterns ----------

QUOTA_SAMPLES = [
    "Claude AI usage limit reached|1752392400",
    "You've hit your usage limit. Try again in 4 hours 13 minutes.",
    'API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}',
    "stream error: 429 Too Many Requests",
    'API Error: 529 {"type":"error","error":{"type":"overloaded_error"}}',
    "Your credit balance is too low to access the Anthropic API",
    "Rate limit reached. Please try again in 27 seconds.",
]


def test_quota_patterns_match():
    for s in QUOTA_SAMPLES:
        assert looks_like_quota(s), s


def test_quota_patterns_no_false_positive():
    assert not looks_like_quota("Fixed the bug in parser.py, all tests pass")


def test_reset_hint_epoch():
    assert parse_reset_hint("limit reached|1752392400", now=1752390000) == 1752392400


def test_reset_hint_duration():
    now = time.time()
    got = parse_reset_hint("Try again in 4 hours 13 minutes.", now=now)
    assert got is not None and abs(got - (now + 4 * 3600 + 13 * 60)) < 2
    got = parse_reset_hint("try again in 27 seconds", now=now)
    assert got is not None and abs(got - (now + 27)) < 2


def test_reset_hint_absent():
    assert parse_reset_hint("rate limit exceeded") is None


# ---------- claude ----------

def test_claude_success_json(tmp_path):
    a = _claude(tmp_path)
    out = "\n".join([
        "some log line",
        json.dumps({"type": "result", "subtype": "success", "is_error": False,
                    "result": "done", "session_id": "sess-42",
                    "total_cost_usd": 0.0123}),
    ])
    o = a.classify(0, out)
    assert o.kind == "success"
    assert o.cost_usd == 0.0123
    assert a.extract_session_id(out) == "sess-42"


def test_claude_quota_plain(tmp_path):
    a = _claude(tmp_path)
    future = int(time.time()) + 3600
    o = a.classify(1, f"Claude AI usage limit reached|{future}")
    assert o.kind == "quota"
    assert o.reset_at == future


def test_claude_plain_failure(tmp_path):
    a = _claude(tmp_path)
    o = a.classify(1, "Error: something exploded")
    assert o.kind == "failure"


def test_claude_error_json_quota(tmp_path):
    a = _claude(tmp_path)
    out = json.dumps({"type": "result", "is_error": True,
                      "result": "rate limit exceeded", "session_id": "s"})
    o = a.classify(1, out)
    assert o.kind == "quota"


# ---------- codex ----------

def test_codex_success_and_session(tmp_path):
    a = _codex(tmp_path)
    out = "OpenAI Codex v0.144\nsession id: 0199aaaa-bbbb-cccc-dddd-eeeeffff0000\nAll done."
    assert a.classify(0, out).kind == "success"
    assert a.extract_session_id(out) == "0199aaaa-bbbb-cccc-dddd-eeeeffff0000"


def test_codex_quota_with_duration(tmp_path):
    a = _codex(tmp_path)
    o = a.classify(1, "ERROR: You've hit your usage limit. Try again in 2 hours 5 minutes.")
    assert o.kind == "quota"
    assert o.reset_at is not None


def test_codex_failure(tmp_path):
    a = _codex(tmp_path)
    o = a.classify(2, "error: not logged in\nRun codex login first")
    assert o.kind == "failure"
    assert "logged in" in o.reason
