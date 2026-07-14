"""Fake adapter, 只在 AGENTBAR_ENABLE_FAKE=1 时注册。用于测试与冒烟，不进生产菜单。"""

from __future__ import annotations

import sys
from pathlib import Path

from ..models import Task
from .base import Adapter, Outcome, looks_like_quota, parse_reset_hint

_FAKE_CLI = Path(__file__).resolve().parent.parent / "testing" / "fake_cli.py"


class FakeAdapter(Adapter):
    name = "fake"
    display_name = "Fake (testing)"
    effort_choices = ("low", "medium", "high")

    def binary(self) -> str | None:
        return sys.executable

    def build_argv(self, task: Task, resume: bool, binary: str) -> list[str]:
        argv = [binary, str(_FAKE_CLI)]
        if resume and task.session_id:
            argv += ["--resume", task.session_id]
        argv.append(task.prompt)
        return argv

    def classify(self, exit_code: int | None, output: str) -> Outcome:
        if exit_code == 0:
            return Outcome("success", "完成")
        if looks_like_quota(output):
            return Outcome("quota", "fake 额度", reset_at=parse_reset_hint(output))
        return Outcome("failure", f"退出码 {exit_code}")

    def extract_session_id(self, output: str) -> str | None:
        for line in output.splitlines():
            if line.startswith("session id: "):
                return line.split(": ", 1)[1].strip()
        return None
