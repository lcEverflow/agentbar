import threading
import time

from agentbar import processes


def test_discovers_external_and_managed_without_command_lines(monkeypatch):
    rows = {
        10: {"pid": 10, "ppid": 1, "state": "S", "elapsed": "00:20", "executable": "/usr/local/bin/node"},
        11: {"pid": 11, "ppid": 10, "state": "S", "elapsed": "00:19", "executable": "/opt/bin/codex"},
        20: {"pid": 20, "ppid": 1, "state": "R", "elapsed": "02:00", "executable": "/opt/bin/claude.exe"},
        30: {"pid": 30, "ppid": 1, "state": "S", "elapsed": "10:00", "executable": "/opt/bin/not-agent"},
    }
    monkeypatch.setattr(processes, "_processes", lambda: rows)
    found = processes.discover_cli_processes({10: {"tool": "codex", "task_id": "t1", "title": "my task", "cwd": "/work/10"}})
    assert found == [
        {"pid": 10, "tool": "codex", "kind": "managed", "task_id": "t1", "title": "my task", "state": "S", "elapsed": "00:20", "cwd": "/work/10"},
        {"pid": 20, "tool": "claude", "kind": "external", "task_id": None, "title": None, "state": "R", "elapsed": "02:00", "cwd": None},
    ]


def test_process_cache_refresh_is_nonblocking_and_single_flight(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []
    rows = {
        20: {
            "pid": 20,
            "ppid": 1,
            "state": "R",
            "elapsed": "00:01",
            "executable": "/opt/bin/claude",
        },
    }

    def blocking_scan():
        calls.append(True)
        entered.set()
        assert release.wait(2)
        return rows

    monkeypatch.setattr(processes, "_scan_processes", blocking_scan)
    with processes._cache_lock:
        processes._cache_at = 0.0
        processes._cache_processes = {}
        processes._scan_inflight = False

    started = time.monotonic()
    assert processes._processes() == {}
    assert time.monotonic() - started < 0.05
    assert entered.wait(1)

    # Repeated UI/Web snapshots return the same cache immediately and must not
    # launch a second ps process while the first worker is still blocked.
    started = time.monotonic()
    assert processes._processes() == {}
    assert time.monotonic() - started < 0.05
    assert calls == [True]

    release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with processes._cache_lock:
            if not processes._scan_inflight:
                break
        time.sleep(0.01)
    assert processes._processes() == rows
    assert calls == [True]
