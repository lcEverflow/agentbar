import json
import urllib.error
import urllib.request

import pytest

from agentbar.server import ApiServer

from conftest import wait_for


@pytest.fixture
def api(core, settings):
    settings.port = 0  # 随机端口
    srv = ApiServer(core, settings)
    srv.start()
    yield srv, settings
    srv.stop()


def _call(srv, path, method="GET", token=None, body=None, host=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.port}{path}",
        method=method,
        data=json.dumps(body).encode() if body else None,
    )
    if token:
        req.add_header("X-Agentbar-Token", token)
    if host:
        req.add_header("Host", host)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_ping_no_auth(api):
    srv, _ = api
    code, j = _call(srv, "/api/ping")
    assert code == 200 and j["app"] == "agentbar"


def test_state_requires_token(api):
    srv, s = api
    code, _ = _call(srv, "/api/state")
    assert code == 401
    code, _ = _call(srv, "/api/state", token="wrong-token")
    assert code == 401
    code, j = _call(srv, "/api/state", token=s.token)
    assert code == 200 and j["ok"] and "tasks" in j


def test_dns_rebinding_blocked(api):
    srv, s = api
    code, _ = _call(srv, "/api/state", token=s.token, host="evil.example.com")
    assert code == 403


def test_lan_ip_host_allowed(api):
    """lan_access 模式下 IP 字面量 Host 放行（手机以 http://10.x… 访问）。"""
    srv, s = api
    code, j = _call(srv, "/api/state", token=s.token, host="172.20.118.198:8737")
    assert code == 200 and j["ok"]
    # IPv6 字面量
    code, _ = _call(srv, "/api/ping", host="[fe80::1]:8737")
    assert code == 200


def test_lan_host_rejected_when_disabled(core, settings):
    settings.port = 0
    settings.lan_access = False
    srv = ApiServer(core, settings)
    srv.start()
    try:
        code, _ = _call(srv, "/api/ping", host="172.20.118.198:8737")
        assert code == 403
    finally:
        srv.stop()


def test_tunnel_host_dynamic_allow(api):
    """公网隧道域名动态注册后放行，注销后恢复 403。"""
    srv, s = api
    host = "abc-def.trycloudflare.com"
    code, _ = _call(srv, "/api/ping", host=host)
    assert code == 403
    srv.allow_host(host)
    code, j = _call(srv, "/api/ping", host=host)
    assert code == 200 and j["app"] == "agentbar"
    srv.disallow_host(host)
    code, _ = _call(srv, "/api/ping", host=host)
    assert code == 403


def test_index_served(api):
    srv, _ = api
    with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/", timeout=5) as r:
        assert r.status == 200
        assert "AgentBar" in r.read().decode()


def test_mobile_page_served(api):
    srv, _ = api
    with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/m", timeout=5) as r:
        assert r.status == 200
        body = r.read().decode()
        assert "AgentBar" in body and "agentbar_token" in body


def test_add_task_and_lifecycle_via_api(api, core, tmp_path):
    srv, s = api
    code, j = _call(srv, "/api/tasks", "POST", s.token,
                    {"prompt": "OK", "tool": "fake", "cwd": str(tmp_path)})
    assert code == 200, j
    tid = j["task"]["id"]
    wait_for(lambda: _call(srv, "/api/state", token=s.token)[1]["tasks"][-1]["state"]
             == "succeeded", desc="task done via api")
    code, j = _call(srv, f"/api/tasks/{tid}/log", token=s.token)
    assert code == 200 and "FAKE DONE" in j["log"]


def test_add_task_validation_errors(api, tmp_path):
    srv, s = api
    for body in (
        {"prompt": "", "tool": "fake", "cwd": str(tmp_path)},
        {"prompt": "OK", "tool": "nope", "cwd": str(tmp_path)},
        {"prompt": "OK", "tool": "fake", "cwd": "/no/such/dir"},
        {"prompt": "OK", "tool": "fake", "cwd": str(tmp_path), "profile": "full"},
        {"prompt": "OK", "tool": "fake", "cwd": str(tmp_path), "effort": "ultra"},
    ):
        code, j = _call(srv, "/api/tasks", "POST", s.token, body)
        assert code == 400, body


def test_add_task_records_model_and_effort(api, tmp_path):
    srv, s = api
    code, j = _call(srv, "/api/tasks", "POST", s.token, {
        "prompt": "OK", "tool": "fake", "cwd": str(tmp_path),
        "model": "custom-small", "effort": "low",
    })
    assert code == 200, j
    assert j["task"]["model"] == "custom-small"
    assert j["task"]["effort"] == "low"


def test_edit_queued_task_via_api(api, core, tmp_path):
    srv, s = api
    core.pause_all()  # keep the fake task queued while editing it
    code, j = _call(srv, "/api/tasks", "POST", s.token, {
        "prompt": "old", "tool": "fake", "cwd": str(tmp_path),
    })
    assert code == 200, j
    task_id = j["task"]["id"]

    code, j = _call(srv, f"/api/tasks/{task_id}", "PUT", s.token, {
        "prompt": "new prompt", "title": "new title", "tool": "fake",
        "cwd": str(tmp_path), "profile": "edits", "model": "small", "effort": "low",
    })
    assert code == 200, j
    assert j["task"]["state"] == "queued"
    assert j["task"]["prompt"] == "new prompt"
    assert j["task"]["model"] == "small"
    assert j["task"]["effort"] == "low"


def test_panel_url_preserves_token_and_quick_add_intent(api):
    srv, s = api
    url = srv.url(with_token=True, tool="claude", focus="prompt")
    assert f"token={s.token}" in url
    assert "tool=claude" in url
    assert "focus=prompt" in url


def test_pause_resume_all(api, core):
    srv, s = api
    assert _call(srv, "/api/pause-all", "POST", s.token)[0] == 200
    assert core.paused is True
    assert _call(srv, "/api/resume-all", "POST", s.token)[0] == 200
    assert core.paused is False


def test_quota_refresh_endpoint(api):
    srv, s = api
    code, j = _call(srv, "/api/quota/refresh", "POST", s.token)
    assert code == 202 and j["ok"]


def test_debug_dispatch_404_without_menubar(api):
    srv, s = api
    code, _ = _call(srv, "/api/debug/dispatch", "POST", s.token,
                    {"action": "open_panel"})
    assert code == 404


def test_debug_dispatch_routes_to_hook(api):
    srv, s = api
    seen = []
    srv.hooks["dispatch"] = seen.append
    code, j = _call(srv, "/api/debug/dispatch", "POST", s.token,
                    {"action": "open_panel"})
    assert code == 202 and seen == ["open_panel"]
    # 白名单外的动作（quit 等）拒绝远程触发
    code, _ = _call(srv, "/api/debug/dispatch", "POST", s.token, {"action": "quit"})
    assert code == 400


def test_tools_endpoint(api):
    srv, s = api
    code, j = _call(srv, "/api/tools", token=s.token)
    names = {t["name"] for t in j["tools"]}
    assert {"claude", "codex", "fake"} <= names
