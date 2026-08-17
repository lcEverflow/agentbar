import json
import os
import textwrap
import time

import pytest

from agentbar.config import Settings
from agentbar.usage import (
    ClaudeUsageFetcher,
    CodexUsageFetcher,
    credential_status,
)


def _make_cli(tmp_path, name, body):
    path = tmp_path / name
    path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(0o700)
    return path


def test_claude_uses_status_command_only_and_discards_pii(tmp_path, monkeypatch):
    cli = _make_cli(tmp_path, "claude", """
        import json
        print(json.dumps({
            "loggedIn": True,
            "authMethod": "claude.ai",
            "subscriptionType": "max",
            "email": "private@example.test",
            "orgId": "secret-org-id",
            "orgName": "Secret Org",
            "accessToken": "must-never-escape",
        }))
    """)
    monkeypatch.setattr(
        "agentbar.usage._http_get_json",
        lambda *args, **kwargs: pytest.fail("Claude must not call a private usage API"),
    )

    fetcher = ClaudeUsageFetcher(binary=str(cli))
    creds = fetcher.load_credentials()
    snap = fetcher.fetch()

    assert creds == {"plan": "max", "auth_method": "claude.ai"}
    assert snap.windows == []
    assert snap.plan == "max"
    assert snap.source == "claude_auth_status"
    serialized = json.dumps({"creds": creds, "snap": snap.to_dict()})
    assert "private@example" not in serialized
    assert "secret-org" not in serialized
    assert "must-never-escape" not in serialized


def test_claude_logged_out_is_diagnostic_and_has_no_fake_windows(tmp_path):
    cli = _make_cli(tmp_path, "claude", """
        import json
        print(json.dumps({"loggedIn": False, "email": "private@example.test"}))
    """)

    snap = ClaudeUsageFetcher(binary=str(cli)).fetch()

    assert snap.windows == []
    assert "未登录" in snap.error
    assert "private@example" not in snap.error


def test_credential_status_is_sanitized_and_cached(monkeypatch):
    import agentbar.usage as usage_module

    usage_module._clear_credential_status_cache()
    calls = []

    def fake_resolver(**kwargs):
        calls.append(kwargs)
        return usage_module._CredentialResolution(
            {"token": "never-public"}, "claude_auth_status", "available", "ready"
        )

    monkeypatch.setattr(usage_module, "_resolve_claude_credentials", fake_resolver)
    first = credential_status("claude")
    second = credential_status("claude")
    refreshed = credential_status("claude", refresh=True)

    assert first == second == refreshed == {
        "available": True,
        "source": "claude_auth_status",
        "status": "available",
        "detail": "ready",
        "needs_authorization": False,
    }
    assert len(calls) == 2
    assert "token" not in first


def test_codex_fetches_official_app_server_rate_limits(tmp_path):
    cli = _make_cli(tmp_path, "codex", """
        import json, sys
        for line in sys.stdin:
            message = json.loads(line)
            method = message.get("method")
            if method == "initialize":
                print(json.dumps({"id": message["id"], "result": {"serverInfo": {}}}), flush=True)
            elif method == "account/read":
                print(json.dumps({"id": message["id"], "result": {
                    "account": {"type": "chatgpt", "planType": "plus", "email": "private@example.test"},
                    "requiresOpenaiAuth": True,
                }}), flush=True)
            elif method == "account/rateLimits/read":
                base = {
                    "limitId": "codex", "limitName": None, "planType": "plus",
                    "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1800000000},
                    "secondary": {"usedPercent": 50, "windowDurationMins": 10080, "resetsAt": 1800100000},
                    "rateLimitReachedType": None,
                }
                other = {
                    "limitId": "codex_other", "limitName": "Codex Other",
                    "primary": {"usedPercent": 42, "windowDurationMins": 60, "resetsAt": 1800200000},
                    "secondary": None, "rateLimitReachedType": "rate_limit_reached",
                }
                print(json.dumps({"id": message["id"], "result": {
                    "rateLimits": base,
                    "rateLimitsByLimitId": {"codex": base, "codex_other": other},
                    "untrustedSecret": "must-never-escape",
                }}), flush=True)
    """)

    snap = CodexUsageFetcher(binary=str(cli), model="codex_other").fetch()

    assert snap.error is None
    assert snap.plan == "plus"
    assert snap.available_models == ["codex", "codex_other"]
    assert [(w.label, w.used_percent, w.model, w.limited) for w in snap.windows] == [
        ("账户 5h", 25.0, None, False),
        ("账户 7d", 50.0, None, False),
        ("Codex Other 1h", 42.0, "Codex Other", True),
    ]
    assert "private@example" not in json.dumps(snap.to_dict())
    assert "must-never-escape" not in json.dumps(snap.to_dict())


