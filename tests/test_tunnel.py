"""TunnelManager 状态机测试 — 用假 cloudflared 脚本，不出网。"""

import os
import stat
import time

from agentbar.tunnel import _URL_RE, TunnelManager


def _fake_cloudflared(tmp_path, body: str) -> str:
    p = tmp_path / "fake-cloudflared"
    p.write_text("#!/bin/bash\n" + body)
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    return str(p)


def test_url_regex_matches_real_log_line():
    line = ("2026-07-14T13:37:41Z INF |  https://engineer-eve-happen-compact"
            ".trycloudflare.com  |")
    m = _URL_RE.search(line)
    assert m and m.group(0) == "https://engineer-eve-happen-compact.trycloudflare.com"


def test_start_up_and_stop(tmp_path):
    fake = _fake_cloudflared(
        tmp_path,
        'echo "INF https://abc-def.trycloudflare.com registered"\nsleep 30\n',
    )
    allowed, removed = [], []
    tm = TunnelManager(8737, on_up=allowed.append, on_down=removed.append,
                       binary_override=fake)
    assert tm.start(timeout=10) is True
    st = tm.status()
    assert st["state"] == "up"
    assert st["url"] == "https://abc-def.trycloudflare.com"
    assert allowed == ["abc-def.trycloudflare.com"]
    assert tm.url == "https://abc-def.trycloudflare.com"

    tm.stop()
    st = tm.status()
    assert st["state"] == "off" and st["url"] is None
    assert removed == ["abc-def.trycloudflare.com"]
    assert tm.url is None


def test_start_timeout_marks_error(tmp_path):
    fake = _fake_cloudflared(tmp_path, 'echo "no url here"\nsleep 30\n')
    tm = TunnelManager(8737, binary_override=fake)
    assert tm.start(timeout=1.5) is False
    assert tm.status()["state"] == "error"


def test_missing_binary(tmp_path):
    tm = TunnelManager(8737, binary_override=str(tmp_path / "nonexistent"))
    assert tm.start() is False
    st = tm.status()
    assert st["state"] == "error" and "cloudflared" in st["error"]


def test_process_death_detected(tmp_path):
    fake = _fake_cloudflared(
        tmp_path, 'echo "INF https://dies.trycloudflare.com up"\n',  # 打印后立即退出
    )
    removed = []
    tm = TunnelManager(8737, on_down=removed.append, binary_override=fake)
    assert tm.start(timeout=10) is True
    deadline = time.time() + 5
    while time.time() < deadline and tm.status()["state"] == "up":
        time.sleep(0.1)
    assert tm.status()["state"] == "error"
