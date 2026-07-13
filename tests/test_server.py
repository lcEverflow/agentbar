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


def test_index_served(api):
    srv, _ = api
    with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/", timeout=5) as r:
        assert r.status == 200
        assert "AgentBar" in r.read().decode()


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
    ):
        code, j = _call(srv, "/api/tasks", "POST", s.token, body)
        assert code == 400, body


def test_pause_resume_all(api, core):
    srv, s = api
    assert _call(srv, "/api/pause-all", "POST", s.token)[0] == 200
    assert core.paused is True
    assert _call(srv, "/api/resume-all", "POST", s.token)[0] == 200
    assert core.paused is False


def test_tools_endpoint(api):
    srv, s = api
    code, j = _call(srv, "/api/tools", token=s.token)
    names = {t["name"] for t in j["tools"]}
    assert {"claude", "codex", "fake"} <= names
