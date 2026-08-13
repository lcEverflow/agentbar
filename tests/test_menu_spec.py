"""菜单 spec 纯函数测试 — 不需要 AppKit，保证"没有点不动的行"。"""

import time

import pytest

from agentbar.menu_spec import build_menu_spec, build_ring_progress, build_title

BASE_SNAPSHOT = {
    "status": "idle",
    "paused": False,
    "running_titles": [],
    "queued": 0,
    "waiting_quota": 0,
    "tasks": [],
    "quota": {},
}


def _snap(**over):
    d = {**BASE_SNAPSHOT, **over}
    return d


def _leaves(nodes):
    for n in nodes:
        if n["kind"] == "sep":
            continue
        if n.get("children") is not None:
            yield from _leaves(n["children"])
        else:
            yield n


def test_enabled_rows_are_actionable():
    """Enabled (clickable) rows must have an action; disabled info rows may have action=None."""
    spec = build_menu_spec(_snap(
        running_titles=["任务A"],
        quota={"claude": {"state": "ok", "windows": [
            {"label": "5h", "used_percent": 37.0, "resets_at": time.time() + 3600}],
            "source": "usage_api", "fetched_at": time.time(), "detail": "", "error": None,
            "plan": "max"}},
    ))
    for leaf in _leaves(spec):
        if leaf.get("enabled"):
            assert leaf["action"], f"启用的行没有 action: {leaf['title']}"


def test_pause_resume_toggle():
    spec = build_menu_spec(_snap(paused=False))
    actions = [n["action"] for n in spec if n["kind"] == "action"]
    assert "pause_all" in actions and "resume_all" not in actions
    spec = build_menu_spec(_snap(paused=True, status="paused"))
    actions = [n["action"] for n in spec if n["kind"] == "action"]
    assert "resume_all" in actions and "pause_all" not in actions


def test_quota_submenu_contents():
    now = time.time()
    spec = build_menu_spec(_snap(quota_source_config={
        "claude": {"enabled": True, "key_set": True},
        "codex": {"enabled": False, "key_set": False},
    }, quota={
        "claude": {"state": "limited",
                   "windows": [{"label": "5h", "used_percent": 100.0, "resets_at": now + 600},
                               {"label": "7d", "used_percent": 41.0, "resets_at": None}],
                   "source": "usage_api", "fetched_at": now, "plan": "max",
                   "detail": "", "error": None},
        "codex": {"state": "unknown", "windows": [], "source": "none",
                  "fetched_at": None, "plan": None, "detail": "未知（尚无额度数据）",
                  "error": "未读到 ~/.codex/auth.json（先运行 codex login）"},
    }))
    subs = [n for n in spec if n["kind"] == "submenu" and "手机访问" not in n["title"]]
    assert len(subs) == 2
    claude = subs[0]
    assert "100%" in claude["title"] or "5h 100%" in claude["title"]
    child_actions = [c["action"] for c in claude["children"] if c["kind"] == "action"]
    assert "refresh_quota:claude" in child_actions
    # 手动 OAuth 模式不再提供 Keychain 授权入口。
    codex = subs[1]
    child_actions = [c["action"] for c in codex["children"] if c["kind"] == "action"]
    assert "authorize_keychain" not in child_actions


def test_legacy_keychain_error_never_reintroduces_authorize_action():
    spec = build_menu_spec(_snap(quota={
        "claude": {"state": "unknown", "windows": [], "source": "none",
                   "fetched_at": None, "plan": None, "detail": "",
                   "error": "未读到 Claude 凭据（Keychain 静默读取被拒？菜单里可手动授权）"},
    }))
    sub = next(n for n in spec if n["kind"] == "submenu")
    child_actions = [c["action"] for c in sub["children"] if c["kind"] == "action"]
    assert "authorize_keychain" not in child_actions


def test_core_actions_present():
    spec = build_menu_spec(_snap())
    actions = {n["action"] for n in spec if n["kind"] == "action"}
    assert {"open_panel", "quick_add", "provider_settings", "quit"} <= actions
    assert "open_web_panel" not in actions


def test_unconfigured_corp_providers_stay_discoverable_in_menu():
    spec = build_menu_spec(_snap(provider_config={
        "mytoken": {"enabled": False, "cookie_set": False, "unit": "credits"},
        "tokenverse": {"enabled": True, "cookie_set": False, "unit": "credits"},
    }))
    provider_rows = [
        row for row in spec
        if row["kind"] == "submenu"
        and ("MyToken" in row["title"] or "Tokenverse" in row["title"])
    ]
    assert len(provider_rows) == 2
    assert "未配置" in provider_rows[0]["title"]
    assert "缺少 Cookie" in provider_rows[1]["title"]
    for row in provider_rows:
        assert any(child.get("action") == "provider_settings" for child in row["children"])


