"""Codex CLI adapter.

调用形态: `codex exec [--sandbox …] --skip-git-repo-check -` （prompt 走 stdin）
恢复:     `codex exec resume <session_id> …`（codex >= 0.4x 支持）
session id 从输出 banner 的 `session id: <uuid>` 行提取。
"""

from __future__ import annotations

import json
import re

from ..models import Task
from .base import Adapter, Outcome, looks_like_quota, parse_reset_hint

_SESSION_RE = re.compile(
    r"session[ _]?id:?\s*([0-9a-fA-F][0-9a-fA-F-]{15,})", re.IGNORECASE
)


class CodexAdapter(Adapter):
    name = "codex"
    display_name = "Codex CLI"

    def _sandbox_flags(self, task: Task) -> list[str]:
        if task.profile == "readonly":
            return ["--sandbox", "read-only"]
        if task.profile == "full":
            return ["--dangerously-bypass-approvals-and-sandbox"]
        return ["--sandbox", "workspace-write"]  # edits（默认）

    def build_argv(self, task: Task, resume: bool, binary: str) -> list[str]:
        argv = [binary, "exec"]
        if resume and task.session_id:
            argv += ["resume", task.session_id]
        if task.model:
            argv += ["--model", task.model]
        if task.effort:
            # Codex 通过 config override 设置推理强度，不伪造不存在的 --effort flag。
            argv += ["--config", f"model_reasoning_effort={json.dumps(task.effort)}"]
        argv += self._sandbox_flags(task)
        argv += ["--skip-git-repo-check", "-"]
        return argv

    def stdin_payload(self, task: Task, resume: bool) -> str | None:
        if resume and task.session_id:
            return self.continuation_prompt(task)
        return task.prompt

    def classify(self, exit_code: int | None, output: str) -> Outcome:
        if exit_code == 0:
            return Outcome("success", "完成")
        if looks_like_quota(output):
            return Outcome("quota", "Codex 额度/限流", reset_at=parse_reset_hint(output))
        tail = output.strip().splitlines()[-3:]
        return Outcome("failure", (" / ".join(tail))[:300] or f"退出码 {exit_code}")

    def extract_session_id(self, output: str) -> str | None:
        matches = _SESSION_RE.findall(output)
        return matches[-1] if matches else None
