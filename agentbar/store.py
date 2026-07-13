"""State persistence: atomic JSON writes + per-task log files + runtime info."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


class StateStore:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.state_path = self.state_dir / "state.json"
        self.runtime_path = self.state_dir / "runtime.json"
        self.logs_dir = self.state_dir / "logs"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ---------- state.json ----------

    def load(self) -> dict:
        with self._lock:
            if not self.state_path.exists():
                return {}
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                # 损坏的状态文件：备份后从空状态启动，不让整个调度器起不来
                backup = self.state_path.with_name(
                    f"state.json.corrupt-{int(time.time())}"
                )
                try:
                    os.replace(self.state_path, backup)
                except OSError:
                    pass
                return {}

    def save(self, data: dict) -> None:
        with self._lock:
            tmp = self.state_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            os.replace(tmp, self.state_path)

    # ---------- per-task logs ----------

    def log_path(self, task_id: str) -> Path:
        return self.logs_dir / f"{task_id}.log"

    def read_log_tail(self, task_id: str, max_bytes: int = 65536) -> str:
        p = self.log_path(task_id)
        if not p.exists():
            return ""
        try:
            size = p.stat().st_size
            with open(p, "rb") as f:
                if size > max_bytes:
                    f.seek(size - max_bytes)
                data = f.read()
            return data.decode("utf-8", errors="replace")
        except OSError:
            return ""

    # ---------- runtime.json (实际端口/PID，供 CLI 客户端发现) ----------

    def write_runtime(self, port: int) -> None:
        self.runtime_path.write_text(
            json.dumps({"port": port, "pid": os.getpid(), "started_at": time.time()}),
            encoding="utf-8",
        )

    def read_runtime(self) -> dict | None:
        if not self.runtime_path.exists():
            return None
        try:
            return json.loads(self.runtime_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def clear_runtime(self) -> None:
        try:
            self.runtime_path.unlink(missing_ok=True)
        except OSError:
            pass
