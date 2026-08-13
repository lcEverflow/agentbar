"""Localhost HTTP API + task manager web UI (+ optional LAN access for mobile).

安全：默认绑 127.0.0.1；lan_access=true 时绑 0.0.0.0 供同局域网手机访问。
所有 /api（除 /api/ping）要求 Header token。Host 头校验只放行
IP 字面量（DNS rebinding 必须借助域名，放行裸 IP 不破坏该防御）。
token 存于 state 目录 config.json（0600）。
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlencode, urlparse

from . import __version__
from .browser_cookies import CookieImportError, import_cookie_header
from .config import (
    DEFAULT_QUOTA_SOURCES,
    DEFAULT_PROVIDERS,
    PROVIDER_HOSTS,
    PROVIDER_UNITS,
    Settings,
    save_settings,
)
from .scheduler import Scheduler

log = logging.getLogger("agentbar.server")

MAX_BODY = 200_000
REQUEST_IO_TIMEOUT_SECONDS = 15
HANDLER_DRAIN_SECONDS = 5
ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost"}
_PROXY_HEADERS = {
    "forwarded",
    "via",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-real-ip",
    "true-client-ip",
}
_HTML_CSP = (
    "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' data:"
)


class _BodyError(ValueError):
    """A safe client-facing request-body error with an explicit HTTP status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class _AdmissionClosed(RuntimeError):
    """Raised when a request reaches its commit point during server shutdown."""


class _RequestAdmission:
    """Close mutation admission instantly, then drain accepted handlers boundedly."""

    def __init__(self) -> None:
        self.stopping = threading.Event()
        self._condition = threading.Condition()
        self._active_handlers = 0

    def enter_handler(self) -> bool:
        with self._condition:
            if self.stopping.is_set():
                return False
            self._active_handlers += 1
            return True

    def leave_handler(self) -> None:
        with self._condition:
            self._active_handlers -= 1
            if self._active_handlers == 0:
                self._condition.notify_all()

    def close(self) -> None:
        # Event.set() does not wait for a long-running handler. Every mutation
        # checks this predicate again immediately before touching core/settings.
        self.stopping.set()

    def require_open(self) -> None:
        if self.stopping.is_set():
            raise _AdmissionClosed("HTTP server is stopping")

    def wait_for_handlers(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._active_handlers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True


def _host_of(header: str) -> str:
    """Extract host part from a Host header ([::1]:8737 / 10.1.2.3:8737 / localhost)."""
    header = (header or "").strip()
    if not header or any(char.isspace() for char in header):
        return ""
    if header.startswith("["):
        end = header.find("]")
        if end < 0:
            return ""
        suffix = header[end + 1:]
        if suffix and not (suffix.startswith(":") and suffix[1:].isdigit()):
            return ""
        return header[1:end].lower()
    if header.count(":") > 1:  # IPv6 Host 必须使用 [addr]:port
        return ""
    host, sep, port = header.partition(":")
    if sep and not port.isdigit():
        return ""
    return host.lower()


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


def _cookie_preview(cookie: str) -> str:
    if not cookie:
        return ""
    names = []
    for part in cookie.split(";"):
        raw = part.strip()
        if "=" not in raw:
            continue
        name = raw.split("=", 1)[0].strip()
        # Never echo arbitrary user input. Only cookie-name tokens are safe to
        # expose; a pasted JWT/Bearer without '=' must remain completely masked.
        if re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,128}", name):
            names.append(name)
    shown = ", ".join(names[:4]) or "已配置"
    return shown + (" ..." if len(names) > 4 else "")