def test_quota_menu_shows_selected_model_and_refreshes_only_that_source():
    now = time.time()
    spec = build_menu_spec(_snap(quota_source_config={
        "codex": {"enabled": True, "key_set": True, "model": "codex_bengalfox"},
    }, quota={
        "codex": {
            "state": "ok",
            "model": "codex_bengalfox",
            "windows": [{"label": "5h", "used_percent": 28.0,
                         "resets_at": now + 600}],
            "source": "wham_api",
            "fetched_at": now,
            "plan": "pro",
            "available_models": ["codex_bengalfox"],
            "detail": "",
            "error": None,
        },
    }))
    row = next(n for n in spec if n["kind"] == "submenu" and "Codex" in n["title"])
    assert "codex_bengalfox" in row["title"]
    assert any("模型 codex_bengalfox" in child["title"] for child in row["children"])
    assert any("可选额度标识 codex_bengalfox" in child["title"] for child in row["children"])
    actions = [child.get("action") for child in row["children"]]
    assert "refresh_quota:codex" in actions
    assert "refresh_quota:claude" not in actions
    assert "refresh_quota" not in actions


def test_stale_quota_is_labeled_in_menu_instead_of_looking_current():
    spec = build_menu_spec(_snap(quota_source_config={
        "codex": {"enabled": True, "key_set": True},
    }, quota={
        "codex": {
            "state": "unknown",
            "windows": [{"label": "5h", "used_percent": 42.0, "resets_at": None}],
            "source": "wham_api",
            "fetched_at": time.time() - 3600,
            "stale": True,
            "detail": "上次额度数据已过期，请手动刷新",
            "error": None,
        },
    }))

    row = next(n for n in spec if n["kind"] == "submenu" and "Codex" in n["title"])
    assert "已过期" in row["title"]
    assert any("数据已过期" in child["title"] for child in row["children"])


@pytest.mark.parametrize(
    "source_cfg",
    [
        {"enabled": False, "key_set": True, "model": "sonnet"},
        {"enabled": True, "key_set": False, "model": "sonnet"},
    ],
)
def test_observed_quota_for_unconfigured_source_has_setup_but_no_refresh(source_cfg):
    spec = build_menu_spec(_snap(
        quota_source_config={"claude": source_cfg},
        quota={
            "claude": {
                "state": "limited",
                "windows": [],
                "source": "observed",
                "fetched_at": None,
                "plan": None,
                "detail": "任务观测到额度受限",
                "error": None,
            },
        },
    ))

    row = next(n for n in spec if n["kind"] == "submenu" and "Claude" in n["title"])
    actions = [child.get("action") for child in row["children"]]
    assert "provider_settings" in actions
    assert "refresh_quota:claude" not in actions


def test_disabled_manual_quota_sources_stay_discoverable_without_refresh():
    spec = build_menu_spec(_snap(quota_source_config={
        "claude": {"enabled": False, "key_set": False, "model": ""},
        "codex": {
            "enabled": False,
            "key_set": True,
            "model": "codex_bengalfox",
            "account_id_set": True,
        },
    }))
    rows = [
        row for row in spec
        if row["kind"] == "submenu"
        and ("Claude" in row["title"] or "Codex" in row["title"])
    ]
    assert len(rows) == 2
    claude = next(row for row in rows if "Claude" in row["title"])
    codex = next(row for row in rows if "Codex" in row["title"])
    assert "未配置" in claude["title"]
    assert "未启用" in codex["title"]
    assert "codex_bengalfox" in codex["title"]
    for row in rows:
        actions = [child.get("action") for child in row["children"]]
        assert "provider_settings" in actions
        assert not any(str(action).startswith("refresh_quota:") for action in actions)


def test_enabled_manual_quota_source_waiting_row_can_refresh_only_itself():
    spec = build_menu_spec(_snap(quota_source_config={
        "claude": {
            "enabled": True,
            "key_set": True,
            "model": "sonnet",
            "account_id_set": False,
        },
    }))
    row = next(n for n in spec if n["kind"] == "submenu" and "Claude" in n["title"])
    assert "等待刷新" in row["title"]
    assert "sonnet" in row["title"]
    actions = [child.get("action") for child in row["children"]]
    assert "refresh_quota:claude" in actions
    assert "refresh_quota:codex" not in actions


def _mobile_children(spec):
    sub = next(n for n in spec if n["kind"] == "submenu" and "手机访问" in n["title"])
    return sub, [c["action"] for c in sub["children"] if c["kind"] == "action"]


def test_mobile_submenu_tunnel_off():
    spec = build_menu_spec(_snap(tunnel={"state": "off", "installed": True}))
    sub, actions = _mobile_children(spec)
    assert "mobile_qr" in actions and "tunnel_start" in actions
    assert "tunnel_stop" not in actions


