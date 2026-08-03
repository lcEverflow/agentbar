import json
import os
import time

from agentbar.usage import ClaudeUsageFetcher, CodexUsageFetcher


def test_claude_usage_parse_windows():
    snap = ClaudeUsageFetcher().parse({
        "five_hour": {"utilization": 32.5, "resets_at": "2026-07-13T12:00:00Z"},
        "seven_day": {"utilization": 88, "resets_at": "2026-07-19T12:00:00Z"},
    }, plan="pro")
    assert snap.plan == "pro"
    assert [(w.label, w.used_percent) for w in snap.windows] == [("5h", 32.5), ("7d", 88.0)]


def test_claude_keychain_parse_keeps_refresh_token():
    from agentbar.usage import _parse_claude_credentials

    raw = json.dumps({"claudeAiOauth": {
        "accessToken": "access-old",
        "refreshToken": "refresh-long-lived",
        "expiresAt": 1_800_000_000_000,
        "subscriptionType": "pro",
        "scopes": ["user:inference"],
    }}).encode()
    creds = _parse_claude_credentials(raw)
    assert creds == {
        "token": "access-old",
        "refresh_token": "refresh-long-lived",
        "expires_at": 1_800_000_000.0,
        "plan": "pro",
        "scopes": ["user:inference"],
    }


def test_claude_expired_cache_auto_refreshes_without_keychain(monkeypatch, tmp_path):
    import agentbar.usage as usage

    cache = tmp_path / "claude_credentials.json"
    cache.write_text(json.dumps({
        "token": "access-old",
        "refresh_token": "refresh-old",
        "expires_at": time.time() - 60,
        "plan": "pro",
    }))
    monkeypatch.setattr(usage, "CLAUDE_CRED_CACHE", cache)
    monkeypatch.setattr(usage, "_keychain_read",
                        lambda interactive=False: (_ for _ in ()).throw(
                            AssertionError("Keychain must not be read")))

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({
                "access_token": "access-new",
                "refresh_token": "refresh-new",
                "expires_in": 3600,
            }).encode()

    def fake_urlopen(request, timeout):
        assert request.full_url == usage.CLAUDE_OAUTH_TOKEN_URL
        assert timeout == usage.HTTP_TIMEOUT
        assert json.loads(request.data) == {
            "grant_type": "refresh_token",
            "refresh_token": "refresh-old",
            "client_id": usage.CLAUDE_CODE_CLIENT_ID,
        }
        return Response()

    monkeypatch.setattr(usage.urllib.request, "urlopen", fake_urlopen)
    creds = ClaudeUsageFetcher().load_credentials()
    assert creds["token"] == "access-new"
    assert creds["refresh_token"] == "refresh-new"
    saved = json.loads(cache.read_text())
    assert saved["token"] == "access-new"
    assert saved["refresh_token"] == "refresh-new"
    assert os.stat(cache).st_mode & 0o777 == 0o600


def test_claude_expired_unrefreshable_cache_falls_back_to_keychain(monkeypatch, tmp_path):
    import agentbar.usage as usage

    cache = tmp_path / "claude_credentials.json"
    cache.write_text(json.dumps({
        "token": "stale-access-only",
        "expires_at": time.time() - 60,
    }))
    monkeypatch.setattr(usage, "CLAUDE_CRED_CACHE", cache)
    fresh_expiry_ms = int((time.time() + 3600) * 1000)
    raw = json.dumps({"claudeAiOauth": {
        "accessToken": "keychain-access",
        "refreshToken": "keychain-refresh",
        "expiresAt": fresh_expiry_ms,
    }}).encode()
    monkeypatch.setattr(usage, "_keychain_read", lambda interactive=False: raw)

    creds = ClaudeUsageFetcher().load_credentials()
    assert creds["token"] == "keychain-access"
    assert creds["refresh_token"] == "keychain-refresh"
    assert json.loads(cache.read_text())["refresh_token"] == "keychain-refresh"


def test_claude_refresh_network_error_does_not_show_keychain_authorization(monkeypatch, tmp_path):
    import agentbar.usage as usage

    cache = tmp_path / "claude_credentials.json"
    cache.write_text(json.dumps({
        "token": "expired-access",
        "refresh_token": "saved-refresh",
        "expires_at": time.time() - 60,
    }))
    monkeypatch.setattr(usage, "CLAUDE_CRED_CACHE", cache)
    keychain_reads = []
    monkeypatch.setattr(
        usage, "_keychain_read",
        lambda interactive=False: keychain_reads.append(interactive) or None,
    )
    monkeypatch.setattr(
        usage.urllib.request, "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            usage.urllib.error.URLError("offline")),
    )

    snap = ClaudeUsageFetcher().fetch()
    assert "自动续期暂时失败" in snap.error
    assert "Keychain" not in snap.error
    assert keychain_reads == [False]


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
