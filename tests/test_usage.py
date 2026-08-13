import pytest

from agentbar.config import Settings
from agentbar.usage import ClaudeUsageFetcher, CodexUsageFetcher


def test_claude_usage_parse_windows():
    snap = ClaudeUsageFetcher().parse({
        "five_hour": {"utilization": 32.5, "resets_at": "2026-07-13T12:00:00Z"},
        "seven_day": {"utilization": 88, "resets_at": "2026-07-19T12:00:00Z"},
    }, plan="pro")
    assert snap.plan == "pro"
    assert [(w.label, w.used_percent) for w in snap.windows] == [("5h", 32.5), ("7d", 88.0)]
    assert [w.model for w in snap.windows] == [None, None]


def test_claude_selected_opus_keeps_common_and_opus_windows_only():
    snap = ClaudeUsageFetcher(model="claude-opus-4-5").parse({
        "five_hour": {"utilization": 10},
        "seven_day": {"utilization": 20},
        "seven_day_opus": {"utilization": 30},
        "seven_day_sonnet": {"utilization": 40},
    })

    assert snap.model == "claude-opus-4-5"
    assert snap.available_models == ["opus", "sonnet"]
    assert [(w.label, w.used_percent, w.model) for w in snap.windows] == [
        ("5h", 10.0, None),
        ("7d", 20.0, None),
        ("7d Opus", 30.0, "opus"),
    ]
    assert snap.to_dict()["windows"][-1]["model"] == "opus"


def test_manual_credentials_are_the_only_credential_source(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "must-not-be-read")
    monkeypatch.setenv("CODEX_HOME", "/must/not/be/read")

    assert ClaudeUsageFetcher().load_credentials() is None
    assert CodexUsageFetcher().load_credentials() is None
    assert ClaudeUsageFetcher(access_token=" configured-claude ").load_credentials() == {
        "token": "configured-claude",
        "plan": None,
    }
    assert CodexUsageFetcher(
        access_token=" configured-codex ", account_id=" account-123 "
    ).load_credentials() == {
        "token": "configured-codex",
        "account_id": "account-123",
        "plan": None,
    }


def test_codex_malformed_non_object_jwt_payload_does_not_break_manual_token():
    # base64url("[]") = W10; the access token is still sent, but no claims can be
    # inferred from a non-object JWT payload.
    assert CodexUsageFetcher(access_token="x.W10.y").load_credentials() == {
        "token": "x.W10.y",
        "account_id": None,
        "plan": None,
    }


def test_codex_usage_parse_windows():
    snap = CodexUsageFetcher().parse({"rate_limits": {
        "primary": {"used_percent": 40, "resets_at": 1_800_000_000, "window_duration_mins": 300},
        "secondary": {"used_percent": 80, "resets_at": 1_800_100_000, "window_duration_mins": 10_080},
    }})
    assert [(w.label, w.used_percent) for w in snap.windows] == [("5h", 40.0), ("7d", 80.0)]
    assert [(w.model, w.limited) for w in snap.windows] == [(None, False), (None, False)]


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
    assert snap.windows[0].model is None
    assert snap.windows[0].limited is True


def _codex_additional_limits(*, allowed=True, account_allowed=True):
    return {
        "plan_type": "plus",
        "rate_limit": {
            "allowed": account_allowed,
            "limit_reached": False,
            "primary_window": {
                "used_percent": 7,
                "limit_window_seconds": 18_000,
                "reset_at": 1_800_000_001,
            },
        },
        "additional_rate_limits": [{
            "limit_name": "GPT-5.3-Codex-Spark",
            "metered_feature": "codex_bengalfox",
            "rate_limit": {
                "allowed": allowed,
                "limit_reached": False,
                "primary_window": {
                    "used_percent": 23,
                    "limit_window_seconds": 604_800,
                    "reset_at": 1_800_000_123,
                },
            },
        }],
    }


@pytest.mark.parametrize("selector", ["codex_bengalfox", "GPT-5.3-Codex-Spark"])
def test_codex_selects_additional_rate_limit_by_feature_or_display_name(selector):
    snap = CodexUsageFetcher(model=selector).parse(_codex_additional_limits())

    assert snap.error is None
    assert snap.model == "GPT-5.3-Codex-Spark"
    assert snap.available_models == ["codex_bengalfox"]
    assert [(w.label, w.used_percent, w.model, w.limited) for w in snap.windows] == [
        ("账户 5h", 7.0, None, False),
        (
            "GPT-5.3-Codex-Spark 7d",
            23.0,
            "GPT-5.3-Codex-Spark",
            False,
        ),
    ]
    assert snap.windows[1].resets_at == 1_800_000_123


def test_codex_unknown_model_selector_is_an_explicit_error():
    snap = CodexUsageFetcher(model="missing-model").parse(_codex_additional_limits())

    assert snap.windows == []
    assert snap.model == "missing-model"
    assert snap.available_models == ["codex_bengalfox"]
    assert "未找到模型额度" in snap.error
    assert "codex_bengalfox" in snap.error


def test_codex_allowed_false_marks_selected_limit_limited():
    snap = CodexUsageFetcher(model="codex_bengalfox").parse(
        _codex_additional_limits(allowed=False)
    )

    assert snap.windows
    assert snap.limited is True
    assert [(w.model, w.limited) for w in snap.windows] == [
        (None, False),
        ("GPT-5.3-Codex-Spark", True),
    ]


def test_codex_selected_limit_keeps_account_wide_limited_window():
    snap = CodexUsageFetcher(model="codex_bengalfox").parse(
        _codex_additional_limits(account_allowed=False)
    )

    assert snap.limited is True
    assert [(w.model, w.limited) for w in snap.windows] == [
        (None, True),
        ("GPT-5.3-Codex-Spark", False),
    ]


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
    assert get_usage_fetchers(None) == {}


def test_subscription_fetchers_require_enabled_and_manual_key(tmp_path, monkeypatch):
    settings = Settings(state_dir=tmp_path)
    settings.quota_sources = {
        "claude": {"enabled": True, "access_token": "", "model": "opus"},
        "codex": {"enabled": True, "access_token": "", "model": "codex_bengalfox"},
    }
    # Even discoverable legacy credentials must not opt a source in implicitly.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "legacy-claude")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    assert get_usage_fetchers(settings) == {}

    settings.quota_sources["claude"]["access_token"] = "manual-claude"
    settings.quota_sources["codex"].update({
        "access_token": "manual-codex",
        "account_id": "account-123",
    })
    fetchers = get_usage_fetchers(settings)

    assert set(fetchers) == {"claude", "codex"}
    assert fetchers["claude"].access_token == "manual-claude"
    assert fetchers["claude"].model == "opus"
    assert fetchers["codex"].access_token == "manual-codex"
    assert fetchers["codex"].account_id == "account-123"
    assert fetchers["codex"].model == "codex_bengalfox"
