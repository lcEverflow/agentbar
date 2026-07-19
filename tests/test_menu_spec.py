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
    spec = build_menu_spec(_snap(quota={
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
    assert "refresh_quota" in child_actions
    # codex 报错但没有 Keychain 字样 → 不出现授权入口
    codex = subs[1]
    child_actions = [c["action"] for c in codex["children"] if c["kind"] == "action"]
    assert "authorize_keychain" not in child_actions


def test_keychain_authorize_only_on_keychain_error():
    spec = build_menu_spec(_snap(quota={
        "claude": {"state": "unknown", "windows": [], "source": "none",
                   "fetched_at": None, "plan": None, "detail": "",
                   "error": "未读到 Claude 凭据（Keychain 静默读取被拒？菜单里可手动授权）"},
    }))
    sub = next(n for n in spec if n["kind"] == "submenu")
    child_actions = [c["action"] for c in sub["children"] if c["kind"] == "action"]
    assert "authorize_keychain" in child_actions


def test_core_actions_present():
    spec = build_menu_spec(_snap())
    actions = {n["action"] for n in spec if n["kind"] == "action"}
    assert {"open_panel", "quick_add", "quit"} <= actions
    assert "open_web_panel" not in actions


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
    assert "37%" in t and "◆2" in t


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
