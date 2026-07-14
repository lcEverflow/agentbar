"""Session transcript finder and parser for Claude Code and Codex CLI sessions."""

from __future__ import annotations

import glob
import json
import os
import time
from pathlib import Path


def find_session_file(tool: str, cwd: str | None, session_id: str) -> Path | None:
    """Locate the JSONL session file for a given tool + session_id."""
    if not session_id:
        return None

    if tool == "claude":
        # ~/.claude/projects/<cwd with "/" → "-" (leading slash becomes leading dash)>/<sid>.jsonl
        if cwd:
            cwd_key = cwd.replace("/", "-")
            direct = Path.home() / ".claude" / "projects" / cwd_key / f"{session_id}.jsonl"
            if direct.exists():
                return direct
        pattern = str(Path.home() / ".claude" / "projects" / "*" / f"{session_id}.jsonl")
        matches = glob.glob(pattern)
        if matches:
            return Path(matches[0])

    elif tool == "codex":
        pattern = str(Path.home() / ".codex" / "sessions" / "**" / f"rollout-*-{session_id}.jsonl")
        matches = glob.glob(pattern, recursive=True)
        if matches:
            return Path(sorted(matches)[-1])

    return None


def resume_command(tool: str, cwd: str, session_id: str) -> str:
    """Return the shell command to resume a session in the user's terminal."""
    if tool == "claude":
        return f"cd {cwd} && claude --resume {session_id}"
    if tool == "codex":
        return f"cd {cwd} && codex exec resume {session_id} -"
    return f"# 未知工具 {tool}"


def parse_transcript(tool: str, path: Path, max_chars: int = 100_000) -> str:
    """Parse a session JSONL file and return a human-readable conversation string."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        return f"[无法读取文件: {e}]"

    chunks: list[str] = []

    if tool == "claude":
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            role_key = obj.get("type")
            if role_key not in ("user", "assistant"):
                continue
            label = "用户" if role_key == "user" else "Claude"
            content = (obj.get("message") or {}).get("content", "")
            text = _extract_claude_content(content)
            if text:
                chunks.append(f"[{label}]\n{text}")

    elif tool == "codex":
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "response_item":
                continue
            payload = obj.get("payload") or {}
            msg = payload.get("message") or {}
            role = msg.get("role", "")
            if role not in ("user", "assistant"):
                continue
            label = "用户" if role == "user" else "Codex"
            content = msg.get("content", [])
            text = _extract_codex_content(content)
            if text:
                chunks.append(f"[{label}]\n{text}")

    result = "\n\n---\n\n".join(chunks)
    if not result:
        return "[对话记录为空或格式不支持]"
    if len(result) > max_chars:
        result = result[:max_chars] + f"\n\n… [已截断，共 {len(result)} 字符]"
    return result


def _extract_claude_content(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            t = (block.get("text") or "").strip()
            if t:
                parts.append(t)
        elif btype == "thinking":
            snippet = (block.get("thinking") or "")[:120]
            parts.append(f"<thinking>{snippet}…</thinking>")
        elif btype == "tool_use":
            name = block.get("name", "?")
            inp = json.dumps(block.get("input") or {}, ensure_ascii=False)
            parts.append(f"[→ {name}({inp[:120]})]")
        elif btype == "tool_result":
            c = block.get("content", "")
            if isinstance(c, list):
                c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
            parts.append(f"[← {str(c)[:200]}]")
    return "\n".join(parts)


def _extract_codex_content(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text") or block.get("output_text") or ""
        if text:
            parts.append(text.strip())
    return "\n".join(parts)
