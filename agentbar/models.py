"""Task model and lifecycle states."""

from __future__ import annotations

import dataclasses
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class TaskState(str, Enum):
    QUEUED = "queued"                # 等待调度
    RUNNING = "running"              # 执行中
    SUCCEEDED = "succeeded"          # 成功
    FAILED = "failed"                # 失败
    WAITING_QUOTA = "waiting_quota"  # 额度受限，等待恢复后自动重试
    PAUSED = "paused"                # 人工暂停
    CANCELLED = "cancelled"          # 已取消


ACTIVE_STATES = {
    TaskState.QUEUED,
    TaskState.RUNNING,
    TaskState.WAITING_QUOTA,
    TaskState.PAUSED,
}
FINISHED_STATES = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}

# 权限档位：readonly=只读, edits=可编辑文件(默认), full=完全权限(必须显式开启)
PROFILES = ("readonly", "edits", "full")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class Task:
    id: str
    title: str
    prompt: str
    tool: str      # adapter 名: "claude" | "codex" | ...
    cwd: str
    profile: str = "edits"
    state: TaskState = TaskState.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    attempts: int = 0
    quota_waits: int = 0
    next_retry_at: float | None = None
    session_id: str | None = None
    resume_next: bool = False   # 下次执行时尝试恢复 session（额度中断/重启后）
    exit_code: int | None = None
    cost_usd: float | None = None
    state_reason: str = ""

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["state"] = self.state.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        names = {f.name for f in dataclasses.fields(cls)}
        kw = {k: v for k, v in d.items() if k in names}
        raw_state = kw.get("state", TaskState.QUEUED.value)
        try:
            kw["state"] = TaskState(raw_state)
        except ValueError:
            kw["state"] = TaskState.FAILED
            kw["state_reason"] = f"载入时遇到未知状态: {raw_state}"
        return cls(**kw)


def default_title(prompt: str) -> str:
    line = prompt.strip().splitlines()[0] if prompt.strip() else "(空)"
    return line[:48] + ("…" if len(line) > 48 else "")
