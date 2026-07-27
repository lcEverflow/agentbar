from agentbar.usage import ClaudeUsageFetcher, CodexUsageFetcher


def test_claude_usage_parse_windows():
    snap = ClaudeUsageFetcher().parse({
        "five_hour": {"utilization": 32.5, "resets_at": "2026-07-13T12:00:00Z"},
        "seven_day": {"utilization": 88, "resets_at": "2026-07-19T12:00:00Z"},
    }, plan="pro")
    assert snap.plan == "pro"
    assert [(w.label, w.used_percent) for w in snap.windows] == [("5h", 32.5), ("7d", 88.0)]


def test_codex_usage_parse_windows():
    snap = CodexUsageFetcher().parse({"rate_limits": {
        "primary": {"used_percent": 40, "resets_at": 1_800_000_000, "window_duration_mins": 300},
        "secondary": {"used_percent": 80, "resets_at": 1_800_100_000, "window_duration_mins": 10_080},
    }})
    assert [(w.label, w.used_percent) for w in snap.windows] == [("5h", 40.0), ("7d", 80.0)]


def test_codex_usage_parses_current_wham_shape(monkeypatch):
    monkeypatch.setattr("agentbar.usage.time.time", lambda: 1_700_000_000)
    snap = CodexUsageFetcher().parse({
        "plan_type": "plus",
        "rate_limit": {
            "limit_reached": True,
            "primary_window": {
                "used_percent": 100,
                "limit_window_seconds": 18_000,
                "reset_after_seconds": 600,
            },
        },
    })
    assert snap.plan == "plus"
    assert snap.limited is True
    assert snap.windows[0].label == "5h"
    assert snap.windows[0].resets_at == 1_700_000_600


# ---------- 快手内部 provider（MyToken / Tokenverse） ----------

from agentbar.usage import (  # noqa: E402
    MyTokenUsageFetcher,
    TokenverseUsageFetcher,
    get_usage_fetchers,
)


def test_mytoken_credits_window(monkeypatch):
    def fake_env(url, cookie, extra=None):
        if url.endswith("/api/auth/sso/user"):
            return {"name": "zhangsan"}
        if url.endswith("/api/v1/billing/account"):
            return {"account": {"tierName": "Pro", "tierCode": "professional"},
                    "summary": {"creditTotal": 1000, "creditUsed": 250,
                                "creditAvailable": 750, "creditUnit": "credits",
                                "renewAt": 1_800_000_000_000}}
        raise AssertionError(url)
    monkeypatch.setattr("agentbar.usage._corp_envelope", fake_env)
    snap = MyTokenUsageFetcher(cookie="c=1", unit="credits").fetch()
    assert snap.error is None
    assert snap.plan == "Pro"
    w = snap.windows[0]
    assert (w.used, w.total, w.unit) == (250.0, 1000.0, "credits")
    assert w.used_percent == 25.0
    assert w.resets_at == 1_800_000_000.0


def test_mytoken_missing_cookie():
    snap = MyTokenUsageFetcher(cookie="").fetch()
    assert snap.windows == []
    assert "cookie" in snap.error


def test_tokenverse_percent_unit(monkeypatch):
    def fake_env(url, cookie, extra=None):
        if url.endswith("/api/coding-plan/status"):
            return {"monthlyCredits": 500, "planType": 1}
        if "/api/coding-plan/usage/summary" in url:
            return {"totalCredits": 400, "totalTokens": 123456}
        raise AssertionError(url)
    monkeypatch.setattr("agentbar.usage._corp_envelope", fake_env)
    snap = TokenverseUsageFetcher(cookie="c=1", unit="percent").fetch()
    assert snap.error is None
    assert snap.plan == "Standard"
    w = snap.windows[0]
    assert w.used_percent == 80.0
    assert (w.used, w.total, w.unit) == (80.0, 100.0, "percent")


def test_tokenverse_token_unit(monkeypatch):
    def fake_env(url, cookie, extra=None):
        if url.endswith("/api/coding-plan/status"):
            return {"openModelCreditsPerMon": 300, "closeModelCreditsPerMon": 200,
                    "planType": 1}
        return {"totalCredits": 100, "totalTokens": 9_000}
    monkeypatch.setattr("agentbar.usage._corp_envelope", fake_env)
    snap = TokenverseUsageFetcher(cookie="c=1", unit="token").fetch()
    w = snap.windows[0]
    assert w.used_percent == 20.0            # 100 / (300+200)
    assert (w.used, w.unit) == (9_000.0, "token")


def test_get_usage_fetchers_gated_by_config():
    class S:
        providers = {
            "mytoken": {"enabled": True, "cookie": "c=1", "unit": "credits"},
            "tokenverse": {"enabled": False, "cookie": "c=1", "unit": "credits"},
        }
    f = get_usage_fetchers(S())
    assert "mytoken" in f and "tokenverse" not in f
    assert set(get_usage_fetchers(None)) == {"claude", "codex"}
