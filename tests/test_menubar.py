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
    def url(self, with_token=False, **query):
        q = "&".join(f"{k}={v}" for k, v in sorted(query.items()))
        return f"http://127.0.0.1:8737/?token=abc" + (f"&{q}" if q else "")


def _app():
    return AgentBarApp(_FakeCore(), settings=None, server=_FakeServer())


def test_open_panel_dispatch_is_nonblocking(monkeypatch):
    app = _app()
    opened = []
    monkeypatch.setattr("agentbar.menubar.open_url_async",
                        lambda url: opened.append(url) or True)
    start = time.monotonic()
    app._dispatch("open_panel")
    assert time.monotonic() - start < 0.05, "菜单动作必须毫秒级返回"
    assert opened == ["http://127.0.0.1:8737/?token=abc"]


def test_quick_add_opens_editable_claude_panel(monkeypatch):
    app = _app()
    opened = []
    monkeypatch.setattr("agentbar.menubar.open_url_async",
                        lambda url: opened.append(url) or True)
    app._dispatch("quick_add")
    assert opened == ["http://127.0.0.1:8737/?token=abc&focus=prompt&tool=claude"]


def test_refresh_quota_dispatch_only_sets_event():
    app = _app()
    app._dispatch("refresh_quota")
    assert app.core.quota.refreshed == 1


def test_pause_resume_dispatch():
    app = _app()
    app._dispatch("pause_all")
    app._dispatch("resume_all")
    assert app.core.paused_calls == ["pause", "resume"]


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
