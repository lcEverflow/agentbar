"""Localhost HTTP API + task manager web UI (+ optional LAN access for mobile).

安全：默认绑 127.0.0.1；lan_access=true 时绑 0.0.0.0 供同局域网手机访问。
所有 /api（除 /api/ping）要求 token（Header 或 query）。Host 头校验只放行
IP 字面量（DNS rebinding 必须借助域名，放行裸 IP 不破坏该防御）。
token 存于 state 目录 config.json（0600）。
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlencode, urlparse

from . import __version__
from .config import Settings
from .scheduler import Scheduler

log = logging.getLogger("agentbar.server")

MAX_BODY = 200_000
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


def _host_of(header: str) -> str:
    """Extract host part from a Host header ([::1]:8737 / 10.1.2.3:8737 / localhost)."""
    header = header or ""
    if header.startswith("["):
        return header[1:].split("]")[0].lower()
    return header.split(":")[0].lower()


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def lan_ip() -> str | None:
    """Best-effort LAN IP：UDP connect 只选路由不发包，离线也不阻塞。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None
    finally:
        s.close()


class ApiServer:
    def __init__(self, core: Scheduler, settings: Settings):
        self.core = core
        self.settings = settings
        # menu bar 进程注入：把 action 转发到主线程菜单分发器（调试/远程触发用）
        self.hooks: dict = {"dispatch": None}
        # 动态 Host 白名单（公网隧道域名启动后注册进来；其余域名一律 403）
        self.extra_hosts: set[str] = set()
        handler = _make_handler(core, settings, self.hooks, self.extra_hosts)
        bind = "0.0.0.0" if settings.lan_access else "127.0.0.1"
        self.httpd = ThreadingHTTPServer((bind, settings.port), handler)
        self.httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def url(self, with_token: bool = False, **query: str) -> str:
        """Return a local panel URL, optionally carrying an authenticated UI intent.

        The menu-bar quick-add action deliberately uses the same full editor as
        the panel; a native one-line prompt dialog cannot safely expose cwd,
        model, effort and permission choices.
        """
        base = f"http://127.0.0.1:{self.port}/"
        params = {key: str(value) for key, value in query.items() if value is not None}
        if with_token:
            params["token"] = self.settings.token
        return base + (f"?{urlencode(params)}" if params else "")

    def allow_host(self, hostname: str) -> None:
        self.extra_hosts.add(hostname.lower())

    def disallow_host(self, hostname: str) -> None:
        self.extra_hosts.discard(hostname.lower())

    def mobile_url(self) -> str | None:
        """手机可扫的 LAN 地址（带 token）；未启用 LAN 或取不到 IP 时返回 None。"""
        if not self.settings.lan_access:
            return None
        ip = lan_ip()
        if not ip:
            return None
        return f"http://{ip}:{self.port}/m?token={self.settings.token}"

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, name="agentbar-http", daemon=True
        )
        self._thread.start()
        log.info("api server on %s", self.url())

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _load_web(name: str) -> str:
    return resources.files("agentbar").joinpath(f"web/{name}").read_text("utf-8")


