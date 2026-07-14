"""Session transcript finder, parser and HTML renderer for Claude Code and Codex CLI."""

from __future__ import annotations

import glob
import html
import json
import os
import re
import time
from pathlib import Path

# ---------- HTML template ----------

_HTML_TEMPLATE = """\
<!DOCTYPE html><html><head><meta charset="utf-8">
<script>
MathJax = {{tex:{{inlineMath:[['$','$'],['\\\\(','\\\\)']],
                  displayMath:[['$$','$$'],['\\\\[','\\\\]']]}},
           options:{{skipHtmlTags:['script','noscript','style','textarea','pre']}}}};
</script>
<script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js" async></script>
<style>
*{{box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',sans-serif;
     font-size:13px;margin:0;padding:12px 16px;background:#f5f5f5;line-height:1.5}}
.turn{{margin:8px 0}}
.u{{background:#e8f4fd;border-left:3px solid #2980b9;padding:8px 12px;border-radius:0 6px 6px 0}}
.a{{background:#fff;border-left:3px solid #27ae60;padding:8px 12px;border-radius:0 6px 6px 0}}
.r{{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;margin-bottom:5px}}
.u .r{{color:#2980b9}} .a .r{{color:#27ae60}}
.tool{{background:#fff9e6;border-left:3px solid #f39c12;padding:4px 8px;margin:4px 0;
       border-radius:0 4px 4px 0;font-size:11px;color:#7d5a00;font-family:'SF Mono',Monaco,monospace}}
.think{{color:#bbb;font-style:italic;font-size:11px;margin:4px 0;padding:4px 8px;
        border-left:2px solid #ddd}}
pre{{background:#1e1e2e;color:#cdd6f4;padding:10px 14px;border-radius:6px;
     overflow-x:auto;font-size:11.5px;line-height:1.45;margin:6px 0;white-space:pre}}
code{{background:#e8e8e8;padding:1px 5px;border-radius:3px;
      font-size:11.5px;font-family:'SF Mono',Monaco,monospace}}
pre code{{background:none;padding:0}}
p{{margin:4px 0}}
</style></head><body>{body}</body></html>"""

# ---------- file discovery ----------

def find_session_file(tool: str, cwd: str | None, session_id: str) -> Path | None:
    if not session_id:
        return None
    if tool == "claude":
        if cwd:
            cwd_key = cwd.replace("/", "-")
            direct = Path.home() / ".claude" / "projects" / cwd_key / f"{session_id}.jsonl"
            if direct.exists():
                return direct
        for p in glob.glob(str(Path.home() / ".claude" / "projects" / "*" / f"{session_id}.jsonl")):
            return Path(p)
    elif tool == "codex":
        matches = glob.glob(
            str(Path.home() / ".codex" / "sessions" / "**" / f"rollout-*-{session_id}.jsonl"),
            recursive=True,
        )
        if matches:
            return Path(sorted(matches)[-1])
    return None


def resume_command(tool: str, cwd: str, session_id: str) -> str:
    if tool == "claude":
        return f"cd {cwd} && claude --resume {session_id}"
    if tool == "codex":
        return f"cd {cwd} && codex exec resume {session_id} -"
    return f"# unknown tool {tool}"


# ---------- HTML generation (for WKWebView) ----------