def test_codex_api_key_auth_never_requests_subscription_limits(tmp_path):
    marker = tmp_path / "rate-requested"
    cli = _make_cli(tmp_path, "codex", f"""
        import json, pathlib, sys
        marker = pathlib.Path({str(marker)!r})
        for line in sys.stdin:
            message = json.loads(line)
            method = message.get("method")
            if method == "initialize":
                print(json.dumps({{"id": message["id"], "result": {{}}}}), flush=True)
            elif method == "account/read":
                print(json.dumps({{"id": message["id"], "result": {{
                    "account": {{"type": "apiKey"}}, "requiresOpenaiAuth": True
                }}}}), flush=True)
            elif method == "account/rateLimits/read":
                marker.write_text("unexpected")
    """)

    snap = CodexUsageFetcher(binary=str(cli)).fetch()

    assert snap.windows == []
    assert "API Key" in snap.error
    assert not marker.exists()


def test_codex_app_server_timeout_kills_process_group(tmp_path, monkeypatch):
    import agentbar.usage as usage_module

    pid_file = tmp_path / "pid"
    cli = _make_cli(tmp_path, "codex", f"""
        import json, os, pathlib, sys, time
        for line in sys.stdin:
            message = json.loads(line)
            if message.get("method") == "initialize":
                print(json.dumps({{"id": message["id"], "result": {{}}}}), flush=True)
            elif message.get("method") == "account/read":
                pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))
                time.sleep(60)
    """)
    monkeypatch.setattr(usage_module, "_CODEX_APP_SERVER_TIMEOUT_SECONDS", 2.0)

    started = time.monotonic()
    snap = CodexUsageFetcher(binary=str(cli)).fetch()

    assert time.monotonic() - started < 3
    assert "不可用" in snap.error
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_codex_app_server_rejects_oversized_or_server_request_output(
    tmp_path, monkeypatch
):
    import agentbar.usage as usage_module

    cli = _make_cli(tmp_path, "codex", """
        import json, sys, time
        for line in sys.stdin:
            message = json.loads(line)
            if message.get("method") == "initialize":
                print(json.dumps({"id": message["id"], "result": {}}), flush=True)
            elif message.get("method") == "account/read":
                sys.stdout.write("x" * 1024)
                sys.stdout.flush()
                time.sleep(60)
    """)
    monkeypatch.setattr(usage_module, "_CODEX_APP_SERVER_MAX_LINE_BYTES", 128)

    snap = CodexUsageFetcher(binary=str(cli)).fetch()

    assert snap.windows == []
    assert "不可用" in snap.error


def test_codex_app_server_fails_closed_on_server_request(tmp_path):
    cli = _make_cli(tmp_path, "codex", """
        import json, sys, time
        for line in sys.stdin:
            message = json.loads(line)
            if message.get("method") == "initialize":
                print(json.dumps({"id": message["id"], "result": {}}), flush=True)
            elif message.get("method") == "account/read":
                print(json.dumps({
                    "id": 99,
                    "method": "account/chatgptAuthTokens/refresh",
                    "params": {"previousAccountId": "must-not-be-retained"},
                }), flush=True)
                time.sleep(60)
    """)

    started = time.monotonic()
    snap = CodexUsageFetcher(binary=str(cli)).fetch()

    assert time.monotonic() - started < 2
    assert snap.windows == []
    assert "不可用" in snap.error
    assert "must-not-be-retained" not in snap.error


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


def test_subscription_fetchers_require_only_explicit_enablement(tmp_path):
    settings = Settings(state_dir=tmp_path)
    settings.quota_sources = {
        "claude": {"enabled": True},
        "codex": {"enabled": True, "model": "codex_bengalfox"},
    }
    fetchers = get_usage_fetchers(settings)

    assert set(fetchers) == {"claude", "codex"}
    assert fetchers["codex"].model == "codex_bengalfox"

    settings.quota_sources["claude"]["enabled"] = False
    settings.quota_sources["codex"]["enabled"] = False
    assert get_usage_fetchers(settings) == {}
