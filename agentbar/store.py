"""State persistence: atomic JSON writes + per-task log files + runtime info."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path


_PRIVATE_STATE_FILES = {
    "state.json",
    "runtime.json",
    "menu-debug.json",
    "agentbar.log",
    "launchd.stdout.log",
    "launchd.stderr.log",
}
_TASK_LOG_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\.log")
_CORRUPT_STATE_NAME = re.compile(r"state\.json\.corrupt-[0-9]+")


class StateStore:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.state_path = self.state_dir / "state.json"
        self.runtime_path = self.state_dir / "runtime.json"
        self.logs_dir = self.state_dir / "logs"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        for directory in (self.state_dir, self.logs_dir):
            try:
                os.chmod(directory, stat.S_IRWXU)
            except OSError:
                pass
        self._repair_existing_permissions()
        self._lock = threading.RLock()

    @staticmethod
    def _repair_private_file(path: Path) -> None:
        """Repair a known private regular file without following symlinks."""
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(path, flags)
        except OSError:
            return
        try:
            if stat.S_ISREG(os.fstat(fd).st_mode):
                os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _repair_existing_permissions(self) -> None:
        """Self-heal permissions left by older releases, preserving contents."""
        try:
            state_entries = tuple(self.state_dir.iterdir())
        except OSError:
            state_entries = ()
        for path in state_entries:
            if path.name in _PRIVATE_STATE_FILES or _CORRUPT_STATE_NAME.fullmatch(path.name):
                self._repair_private_file(path)

        try:
            log_entries = tuple(self.logs_dir.iterdir())
        except OSError:
            log_entries = ()
        for path in log_entries:
            if _TASK_LOG_NAME.fullmatch(path.name):
                self._repair_private_file(path)

    @staticmethod
    def _safe_task_id(task_id: str) -> str:
        if not isinstance(task_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", task_id,
        ):
            raise ValueError("invalid task id")
        return task_id

    @staticmethod
    def _write_private_json(path: Path, payload: dict) -> None:
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                json.dump(payload, handle, ensure_ascii=False, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            # mkstemp + fchmod already made the committed inode private. Keep
            # this repair best-effort so a post-commit metadata error cannot be
            # reported as an uncommitted write to transactional callers.
            try:
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            tmp = ""
        finally:
            if fd >= 0:
                os.close(fd)
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # ---------- state.json ----------

    def load(self) -> dict:
        with self._lock:
            if not self.state_path.exists():
                return {}
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = None
            if isinstance(data, dict):
                return data
            # Invalid JSON and valid non-object JSON are both unusable scheduler
            # states. Back either up, then boot empty instead of crashing in
            # Scheduler._load() on a missing dict interface.
            backup = self.state_path.with_name(
                f"state.json.corrupt-{time.time_ns()}"
            )
            try:
                os.replace(self.state_path, backup)
            except OSError:
                pass
            return {}

    def save(self, data: dict) -> None:
        with self._lock:
            self._write_private_json(self.state_path, data)

    # ---------- per-task logs ----------

    def log_path(self, task_id: str) -> Path:
        return self.logs_dir / f"{self._safe_task_id(task_id)}.log"

    def open_log_append(self, task_id: str):
        """Open a task transcript without ever creating a world-readable file."""
        path = self.log_path(task_id)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(
            path,
            flags,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("task log is not a regular file")
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            os.close(fd)
            raise
        return os.fdopen(fd, "ab")

    def _open_log_read(self, task_id: str):
        """Open an existing regular task log without following symlinks."""
        path = self.log_path(task_id)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("task log is not a regular file")
        except Exception:
            os.close(fd)
            raise
        return os.fdopen(fd, "rb")

    def read_log_head(self, task_id: str, max_bytes: int = 16384) -> str:
        """输出开头（codex 的 session id 打在 header，长输出会被挤出 tail）。"""
        try:
            with self._open_log_read(task_id) as f:
                return f.read(max_bytes).decode("utf-8", errors="replace")
        except OSError:
            return ""

    def read_log_tail(self, task_id: str, max_bytes: int = 65536) -> str:
        try:
            with self._open_log_read(task_id) as f:
                size = os.fstat(f.fileno()).st_size
                if size > max_bytes:
                    f.seek(size - max_bytes)
                data = f.read()
            return data.decode("utf-8", errors="replace")
        except OSError:
            return ""

    def delete_log(self, task_id: str) -> None:
        """Delete one retired task log without following a malicious symlink."""
        path = self.log_path(task_id)
        with self._lock:
            # unlink(2) removes the directory entry itself. If an attacker has
            # replaced it with a symlink, its target is never followed.
            path.unlink(missing_ok=True)

    # ---------- runtime.json (实际端口/PID，供 CLI 客户端发现) ----------

    def write_runtime(self, port: int) -> None:
        with self._lock:
            with self._runtime_file_lock():
                self._write_private_json(self.runtime_path, {
                    "port": port,
                    "pid": os.getpid(),
                    "started_at": time.time(),
                })

    def read_runtime(self) -> dict | None:
        with self._lock:
            if not self.runtime_path.exists():
                return None
            try:
                data = json.loads(self.runtime_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return None
            if not isinstance(data, dict):
                return None
            port = data.get("port")
            if isinstance(port, bool) or not isinstance(port, int):
                return None
            if not 1 <= port <= 65_535:
                return None
            return data

    def clear_runtime(self, owner_pid: int | None = None) -> None:
        """Delete only this instance's discovery record.

        During a restart, a replacement process may bind/write runtime.json while
        the old process is still finishing scheduler shutdown. The cross-process
        lock makes check+unlink atomic with writes, and the PID check prevents the
        old process from erasing its replacement's record.
        """
        expected_pid = os.getpid() if owner_pid is None else owner_pid
        with self._lock:
            with self._runtime_file_lock():
                try:
                    data = json.loads(self.runtime_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    return
                if not isinstance(data, dict) or data.get("pid") != expected_pid:
                    return
                try:
                    self.runtime_path.unlink(missing_ok=True)
                except OSError:
                    pass

    @contextmanager
    def _runtime_file_lock(self):
        """Hold the runtime ownership lock without exposing its descriptor."""
        path = self.runtime_path.with_name(".runtime.lock")
        fd = os.open(
            path,
            os.O_RDWR | os.O_CREAT,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        try:
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
