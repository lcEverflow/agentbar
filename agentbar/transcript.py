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
p{{margin:6px 0}}
h1,h2,h3,h4{{margin:14px 0 6px;line-height:1.3}}
h1{{font-size:17px}} h2{{font-size:15px;border-bottom:1px solid #e5e5e5;padding-bottom:3px}}
h3{{font-size:13.5px}} h4{{font-size:12.5px}}
ul,ol{{margin:6px 0;padding-left:22px}}
li{{margin:3px 0}}
table{{border-collapse:collapse;margin:8px 0;font-size:12px;display:block;
       overflow-x:auto;max-width:100%}}
th,td{{border:1px solid #d9d9d9;padding:5px 9px;text-align:left;vertical-align:top}}
th{{background:#f0f0f0;font-weight:600}}
blockquote{{margin:8px 0;padding:4px 12px;border-left:3px solid #c5c5c5;
            color:#666;background:#fafafa}}
a{{color:#2980b9;text-decoration:none}} a:hover{{text-decoration:underline}}
hr{{border:none;border-top:1px solid #ddd;margin:12px 0}}
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


_ROLLOUT_RE = re.compile(
    r"rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-(.+)\.jsonl$"
)


def recover_session_id(
    tool: str, cwd: str,
    started_at: float | None, finished_at: float | None = None,
) -> str | None:
    """按时间窗 + cwd 模糊匹配会话文件，恢复丢失的 session id。

    历史任务可能没存下 session id（旧版只扫输出尾部，codex 的 id 在开头，
    长输出会被挤出缓冲）。会话文件的创建时间与任务启动时间只差几秒，
    以此反查。codex 用 rollout 文件名内嵌的本地时间戳，claude 用文件 mtime。
    """
    if not started_at:
        return None
    lo = started_at - 120
    hi = (finished_at or started_at + 7200) + 300

    if tool == "codex":
        best = None
        for p in glob.glob(
            str(Path.home() / ".codex" / "sessions" / "**" / "rollout-*.jsonl"),
            recursive=True,
        ):
            m = _ROLLOUT_RE.search(os.path.basename(p))
            if not m:
                continue
            try:
                ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%dT%H-%M-%S"))
            except ValueError:
                continue
            if not (lo <= ts <= hi):
                continue
            # cwd 吻合的候选优先，其次时间最接近任务启动的
            key = (0 if _codex_cwd_matches(p, cwd) else 1, abs(ts - started_at))
            if best is None or key < best[0]:
                best = (key, m.group(2))
        return best[1] if best else None

    if tool == "claude" and cwd:
        cwd_key = cwd.replace("/", "-")
        cands = []
        for p in glob.glob(str(Path.home() / ".claude" / "projects" / cwd_key / "*.jsonl")):
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if lo <= mt <= hi:
                cands.append((abs(mt - (finished_at or started_at)), Path(p).stem))
        return min(cands)[1] if cands else None
    return None


def _codex_cwd_matches(path: str, cwd: str) -> bool:
    """codex 会话头部记录了 cwd（注意 /tmp → /private/tmp 符号链接归一）。"""
    if not cwd:
        return False
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
    except OSError:
        return False
    real = os.path.realpath(os.path.expanduser(cwd))
    return cwd in head or real in head


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
            if role_key == "user" and _is_pure_tool_result(content):
                continue  # skip tool-result echo turns
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
    """Return True if this user message is a system-injected context block to skip."""
    if not isinstance(content, list):
        return False
    for block in content[:3]:
        if isinstance(block, dict):
            text = block.get("text") or block.get("input_text") or ""
            if (
                "# AGENTS.md" in text
                or "AGENTS.md instructions" in text
                or text.lstrip().startswith("<environment_context>")
            ):
                return True
    return False


def _tool_call_summary(name: str, inp: dict) -> str:
    """Return a compact, human-readable summary of a tool call."""
    _prefer = {
        "Write": "file_path", "Read": "file_path", "Edit": "file_path",
        "Bash": "command", "Grep": "pattern", "Glob": "pattern",
        "WebFetch": "url", "WebSearch": "query",
    }
    key = _prefer.get(name)
    val = None
    if key and key in inp:
        val = str(inp[key])
    else:
        for v in inp.values():
            if isinstance(v, str):
                val = v
                break
    if val:
        if len(val) > 120:
            val = val[:117] + "…"
        return f"{name} · {val}"
    return name


def _is_pure_tool_result(content) -> bool:
    """True if a user message consists only of tool_result blocks (no real user text)."""
    if not isinstance(content, list) or not content:
        return False
    return all(
        isinstance(b, dict) and b.get("type") == "tool_result"
        for b in content
    )


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
            raw = block.get("thinking") or ""
            snippet = html.escape(raw[:300])
            ellipsis = "…" if len(raw) > 300 else ""
            parts.append(f'<div class="think">💭 {snippet}{ellipsis}</div>')
        elif btype == "tool_use":
            name = block.get("name") or "?"
            inp = block.get("input") or {}
            summary = html.escape(_tool_call_summary(name, inp))
            parts.append(f'<div class="tool">→ {summary}</div>')
        elif btype == "tool_result":
            pass  # pure tool-result turns are skipped at caller; mixed ones ignored here
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

_MD = None


def _markdown():
    """惰性构建 mistune 渲染器（表格/删除线/数学公式/自动链接）。"""
    global _MD
    if _MD is None:
        import mistune
        # escape=True：把消息里的原始 HTML 转义掉，只认 markdown 语法
        _MD = mistune.create_markdown(
            escape=True, plugins=["table", "strikethrough", "math", "url"]
        )
    return _MD


def _text_to_html(text: str) -> str:
    """Markdown → HTML（mistune；math 插件输出 \\(..\\)/\\[..\\] 交给 MathJax）。"""
    try:
        return _markdown()(text)
    except Exception:
        return _text_to_html_legacy(text)


def _text_to_html_legacy(text: str) -> str:
    """手写降级渲染（mistune 不可用时）：代码块/行内样式/数学穿透。"""
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
