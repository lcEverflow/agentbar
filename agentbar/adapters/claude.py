"""Claude Code adapter.

调用形态: `claude -p --output-format json [权限flags] [--resume <sid>]`，
prompt 通过 stdin 传入（避免以 `-` 开头的 prompt 被解析成 flag）。
结果 JSON 里带 session_id / total_cost_usd / is_error。
"""

from __future__ import annotations

import json

from ..models import Task
from .base import Adapter, Outcome, looks_like_quota, parse_reset_hint

READONLY_TOOLS = "Read,Glob,Grep,WebFetch,WebSearch,TodoWrite"


class ClaudeAdapter(Adapter):
    name = "claude"
    display_name = "Claude Code"

    def build_argv(self, task: Task, resume: bool, binary: str) -> list[str]:
        argv = [binary, "-p", "--output-format", "json"]
        if task.profile == "readonly":
            argv += ["--allowedTools", READONLY_TOOLS,
                     "--disallowedTools", "Bash,Edit,Write,NotebookEdit"]
        elif task.profile == "full":
            argv += ["--dangerously-skip-permissions"]
        else:  # edits（默认）：自动接受文件编辑，Bash 等仍被拒绝
            argv += ["--permission-mode", "acceptEdits"]
        if resume and task.session_id:
            argv += ["--resume", task.session_id]
        return argv

    def stdin_payload(self, task: Task, resume: bool) -> str | None:
        if resume and task.session_id:
            return self.continuation_prompt(task)
        return task.prompt

    def classify(self, exit_code: int | None, output: str) -> Outcome:
        result = _parse_result_json(output)
        cost = result.get("total_cost_usd") if result else None
        if exit_code == 0 and result and not result.get("is_error"):
            return Outcome("success", "完成", cost_usd=cost)
        if exit_code == 0 and not result:
            # 正常退出但没解析出 result JSON（版本差异），按成功处理
            return Outcome("success", "完成（未解析到结果 JSON）")
        text = output + (json.dumps(result, ensure_ascii=False) if result else "")
        if looks_like_quota(text):
            return Outcome("quota", "Claude 额度/限流", reset_at=parse_reset_hint(text))
        reason = (result or {}).get("result") or f"退出码 {exit_code}"
        return Outcome("failure", str(reason)[:300], cost_usd=cost)

    def extract_session_id(self, output: str) -> str | None:
        result = _parse_result_json(output)
        if result:
            return result.get("session_id")
        return None


def _parse_result_json(output: str) -> dict | None:
    """在输出末尾找 {"type":"result",...} 的 JSON 行。"""
    for line in reversed(output.strip().splitlines()[-50:]):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            return obj
    return None
