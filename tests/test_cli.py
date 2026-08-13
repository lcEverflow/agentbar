import json
import sys
from types import SimpleNamespace
import time

import pytest

from agentbar import cli


def test_cmd_open_uses_fragment_without_printing_token(monkeypatch, capsys):
    secret = "audit-super-secret-token"
    opened = []
    monkeypatch.setattr(cli, "_endpoint", lambda _settings: ("http://127.0.0.1:8737", secret))
    monkeypatch.setattr(cli, "_instance_alive", lambda _settings: True)
    monkeypatch.setattr(cli, "open_panel_url", lambda url: opened.append(url) or True)

    assert cli.cmd_open(SimpleNamespace(), SimpleNamespace()) == 0
    assert opened == [f"http://127.0.0.1:8737/#token={secret}"]
    output = capsys.readouterr().out
    assert secret not in output
    assert "?token=" not in output


def test_cmd_open_browser_failure_does_not_suggest_copying_unauthenticated_url(
    monkeypatch, capsys,
):
    secret = "audit-super-secret-token"
    monkeypatch.setattr(cli, "_endpoint", lambda _settings: ("http://127.0.0.1:8737", secret))
    monkeypatch.setattr(cli, "_instance_alive", lambda _settings: True)
    monkeypatch.setattr(cli, "open_panel_url", lambda _url: False)

    assert cli.cmd_open(SimpleNamespace(), SimpleNamespace()) == 1
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert "未在终端输出完整链接" in output.err
    assert "复制上面的链接" not in output.err


def test_cli_client_does_not_migrate_token_of_live_legacy_server(
    monkeypatch, tmp_path,
):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    legacy = {"lan_access": True, "token": "legacy-running-token-123456"}
    config_path = state_dir / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")
    seen = []

    def fake_status(_args, settings):
        seen.append((settings.token, settings.lan_access))
        return 0

    monkeypatch.setattr(cli, "cmd_status", fake_status)
    with pytest.raises(SystemExit, match="0"):
        cli.main(["--state-dir", str(state_dir), "status"])

    assert seen == [(legacy["token"], True)]
    assert json.loads(config_path.read_text(encoding="utf-8")) == legacy


def test_cli_run_checks_live_legacy_server_before_secure_migration(
    monkeypatch, tmp_path,
):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    legacy = {"lan_access": True, "token": "legacy-running-token-123456"}
    config_path = state_dir / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")
    seen = []
    monkeypatch.setattr(
        cli,
        "_instance_alive",
        lambda settings: seen.append(settings.token) or True,
    )
    monkeypatch.setattr(
        cli,
        "cmd_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not start or migrate while legacy server is alive")
        ),
    )

    with pytest.raises(SystemExit, match="2"):
        cli.main(["--state-dir", str(state_dir), "run", "--headless"])

    assert seen == [legacy["token"]]
    assert json.loads(config_path.read_text(encoding="utf-8")) == legacy


