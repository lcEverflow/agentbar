import json
import threading
import urllib.error
import urllib.request

import pytest

from agentbar.server import ApiServer, _load_web

from conftest import wait_for


@pytest.fixture(autouse=True)
def _stub_cli_credential_status(monkeypatch):
    """HTTP tests never probe the developer machine's real CLI login state."""
    monkeypatch.setattr("agentbar.server.credential_status", lambda tool, **kwargs: {
        "available": False,
        "source": f"{tool}_cli",
        "status": "not_logged_in",
        "detail": "not logged in",
        "needs_authorization": False,
    })


@pytest.fixture
def api(core, settings):
    settings.port = 0  # 随机端口
    srv = ApiServer(core, settings)
    srv.start()
    yield srv, settings
    srv.stop()


def _call(srv, path, method="GET", token=None, body=None, host=None, headers=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.port}{path}",
        method=method,
        data=json.dumps(body).encode() if body else None,
    )
    if token:
        req.add_header("X-Agentbar-Token", token)
    if host:
        req.add_header("Host", host)
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _call_raw(srv, path, data, token):
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.port}{path}",
        method="POST",
        data=data,
        headers={
            "X-Agentbar-Token": token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode())


def test_ping_no_auth(api):
    srv, _ = api
    code, j = _call(srv, "/api/ping")
    assert code == 200 and j["app"] == "agentbar"


def test_server_stop_before_start_is_bounded_and_idempotent(core, settings):
    settings.port = 0
    srv = ApiServer(core, settings)
    stopped = threading.Event()
    thread = threading.Thread(target=lambda: (srv.stop(), stopped.set()), daemon=True)
    thread.start()

    assert stopped.wait(2)
    thread.join(timeout=1)
    srv.stop()
    srv.start()  # stopped instances cannot be resurrected on a closed socket
    assert srv._thread is None


def test_state_requires_token(api):
    srv, s = api
    code, _ = _call(srv, "/api/state")
    assert code == 401
    code, _ = _call(srv, "/api/state", token="wrong-token")
    assert code == 401
    code, j = _call(srv, "/api/state", token=s.token)
    assert code == 200 and j["ok"] and "tasks" in j


def test_query_token_is_rejected(api):
    """Access tokens must never work from a URL query string."""
    srv, s = api
    code, j = _call(srv, f"/api/state?token={s.token}")
    assert code == 401
    assert j["error"] == "unauthorized"


def test_dns_rebinding_blocked(api):
    srv, s = api
    code, _ = _call(srv, "/api/state", token=s.token, host="evil.example.com")
    assert code == 403


def test_lan_ip_host_allowed(api):
    """lan_access 模式下 IP 字面量 Host 放行（手机以 http://10.x… 访问）。"""
    _srv, s = api
    s.port = 0
    s.lan_access = True
    srv = ApiServer(_srv.core, s)
    srv.start()
    try:
        code, j = _call(srv, "/api/state", token=s.token, host="172.20.118.198:8737")
        assert code == 200 and j["ok"]
        # IPv6 字面量
        code, _ = _call(srv, "/api/ping", host="[fe80::1]:8737")
        assert code == 200
    finally:
        srv.stop()


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


def test_html_and_api_responses_have_security_headers(api):
    srv, s = api
    with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/", timeout=5) as r:
        assert r.headers["Cache-Control"] == "no-store"
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["Referrer-Policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]

    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.port}/api/state",
        headers={"X-Agentbar-Token": s.token},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.headers["Cache-Control"] == "no-store"
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["X-Content-Type-Options"] == "nosniff"


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


def test_log_endpoint_rejects_unknown_or_invalid_task_id(api):
    srv, settings = api

    code, payload = _call(
        srv, "/api/tasks/not-a-task/log", token=settings.token,
    )
    assert code == 404
    assert payload["error"] == "任务不存在"

    code, payload = _call(
        srv, "/api/tasks/%2E%2E%2Funsafe/log", token=settings.token,
    )
    assert code in {400, 404}
    assert payload["ok"] is False


