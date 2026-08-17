"""menubar 动作层测试 — 核心断言：菜单动作绝不阻塞主线程。

AgentBarApp 构造函数不触碰 AppKit（run() 才会），所以可以用假 core/server
直接驱动 _dispatch。
"""

import os
import stat
import threading
import time
from importlib import resources
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from agentbar.browser import open_panel_url
from agentbar.menubar import AgentBarApp, _atomic_private_write, _token_fragment_url
from agentbar.panel_window import PanelWindowController





class _FakeQuota:
    def __init__(self):
        self.refreshed = []

    def refresh_now(self, tool=None):
        self.refreshed.append(tool)  # 真实实现只置 Event，同样即时


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
        return "http://127.0.0.1:8737/" + (f"?{q}" if q else "") + "#token=abc"

    def mobile_url(self):
        # Deliberately emulate a legacy server URL; menubar must normalize it.
        return "http://192.0.2.1:8737/m?token=legacy"

    def allow_host(self, host):
        pass

    def disallow_host(self, host):
        pass


def _app():
    settings = SimpleNamespace(token="secret token")
    return AgentBarApp(_FakeCore(), settings=settings, server=_FakeServer())


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


def test_refresh_quota_dispatch_is_source_scoped():
    app = _app()
    app._dispatch("refresh_quota")
    app._dispatch("refresh_quota:")
    app._dispatch("refresh_quota:codex")
    assert app.core.quota.refreshed == ["codex"]


def test_token_fragment_url_removes_query_secret_and_preserves_intent():
    url = _token_fragment_url(
        "http://127.0.0.1:8737/m?token=old&focus=prompt#tool=codex",
        "new secret",
    )
    parts = urlsplit(url)
    assert parse_qs(parts.query) == {"focus": ["prompt"]}
    assert parse_qs(parts.fragment) == {
        "tool": ["codex"],
        "token": ["new secret"],
    }
    assert "token=" not in parts.query


def test_mobile_qr_normalizes_bootstrap_token_to_fragment(monkeypatch):
    app = _app()
    shown = []
    monkeypatch.setattr(
        AgentBarApp,
        "_show_qr_window",
        lambda self, url, mode, note: shown.append((url, mode, note)),
    )
    app._show_mobile_qr()
    parts = urlsplit(shown[0][0])
    assert "token=" not in parts.query
    assert parse_qs(parts.fragment) == {"token": ["secret token"]}


def test_tunnel_qr_uses_fragment_token(monkeypatch):
    app = _app()
    app.tunnel = SimpleNamespace(url="https://example.trycloudflare.com")
    shown = []
    monkeypatch.setattr(
        AgentBarApp,
        "_show_qr_window",
        lambda self, url, mode, note: shown.append((url, mode, note)),
    )
    app._show_tunnel_qr()
    parts = urlsplit(shown[0][0])
    assert parts.path == "/m"
    assert not parts.query
    assert parse_qs(parts.fragment) == {"token": ["secret token"]}


def test_debug_dump_write_is_atomic_and_private(tmp_path, monkeypatch):
    target = tmp_path / "menu-debug.json"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o644)
    replacements = []
    real_replace = os.replace

    def observe_replace(source, destination):
        source = Path(source)
        replacements.append((source.parent, source.read_text("utf-8"),
                             stat.S_IMODE(source.stat().st_mode)))
        real_replace(source, destination)

    monkeypatch.setattr("agentbar.menubar.os.replace", observe_replace)
    _atomic_private_write(target, '{"task":"private"}')

    assert replacements == [(tmp_path, '{"task":"private"}', 0o600)]
    assert target.read_text("utf-8") == '{"task":"private"}'
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(tmp_path.glob(".*.tmp")) == []


def test_menu_debug_fsync_is_off_main_thread_and_rapid_updates_coalesce(
    tmp_path, monkeypatch,
):
    app = _app()
    app.settings.state_dir = tmp_path
    app._item = SimpleNamespace(
        button=lambda: SimpleNamespace(title=lambda: "AgentBar")
    )
    app._menu = object()
    app._dump = lambda _menu: []
    entered = threading.Event()
    release = threading.Event()
    writes = []

    def blocked_write(_path, text):
        entered.set()
        assert release.wait(2)
        writes.append(text)

    monkeypatch.setattr("agentbar.menubar._atomic_private_write", blocked_write)

    start = time.monotonic()
    app._write_debug_dump()
    assert time.monotonic() - start < 0.05
    assert entered.wait(1)
    app._write_debug_dump()
    app._write_debug_dump()
    release.set()
    deadline = time.time() + 2
    while app._debug_worker_running and time.time() < deadline:
        time.sleep(0.01)

    assert len(writes) == 2  # first in flight + one coalesced latest snapshot


