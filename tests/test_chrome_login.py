import time
import subprocess
from pathlib import Path

import pytest

from agentbar.chrome_login import ChromeCDPLogin, ChromeLoginError, _cookie_header


def test_cookie_header_filters_target_domain_expiry_and_duplicates():
    raw = [
        {"name": "accessproxy_session", "value": "parent", "domain": ".corp.kuaishou.com", "path": "/"},
        {"name": "accessproxy_session", "value": "exact", "domain": "mytoken.corp.kuaishou.com", "path": "/"},
        {"name": "JSESSIONID", "value": "ok", "domain": "mytoken.corp.kuaishou.com", "path": "/"},
        {"name": "expired", "value": "no", "domain": "mytoken.corp.kuaishou.com", "expires": time.time() - 1},
        {"name": "foreign", "value": "no", "domain": "example.com", "path": "/"},
    ]
    header = _cookie_header(raw, "mytoken.corp.kuaishou.com")
    assert "accessproxy_session=exact" in header
    assert "accessproxy_session=parent" not in header
    assert "JSESSIONID=ok" in header
    assert "expired=" not in header and "foreign=" not in header


def test_chrome_login_rejects_unknown_provider():
    with pytest.raises(ChromeLoginError, match="未知 provider"):
        ChromeCDPLogin("unknown")


def test_cancel_only_signals_process_without_waiting():
    class Process:
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):  # pragma: no cover - must never be called here
            raise AssertionError("cancel must not block on wait")

    login = ChromeCDPLogin("mytoken")
    process = Process()
    login._process = process
    login.cancel()
    assert login._cancel.is_set()
    assert process.terminated is True


def test_debugger_only_accepts_the_exact_loopback_origin():
    login = ChromeCDPLogin("mytoken")
    login._profile_dir = Path("/tmp/isolated-profile")
    args = login._chrome_args(Path("/Applications/Chrome"))

    assert "--remote-debugging-address=127.0.0.1" in args
    assert f"--remote-allow-origins=http://127.0.0.1:{login.port}" in args
    assert "--remote-allow-origins=*" not in args


def test_close_cleans_profile_even_if_process_never_reports_exit(tmp_path):
    class Process:
        terminated = False
        killed = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("chrome", timeout)

    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "secret-cookie-state").write_text("private", encoding="utf-8")
    process = Process()
    login = ChromeCDPLogin("mytoken")
    login._process = process
    login._profile_dir = profile

    login.close()

    assert process.terminated is True
    assert process.killed is True
    assert not profile.exists()
    assert login._process is None
    assert login._profile_dir is None
