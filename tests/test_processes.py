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