def test_web_panel_uses_fragment_and_session_storage_for_token_bootstrap():
    html = resources.files("agentbar").joinpath("web/index.html").read_text("utf-8")
    assert "new URLSearchParams(u.hash" in html
    assert 'sessionStorage.setItem("agentbar_token"' in html
    assert 'sessionStorage.getItem("agentbar_token"' in html
    assert 'localStorage.removeItem("agentbar_token"' in html
    assert 'localStorage.setItem("agentbar_token"' not in html
    assert ".api_key" not in html
    assert "access_token" not in html
    assert "account_id" not in html
    assert "自动" in html
    assert 'id="btnRefreshQuota"' not in html
    assert "refreshQuotaSource('${name}')" in html
    # Clearing one cookie must not serialize unrelated unsaved form controls.
    clear_body = html.split("window.clearProviderCookie", 1)[1].split(
        "window.refreshQuotaSource", 1
    )[0]
    assert "providerPayload(" not in clear_body


def test_provider_settings_dispatch_opens_native_window(monkeypatch):
    app = _app()
    shown = []
    monkeypatch.setattr(
        AgentBarApp, "_show_provider_settings", lambda self: shown.append(True)
    )
    start = time.monotonic()
    app._dispatch("provider_settings")
    assert time.monotonic() - start < 0.05
    assert shown == [True]


def test_pause_resume_dispatch():
    app = _app()
    app._dispatch("pause_all")
    app._dispatch("resume_all")
    assert app.core.paused_calls == ["pause", "resume"]


def test_queued_dispatch_is_dropped_after_shutdown_begins(monkeypatch):
    app = _app()
    queued = []
    started = []
    monkeypatch.setattr("agentbar.menubar.AppHelper.callAfter", lambda fn, *args: queued.append((fn, args)))
    app._start_tunnel_bg = lambda: started.append(True)

    app.dispatch_async("tunnel_start")
    assert len(queued) == 1
    app.begin_shutdown()
    fn, args = queued.pop()
    fn(*args)

    assert started == []


def test_menu_quit_stops_admission_before_scheduler_shutdown():
    app = _app()
    order = []
    app.tunnel = SimpleNamespace(stop=lambda: order.append("tunnel"))
    app._provider_panel = SimpleNamespace(
        close_active_logins=lambda: order.append("logins")
    )
    app.server = SimpleNamespace(stop=lambda: order.append("server"))
    app.core = SimpleNamespace(
        shutdown=lambda: order.append("core"),
        store=SimpleNamespace(clear_runtime=lambda: order.append("runtime")),
    )
    app._terminate = lambda: order.append("terminate")
    app.begin_shutdown = lambda: order.append("admission")

    app._quit()

    assert order == [
        "admission", "server", "tunnel", "logins", "core", "runtime", "terminate",
    ]


def test_menu_quit_runs_later_cleanup_after_scheduler_failure():
    app = _app()
    order = []
    app.server = SimpleNamespace(stop=lambda: order.append("server"))
    app.tunnel = SimpleNamespace(stop=lambda: order.append("tunnel"))
    app._provider_panel = None

    def fail_scheduler():
        order.append("core")
        raise OSError("disk full")

    app.core = SimpleNamespace(
        shutdown=fail_scheduler,
        store=SimpleNamespace(clear_runtime=lambda: order.append("runtime")),
    )
    app._terminate = lambda: order.append("terminate")

    try:
        app._quit()
    except OSError as exc:
        assert "disk full" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("scheduler failure must still be reported")

    assert order == ["server", "tunnel", "core", "runtime", "terminate"]


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
    """双环图标离屏渲染冒烟：18pt 模板图，各进度/状态组合都不能抛异常。"""
    from agentbar.menubar import _ring_icon

    for outer, inner in ((0.37, 0.8), (None, None), (1.0, 0.0), (1.5, None)):
        for status in ("idle", "running", "waiting", "paused"):
            img = _ring_icon(outer, inner, status)
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
    assert open_panel_url("http://127.0.0.1:8737/#token=abc") is True
    assert captured["argv"] == ["/usr/bin/open", "http://127.0.0.1:8737/#token=abc"]
    assert captured["kwargs"]["timeout"] == 8


