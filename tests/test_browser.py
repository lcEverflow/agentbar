"""browser.py — GUI 打开路径必须是非阻塞的（菜单卡死事故的回归测试）。"""

import subprocess
import time

from agentbar import browser


class _FakeProc:
    pid = 12345

    def poll(self):
        return 0


def test_open_url_async_uses_popen_and_returns_fast(monkeypatch):
    calls = {}

    def fake_popen(argv, **kw):
        calls["argv"] = argv
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    start = time.monotonic()
    ok = browser.open_url_async("http://127.0.0.1:8737/?token=x")
    assert ok is True
    assert time.monotonic() - start < 0.05, "GUI 路径必须毫秒级返回"
    assert calls["argv"][0] == "/usr/bin/open"


def test_open_url_async_never_calls_webbrowser(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("GUI 路径禁止走 webbrowser/osascript")

    monkeypatch.setattr(browser.webbrowser, "open", boom)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakeProc())
    assert browser.open_url_async("http://127.0.0.1:1/") is True


def test_open_url_async_false_when_popen_fails(monkeypatch):
    def fail(*a, **k):
        raise OSError("no open binary")

    monkeypatch.setattr(subprocess, "Popen", fail)
    assert browser.open_url_async("http://127.0.0.1:1/") is False


def test_blocking_variant_falls_back_to_webbrowser(monkeypatch):
    class R:
        returncode = 1

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    called = {}
    monkeypatch.setattr(browser.webbrowser, "open",
                        lambda url, new=0: called.setdefault("url", url) or True)
    assert browser.open_panel_url("http://x/") is True
    assert called["url"] == "http://x/"
