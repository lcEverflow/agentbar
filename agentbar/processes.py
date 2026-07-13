"""Read-only discovery of locally running Claude/Codex CLI processes.

This deliberately reads *only* process metadata (PID, executable, elapsed time
and state). Prompts, complete command lines and external process cwd frequently
contain sensitive project context, so AgentBar never collects or renders them.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

_CACHE_SECONDS = 3.0
_cache_lock = threading.Lock()
_cache_at = 0.0
_cache_processes: dict[int, dict] = {}


def _tool_for_executable(executable: str) -> str | None:
    name = Path(executable).name.lower()
    if name in {"claude", "claude.exe"}:
        return "claude"
    if name == "codex":
        return "codex"
    return None


def _scan_processes() -> dict[int, dict]:
    """Use `comm`, not `command`, so no command arguments/prompt are read."""
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,stat=,etime=,comm="],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    rows: dict[int, dict] = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=4)
        if len(fields) != 5:
            continue
        try:
            pid, ppid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        rows[pid] = {
            "pid": pid,
            "ppid": ppid,
            "state": fields[2],
            "elapsed": fields[3],
            "executable": fields[4],
        }
    return rows


def _processes() -> dict[int, dict]:
    global _cache_at, _cache_processes
    now = time.monotonic()
    with _cache_lock:
        if now - _cache_at >= _CACHE_SECONDS:
            _cache_processes = _scan_processes()
            _cache_at = now
        return dict(_cache_processes)


def _owner_for(pid: int, rows: dict[int, dict], owned: dict[int, dict]) -> dict | None:
    """Find an AgentBar-owned ancestor for a wrapper/child process."""
    seen: set[int] = set()
    while pid and pid not in seen:
        seen.add(pid)
        if pid in owned:
            return owned[pid]
        row = rows.get(pid)
        if not row:
            return None
        pid = row["ppid"]
    return None


def discover_cli_processes(owned: dict[int, dict] | None = None) -> list[dict]:
    """Return running CLI processes, marking scheduler-owned tasks when known.

    The caller's `owned` map is `{pid: {tool, task_id, title}}`. External
    processes are informational only; AgentBar never sends them signals.
    """
    owned = owned or {}
    rows = _processes()
    result: list[dict] = []

    # An invoked CLI can be a node wrapper, so include direct Popen PIDs even
    # when their executable name is not literally claude/codex.
    for pid, meta in owned.items():
        row = rows.get(pid, {})
        result.append({
            "pid": pid,
            "tool": meta["tool"],
            "kind": "managed",
            "task_id": meta["task_id"],
            "title": meta["title"],
            "state": row.get("state", "?"),
            "elapsed": row.get("elapsed", "?"),
            "cwd": meta.get("cwd"),
        })

    for pid, row in rows.items():
        tool = _tool_for_executable(row["executable"])
        if not tool or pid in owned:
            continue
        # Child binaries of a managed wrapper were already shown above.
        if _owner_for(pid, rows, owned):
            continue
        result.append({
            "pid": pid,
            "tool": tool,
            "kind": "external",
            "task_id": None,
            "title": None,
            "state": row["state"],
            "elapsed": row["elapsed"],
            "cwd": None,
        })
    return sorted(result, key=lambda p: (p["kind"] != "managed", p["tool"], p["pid"]))