def test_transcript_session_recovery_persist_failure_rolls_back_and_returns_503(
    api, core, tmp_path, monkeypatch,
):
    srv, settings = api
    core.pause_all()
    task = core.add_task("original", "fake", str(tmp_path))
    with core._lock:
        task.started_at = 100
        task.session_id = None

    with monkeypatch.context() as patcher:
        patcher.setattr(
            "agentbar.transcript.recover_session_id",
            lambda *_args: "recovered-session",
        )
        patcher.setattr(
            core.store,
            "save",
            lambda _data: (_ for _ in ()).throw(OSError("secret disk detail")),
        )

        code, payload = _call(
            srv, f"/api/tasks/{task.id}/transcript", token=settings.token,
        )

    assert code == 503
    assert "操作未生效" in payload["error"]
    assert "secret disk detail" not in json.dumps(payload)
    assert task.session_id is None


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


@pytest.mark.parametrize(
    "field,value",
    [
        ("prompt", ["not", "text"]),
        ("tool", ["fake"]),
        ("profile", {"name": "edits"}),
        ("model", ["small"]),
        ("effort", {"level": "low"}),
        ("cwd", ["/tmp"]),
    ],
)
def test_add_task_rejects_non_string_fields(api, tmp_path, field, value):
    srv, settings = api
    body = {"prompt": "OK", "tool": "fake", "cwd": str(tmp_path)}
    body[field] = value

    code, payload = _call(srv, "/api/tasks", "POST", settings.token, body)

    assert code == 400
    assert payload["ok"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("prompt", ["not", "text"]),
        ("tool", ["fake"]),
        ("profile", {"name": "edits"}),
        ("model", ["small"]),
        ("effort", {"level": "low"}),
        ("cwd", ["/tmp"]),
    ],
)
def test_edit_task_rejects_non_string_fields(api, core, tmp_path, field, value):
    srv, settings = api
    core.pause_all()
    task = core.add_task("original", "fake", str(tmp_path))

    code, payload = _call(
        srv, f"/api/tasks/{task.id}", "PUT", settings.token, {field: value},
    )

    assert code == 400
    assert payload["ok"] is False
    assert core.snapshot()["tasks"][-1]["prompt"] == "original"


def test_json_body_errors_have_explicit_statuses(api):
    srv, settings = api

    code, payload = _call_raw(
        srv, "/api/tasks", b"{not-json", settings.token,
    )
    assert code == 400
    assert payload == {"ok": False, "error": "请求体必须是合法 JSON object"}

    code, payload = _call(
        srv, "/api/tasks", "POST", settings.token, ["not", "an", "object"],
    )
    assert code == 400
    assert payload == {"ok": False, "error": "请求体必须是 JSON object"}

    code, payload = _call_raw(
        srv, "/api/tasks", b"x" * 200_001, settings.token,
    )
    assert code == 413
    assert "请求体过大" in payload["error"]


@pytest.mark.parametrize("operation", ["add", "edit", "cancel", "pause-all", "resume-all"])
def test_task_mutation_persistence_failures_return_503_and_roll_back(
    api, core, tmp_path, monkeypatch, operation,
):
    srv, settings = api
    task = None
    if operation in {"add", "edit", "cancel", "resume-all"}:
        core.pause_all()
    if operation in {"edit", "cancel"}:
        task = core.add_task("original", "fake", str(tmp_path))

    tasks_before = core.snapshot()["tasks"]
    paused_before = core.paused
    with monkeypatch.context() as patcher:
        patcher.setattr(
            core.store,
            "save",
            lambda _data: (_ for _ in ()).throw(OSError("secret disk detail")),
        )
        if operation == "add":
            code, payload = _call(srv, "/api/tasks", "POST", settings.token, {
                "prompt": "must roll back", "tool": "fake", "cwd": str(tmp_path),
            })
        elif operation == "edit":
            code, payload = _call(
                srv, f"/api/tasks/{task.id}", "PUT", settings.token,
                {"title": "must roll back"},
            )
        elif operation == "cancel":
            code, payload = _call(
                srv, f"/api/tasks/{task.id}/cancel", "POST", settings.token,
            )
        elif operation == "pause-all":
            code, payload = _call(srv, "/api/pause-all", "POST", settings.token)
        else:
            code, payload = _call(srv, "/api/resume-all", "POST", settings.token)

    assert code == 503
    assert payload["ok"] is False
    assert "操作未生效" in payload["error"]
    assert "secret disk detail" not in json.dumps(payload)
    assert core.snapshot()["tasks"] == tasks_before
    assert core.paused is paused_before


