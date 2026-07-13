"""browser.py — GUI 打开路径必须非阻塞，且 open 失败绝不静默（回归测试）。"""

import subprocess
import time

from agentbar import browser


class _FakeProc:
    def __init__(self, rc=0, err=b""):
        self.returncode = rc
        self._err = err

    def communicate(self, timeout=None):
        return b"", self._err


def _wait_for(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_open_url_async_returns_fast(monkeypatch):
    calls = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda argv, **kw: calls.setdefault("argv", argv) or _FakeProc())
    start = time.monotonic()
    ok = browser.open_url_async("http://127.0.0.1:8737/?token=x")
    assert ok is True
    assert time.monotonic() - start < 0.05, "GUI 路径必须毫秒级返回"
    assert calls["argv"][0] == "/usr/bin/open"


def test_open_url_async_reports_success(monkeypatch):
    results = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakeProc(rc=0))
    browser.open_url_async("http://x/", on_result=lambda u, rc, e: results.append(rc))
    assert _wait_for(lambda: results)
    assert results == [0]


def test_open_url_async_reports_failure(monkeypatch):
    results = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: _FakeProc(rc=1, err=b"LSOpenURLsWithRole failed"))
    browser.open_url_async("http://x/", on_result=lambda u, rc, e: results.append((rc, e)))
    assert _wait_for(lambda: results)
    rc, err = results[0]
    assert rc == 1 and "LSOpen" in err


def test_open_url_async_spawn_failure(monkeypatch):
    results = []

    def fail(*a, **k):
        raise OSError("no open binary")

    monkeypatch.setattr(subprocess, "Popen", fail)
    ok = browser.open_url_async("http://x/", on_result=lambda u, rc, e: results.append(rc))
    assert ok is False
    assert results == [-1]


def test_open_url_async_never_calls_webbrowser(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("GUI 路径禁止走 webbrowser/osascript")

    monkeypatch.setattr(browser.webbrowser, "open", boom)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakeProc())
    assert browser.open_url_async("http://127.0.0.1:1/") is True


def test_blocking_variant_falls_back_to_webbrowser(monkeypatch):
    class R:
        returncode = 1

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    called = {}
    monkeypatch.setattr(browser.webbrowser, "open",
                        lambda url, new=0: called.setdefault("url", url) or True)
    assert browser.open_panel_url("http://x/") is True
    assert called["url"] == "http://x/"
