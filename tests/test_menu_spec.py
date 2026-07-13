"""菜单 spec 纯函数测试 — 不需要 AppKit，保证"没有点不动的行"。"""

import time

from agentbar.menu_spec import build_menu_spec, build_title

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


def test_every_row_is_actionable():
    spec = build_menu_spec(_snap(
        running_titles=["任务A"],
        quota={"claude": {"state": "ok", "windows": [
            {"label": "5h", "used_percent": 37.0, "resets_at": time.time() + 3600}],
            "source": "usage_api", "fetched_at": time.time(), "detail": "", "error": None,
            "plan": "max"}},
    ))
    for leaf in _leaves(spec):
        assert leaf["action"], f"死行: {leaf['title']}"
        assert leaf["enabled"] is True


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
    subs = [n for n in spec if n["kind"] == "submenu"]
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