def _make_handler(core: Scheduler, settings: Settings, hooks: dict | None = None,
                  extra_hosts: set | None = None):
    hooks = hooks if hooks is not None else {}
    extra_hosts = extra_hosts if extra_hosts is not None else set()
    class Handler(BaseHTTPRequestHandler):
        server_version = f"AgentBar/{__version__}"

        # ---------- plumbing ----------

        def log_message(self, fmt, *args):  # 安静，不刷 stderr
            log.debug("http: " + fmt, *args)

        def _json(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _html(self, text: str) -> None:
            body = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self) -> bool:
            host = _host_of(self.headers.get("Host"))
            if host in ALLOWED_HOSTS or host in extra_hosts:
                return True
            # LAN 模式放行 IP 字面量（手机浏览器以 http://10.x.x.x:8737 访问）。
            # 其余域名一律拒绝：DNS rebinding 攻击必须经由域名；
            # 公网隧道域名走 extra_hosts 动态注册。
            return settings.lan_access and _is_ip_literal(host)

        def _authed(self, query: dict) -> bool:
            token = self.headers.get("X-Agentbar-Token") or (
                query.get("token", [""])[0]
            )
            return bool(token) and hmac.compare_digest(token, settings.token)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {}

        # ---------- routing ----------

        def do_GET(self):
            if not self._host_ok():
                self._json(403, {"ok": False, "error": "bad host"})
                return
            u = urlparse(self.path)
            q = parse_qs(u.query)
            path = u.path.rstrip("/") or "/"

            if path == "/":
                self._html(_INDEX_HTML)
                return
            if path == "/m":
                self._html(_MOBILE_HTML)
                return
            if path == "/api/ping":
                self._json(200, {"ok": True, "app": "agentbar", "version": __version__})
                return
            if not self._authed(q):
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            if path == "/api/state":
                snap = core.snapshot()
                tstat = hooks.get("tunnel_status")
                if tstat:
                    try:
                        snap["tunnel"] = tstat()
                    except Exception:
                        pass
                self._json(200, {"ok": True, **snap})
                return
            if path == "/api/tools":
                tools = [a.availability() for a in core.registry.values()]
                self._json(200, {"ok": True, "tools": tools,
                                 "default_cwd": settings.default_cwd,
                                 "allow_full_profile": settings.allow_full_profile})
                return
            parts = path.split("/")
            if len(parts) == 5 and parts[1:3] == ["api", "tasks"] and parts[4] == "log":
                tail = min(int(q.get("tail_bytes", ["30000"])[0]), 200_000)
                text = core.store.read_log_tail(parts[3], tail)
                self._json(200, {"ok": True, "log": text})
                return
            if len(parts) == 5 and parts[1:3] == ["api", "tasks"] and parts[4] == "transcript":
                from .transcript import (
                    find_session_file,
                    parse_transcript,
                    recover_session_id,
                )
                task_id = parts[3]
                with core._lock:
                    t = core._tasks.get(task_id)
                if not t:
                    self._json(404, {"ok": False, "error": "任务不存在"})
                    return
                if not t.session_id:
                    sid = recover_session_id(t.tool, t.cwd, t.started_at, t.finished_at)
                    if sid:
                        with core._lock:
                            if not t.session_id:
                                t.session_id = sid
                                core._persist_locked()
                if not t.session_id:
                    self._json(200, {"ok": True, "transcript": "", "message": "该任务尚无会话 ID"})
                    return
                path = find_session_file(t.tool, t.cwd, t.session_id)
                if not path:
                    self._json(200, {"ok": True, "transcript": "",
                                     "message": f"未找到会话文件（sid={t.session_id}）"})
                    return
                text = parse_transcript(t.tool, path)
                self._json(200, {"ok": True, "transcript": text,
                                 "session_id": t.session_id, "path": str(path)})
                return
            self._json(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            if not self._host_ok():
                self._json(403, {"ok": False, "error": "bad host"})
                return
            u = urlparse(self.path)
            q = parse_qs(u.query)
            path = u.path.rstrip("/")
            if not self._authed(q):
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            body = self._body()

            if path == "/api/tasks":
                try:
                    t = core.add_task(
                        prompt=body.get("prompt", ""),
                        tool=body.get("tool", "claude"),
                        cwd=body.get("cwd", ""),
                        title=body.get("title"),
                        profile=body.get("profile", "edits"),
                        model=body.get("model"),
                        effort=body.get("effort"),
                        scheduled_at=body.get("scheduled_at"),
                    )
                except ValueError as e:
                    self._json(400, {"ok": False, "error": str(e)})
                    return
                self._json(200, {"ok": True, "task": t.to_dict()})
                return
            if path == "/api/pause-all":
                core.pause_all()
                self._json(200, {"ok": True})
                return
            if path == "/api/resume-all":
                core.resume_all()
                self._json(200, {"ok": True})
                return
            if path == "/api/quota/refresh":
                core.quota.refresh_now()
                self._json(202, {"ok": True, "message": "额度刷新已触发"})
                return
            if path == "/api/quota/authorize-claude":
                # 仅由用户在本机面板主动调用，后台刷新不会触发 Keychain 弹窗。
                ok = core.quota.authorize_claude_keychain()
                self._json(200 if ok else 400, {
                    "ok": ok,
                    "message": "Claude Keychain 已授权并刷新" if ok else "未取得 Claude Keychain 授权",
                })
                return
            if path == "/api/debug/dispatch":
                # 触发与真实菜单点击完全相同的 _dispatch 路径（主线程执行），
                # 用于无 GUI 交互的端到端验证。白名单限定只读性动作。
                fn = hooks.get("dispatch")
                action = str(body.get("action") or "")
                if fn is None:
                    self._json(404, {"ok": False,
                                     "error": "menu bar 未运行（headless 无此通道）"})
                    return
                if action not in {"open_panel", "quick_add", "refresh_quota",
                                  "tunnel_start", "tunnel_stop"}:
                    self._json(400, {"ok": False, "error": f"action 不在白名单: {action!r}"})
                    return
                fn(action)
                self._json(202, {"ok": True, "action": action})
                return
            parts = path.split("/")
            if len(parts) == 5 and parts[1:3] == ["api", "tasks"]:
                ok, msg = core.act(parts[3], parts[4])
                self._json(200 if ok else 400, {"ok": ok, "message": msg, "error": msg})
                return
            self._json(404, {"ok": False, "error": "not found"})

        def do_PUT(self):
            if not self._host_ok():
                self._json(403, {"ok": False, "error": "bad host"})
                return
            u = urlparse(self.path)
            q = parse_qs(u.query)
            path = u.path.rstrip("/")
            if not self._authed(q):
                self._json(401, {"ok": False, "error": "unauthorized"})
                return

            parts = path.split("/")
            if len(parts) != 4 or parts[1:3] != ["api", "tasks"]:
                self._json(404, {"ok": False, "error": "not found"})
                return
            try:
                t = core.edit_task(parts[3], self._body())
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
                return
            self._json(200, {"ok": True, "task": t.to_dict()})

    _INDEX_HTML = _load_web("index.html")
    _MOBILE_HTML = _load_web("mobile.html")
    return Handler