def test_cli_run_migrates_only_after_provisional_instance_check(
    monkeypatch, tmp_path,
):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    legacy = {"lan_access": True, "token": "legacy-running-token-123456"}
    (state_dir / "config.json").write_text(
        json.dumps(legacy), encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(
        cli,
        "_instance_alive",
        lambda settings: calls.append(("check", settings.token)) or False,
    )

    def fake_run(_args, settings, *, instance_checked=False):
        calls.append(("run", settings.token, settings.lan_access, instance_checked))
        return 0

    monkeypatch.setattr(cli, "cmd_run", fake_run)
    with pytest.raises(SystemExit, match="0"):
        cli.main(["--state-dir", str(state_dir), "run", "--headless"])

    assert calls[0] == ("check", legacy["token"])
    assert calls[1][0] == "run"
    assert calls[1][1] != legacy["token"]
    assert calls[1][2:] == (False, True)


def test_cmd_run_startup_output_never_requests_tokenized_url(monkeypatch, tmp_path, capsys):
    calls = []
    shutdown_order = []

    class FakeEvent:
        def set(self):
            pass

        def is_set(self):
            return False

        def wait(self, _timeout=None):
            return True

    class FakeStore:
        def __init__(self, _state_dir):
            pass

        def write_runtime(self, _port):
            pass

        def clear_runtime(self):
            shutdown_order.append("runtime")

    class FakeCore:
        def __init__(self, _settings, _store):
            pass

        def start(self):
            pass

        def shutdown(self):
            shutdown_order.append("core")

    class FakeServer:
        port = 8737

        def __init__(self, _core, _settings):
            pass

        def start(self):
            pass

        def stop(self):
            shutdown_order.append("server")

        def url(self, with_token=False):
            calls.append(with_token)
            return "http://127.0.0.1:8737/"

    monkeypatch.setattr(cli, "_instance_alive", lambda _settings: False)
    monkeypatch.setattr(cli, "_setup_logging", lambda _settings: None)
    monkeypatch.setattr(cli, "StateStore", FakeStore)
    monkeypatch.setattr(cli, "Scheduler", FakeCore)
    monkeypatch.setattr(cli, "ApiServer", FakeServer)
    monkeypatch.setattr(cli.threading, "Event", FakeEvent)
    monkeypatch.setattr(cli.signal, "signal", lambda *_args: None)

    settings = SimpleNamespace(state_dir=tmp_path, port=8737, token="must-not-print")
    args = SimpleNamespace(port=None, headless=True)
    assert cli.cmd_run(args, settings) == 0
    assert calls == [False]
    assert shutdown_order == ["server", "core", "runtime"]
    assert "must-not-print" not in capsys.readouterr().out


@pytest.mark.parametrize("failure_stage", ["core", "runtime"])
def test_cmd_run_startup_failure_rolls_back_all_components(
    monkeypatch, tmp_path, failure_stage,
):
    order = []

    class FakeStore:
        def __init__(self, _state_dir):
            pass

        def write_runtime(self, _port):
            order.append("runtime-write")
            if failure_stage == "runtime":
                raise OSError("runtime write failed")

        def clear_runtime(self):
            order.append("runtime-clear")

    class FakeCore:
        def __init__(self, _settings, _store):
            pass

        def start(self):
            order.append("core-start")
            if failure_stage == "core":
                raise RuntimeError("core start failed")

        def shutdown(self):
            order.append("core-shutdown")

    class FakeServer:
        port = 8737

        def __init__(self, _core, _settings):
            pass

        def start(self):
            order.append("server-start")

        def stop(self):
            order.append("server-stop")

    monkeypatch.setattr(cli, "_instance_alive", lambda _settings: False)
    monkeypatch.setattr(cli, "_setup_logging", lambda _settings: None)
    monkeypatch.setattr(cli, "StateStore", FakeStore)
    monkeypatch.setattr(cli, "Scheduler", FakeCore)
    monkeypatch.setattr(cli, "ApiServer", FakeServer)
    monkeypatch.setattr(cli.signal, "signal", lambda *_args: None)

    settings = SimpleNamespace(state_dir=tmp_path, port=8737, token="secret")
    expected = RuntimeError if failure_stage == "core" else OSError
    with pytest.raises(expected):
        cli.cmd_run(SimpleNamespace(port=None, headless=True), settings)

    prefix = ["server-start", "core-start"]
    if failure_stage == "runtime":
        prefix.append("runtime-write")
    assert order == prefix + ["server-stop", "core-shutdown", "runtime-clear"]


def test_signal_during_startup_rolls_back_before_entering_event_loop(
    monkeypatch, tmp_path,
):
    order = []
    handlers = {}

    class FakeStore:
        def __init__(self, _state_dir):
            pass

        def write_runtime(self, _port):
            order.append("runtime-write")

        def clear_runtime(self):
            order.append("runtime-clear")

    class FakeCore:
        def __init__(self, _settings, _store):
            pass

        def start(self):
            order.append("core-start")
            handlers[cli.signal.SIGTERM]()

        def shutdown(self):
            order.append("core-shutdown")

    class FakeServer:
        port = 8737

        def __init__(self, _core, _settings):
            pass

        def start(self):
            order.append("server-start")

        def stop(self):
            order.append("server-stop")

    monkeypatch.setattr(cli, "_instance_alive", lambda _settings: False)
    monkeypatch.setattr(cli, "_setup_logging", lambda _settings: None)
    monkeypatch.setattr(cli, "StateStore", FakeStore)
    monkeypatch.setattr(cli, "Scheduler", FakeCore)
    monkeypatch.setattr(cli, "ApiServer", FakeServer)
    monkeypatch.setattr(
        cli.signal,
        "signal",
        lambda number, callback: handlers.__setitem__(number, callback),
    )

    settings = SimpleNamespace(state_dir=tmp_path, port=8737, token="secret")
    result = cli.cmd_run(SimpleNamespace(port=None, headless=True), settings)

    assert result == 0
    assert order == [
        "server-start", "core-start", "runtime-write",
        "server-stop", "core-shutdown", "runtime-clear",
    ]


@pytest.mark.parametrize("failed", ["server", "tunnel", "logins", "core", "runtime"])
def test_cleanup_runtime_runs_every_stage_when_one_fails(failed):
    order = []

    def action(name):
        def run():
            order.append(name)
            if name == failed:
                raise OSError(f"{name} failed")
        return run

    core = SimpleNamespace(shutdown=action("core"))
    server = SimpleNamespace(stop=action("server"))
    store = SimpleNamespace(clear_runtime=action("runtime"))
    app = SimpleNamespace(
        tunnel=SimpleNamespace(stop=action("tunnel")),
        close_active_provider_logins=action("logins"),
    )

    with pytest.raises(OSError, match=f"{failed} failed"):
        cli._cleanup_runtime(core, server, store, app)

    assert order == ["server", "tunnel", "logins", "core", "runtime"]


def test_menu_signal_shutdown_reclaims_tunnel_and_active_logins(
    monkeypatch, tmp_path, capsys,
):
    order = []
    handlers = {}

    class FakeStore:
        def __init__(self, _state_dir):
            pass

        def write_runtime(self, _port):
            pass

        def clear_runtime(self):
            order.append("runtime")

    class FakeCore:
        def __init__(self, _settings, store):
            self.store = store

        def start(self):
            pass

        def shutdown(self):
            order.append("core")

    class FakeServer:
        port = 8737

        def __init__(self, _core, _settings):
            self.hooks = {}

        def start(self):
            pass

        def stop(self):
            order.append("server")

        def url(self, with_token=False):
            return "http://127.0.0.1:8737/"

    class FakeApp:
        def __init__(self, _core, _settings, _server):
            self.stopped = False
            self.tunnel = SimpleNamespace(stop=lambda: order.append("tunnel"))

        def dispatch_async(self, _action):
            pass

        def close_active_provider_logins(self):
            order.append("logins")

        def stop_from_thread(self):
            order.append("app")
            self.stopped = True

        def run(self):
            handlers[cli.signal.SIGTERM]()
            deadline = time.monotonic() + 2
            while not self.stopped and time.monotonic() < deadline:
                time.sleep(0.01)
            assert self.stopped

    monkeypatch.setattr(cli, "_instance_alive", lambda _settings: False)
    monkeypatch.setattr(cli, "_setup_logging", lambda _settings: None)
    monkeypatch.setattr(cli, "StateStore", FakeStore)
    monkeypatch.setattr(cli, "Scheduler", FakeCore)
    monkeypatch.setattr(cli, "ApiServer", FakeServer)
    monkeypatch.setattr(
        cli.signal, "signal", lambda signal_number, callback: handlers.__setitem__(
            signal_number, callback
        )
    )
    # Linux CI intentionally has no PyObjC. Inject the delayed macOS frontend
    # module so this cross-platform lifecycle test exercises cmd_run without
    # importing the real AppKit implementation.
    monkeypatch.setitem(
        sys.modules, "agentbar.menubar", SimpleNamespace(AgentBarApp=FakeApp)
    )

    settings = SimpleNamespace(state_dir=tmp_path, port=8737, token="secret")
    assert cli.cmd_run(SimpleNamespace(port=None, headless=False), settings) == 0
    one_cleanup = ["server", "tunnel", "logins", "core", "runtime"]
    # The watcher owns signal cleanup; the event-loop finally defensively calls
    # the same idempotent component stops after AppKit returns.
    assert order == one_cleanup + ["app"] + one_cleanup
    assert "secret" not in capsys.readouterr().out
