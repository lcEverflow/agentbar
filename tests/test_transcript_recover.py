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