def _provider_config_payload(settings: Settings) -> dict:
    providers = {}
    for name, defaults in DEFAULT_PROVIDERS.items():
        cfg = (settings.providers or {}).get(name) or {}
        cookie = str(cfg.get("cookie") or "")
        providers[name] = {
            "enabled": bool(cfg.get("enabled")),
            "unit": cfg.get("unit") if cfg.get("unit") in PROVIDER_UNITS else defaults["unit"],
            "refresh_seconds": int(cfg.get("refresh_seconds") or defaults["refresh_seconds"]),
            "cookie_set": bool(cookie.strip()),
            "cookie_preview": _cookie_preview(cookie),
            "host": PROVIDER_HOSTS.get(name, ""),
        }
    return {
        "ok": True,
        "providers": providers,
        "quota_sources": {
            name: {
                "enabled": bool(((settings.quota_sources or {}).get(name) or {}).get("enabled")),
                "model": str(((settings.quota_sources or {}).get(name) or {}).get("model") or ""),
                "key_set": bool(str(
                    ((settings.quota_sources or {}).get(name) or {}).get("access_token") or ""
                ).strip()),
                "account_id_set": bool(str(
                    ((settings.quota_sources or {}).get(name) or {}).get("account_id") or ""
                ).strip()),
            }
            for name in DEFAULT_QUOTA_SOURCES
        },
        "usage_auto_refresh": bool(settings.usage_auto_refresh),
        "title_provider": settings.title_provider,
    }


def _apply_provider_settings(
    settings: Settings,
    payload: dict,
    before_commit=None,
) -> None:
    if not isinstance(payload, dict):
        raise ValueError("配置请求必须是 JSON object")
    with settings._lock:
        providers = payload.get("providers", {})
        if not isinstance(providers, dict):
            raise ValueError("providers 必须是 JSON object")
        merged = json.loads(json.dumps(settings.providers or DEFAULT_PROVIDERS))
        for name, defaults in DEFAULT_PROVIDERS.items():
            incoming = providers.get(name)
            if incoming is None:
                continue
            if not isinstance(incoming, dict):
                raise ValueError(f"providers.{name} 必须是 JSON object")
            cfg = merged.setdefault(name, dict(defaults))
            if "enabled" in incoming:
                cfg["enabled"] = _clean_bool_field(
                    incoming.get("enabled"), f"providers.{name}.enabled",
                )
            if "unit" in incoming:
                unit = incoming.get("unit")
                if not isinstance(unit, str) or unit not in PROVIDER_UNITS:
                    raise ValueError(
                        f"providers.{name}.unit 必须是 "
                        f"{', '.join(PROVIDER_UNITS)} 之一"
                    )
                cfg["unit"] = unit
            if "refresh_seconds" in incoming:
                seconds = incoming.get("refresh_seconds")
                if isinstance(seconds, bool) or not isinstance(seconds, int):
                    raise ValueError(
                        f"providers.{name}.refresh_seconds 必须是整数"
                    )
                cfg["refresh_seconds"] = min(86_400, max(60, seconds))
            if "cookie" in incoming:
                # Missing cookie keeps the existing secret; explicit empty string clears it.
                cfg["cookie"] = _clean_credential_field(
                    incoming.get("cookie"), "Cookie", 65_536,
                )

        next_title = settings.title_provider
        if "title_provider" in payload:
            title = payload.get("title_provider")
            if not isinstance(title, str) or title not in {
                "claude", "codex", *DEFAULT_PROVIDERS.keys(),
            }:
                raise ValueError("title_provider 无效")
            next_title = title
        source_payload = payload.get("quota_sources", {})
        if not isinstance(source_payload, dict):
            raise ValueError("quota_sources 必须是 JSON object")
        sources = json.loads(json.dumps(settings.quota_sources or DEFAULT_QUOTA_SOURCES))
        for name, defaults in DEFAULT_QUOTA_SOURCES.items():
            incoming = source_payload.get(name)
            if incoming is None:
                continue
            if not isinstance(incoming, dict):
                raise ValueError(f"quota_sources.{name} 必须是 JSON object")
            cfg = sources.setdefault(name, dict(defaults))
            cfg.pop("api_key", None)  # 旧字段只读迁移，永不再落盘。
            if "enabled" in incoming:
                cfg["enabled"] = _clean_bool_field(
                    incoming.get("enabled"), f"quota_sources.{name}.enabled",
                )
            if "model" in incoming:
                cfg["model"] = _clean_credential_field(incoming.get("model"), "模型", 160)
            token_submitted = "access_token" in incoming or "api_key" in incoming
            if token_submitted:
                # access_token 是唯一正式字段；api_key 仅接受旧 Web 客户端迁移。
                raw_token = (
                    incoming.get("access_token")
                    if "access_token" in incoming
                    else incoming.get("api_key")
                )
                cfg["access_token"] = _clean_credential_field(
                    raw_token, "OAuth Access Token", 16_384,
                )
            if "account_id" in incoming:
                cfg["account_id"] = _clean_credential_field(
                    incoming.get("account_id"), "Account ID", 256,
                )
            if token_submitted and not cfg.get("access_token"):
                # 显式清空凭据时同时停用，避免留下会持续报错的半配置。
                if incoming.get("enabled") is True:
                    raise ValueError(f"{name} 启用前必须输入 OAuth Access Token")
                cfg["enabled"] = False
            if cfg.get("enabled") and not str(cfg.get("access_token") or "").strip():
                raise ValueError(f"{name} 启用前必须输入 OAuth Access Token")
        next_auto_refresh = settings.usage_auto_refresh
        if "usage_auto_refresh" in payload:
            next_auto_refresh = _clean_bool_field(
                payload.get("usage_auto_refresh"), "usage_auto_refresh",
            )

        # 保存失败时恢复旧内存状态，避免 API 回报失败却部分生效。
        old = (
            settings.title_provider,
            settings.providers,
            settings.quota_sources,
            settings.usage_auto_refresh,
        )
        if before_commit is not None:
            before_commit()
        settings.title_provider = next_title
        settings.providers = merged
        settings.quota_sources = sources
        settings.usage_auto_refresh = next_auto_refresh
        try:
            save_settings(settings)
        except Exception:
            (
                settings.title_provider,
                settings.providers,
                settings.quota_sources,
                settings.usage_auto_refresh,
            ) = old
            raise


