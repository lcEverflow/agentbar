"""TunnelManager 状态机测试 — 用假 cloudflared 脚本，不出网。"""

import os
import stat
import threading
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
    # The helper may exit between publishing the URL and start() checking its
    # process. Both an immediate False and a briefly-up True are valid; neither
    # may leave the manager claiming the dead tunnel is usable.
    tm.start(timeout=10)
    deadline = time.time() + 5
    while time.time() < deadline and tm.status()["state"] == "up":
        time.sleep(0.1)
    assert tm.status()["state"] == "error"


def test_stop_while_starting_reaps_unpublished_process(tmp_path):
    fake = _fake_cloudflared(tmp_path, 'echo "waiting without a URL"\nsleep 30\n')
    tm = TunnelManager(8737, binary_override=fake)
    result = []
    thread = threading.Thread(target=lambda: result.append(tm.start(timeout=10)))
    thread.start()
    deadline = time.time() + 3
    while time.time() < deadline and tm._proc is None:
        time.sleep(0.01)
    proc = tm._proc
    assert proc is not None and proc.poll() is None

    tm.stop()
    thread.join(timeout=3)

    assert result == [False]
    assert not thread.is_alive()
    assert proc.poll() is not None
    assert tm.status()["state"] == "off"


def test_stop_compensates_late_on_up_callback(tmp_path):
    fake = _fake_cloudflared(
        tmp_path,
        'echo "INF https://late-up.trycloudflare.com registered"\nsleep 30\n',
    )
    allowed = set()
    callback_entered = threading.Event()
    release_callback = threading.Event()

    def delayed_allow(host):
        callback_entered.set()
        assert release_callback.wait(3)
        allowed.add(host)

    tm = TunnelManager(
        8737,
        on_up=delayed_allow,
        on_down=allowed.discard,
        binary_override=fake,
    )
    result = []
    starter = threading.Thread(target=lambda: result.append(tm.start(timeout=10)))
    starter.start()
    assert callback_entered.wait(3)

    # stop() does not wait on the unknown callback and may complete before its
    # delayed allow-list mutation. start() must detect that and undo the late add.
    tm.stop()
    release_callback.set()
    starter.join(timeout=3)

    assert result == [False]
    assert not starter.is_alive()
    assert allowed == set()
    assert tm.status()["state"] == "off"


def test_failed_on_up_callback_reaps_tunnel_and_compensates(tmp_path):
    fake = _fake_cloudflared(
        tmp_path,
        'echo "INF https://callback-fail.trycloudflare.com registered"\nsleep 30\n',
    )
    allowed = set()

    def failing_allow(host):
        allowed.add(host)
        raise RuntimeError("allow-list unavailable")

    tm = TunnelManager(
        8737,
        on_up=failing_allow,
        on_down=allowed.discard,
        binary_override=fake,
    )

    assert tm.start(timeout=10) is False

    assert allowed == set()
    assert tm._proc is None
    assert tm.status()["state"] == "error"
    assert "注册失败" in tm.status()["error"]
