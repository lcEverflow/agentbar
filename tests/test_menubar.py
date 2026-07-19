"""menubar 动作层测试 — 核心断言：菜单动作绝不阻塞主线程。

AgentBarApp 构造函数不触碰 AppKit（run() 才会），所以可以用假 core/server
直接驱动 _dispatch。
"""

import time

from agentbar.browser import open_panel_url
from agentbar.menubar import AgentBarApp





class _FakeQuota:
    def __init__(self):
        self.refreshed = 0

    def refresh_now(self):
        self.refreshed += 1  # 真实实现只置 Event，同样即时


class _FakeCore:
    def __init__(self):
        self.quota = _FakeQuota()
        self.paused_calls = []

    def snapshot(self):
        return {"status": "idle", "paused": False, "running_titles": [],
                "queued": 0, "waiting_quota": 0, "tasks": [], "quota": {}}

    def pause_all(self):
        self.paused_calls.append("pause")

    def resume_all(self):
        self.paused_calls.append("resume")


class _FakeServer:
    port = 8737

    def __init__(self):
        self.hooks = {}

    def url(self, with_token=False, **query):
        q = "&".join(f"{k}={v}" for k, v in sorted(query.items()))
        return f"http://127.0.0.1:8737/?token=abc" + (f"&{q}" if q else "")

    def allow_host(self, host):
        pass

    def disallow_host(self, host):
        pass


def _app():
    return AgentBarApp(_FakeCore(), settings=None, server=_FakeServer())


def test_open_panel_opens_native_window(monkeypatch):
    """open_panel / quick_add 走原生窗口（不再依赖浏览器），且毫秒级返回。"""
    app = _app()
    shown = []
    monkeypatch.setattr(AgentBarApp, "_show_panel",
                        lambda self, focus: shown.append(focus))
    start = time.monotonic()
    app._dispatch("open_panel")
    app._dispatch("quick_add")
    assert time.monotonic() - start < 0.05, "菜单动作必须毫秒级返回"
    assert shown == [False, True]  # quick_add 聚焦 prompt 输入框


def test_refresh_quota_dispatch_only_sets_event():
    app = _app()
    app._dispatch("refresh_quota")
    assert app.core.quota.refreshed == 1


def test_pause_resume_dispatch():
    app = _app()
    app._dispatch("pause_all")
    app._dispatch("resume_all")
    assert app.core.paused_calls == ["pause", "resume"]


def test_main_menu_has_ctrl_and_cmd_shortcuts():
    """⌃W/⌘W 关窗、⌃Q/⌘Q 退出：主菜单里必须各挂两份键位，quit 走 bridge 完整清理。"""
    from AppKit import (
        NSApplication,
        NSEventModifierFlagCommand,
        NSEventModifierFlagControl,
    )
    from agentbar.menubar import _Bridge

    app = _app()
    app._nsapp = NSApplication.sharedApplication()
    app._bridge = _Bridge.alloc().initWithOwner_(app)
    app._install_main_menu()

    main = app._nsapp.mainMenu()
    found = []  # (title, key, mask, representedObject)
    for i in range(main.numberOfItems()):
        sub = main.itemAtIndex_(i).submenu()
        if sub is None:
            continue
        for j in range(sub.numberOfItems()):
            it = sub.itemAtIndex_(j)
            if str(it.keyEquivalent()) in ("w", "q"):
                found.append((str(it.keyEquivalent()),
                              int(it.keyEquivalentModifierMask()),
                              str(it.representedObject() or "")))
    def has(key, mask, rep=""):
        return any(k == key and m & mask and r == rep for k, m, r in found)

    assert has("w", NSEventModifierFlagCommand) and has("w", NSEventModifierFlagControl)
    assert has("q", NSEventModifierFlagCommand, "quit") and has("q", NSEventModifierFlagControl, "quit")


def test_ring_icon_renders_offscreen():
    """双环图标离屏渲染冒烟：18pt 模板图，None/数值进度都不能抛异常。"""
    from agentbar.menubar import _ring_icon

    for outer, inner in ((0.37, 0.8), (None, None), (1.0, 0.0), (1.5, None)):
        img = _ring_icon(outer, inner)
        assert img.isTemplate()
        assert img.size().width == 18.0 and img.size().height == 18.0


def test_cli_open_uses_macos_open_command(monkeypatch):
    captured = {}

    class Result:
        returncode = 0

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return Result()

    monkeypatch.setattr("agentbar.browser.subprocess.run", fake_run)
    assert open_panel_url("http://127.0.0.1:8737/?token=abc") is True
    assert captured["argv"] == ["/usr/bin/open", "http://127.0.0.1:8737/?token=abc"]
    assert captured["kwargs"]["timeout"] == 8
