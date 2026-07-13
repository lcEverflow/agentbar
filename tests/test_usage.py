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