def _clean_credential_field(value, label: str, max_len: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label}必须是字符串")
    value = value.strip()
    if len(value) > max_len:
        raise ValueError(f"{label} 过长")
    if any(char in value for char in "\r\n\x00"):
        raise ValueError(f"{label} 不能包含换行或 NUL")
    return value


def _clean_bool_field(value, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} 必须是 boolean")
    return value


class ApiServer:
    def __init__(self, core: Scheduler, settings: Settings):
        self.core = core
        self.settings = settings
        # menu bar 进程注入：把 action 转发到主线程菜单分发器（调试/远程触发用）
        self.hooks: dict = {"dispatch": None}
        # 动态 Host 白名单（公网隧道域名启动后注册进来；其余域名一律 403）
        self.extra_hosts: set[str] = set()
        self._admission = _RequestAdmission()
        handler = _make_handler(
            core, settings, self.hooks, self.extra_hosts, self._admission
        )
        bind = "0.0.0.0" if settings.lan_access else "127.0.0.1"
        self.httpd = ThreadingHTTPServer((bind, settings.port), handler)
        # We provide our own bounded drain. ThreadingMixIn's default unbounded
        # join can hang SIGTERM behind a slow Keychain/browser-cookie import.
        # Lingering daemon handlers cannot commit after admission closes.
        self.httpd.daemon_threads = True
        self.httpd.block_on_close = False
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()
        self._started = False
        self._stopped = False

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    @property
    def stopping(self) -> bool:
        return self._admission.stopping.is_set()

    def url(self, with_token: bool = False, **query: str) -> str:
        """Return a local panel URL, optionally carrying an authenticated UI intent.

        The menu-bar quick-add action deliberately uses the same full editor as
        the panel; a native one-line prompt dialog cannot safely expose cwd,
        model, effort and permission choices.
        """
        base = f"http://127.0.0.1:{self.port}/"
        params = {key: str(value) for key, value in query.items() if value is not None}
        query_string = f"?{urlencode(params)}" if params else ""
        # Fragment 不会进入 HTTP request line / proxy log / Referer。
        fragment = f"#{urlencode({'token': self.settings.token})}" if with_token else ""
        return base + query_string + fragment

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
        return f"http://{ip}:{self.port}/m#{urlencode({'token': self.settings.token})}"

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started or self._stopped:
                return
            self._thread = threading.Thread(
                target=self.httpd.serve_forever, name="agentbar-http", daemon=True
            )
            self._thread.start()
            self._started = True
        log.info("api server on %s", self.url())

    def stop(self) -> None:
        # Hold the lifecycle lock through the drain so concurrent stop callers do
        # not return before the owner has finished waiting for accepted handlers.
        with self._lifecycle_lock:
            if self._stopped:
                return
            self._stopped = True
            # Linearization point: long-running accepted requests may finish
            # their read/import work, but every commit path rejects afterwards.
            self._admission.close()
            thread = self._thread
            if self._started:
                self.httpd.shutdown()
            # server_close is sufficient before start (shutdown would deadlock).
            self.httpd.server_close()
            if thread and thread is not threading.current_thread():
                thread.join(timeout=5)
                if thread.is_alive():
                    log.warning("HTTP server thread did not stop within 5s")
            if not self._admission.wait_for_handlers(HANDLER_DRAIN_SECONDS):
                log.warning(
                    "HTTP handlers did not drain within %ss; late mutations remain rejected",
                    HANDLER_DRAIN_SECONDS,
                )