def test_add_task_is_rejected_once_scheduler_shutdown_begins(api, core, tmp_path):
    """Even an already-accepted HTTP handler cannot enqueue after quiescence."""
    srv, settings = api
    core.shutdown()

    code, payload = _call(srv, "/api/tasks", "POST", settings.token, {
        "prompt": "must not run", "tool": "fake", "cwd": str(tmp_path),
    })

    assert code == 400
    assert payload["ok"] is False
    assert "正在停止" in payload["error"]
    assert core.snapshot()["tasks"] == []


def test_server_stop_drains_and_rejects_uncommitted_accepted_handler(
    api, core, tmp_path, monkeypatch,
):
    srv, settings = api
    entered = threading.Event()
    release = threading.Event()
    response = []
    real_add = core.add_task

    def blocked_add(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return real_add(*args, **kwargs)

    monkeypatch.setattr(core, "add_task", blocked_add)
    requester = threading.Thread(
        target=lambda: response.append(_call(
            srv, "/api/tasks", "POST", settings.token,
            {"prompt": "OK", "tool": "fake", "cwd": str(tmp_path)},
        )),
        daemon=True,
    )
    requester.start()
    assert entered.wait(2)

    stopped = threading.Event()
    stopper = threading.Thread(target=lambda: (srv.stop(), stopped.set()), daemon=True)
    stopper.start()
    assert not stopped.wait(0.1)

    release.set()
    assert stopped.wait(3)
    stopper.join(timeout=1)
    requester.join(timeout=1)
    assert response and response[0][0] == 503
    assert core.snapshot()["tasks"] == []


def test_server_stop_is_bounded_and_late_task_handler_cannot_commit(
    api, core, tmp_path, monkeypatch,
):
    """A handler that outlives bounded drain rechecks admission in Scheduler."""
    srv, settings = api
    entered = threading.Event()
    release = threading.Event()
    response = []
    real_add = core.add_task

    def blocked_add(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return real_add(*args, **kwargs)

    monkeypatch.setattr(core, "add_task", blocked_add)
    monkeypatch.setattr("agentbar.server.HANDLER_DRAIN_SECONDS", 0.1)
    requester = threading.Thread(
        target=lambda: response.append(_call(
            srv, "/api/tasks", "POST", settings.token,
            {"prompt": "must not commit", "tool": "fake", "cwd": str(tmp_path)},
        )),
        daemon=True,
    )
    requester.start()
    assert entered.wait(2)

    stopped = threading.Event()
    stopper = threading.Thread(target=lambda: (srv.stop(), stopped.set()), daemon=True)
    stopper.start()
    assert stopped.wait(2)

    release.set()
    requester.join(timeout=2)
    stopper.join(timeout=1)
    assert response and response[0][0] == 503
    assert core.snapshot()["tasks"] == []


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
    assert f"#token={s.token}" in url
    assert f"?token={s.token}" not in url
    assert "tool=claude" in url
    assert "focus=prompt" in url


def test_mobile_url_uses_fragment_not_query(api, monkeypatch):
    srv, s = api
    s.lan_access = True
    monkeypatch.setattr("agentbar.server.lan_ip", lambda: "192.0.2.8")
    url = srv.mobile_url()
    assert url == f"http://192.0.2.8:{srv.port}/m#token={s.token}"
    assert "?token=" not in url


def test_pause_resume_all(api, core):
    srv, s = api
    assert _call(srv, "/api/pause-all", "POST", s.token)[0] == 200
    assert core.paused is True
    assert _call(srv, "/api/resume-all", "POST", s.token)[0] == 200
    assert core.paused is False


def test_quota_refresh_endpoint_rejects_missing_source(api):
    srv, s = api
    refreshed = []
    srv.core.quota.refresh_now = lambda tool=None: refreshed.append(tool)
    code, j = _call(srv, "/api/quota/refresh", "POST", s.token)
    assert code == 400 and not j["ok"]
    assert "必须指定" in j["error"]
    assert refreshed == []


def test_quota_refresh_endpoint_targets_only_enabled_source(api):
    srv, s = api
    refreshed = []
    srv.core.quota.provider_tools = lambda: ["codex"]
    srv.core.quota.refresh_now = lambda tool=None: refreshed.append(tool)

    code, j = _call(srv, "/api/quota/refresh", "POST", s.token,
                    {"tool": "codex"})
    assert code == 202 and j["ok"]
    assert refreshed == ["codex"]

    srv.core.quota.provider_tools = lambda: ["mytoken"]
    code, j = _call(srv, "/api/quota/refresh", "POST", s.token,
                    {"tool": "mytoken"})
    assert code == 202 and j["ok"]
    assert refreshed == ["codex", "mytoken"]

    code, j = _call(srv, "/api/quota/refresh", "POST", s.token,
                    {"tool": "claude"})
    assert code == 400
    assert "未检测到 Claude Code 登录态" in j["error"]
    assert refreshed == ["codex", "mytoken"]


def test_provider_config_endpoint_masks_cookie(api):
    srv, s = api
    s.providers["mytoken"].update({
        "enabled": True,
        "cookie": "SESSION=secret; token=hidden",
        "unit": "credits",
    })
    code, j = _call(srv, "/api/provider-config", token=s.token)
    assert code == 200
    assert j["providers"]["mytoken"]["enabled"] is True
    assert j["providers"]["mytoken"]["cookie_set"] is True
    assert "SESSION" in j["providers"]["mytoken"]["cookie_preview"]
    assert "secret" not in json.dumps(j)


def test_provider_cookie_preview_never_echoes_raw_or_malformed_secret(api):
    srv, s = api
    secret = "SENTINEL_RAW_COOKIE_SECRET_0123456789"
    s.providers["mytoken"].update({"enabled": True, "cookie": secret})

    code, payload = _call(srv, "/api/provider-config", token=s.token)

    assert code == 200
    preview = payload["providers"]["mytoken"]["cookie_preview"]
    assert preview == "已配置"
    assert secret not in json.dumps(payload)


def test_provider_config_exposes_only_non_secret_cli_status(api, monkeypatch):
    srv, s = api
    monkeypatch.setattr("agentbar.server.credential_status", lambda *a, **k: {
        "available": True,
        "source": "codex_app_server",
        "status": "available",
    })
    s.quota_sources["codex"].update({
        "enabled": True,
        "model": "codex_bengalfox",
        "access_token": "oauth-super-secret",
        "account_id": "account-private-id",
    })
    code, j = _call(srv, "/api/provider-config", token=s.token)
    assert code == 200
    source = j["quota_sources"]["codex"]
    assert source == {
        "enabled": True,
        "model": "codex_bengalfox",
        "credential_available": True,
        "credential_source": "codex_app_server",
        "credential_status": "available",
    }
    serialized = json.dumps(j)
    assert "oauth-super-secret" not in serialized
    assert "account-private-id" not in serialized
    assert "access_token" not in source and "api_key" not in source
    assert "account_id" not in source

    code, state = _call(srv, "/api/state", token=s.token)
    assert code == 200
    serialized_state = json.dumps(state)
    assert "oauth-super-secret" not in serialized_state
    assert "account-private-id" not in serialized_state
    assert "access_token" not in state["quota_source_config"]["codex"]
    assert "account_id" not in state["quota_source_config"]["codex"]


def test_cli_credential_redetect_is_local_noninteractive_and_source_scoped(
    api, monkeypatch,
):
    srv, settings = api
    settings.quota_sources["codex"]["enabled"] = True
    calls = []

    def detect(tool, **kwargs):
        calls.append((tool, kwargs))
        return {
            "available": True,
            "source": "codex_app_server",
            "status": "available",
            "detail": "must not be returned",
            "account_id": "must-not-leak",
        }

    monkeypatch.setattr("agentbar.server.credential_status", detect)
    reloads = []
    refreshes = []
    srv.core.quota.reload_fetchers = lambda refresh=True: reloads.append(refresh)
    srv.core.quota.refresh_now = refreshes.append

    code, payload = _call(
        srv,
        "/api/provider-config/detect-credential",
        "POST",
        settings.token,
        {"source": "codex"},
    )

    assert code == 200
    assert calls and all(call[1]["allow_interactive"] is False for call in calls)
    assert calls[0][1]["refresh"] is True
    assert calls[0][1]["settings"] is settings
    assert reloads == [False]
    assert refreshes == ["codex"]
    source = payload["quota_sources"]["codex"]
    assert source["credential_available"] is True
    assert source["credential_source"] == "codex_app_server"
    assert set(source) == {
        "enabled", "model", "credential_available",
        "credential_source", "credential_status",
    }
    serialized = json.dumps(payload)
    assert "must-not-leak" not in serialized
    assert "must not be returned" not in serialized


def test_cli_credential_redetect_rejects_secret_fields_before_detection(
    api, monkeypatch,
):
    srv, settings = api
    monkeypatch.setattr(
        "agentbar.server.credential_status",
        lambda *a, **k: pytest.fail("secret payload reached detector"),
    )

    code, payload = _call(
        srv,
        "/api/provider-config/detect-credential",
        "POST",
        settings.token,
        {"source": "codex", "access_token": "must-not-save"},
    )

    assert code == 400
    assert "不接受任何凭据" in payload["error"]


def test_provider_config_save_reloads_fetchers(api):
    srv, s = api
    reloaded = []
    srv.core.quota.reload_fetchers = lambda: reloaded.append(True)
    code, j = _call(srv, "/api/provider-config", "POST", s.token, {
        "providers": {
            "mytoken": {
                "enabled": True,
                "cookie": "c=1",
                "unit": "percent",
                "refresh_seconds": 90,
            }
        },
        "title_provider": "mytoken",
    })
    assert code == 200, j
    assert s.providers["mytoken"]["enabled"] is True
    assert s.providers["mytoken"]["cookie"] == "c=1"
    assert s.providers["mytoken"]["unit"] == "percent"
    assert s.title_provider == "mytoken"
    assert reloaded == [True]


@pytest.mark.parametrize(
    "body",
    [
        {"providers": []},
        {"providers": {"mytoken": []}},
        {"providers": {"mytoken": {"enabled": "false"}}},
        {"providers": {"mytoken": {"cookie": {"secret": "value"}}}},
        {"providers": {"mytoken": {"refresh_seconds": True}}},
        {"providers": {"mytoken": {"unit": ["credits"]}}},
        {"quota_sources": []},
        {"quota_sources": {"claude": []}},
        {"quota_sources": {"claude": {"enabled": 1}}},
        {"quota_sources": {"claude": {"model": ["sonnet"]}}},
        {"quota_sources": {"claude": {"access_token": {"token": "secret"}}}},
        {"quota_sources": {"codex": {"account_id": ["account"]}}},
        {"access_token": "top-level-secret"},
        {"usage_auto_refresh": "false"},
        {"title_provider": {"name": "codex"}},
        {"title_provider": "unknown"},
    ],
)
def test_provider_config_rejects_malformed_known_fields_without_mutation(
    api, body,
):
    srv, settings = api
    before = (
        settings.title_provider,
        settings.usage_auto_refresh,
        json.loads(json.dumps(settings.providers)),
        json.loads(json.dumps(settings.quota_sources)),
    )
    srv.core.quota.reload_fetchers = lambda: pytest.fail("invalid config reloaded")

    code, payload = _call(
        srv, "/api/provider-config", "POST", settings.token, body,
    )

    assert code == 400
    assert payload["ok"] is False
    assert (
        settings.title_provider,
        settings.usage_auto_refresh,
        settings.providers,
        settings.quota_sources,
    ) == before


def test_provider_config_cli_source_and_limit_id_roundtrip(api, monkeypatch):
    srv, s = api
    monkeypatch.setattr("agentbar.server.credential_status", lambda *a, **k: {
        "available": True, "source": "codex_app_server", "status": "available",
    })
    reloaded = []
    srv.core.quota.reload_fetchers = lambda: reloaded.append(True)
    code, j = _call(srv, "/api/provider-config", "POST", s.token, {
        "quota_sources": {
            "codex": {
                "enabled": True,
                "model": "codex_bengalfox",
            },
        },
        "usage_auto_refresh": False,
        "title_provider": "codex",
    })
    assert code == 200, j
    assert s.quota_sources["codex"] == {
        "enabled": True,
        "model": "codex_bengalfox",
    }
    assert s.usage_auto_refresh is False
    assert s.title_provider == "codex"
    assert j["quota_sources"]["codex"] == {
        "enabled": True,
        "model": "codex_bengalfox",
        "credential_available": True,
        "credential_source": "codex_app_server",
        "credential_status": "available",
    }
    assert reloaded == [True]


def test_provider_config_rejects_legacy_secret_payload_without_mutation(api):
    srv, s = api
    before = json.loads(json.dumps(s.quota_sources))
    code, j = _call(srv, "/api/provider-config", "POST", s.token, {
        "quota_sources": {
            "claude": {
                "api_key": "legacy-client-token",
            },
        },
    })
    assert code == 400, j
    assert s.quota_sources == before
    serialized = json.dumps(j)
    assert "legacy-client-token" not in serialized
    assert "API 不接收任何密钥" in j["error"]


def test_provider_config_is_direct_local_only_even_through_loopback_proxy(api):
    srv, s = api
    tunnel_host = "audit-security.trycloudflare.com"
    srv.allow_host(tunnel_host)
    before = json.loads(json.dumps(s.quota_sources))

    code, j = _call(
        srv,
        "/api/provider-config",
        "POST",
        s.token,
        {"quota_sources": {"claude": {
            "enabled": True,
            "access_token": "must-not-save",
        }}},
        host=tunnel_host,
    )
    assert code == 403
    assert "本机" in j["error"]
    assert s.quota_sources == before

    code, _ = _call(
        srv,
        "/api/provider-config",
        "POST",
        s.token,
        {"providers": {"mytoken": {"cookie": "must-not-save"}}},
        headers={"Forwarded": "for=203.0.113.8"},
    )
    assert code == 403
    assert s.providers["mytoken"]["cookie"] == ""

    code, _ = _call(
        srv,
        "/api/provider-config",
        "POST",
        s.token,
        {"providers": {"mytoken": {"cookie": "must-not-save"}}},
        headers={"X-Forwarded-Port": "443"},
    )
    assert code == 403
    assert s.providers["mytoken"]["cookie"] == ""

    code, _ = _call(
        srv,
        "/api/provider-config",
        token=s.token,
        host=tunnel_host,
    )
    assert code == 403


def test_cookie_import_and_debug_dispatch_are_direct_local_only(api, monkeypatch):
    srv, s = api
    tunnel_host = "audit-security.trycloudflare.com"
    srv.allow_host(tunnel_host)
    imported = []
    monkeypatch.setattr(
        "agentbar.server.import_cookie_header",
        lambda host: imported.append(host),
    )

    code, _ = _call(
        srv,
        "/api/provider-config/import-cookie",
        "POST",
        s.token,
        {"provider": "mytoken"},
        host=tunnel_host,
    )
    assert code == 403
    assert imported == []

    code, _ = _call(
        srv,
        "/api/provider-config/detect-credential",
        "POST",
        s.token,
        {"source": "codex"},
        host=tunnel_host,
    )
    assert code == 403

    seen = []
    srv.hooks["dispatch"] = seen.append
    code, _ = _call(
        srv,
        "/api/debug/dispatch",
        "POST",
        s.token,
        {"action": "open_panel"},
        host=tunnel_host,
    )
    assert code == 403
    assert seen == []


def test_provider_config_save_failure_rolls_back_memory(api, monkeypatch):
    from agentbar.server import _apply_provider_settings

    _srv, s = api
    before = (
        s.title_provider,
        json.loads(json.dumps(s.providers)),
        json.loads(json.dumps(s.quota_sources)),
    )

    def fail_save(_settings):
        raise OSError("simulated disk failure")

    monkeypatch.setattr("agentbar.server.save_settings", fail_save)
    with pytest.raises(OSError):
        _apply_provider_settings(s, {
            "title_provider": "mytoken",
            "providers": {"mytoken": {"enabled": True}},
        })
    assert s.title_provider == before[0]
    assert s.providers == before[1]
    assert s.quota_sources == before[2]


def test_provider_config_persistence_failure_returns_503_without_secret_echo(
    api, monkeypatch,
):
    srv, settings = api
    before = json.loads(json.dumps(settings.quota_sources))
    monkeypatch.setattr(
        "agentbar.server.save_settings",
        lambda _settings: (_ for _ in ()).throw(OSError("secret disk detail")),
    )

    code, payload = _call(srv, "/api/provider-config", "POST", settings.token, {
        "title_provider": "mytoken",
    })

    assert code == 503
    assert "操作未生效" in payload["error"]
    assert "must-not-echo" not in json.dumps(payload)
    assert "secret disk detail" not in json.dumps(payload)
    assert settings.quota_sources == before


def test_provider_config_save_removes_existing_legacy_secrets(api, monkeypatch):
    srv, s = api
    monkeypatch.setattr("agentbar.server.credential_status", lambda *a, **k: {
        "available": True, "source": "codex_app_server", "status": "available",
    })
    s.quota_sources["codex"].update({
        "enabled": True,
        "model": "old-model",
        "access_token": "keep-this-secret",
        "account_id": "keep-this-account",
    })
    srv.core.quota.reload_fetchers = lambda: None
    code, j = _call(srv, "/api/provider-config", "POST", s.token, {
        "quota_sources": {
            "codex": {"enabled": True, "model": "new-model"},
        },
    })
    assert code == 200, j
    assert s.quota_sources["codex"] == {
        "enabled": True,
        "model": "new-model",
    }
    assert j["quota_sources"]["codex"]["credential_available"] is True


def test_provider_config_rejects_blank_legacy_secret_fields(api):
    srv, s = api
    s.quota_sources["claude"].update({
        "enabled": True,
        "model": "sonnet",
        "access_token": "remove-this-secret",
        "account_id": "remove-this-account",
    })
    srv.core.quota.reload_fetchers = lambda: None
    code, j = _call(srv, "/api/provider-config", "POST", s.token, {
        "quota_sources": {
            "claude": {
                "enabled": False,
                "access_token": "",
                "account_id": "",
            },
        },
    })
    assert code == 400, j
    assert s.quota_sources["claude"]["enabled"] is True
    assert s.quota_sources["claude"]["access_token"] == "remove-this-secret"


def test_provider_config_saves_enabled_source_while_cli_is_logged_out(
    api, monkeypatch,
):
    srv, s = api
    monkeypatch.setattr("agentbar.server.credential_status", lambda *a, **k: {
        "available": False, "source": "claude_cli", "status": "not_logged_in",
    })
    reloaded = []
    srv.core.quota.reload_fetchers = lambda: reloaded.append(True)
    code, j = _call(srv, "/api/provider-config", "POST", s.token, {
        "quota_sources": {
            "claude": {
                "enabled": True,
            },
        },
    })
    assert code == 200
    assert s.quota_sources["claude"]["enabled"] is True
    assert j["quota_sources"]["claude"]["credential_available"] is False
    assert j["quota_sources"]["claude"]["credential_status"] == "not_logged_in"
    assert reloaded == [True]


def test_provider_cookie_import_updates_config(api, monkeypatch):
    from agentbar.browser_cookies import ImportedCookie

    srv, s = api

    def fake_import(host):
        assert host == "tokenverse.corp.kuaishou.com"
        return ImportedCookie("tv=1; sso=2", "Chrome / Default", 2)

    monkeypatch.setattr("agentbar.server.import_cookie_header", fake_import)
    reloaded = []
    srv.core.quota.reload_fetchers = lambda: reloaded.append(True)
    code, j = _call(srv, "/api/provider-config/import-cookie", "POST", s.token,
                    {"provider": "tokenverse"})
    assert code == 200, j
    assert "2 个 Cookie" in j["message"]
    assert s.providers["tokenverse"]["enabled"] is True
    assert s.providers["tokenverse"]["cookie"] == "tv=1; sso=2"
    assert reloaded == [True]


def test_server_stop_is_bounded_and_discards_late_cookie_import(
    api, monkeypatch,
):
    from agentbar.browser_cookies import ImportedCookie

    srv, settings = api
    entered = threading.Event()
    release = threading.Event()
    response = []
    before = json.loads(json.dumps(settings.providers))
    reloads = []

    def blocked_import(_host):
        entered.set()
        assert release.wait(3)
        return ImportedCookie("late=secret", "Chrome / Late", 1)

    monkeypatch.setattr("agentbar.server.import_cookie_header", blocked_import)
    monkeypatch.setattr("agentbar.server.HANDLER_DRAIN_SECONDS", 0.1)
    srv.core.quota.reload_fetchers = lambda: reloads.append(True)
    requester = threading.Thread(
        target=lambda: response.append(_call(
            srv,
            "/api/provider-config/import-cookie",
            "POST",
            settings.token,
            {"provider": "tokenverse"},
        )),
        daemon=True,
    )
    requester.start()
    assert entered.wait(2)

    stopped = threading.Event()
    stopper = threading.Thread(target=lambda: (srv.stop(), stopped.set()), daemon=True)
    stopper.start()
    assert stopped.wait(2)

    release.set()
    requester.join(timeout=2)
    stopper.join(timeout=1)
    assert response and response[0][0] == 503
    assert settings.providers == before
    assert reloads == []


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
    code, j = _call(srv, "/api/debug/dispatch", "POST", s.token,
                    {"action": "provider_settings"})
    assert code == 202 and j["action"] == "provider_settings"
    assert seen[-1] == "provider_settings"
    # 全局额度刷新已移除；调试通道也只能转发带来源的刷新动作。
    code, _ = _call(srv, "/api/debug/dispatch", "POST", s.token,
                    {"action": "refresh_quota"})
    assert code == 400 and seen[-1] == "provider_settings"
    code, _ = _call(srv, "/api/debug/dispatch", "POST", s.token,
                    {"action": "refresh_quota:"})
    assert code == 400 and seen[-1] == "provider_settings"
    code, j = _call(srv, "/api/debug/dispatch", "POST", s.token,
                    {"action": "refresh_quota:codex"})
    assert code == 202 and j["action"] == "refresh_quota:codex"
    assert seen[-1] == "refresh_quota:codex"
    # 白名单外的动作（quit 等）拒绝远程触发
    code, _ = _call(srv, "/api/debug/dispatch", "POST", s.token, {"action": "quit"})
    assert code == 400


def test_tools_endpoint(api):
    srv, s = api
    code, j = _call(srv, "/api/tools", token=s.token)
    names = {t["name"] for t in j["tools"]}
    assert {"claude", "codex", "fake"} <= names


def test_desktop_editor_uses_adapter_capabilities_instead_of_hardcoded_lists():
    html = _load_web("index.html")
    assert "availableTools.find" in html
    assert "const presets = info.models || []" in html
    assert "const efforts = info.efforts || []" in html
    assert "sonnet\", \"opus\", \"fable" not in html
    assert "clearCodexAccountId" not in html
    assert "q-codex-account" not in html
    assert "sourceSecret" not in html
    assert "Codex 额度由 CLI App Server" in html
    assert "可选 limitId" in html
    assert 'cliSource ? { source: name } : { tool: name }' in html
    assert '"/api/provider-config/detect-credential"' in html
    assert '"/api/quota/refresh"' in html