def test_transcript_refresh_is_nonblocking_single_flight_and_drops_stale_result(
    tmp_path, monkeypatch,
):
    old_path = tmp_path / "old.jsonl"
    new_path = tmp_path / "new.jsonl"
    old_path.write_text("old", encoding="utf-8")
    new_path.write_text("new", encoding="utf-8")
    entered = threading.Event()
    release = threading.Event()
    parse_calls = []
    callbacks = []

    def find_session_file(_tool, _cwd, sid):
        return old_path if sid == "old-session" else new_path

    def parse_transcript(_tool, path):
        parse_calls.append(path.name)
        if path == old_path:
            entered.set()
            assert release.wait(3)
        return f"render:{path.stem}"

    monkeypatch.setattr(
        "agentbar.transcript.find_session_file", find_session_file,
    )
    monkeypatch.setattr("agentbar.transcript.parse_transcript", parse_transcript)
    monkeypatch.setattr(
        "agentbar.panel_window.AppHelper.callAfter",
        lambda fn, *args: callbacks.append((fn, args)),
    )

    class View:
        def __init__(self):
            self.values = []

        def setString_(self, value):
            self.values.append((value, threading.current_thread()))

    live = SimpleNamespace(session_id="old-session")
    view = View()
    controller = PanelWindowController.alloc().init()
    controller.core = SimpleNamespace(
        _lock=threading.RLock(), _tasks={"task-1": live},
    )
    controller._transcript_windows = {
        "task-1": SimpleNamespace(isVisible=lambda: True),
    }
    controller._transcript_meta = {
        "task-1": {
            "tool": "codex",
            "cwd": str(tmp_path),
            "sid": "old-session",
            "wkview": view,
            "_generation": 1,
            "_identity": ("codex", str(tmp_path), "old-session"),
        },
    }
    controller._transcript_workers = {}
    controller._transcript_pending = set()
    controller._transcript_generation = {"task-1": 1}

    started = time.monotonic()
    controller._refresh_transcript_window("task-1")
    assert time.monotonic() - started < 0.05
    assert entered.wait(2)

    # A timer tick and a session switch while parsing must neither block nor
    # launch a second parser. The old result is discarded by generation.
    controller._refresh_transcript_window("task-1")
    live.session_id = "new-session"
    started = time.monotonic()
    controller._refresh_transcript_window("task-1")
    assert time.monotonic() - started < 0.05
    assert parse_calls == ["old.jsonl"]

    release.set()
    deadline = time.monotonic() + 3
    while not callbacks and time.monotonic() < deadline:
        time.sleep(0.01)
    assert callbacks
    fn, args = callbacks.pop(0)
    fn(*args)
    assert view.values == []

    deadline = time.monotonic() + 3
    while not callbacks and time.monotonic() < deadline:
        time.sleep(0.01)
    assert callbacks
    fn, args = callbacks.pop(0)
    fn(*args)

    assert parse_calls == ["old.jsonl", "new.jsonl"]
    assert view.values == [("render:new", threading.current_thread())]
    assert controller._transcript_workers == {}


def test_native_panel_mutation_disk_failures_are_alerted_without_ui_reset():
    alerts = []
    refreshed = []

    def disk_full(*_args, **_kwargs):
        raise OSError("disk full secret detail")

    def unexpected_clear(_value):
        raise AssertionError("failed add cleared prompt")

    panel = PanelWindowController.alloc().init()
    panel.prompt_view = SimpleNamespace(
            string=lambda: "keep this prompt",
            setString_=unexpected_clear,
    )
    panel.effort_popup = SimpleNamespace(indexOfSelectedItem=lambda: 0)
    panel.profile_popup = SimpleNamespace(
        titleOfSelectedItem=lambda: "✏️ 可编辑（默认）",
    )
    panel.schedule_check = SimpleNamespace(state=lambda: 0)
    panel.cwd_field = SimpleNamespace(stringValue=lambda: "/tmp")
    panel.model_combo = SimpleNamespace(stringValue=lambda: "")
    panel._current_efforts = []
    panel._tools = [{"name": "codex"}]
    panel.tool_popup = SimpleNamespace(indexOfSelectedItem=lambda: 0)
    panel._alert = lambda title, text: alerts.append((title, text))
    panel.refresh = lambda: refreshed.append(True)

    panel.core = SimpleNamespace(add_task=disk_full)
    panel.onAdd_(None)

    panel.window = object()
    panel.table = SimpleNamespace(selectedRow=lambda: 0)
    panel._rows = [{"id": "task-1"}]
    panel.core = SimpleNamespace(act=disk_full)
    panel._act_selected("cancel")

    panel.core = SimpleNamespace(paused=False, pause_all=disk_full)
    panel.onTogglePause_(None)

    assert len(alerts) == 3
    assert all("本地状态保存失败" in text for _, text in alerts)
    assert all("secret detail" not in text for _, text in alerts)
    assert refreshed == []
