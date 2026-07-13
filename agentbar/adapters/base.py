"""Adapter base: how a specific AI CLI is invoked, and how its output is classified.

新增一个 AI CLI 只需要子类化 Adapter 并在 get_registry() 里注册。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..config import Settings
from ..models import Task

# 额度/限流特征（大小写不敏感）。只在退出码非 0 / is_error 时参与判定，
# 避免把"任务内容里恰好讨论 rate limit"的成功输出误判为额度问题。
QUOTA_PATTERNS = [
    r"usage limit reached",
    r"you'?ve hit your usage limit",
    r"rate[ _-]?limit",
    r"\b429\b",
    r"too many requests",
    r"quota (exceeded|exhausted)",
    r"limit will reset",
    r"overloaded_error",
    r"credit balance is too low",
    r"out of credits",
]
_QUOTA_RE = re.compile("|".join(f"(?:{p})" for p in QUOTA_PATTERNS), re.IGNORECASE)

# "Claude AI usage limit reached|1752392400" → 恢复时间戳（秒或毫秒）
_EPOCH_RE = re.compile(r"limit reached\|(\d{10,13})")
# "try again in 3 hours 12 minutes" / "in 27 seconds" / "in 45 minutes"
_DURATION_RE = re.compile(
    r"try again in\s+(?:(\d+(?:\.\d+)?)\s*h(?:ours?)?)?\s*"
    r"(?:(\d+(?:\.\d+)?)\s*m(?:in(?:utes?)?)?)?\s*"
    r"(?:(\d+(?:\.\d+)?)\s*s(?:ec(?:onds?)?)?)?",
    re.IGNORECASE,
)


def looks_like_quota(text: str) -> bool:
    return bool(_QUOTA_RE.search(text))


def parse_reset_hint(text: str, now: float | None = None) -> float | None:
    """尽力从报错文本里解析额度恢复时间；解析不出返回 None（走退避）。"""
    now = now or time.time()
    m = _EPOCH_RE.search(text)
    if m:
        ts = int(m.group(1))
        if ts > 10**12:  # 毫秒
            ts /= 1000
        if now < ts < now + 7 * 86400:
            return float(ts)
    m = _DURATION_RE.search(text)
    if m and any(m.groups()):
        h = float(m.group(1) or 0)
        mi = float(m.group(2) or 0)
        s = float(m.group(3) or 0)
        total = h * 3600 + mi * 60 + s
        if 0 < total < 7 * 86400:
            return now + total
    return None


@dataclass
class Outcome:
    kind: str            # "success" | "quota" | "failure" | "cancelled" | "interrupted"
    reason: str = ""
    reset_at: float | None = None
    cost_usd: float | None = None


class Adapter(ABC):
    name: str = ""
    display_name: str = ""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._binary_cache: str | None = None

    # ---------- binary resolution ----------

    def binary(self) -> str | None:
        """解析 CLI 可执行文件：配置覆盖 > 当前 PATH > 登录 shell PATH。

        登录 shell 兜底解决 launchd 启动时 PATH 被裁剪（nvm 等安装的
        claude/codex 找不到）的问题。
        """
        configured = self.settings.tool_paths.get(self.name)
        if configured:
            p = os.path.expanduser(configured)
            return p if os.path.exists(p) else None
        if self._binary_cache and os.path.exists(self._binary_cache):
            return self._binary_cache
        found = shutil.which(self.name)
        if not found:
            found = self._login_shell_which(self.name)
        self._binary_cache = found
        return found

    @staticmethod
    def _login_shell_which(name: str) -> str | None:
        try:
            r = subprocess.run(
                ["/bin/zsh", "-lc", f"command -v {name}"],
                capture_output=True, text=True, timeout=15,
            )
            path = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
            return path if path and os.path.exists(path) else None
        except (subprocess.SubprocessError, OSError):
            return None

    def availability(self) -> dict:
        b = self.binary()
        return {
            "name": self.name,
            "display_name": self.display_name,
            "installed": bool(b),
            "path": b,
        }

    # ---------- invocation ----------

    @abstractmethod
    def build_argv(self, task: Task, resume: bool, binary: str) -> list[str]:
        ...

    def stdin_payload(self, task: Task, resume: bool) -> str | None:
        """返回要写入子进程 stdin 的 prompt；None 表示不用 stdin。"""
        return None

    @staticmethod
    def continuation_prompt(task: Task) -> str:
        return (
            "上一次执行因额度限制或调度器重启被中断。"
            "请检查此前进度，从中断处继续，完成原始任务。\n\n"
            f"原始任务：\n{task.prompt}"
        )

    # ---------- result classification ----------

    @abstractmethod
    def classify(self, exit_code: int | None, output: str) -> Outcome:
        ...

    def extract_session_id(self, output: str) -> str | None:
        return None


def get_registry(settings: Settings) -> dict[str, Adapter]:
    from .claude import ClaudeAdapter
    from .codex import CodexAdapter

    registry: dict[str, Adapter] = {}
    for cls in (ClaudeAdapter, CodexAdapter):
        a = cls(settings)
        registry[a.name] = a
    if os.environ.get("AGENTBAR_ENABLE_FAKE") == "1":
        from .fake import FakeAdapter

        a = FakeAdapter(settings)
        registry[a.name] = a
    return registry