def _load_web(name: str) -> str:
    return resources.files("agentbar").joinpath(f"web/{name}").read_text("utf-8")


def _make_handler(
    core: Scheduler,
    settings: Settings,
    hooks: dict | None = None,
    extra_hosts: set | None = None,
    admission: _RequestAdmission | None = None,
):
    hooks = hooks if hooks is not None else {}
    extra_hosts = extra_hosts if extra_hosts is not None else set()
    admission = admission if admission is not None else _RequestAdmission()
    class Handler(BaseHTTPRequestHandler):
        server_version = f"AgentBar/{__version__}"
        sys_version = ""

        # ---------- plumbing ----------

        def setup(self) -> None:
            self._admission_entered = False
            super().setup()
            self._admission_entered = admission.enter_handler()
            # Bound slow header/body clients too. In-process work is governed by
            # the admission predicate and our separate bounded handler drain.
            self.connection.settimeout(REQUEST_IO_TIMEOUT_SECONDS)

        def finish(self) -> None:
            try:
                super().finish()
            finally:
                if self._admission_entered:
                    self._admission_entered = False
                    admission.leave_handler()

        def _reject_if_stopping(self) -> bool:
            if not self._admission_entered or admission.stopping.is_set():
                try:
                    self._json(503, {
                        "ok": False,
                        "error": "AgentBar 正在退出，拒绝新的操作",
                    })
                except OSError:
                    pass
                return True
            return False

        def log_message(self, fmt, *args):  # 安静，不刷 stderr
            log.debug("http: " + fmt, *args)

        def _json(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _html(self, text: str) -> None:
            body = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", _HTML_CSP)
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")

        def _host_ok(self) -> bool:
            host = _host_of(self.headers.get("Host"))
            if host in ALLOWED_HOSTS or host in extra_hosts:
                return True
            # LAN 模式放行 IP 字面量（手机浏览器以 http://10.x.x.x:8737 访问）。
            # 其余域名一律拒绝：DNS rebinding 攻击必须经由域名；
            # 公网隧道域名走 extra_hosts 动态注册。
            return settings.lan_access and _is_ip_literal(host)

        def _authed(self, _query: dict) -> bool:
            # Query token 会泄漏到访问日志/历史/Referer，只接受请求头。
            token = self.headers.get("X-Agentbar-Token") or ""
            return bool(token) and hmac.compare_digest(token, settings.token)

        def _body(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                raise _BodyError(400, "Content-Length 无效")
            if n < 0:
                raise _BodyError(400, "Content-Length 无效")
            if n == 0:
                return {}
            if n > MAX_BODY:
                raise _BodyError(413, f"请求体过大（上限 {MAX_BODY} 字节）")
            try:
                value = json.loads(self.rfile.read(n).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                raise _BodyError(400, "请求体必须是合法 JSON object")
            if not isinstance(value, dict):
                raise _BodyError(400, "请求体必须是 JSON object")
            return value

        def _persistence_unavailable(self, operation: str) -> None:
            # Never echo exception details: filesystem paths or secret-bearing
            # payload fragments may be present in lower-layer errors.
            log.exception("%s persistence failed", operation)
            self._json(503, {
                "ok": False,
                "error": "本地状态保存失败，操作未生效；请检查磁盘后重试",
            })

        def _trusted_local(self) -> bool:
            """Only a direct local request may touch credentials or local UI hooks.

            Cloudflared and other reverse proxies connect from 127.0.0.1 too, so
            peer IP alone is not a trust boundary. Require a loopback Host and
            reject all common proxy provenance headers as well.
            """
            try:
                peer_loopback = ipaddress.ip_address(self.client_address[0]).is_loopback
            except (ValueError, IndexError, TypeError):
                return False
            if not peer_loopback or _host_of(self.headers.get("Host")) not in ALLOWED_HOSTS:
                return False
            for name in self.headers.keys():
                lowered = name.lower()
                if (
                    lowered in _PROXY_HEADERS
                    or lowered.startswith("cf-")
                    or lowered.startswith("x-forwarded-")
                ):
                    return False
            return True

        # ---------- routing ----------

        def do_GET(self):
            if self._reject_if_stopping():
                return
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
            if path == "/api/provider-config":
                if not self._trusted_local():
                    self._json(403, {
                        "ok": False,
                        "error": "额度与凭据配置只能在本机 AgentBar 面板中查看",
                    })
                    return
                self._json(200, _provider_config_payload(settings))
                return
            parts = path.split("/")
            if len(parts) == 5 and parts[1:3] == ["api", "tasks"] and parts[4] == "log":
                task_id = parts[3]
                with core._lock:
                    task_exists = task_id in core._tasks
                if not task_exists:
                    self._json(404, {"ok": False, "error": "任务不存在"})
                    return
                try:
                    tail = int(q.get("tail_bytes", ["30000"])[0])
                except (TypeError, ValueError):
                    tail = 30_000
                tail = min(max(0, tail), 200_000)
                try:
                    text = core.store.read_log_tail(task_id, tail)
                except ValueError:
                    self._json(400, {"ok": False, "error": "任务 ID 无效"})
                    return
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
                                try:
                                    admission.require_open()
                                except _AdmissionClosed:
                                    self._reject_if_stopping()
                                    return
                                checkpoint = t.to_dict()
                                t.session_id = sid
                                try:
                                    core._persist_locked()
                                except OSError:
                                    core._restore_task_locked(t, checkpoint)
                                    self._persistence_unavailable(
                                        "recovered transcript session"
                                    )
                                    return
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
            if self._reject_if_stopping():
                return
            if not self._host_ok():
                self._json(403, {"ok": False, "error": "bad host"})
                return
            u = urlparse(self.path)
            q = parse_qs(u.query)
            path = u.path.rstrip("/")
            if not self._authed(q):
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            # Reject secret/UI-local routes before reading their request body.
            # A proxied browser must never upload a credential to Cloudflare or
            # a LAN hop only to receive a 403 after the bytes have been consumed.
            local_only = {
                "/api/provider-config",
                "/api/provider-config/import-cookie",
                "/api/debug/dispatch",
            }
            if path in local_only and not self._trusted_local():
                self._json(403, {"ok": False, "error": "该操作仅允许本机直连"})
                return
            try:
                body = self._body()
            except _BodyError as e:
                self._json(e.status, {"ok": False, "error": str(e)})
                return

            if path == "/api/tasks":
                try:
                    admission.require_open()
                    t = core.add_task(
                        prompt=body.get("prompt", ""),
                        tool=body.get("tool", "claude"),
                        cwd=body.get("cwd", ""),
                        title=body.get("title"),
                        profile=body.get("profile", "edits"),
                        model=body.get("model"),
                        effort=body.get("effort"),
                        scheduled_at=body.get("scheduled_at"),
                        before_commit=admission.require_open,
                    )
                except _AdmissionClosed:
                    self._reject_if_stopping()
                    return
                except ValueError as e:
                    self._json(400, {"ok": False, "error": str(e)})
                    return
                except OSError:
                    self._persistence_unavailable("add task")
                    return
                self._json(200, {"ok": True, "task": t.to_dict()})
                return
            if path == "/api/pause-all":
                try:
                    admission.require_open()
                    core.pause_all(before_commit=admission.require_open)
                except _AdmissionClosed:
                    self._reject_if_stopping()
                    return
                except ValueError as e:
                    self._json(400, {"ok": False, "error": str(e)})
                    return
                except OSError:
                    self._persistence_unavailable("pause all")
                    return
                self._json(200, {"ok": True})
                return
            if path == "/api/resume-all":
                try:
                    admission.require_open()
                    core.resume_all(before_commit=admission.require_open)
                except _AdmissionClosed:
                    self._reject_if_stopping()
                    return
                except ValueError as e:
                    self._json(400, {"ok": False, "error": str(e)})
                    return
                except OSError:
                    self._persistence_unavailable("resume all")
                    return
                self._json(200, {"ok": True})
                return
            if path == "/api/quota/refresh":
                tool = str(body.get("tool") or "").strip()
                if not tool:
                    self._json(400, {
                        "ok": False,
                        "error": "必须指定一个已启用的额度来源",
                    })
                    return
                if tool not in core.quota.provider_tools():
                    self._json(400, {"ok": False, "error": f"未启用的额度来源: {tool!r}"})
                    return
                try:
                    admission.require_open()
                except _AdmissionClosed:
                    self._reject_if_stopping()
                    return
                core.quota.refresh_now(tool)
                self._json(202, {
                    "ok": True,
                    "message": f"{tool} 额度刷新已触发",
                })
                return
            if path == "/api/provider-config":
                if not self._trusted_local():
                    self._json(403, {
                        "ok": False,
                        "error": "额度与凭据配置只能在本机 AgentBar 面板中保存",
                    })
                    return
                try:
                    _apply_provider_settings(
                        settings, body, before_commit=admission.require_open,
                    )
                    core.quota.reload_fetchers()
                except _AdmissionClosed:
                    self._reject_if_stopping()
                    return
                except ValueError as e:
                    self._json(400, {"ok": False, "error": str(e)})
                    return
                except OSError:
                    self._persistence_unavailable("provider config")
                    return
                except Exception:
                    log.exception("provider config save failed")
                    self._json(500, {"ok": False, "error": "配置保存失败"})
                    return
                self._json(200, {
                    **_provider_config_payload(settings),
                    "message": "额度配置已保存并刷新",
                })
                return
            if path == "/api/provider-config/import-cookie":
                if not self._trusted_local():
                    self._json(403, {"ok": False, "error": "Cookie 导入仅允许本机直连"})
                    return
                provider = str(body.get("provider") or "")
                host = PROVIDER_HOSTS.get(provider)
                if not host:
                    self._json(400, {"ok": False, "error": f"未知 provider: {provider!r}"})
                    return
                try:
                    imported = import_cookie_header(host)
                except CookieImportError as e:
                    self._json(400, {"ok": False, "error": str(e)})
                    return
                try:
                    with settings._lock:
                        # Cookie/Keychain scanning is intentionally outside the
                        # settings lock and can outlive bounded server drain.
                        # Re-check at the real commit point so a late result can
                        # never resurrect credentials during/after shutdown.
                        admission.require_open()
                        old_providers = settings.providers
                        providers = json.loads(json.dumps(
                            settings.providers or DEFAULT_PROVIDERS
                        ))
                        cfg = providers.setdefault(
                            provider, dict(DEFAULT_PROVIDERS[provider])
                        )
                        cfg["enabled"] = True
                        cfg["cookie"] = _clean_credential_field(
                            imported.header, "Cookie", 65_536,
                        )
                        settings.providers = providers
                        try:
                            save_settings(settings)
                        except Exception:
                            settings.providers = old_providers
                            raise
                    core.quota.reload_fetchers()
                except _AdmissionClosed:
                    self._reject_if_stopping()
                    return
                except ValueError as e:
                    self._json(400, {"ok": False, "error": str(e)})
                    return
                except OSError:
                    self._persistence_unavailable("provider cookie import")
                    return
                except Exception:
                    log.exception("provider cookie import save failed")
                    self._json(500, {"ok": False, "error": "Cookie 保存失败"})
                    return
                self._json(200, {
                    **_provider_config_payload(settings),
                    "message": (
                        f"已从 {imported.source} 导入 {imported.count} 个 Cookie，"
                        f"{provider} 已启用"
                    ),
                })
                return
            if path == "/api/debug/dispatch":
                # 触发与真实菜单点击完全相同的 _dispatch 路径（主线程执行），
                # 用于无 GUI 交互的端到端验证。白名单限定只读性动作。
                if not self._trusted_local():
                    self._json(403, {"ok": False, "error": "本机 UI 调试通道仅允许本机直连"})
                    return
                fn = hooks.get("dispatch")
                action = str(body.get("action") or "")
                if fn is None:
                    self._json(404, {"ok": False,
                                     "error": "menu bar 未运行（headless 无此通道）"})
                    return
                scoped_refresh = (
                    action.startswith("refresh_quota:")
                    and bool(action.split(":", 1)[1].strip())
                )
                allowed = action in {
                    "open_panel", "quick_add", "provider_settings",
                    "tunnel_start", "tunnel_stop",
                } or scoped_refresh
                if not allowed:
                    self._json(400, {"ok": False, "error": f"action 不在白名单: {action!r}"})
                    return
                try:
                    admission.require_open()
                except _AdmissionClosed:
                    self._reject_if_stopping()
                    return
                fn(action)
                self._json(202, {"ok": True, "action": action})
                return
            parts = path.split("/")
            if len(parts) == 5 and parts[1:3] == ["api", "tasks"]:
                try:
                    admission.require_open()
                    ok, msg = core.act(
                        parts[3], parts[4], before_commit=admission.require_open,
                    )
                except _AdmissionClosed:
                    self._reject_if_stopping()
                    return
                except OSError:
                    self._persistence_unavailable("task action")
                    return
                self._json(200 if ok else 400, {"ok": ok, "message": msg, "error": msg})
                return
            self._json(404, {"ok": False, "error": "not found"})

        def do_PUT(self):
            if self._reject_if_stopping():
                return
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
                body = self._body()
            except _BodyError as e:
                self._json(e.status, {"ok": False, "error": str(e)})
                return
            try:
                admission.require_open()
                t = core.edit_task(
                    parts[3], body, before_commit=admission.require_open,
                )
            except _AdmissionClosed:
                self._reject_if_stopping()
                return
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
                return
            except OSError:
                self._persistence_unavailable("edit task")
                return
            self._json(200, {"ok": True, "task": t.to_dict()})

    _INDEX_HTML = _load_web("index.html")
    _MOBILE_HTML = _load_web("mobile.html")
    return Handler