def test_mobile_submenu_tunnel_up():
    spec = build_menu_spec(_snap(tunnel={
        "state": "up", "url": "https://x.trycloudflare.com", "installed": True}))
    sub, actions = _mobile_children(spec)
    assert "tunnel_qr" in actions and "tunnel_stop" in actions
    assert "tunnel_start" not in actions
    assert "公网已开通" in sub["title"]


def test_mobile_submenu_cloudflared_missing():
    spec = build_menu_spec(_snap(tunnel={"state": "off", "installed": False}))
    sub, actions = _mobile_children(spec)
    assert "tunnel_start" not in actions
    titles = " ".join(c["title"] for c in sub["children"])
    assert "brew install cloudflared" in titles


def test_title_shows_usage_percent_when_fresh():
    now = time.time()
    snap = _snap(status="running", running_titles=["a", "b"], quota={
        "claude": {"state": "ok",
                   "windows": [{"label": "5h", "used_percent": 37.4, "resets_at": None}],
                   "source": "usage_api", "fetched_at": now, "plan": None,
                   "detail": "", "error": None}})
    t = build_title(snap)
    assert "37%" in t and "2" in t


def test_title_has_no_glyphs_single_icon():
    """状态字符（◇/◆/◐/Ⅱ）全部移入环形图标，标题只留文字——否则像两个图标。"""
    for status, running in (("idle", []), ("running", ["a"]),
                            ("waiting", []), ("paused", [])):
        t = build_title(_snap(status=status, running_titles=running))
        assert not set(t) & set("◇◆◐Ⅱ▶"), f"{status} 标题混入图形字符: {t!r}"
    assert build_title(_snap()) == ""              # 空闲无数据 → 纯图标
    assert build_title(_snap(running_titles=["a"])) == ""  # 单任务运行 → 图标中心点表达


def test_title_hides_stale_usage():
    snap = _snap(quota={
        "claude": {"state": "ok",
                   "windows": [{"label": "5h", "used_percent": 37.4, "resets_at": None}],
                   "source": "usage_api", "fetched_at": time.time() - 3600,
                   "plan": None, "detail": "", "error": None}})
    assert "%" not in build_title(snap)


def _qi(percent=None, state="ok", fetched_at=None):
    windows = [] if percent is None else [
        {"label": "5h", "used_percent": percent, "resets_at": None}]
    return {"state": state, "windows": windows, "fetched_at": fetched_at,
            "source": "usage_api", "plan": None, "detail": "", "error": None}


def test_ring_progress_fresh_usage_both_tools():
    now = time.time()
    outer, inner = build_ring_progress(_snap(quota={
        "claude": _qi(37.0, fetched_at=now),
        "codex": _qi(80.5, fetched_at=now),
    }))
    assert outer == pytest.approx(0.37)
    assert inner == pytest.approx(0.805)


def test_ring_progress_stale_or_missing_is_none():
    outer, inner = build_ring_progress(_snap(quota={
        "claude": _qi(37.0, fetched_at=time.time() - 3600),  # 过期 → 不显示
    }))
    assert outer is None and inner is None
    assert build_ring_progress(_snap()) == (None, None)


def test_ring_progress_limited_without_windows_is_full():
    """observed 限流但没有 usage 窗口数据 → 画满环表示已打满。"""
    outer, inner = build_ring_progress(_snap(quota={
        "codex": _qi(None, state="limited"),
    }))
    assert outer is None and inner == 1.0


def test_ring_progress_clamped():
    now = time.time()
    outer, _ = build_ring_progress(_snap(quota={"claude": _qi(120.0, fetched_at=now)}))
    assert outer == 1.0


# ---------- corp provider 展示（credits/token 单位 + 自定义标题 provider） ----------


def _corp_snapshot(**title):
    now = __import__("time").time()
    return {
        "status": "idle", "queued": 0, "waiting_quota": 0, "running_titles": [],
        "tasks": [], "tunnel": {},
        "quota": {
            "mytoken": {
                "state": "ok", "source": "mytoken_api", "fetched_at": now,
                "windows": [{"label": "本月", "used_percent": 25.0,
                             "resets_at": now + 86400, "used": 250, "total": 1000,
                             "unit": "credits"}],
            },
        },
        **title,
    }


def test_menu_shows_corp_provider_credits():
    rows = build_menu_spec(_corp_snapshot())
    titles = [r["title"] for r in rows]
    assert any("MyToken" in t and "credits" in t for t in titles)
    mytoken = next(r for r in rows if r["kind"] == "submenu" and "MyToken" in r["title"])
    assert any(child.get("action") == "provider_settings" for child in mytoken["children"])


def test_title_provider_selectable():
    snap = _corp_snapshot(title_provider="mytoken")
    assert build_title(snap) == "25%"
    # 默认（claude）无数据 → 空标题
    assert build_title(_corp_snapshot()) == ""