def to_html(tool: str, path: Path) -> str:
    """Parse session file and return a full HTML document with MathJax support."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        return _wrap(f"<p style='color:red'>无法读取文件：{html.escape(str(e))}</p>")

    turns: list[str] = []
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
            css = "u" if role_key == "user" else "a"
            content = (obj.get("message") or {}).get("content", "")
            body_html = _claude_content_to_html(content)
            if body_html:
                turns.append(f'<div class="turn {css}"><div class="r">{label}</div>{body_html}</div>')

    elif tool == "codex":
        user_count = 0
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
            ptype = payload.get("type") or payload.get("role", "")

            if ptype == "message":
                role = payload.get("role", "")
                if role == "developer":
                    continue
                if role == "user":
                    user_count += 1
                    if user_count == 1 and _is_system_context(payload.get("content", [])):
                        continue  # skip AGENTS.md injection
                    label, css = "用户", "u"
                elif role == "assistant":
                    label, css = "Codex", "a"
                else:
                    continue
                body_html = _codex_content_to_html(payload.get("content", []))
                if body_html:
                    turns.append(f'<div class="turn {css}"><div class="r">{label}</div>{body_html}</div>')

            elif ptype == "reasoning":
                text = " ".join(
                    html.escape(b.get("text") or b.get("summary") or "")
                    for b in (payload.get("content") or [])
                    if isinstance(b, dict)
                ).strip()
                if text:
                    turns.append(f'<div class="think">💭 {text[:300]}{"…" if len(text)>300 else ""}</div>')

            elif ptype in ("custom_tool_call", "function_call"):
                name = html.escape(payload.get("name") or "?")
                inp = payload.get("input") or payload.get("arguments") or ""
                if isinstance(inp, dict):
                    inp = json.dumps(inp, ensure_ascii=False)
                inp_snippet = html.escape(str(inp)[:200])
                turns.append(f'<div class="tool">→ {name}({inp_snippet})</div>')

    if not turns:
        return _wrap("<p style='color:#888'>对话记录为空或格式暂不支持</p>")
    return _wrap("\n".join(turns))


def _wrap(body: str) -> str:
    return _HTML_TEMPLATE.format(body=body)


def _is_system_context(content) -> bool:
    """Return True if this user message is AGENTS.md / system context injection."""
    if not isinstance(content, list):
        return False
    for block in content[:2]:
        if isinstance(block, dict):
            text = block.get("text") or block.get("input_text") or ""
            if "# AGENTS.md" in text or "AGENTS.md instructions" in text:
                return True
    return False


def _claude_content_to_html(content) -> str:
    if isinstance(content, str):
        return _text_to_html(content)
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(_text_to_html(block.get("text") or ""))
        elif btype == "thinking":
            snippet = html.escape((block.get("thinking") or "")[:200])
            parts.append(f'<div class="think">💭 {snippet}…</div>')
        elif btype == "tool_use":
            name = html.escape(block.get("name") or "?")
            inp = json.dumps(block.get("input") or {}, ensure_ascii=False)
            parts.append(f'<div class="tool">→ {name}({html.escape(inp[:200])})</div>')
        elif btype == "tool_result":
            c = block.get("content", "")
            if isinstance(c, list):
                c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
            parts.append(f'<div class="tool">← {html.escape(str(c)[:200])}</div>')
    return "".join(parts)


def _codex_content_to_html(content) -> str:
    if isinstance(content, str):
        return _text_to_html(content)
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text") or block.get("output_text") or block.get("input_text") or ""
        if text:
            parts.append(_text_to_html(text))
    return "".join(parts)


# ---------- text → HTML with math passthrough ----------

def _text_to_html(text: str) -> str:
    """Convert plain text with markdown and LaTeX math to HTML."""
    lines = text.splitlines()
    out: list[str] = []
    in_code = False
    code_lang = ""
    code_buf: list[str] = []

    for line in lines:
        if in_code:
            if line.rstrip().startswith("```"):
                code_html = html.escape("\n".join(code_buf))
                lang = f' class="language-{html.escape(code_lang)}"' if code_lang else ""
                out.append(f"<pre><code{lang}>{code_html}</code></pre>")
                code_buf = []
                in_code = False
            else:
                code_buf.append(line)
        elif line.startswith("```"):
            code_lang = line[3:].strip()
            in_code = True
        else:
            formatted = _format_line(line)
            out.append(formatted)

    if in_code and code_buf:
        out.append(f"<pre><code>{html.escape(chr(10).join(code_buf))}</code></pre>")

    return "<p>" + "</p><p>".join(
        "\n".join(g) for g in _group_paragraphs(out)
    ) + "</p>"


def _group_paragraphs(lines: list[str]) -> list[list[str]]:
    """Group consecutive non-block lines into paragraph groups."""
    groups: list[list[str]] = []
    current: list[str] = []
    for ln in lines:
        if ln.startswith("<pre>") or ln.startswith("<div"):
            if current:
                groups.append(current)
                current = []
            groups.append([ln])
        else:
            current.append(ln)
    if current:
        groups.append(current)
    return groups


def _format_line(line: str) -> str:
    """Format one line: preserve math delimiters, escape rest, apply inline markdown."""
    # Split on display math first ($$...$$), then inline math ($...$)
    parts = re.split(r"(\$\$[^$]*?\$\$|\$(?!\$)[^$\n]*?\$)", line)
    result: list[str] = []
    for p in parts:
        if p.startswith("$"):
            result.append(p)  # math: pass through for MathJax
        else:
            result.append(_format_inline(p))
    return "".join(result)


def _format_inline(text: str) -> str:
    """Escape HTML and apply inline code / bold / italic."""
    parts = re.split(r"(`[^`]+`)", text)
    result: list[str] = []
    for p in parts:
        if p.startswith("`") and p.endswith("`") and len(p) >= 3:
            result.append(f"<code>{html.escape(p[1:-1])}</code>")
        else:
            escaped = html.escape(p)
            escaped = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", escaped)
            escaped = re.sub(r"\*([^*\n]+)\*", r"<em>\1</em>", escaped)
            result.append(escaped)
    return "".join(result)


# ---------- plain-text fallback (still used by CLI / server endpoint) ----------

def parse_transcript(tool: str, path: Path, max_chars: int = 100_000) -> str:
    """Return a plain-text transcript (for the HTTP API endpoint)."""
    chunks: list[str] = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        return f"[无法读取文件: {e}]"

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
            text = _plain_claude(content)
            if text:
                chunks.append(f"[{label}]\n{text}")

    elif tool == "codex":
        user_count = 0
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
            ptype = payload.get("type") or payload.get("role", "")
            if ptype == "message":
                role = payload.get("role", "")
                if role == "developer":
                    continue
                if role == "user":
                    user_count += 1
                    if user_count == 1 and _is_system_context(payload.get("content", [])):
                        continue
                    label = "用户"
                elif role == "assistant":
                    label = "Codex"
                else:
                    continue
                text = _plain_codex(payload.get("content", []))
                if text:
                    chunks.append(f"[{label}]\n{text}")

    result = "\n\n---\n\n".join(chunks)
    if not result:
        return "[对话记录为空或格式不支持]"
    if len(result) > max_chars:
        result = result[:max_chars] + f"\n\n… [截断，共 {len(result)} 字符]"
    return result


def _plain_claude(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if not isinstance(b, dict):
            continue
        btype = b.get("type")
        if btype == "text":
            parts.append((b.get("text") or "").strip())
        elif btype == "tool_use":
            parts.append(f"[→ {b.get('name')}]")
        elif btype == "tool_result":
            c = b.get("content", "")
            if isinstance(c, list):
                c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
            parts.append(f"[← {str(c)[:200]}]")
    return "\n".join(p for p in parts if p)


def _plain_codex(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    return "\n".join(
        (b.get("text") or b.get("output_text") or b.get("input_text") or "").strip()
        for b in content if isinstance(b, dict)
    )
