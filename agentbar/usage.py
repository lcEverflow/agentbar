"""Quota and login-state fetchers with no direct access to CLI OAuth tokens.

Claude only invokes ``claude auth status --json`` and whitelists non-sensitive
login metadata. Anthropic exposes no supported third-party subscription quota
API, so AgentBar never fabricates Claude windows.

Codex uses a short-lived official App Server session (``account/read`` and
``account/rateLimits/read``), leaving credential management and refresh to the
Codex process itself.
"""

from __future__ import annotations

import json
import math
import os
import re
import select
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import __version__

HTTP_TIMEOUT = 12
_STATUS_CACHE_TTL_SECONDS = 30.0


@dataclass
class UsageWindow:
    label: str                 # "5h" | "7d" | "7d Opus" | "7d Sonnet" | "本月"
    used_percent: float
    resets_at: float | None = None
    # 信用额度类 provider（MyToken / Tokenverse）附带原始额度值，供 UI 按 unit 展示。
    used: float | None = None       # 已用（credits 或 token 数）
    total: float | None = None      # 总额度
    unit: str | None = None         # "credits" | "percent" | "token"
    # None 表示账户级通用窗口；有值时只限对应模型家族/额度桶。
    model: str | None = None
    limited: bool = False

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "used_percent": round(self.used_percent, 1),
            "resets_at": self.resets_at,
            "model": self.model,
            "limited": self.limited,
            "used": self.used,
            "total": self.total,
            "unit": self.unit,
        }


@dataclass
class UsageSnapshot:
    tool: str
    windows: list[UsageWindow] = field(default_factory=list)
    plan: str | None = None
    source: str = ""
    fetched_at: float = field(default_factory=time.time)
    error: str | None = None
    limited: bool = False
    model: str | None = None
    available_models: list[str] = field(default_factory=list)

    @property
    def primary(self) -> UsageWindow | None:
        return self.windows[0] if self.windows else None

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "windows": [w.to_dict() for w in self.windows],
            "plan": self.plan,
            "source": self.source,
            "fetched_at": self.fetched_at,
            "error": self.error,
            "limited": self.limited,
            "model": self.model,
            "available_models": self.available_models,
        }


def _http_get_json(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


@dataclass(frozen=True)
class _CredentialResolution:
    """Internal result whose representation intentionally omits credentials."""

    credentials: dict | None = field(default=None, repr=False)
    source: str = "none"
    status: str = "not_logged_in"
    detail: str = "未检测到登录态"
    needs_authorization: bool = False

    def public(self) -> dict:
        return {
            "available": self.credentials is not None,
            "source": self.source,
            "status": self.status,
            "detail": self.detail,
            "needs_authorization": self.needs_authorization,
        }


_credential_status_lock = threading.Lock()
_credential_status_condition = threading.Condition(_credential_status_lock)
_credential_status_cache: dict[str, tuple[float, dict]] = {}
_credential_status_inflight: set[str] = set()


def _remember_credential_status(tool: str, result: _CredentialResolution) -> dict:
    public = result.public()
    with _credential_status_condition:
        _credential_status_cache[tool] = (time.monotonic(), public)
        _credential_status_condition.notify_all()
    return dict(public)


def _clear_credential_status_cache(tool: str | None = None) -> None:
    """Clear only non-sensitive status cache (kept private except for tests)."""
    with _credential_status_condition:
        if tool is None:
            _credential_status_cache.clear()
        else:
            for key in list(_credential_status_cache):
                if key == tool or key.startswith(f"{tool}\0"):
                    _credential_status_cache.pop(key, None)
        _credential_status_condition.notify_all()


def _clean_secret(value, *, max_length: int = 65_536) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if not cleaned or len(cleaned) > max_length:
        return ""
    if any(ord(char) < 32 or ord(char) == 127 for char in cleaned):
        return ""
    return cleaned


def _clean_metadata(value, *, max_length: int = 512) -> str | None:
    cleaned = _clean_secret(value, max_length=max_length)
    return cleaned or None


# ================= Local CLI credentials =================


_CLI_STATUS_TIMEOUT_SECONDS = 8.0
_CLI_STATUS_MAX_OUTPUT_BYTES = 64 * 1024
_CODEX_APP_SERVER_TIMEOUT_SECONDS = 15.0
_CODEX_APP_SERVER_MAX_OUTPUT_BYTES = 2 * 1024 * 1024
_CODEX_APP_SERVER_MAX_LINE_BYTES = 256 * 1024
_codex_app_server_lock = threading.Lock()


class _CodexAppServerError(RuntimeError):
    """Deliberately carries no child output, account data, or credentials."""


def _terminate_process_group(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        # The session leader may exit while a descendant keeps inherited FDs
        # open. Its process group still has the original pid, so clean it too.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=0.5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass


def _run_bounded_json_command(
    argv: list[str],
    *,
    env: dict | None = None,
) -> dict:
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        raise _CodexAppServerError("start failed") from exc
    deadline = time.monotonic() + _CLI_STATUS_TIMEOUT_SECONDS
    output = bytearray()
    try:
        if proc.stdout is None:
            raise _CodexAppServerError("missing stdout")
        fd = proc.stdout.fileno()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _CodexAppServerError("timeout")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                raise _CodexAppServerError("timeout")
            try:
                chunk = os.read(fd, 16_384)
            except OSError as exc:
                raise _CodexAppServerError("read failed") from exc
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > _CLI_STATUS_MAX_OUTPUT_BYTES:
                raise _CodexAppServerError("output limit exceeded")
        try:
            proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise _CodexAppServerError("timeout") from exc
        try:
            parsed = json.loads(bytes(output).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _CodexAppServerError("invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise _CodexAppServerError("invalid response")
        return parsed
    finally:
        _terminate_process_group(proc)
        if proc.stdout is not None:
            proc.stdout.close()


def _resolve_claude_credentials(
    *,
    binary: str = "",
    env: dict | None = None,
) -> _CredentialResolution:
    executable = (binary or "").strip() or shutil.which("claude")
    if not executable:
        return _CredentialResolution(
            None,
            "claude_auth_status",
            "unavailable",
            "未找到 Claude Code CLI",
        )
    try:
        raw = _run_bounded_json_command(
            [executable, "auth", "status", "--json"],
            env=env,
        )
    except _CodexAppServerError:
        return _CredentialResolution(
            None,
            "claude_auth_status",
            "unavailable",
            "无法通过 Claude Code CLI 读取登录状态",
        )
    # Whitelist only non-sensitive status fields. In particular, intentionally
    # discard email, orgId and orgName from Claude's JSON response.
    if raw.get("loggedIn") is not True:
        return _CredentialResolution(
            None,
            "claude_auth_status",
            "not_logged_in",
            "Claude Code 未登录，请先运行 claude 登录",
        )
    auth_method = _clean_metadata(raw.get("authMethod"), max_length=160)
    subscription = _clean_metadata(raw.get("subscriptionType"), max_length=160)
    return _CredentialResolution(
        {"plan": subscription, "auth_method": auth_method},
        "claude_auth_status",
        "available",
        "Claude Code 已登录；官方未提供第三方订阅额度接口",
    )


class _JsonLineReader:
    def __init__(self, proc: subprocess.Popen, deadline: float):
        if proc.stdout is None:
            raise _CodexAppServerError("missing stdout")
        self.proc = proc
        self.fd = proc.stdout.fileno()
        self.deadline = deadline
        self.buffer = bytearray()
        self.total = 0

    def _next_line(self) -> bytes | None:
        newline = self.buffer.find(b"\n")
        if newline < 0:
            return None
        line = bytes(self.buffer[:newline])
        del self.buffer[:newline + 1]
        return line

    def response(self, request_id: int) -> dict:
        while True:
            line = self._next_line()
            if line is not None:
                if not line:
                    continue
                if len(line) > _CODEX_APP_SERVER_MAX_LINE_BYTES:
                    raise _CodexAppServerError("line limit exceeded")
                try:
                    message = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise _CodexAppServerError("invalid JSONL") from exc
                if not isinstance(message, dict):
                    raise _CodexAppServerError("invalid message")
                # Notifications have no id and are safe to ignore. A server
                # request needs an explicit host response; fail closed instead
                # of silently deadlocking or accidentally accepting its id.
                if "method" in message and "id" in message:
                    raise _CodexAppServerError("unsupported server request")
                if message.get("id") == request_id and "method" not in message:
                    return message
                continue

            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise _CodexAppServerError("timeout")
            ready, _, _ = select.select([self.fd], [], [], remaining)
            if not ready:
                raise _CodexAppServerError("timeout")
            try:
                chunk = os.read(self.fd, 65_536)
            except OSError as exc:
                raise _CodexAppServerError("read failed") from exc
            if not chunk:
                raise _CodexAppServerError("unexpected EOF")
            self.total += len(chunk)
            if self.total > _CODEX_APP_SERVER_MAX_OUTPUT_BYTES:
                raise _CodexAppServerError("output limit exceeded")
            self.buffer.extend(chunk)
            if (
                b"\n" not in self.buffer
                and len(self.buffer) > _CODEX_APP_SERVER_MAX_LINE_BYTES
            ):
                raise _CodexAppServerError("line limit exceeded")


def _sanitize_codex_window(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    sanitized = {}
    for key in ("usedPercent", "windowDurationMins", "resetsAt"):
        item = value.get(key)
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            try:
                number = float(item)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(number):
                sanitized[key] = number
    return sanitized or None


def _sanitize_codex_limit(value, fallback_id: str = "") -> dict | None:
    if not isinstance(value, dict):
        return None
    limit_id = _clean_metadata(value.get("limitId") or fallback_id, max_length=160)
    if not limit_id:
        return None
    sanitized = {
        "limitId": limit_id,
        "limitName": _clean_metadata(value.get("limitName"), max_length=160),
        "planType": _clean_metadata(value.get("planType"), max_length=160),
        "rateLimitReachedType": _clean_metadata(
            value.get("rateLimitReachedType"), max_length=160
        ),
        "primary": _sanitize_codex_window(value.get("primary")),
        "secondary": _sanitize_codex_window(value.get("secondary")),
    }
    return sanitized


def _sanitize_codex_limits_result(value: dict) -> dict:
    base = _sanitize_codex_limit(value.get("rateLimits"))
    by_id = {}
    raw_by_id = value.get("rateLimitsByLimitId")
    if isinstance(raw_by_id, dict):
        for raw_id, raw_limit in list(raw_by_id.items())[:128]:
            key = _clean_metadata(raw_id, max_length=160)
            if not key:
                continue
            sanitized = _sanitize_codex_limit(raw_limit, key)
            if sanitized:
                by_id[key] = sanitized
    return {"rateLimits": base, "rateLimitsByLimitId": by_id}


def _send_app_server_message(proc: subprocess.Popen, message: dict) -> None:
    if proc.stdin is None:
        raise _CodexAppServerError("missing stdin")
    try:
        proc.stdin.write(
            json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        proc.stdin.flush()
    except (BrokenPipeError, OSError) as exc:
        raise _CodexAppServerError("write failed") from exc


def _codex_app_server_session(
    *,
    binary: str = "",
    env: dict | None = None,
    include_rate_limits: bool,
) -> tuple[dict, dict | None]:
    """Run one bounded official App Server session and return sanitized RPC data."""
    started_at = time.monotonic()
    if not _codex_app_server_lock.acquire(timeout=_CODEX_APP_SERVER_TIMEOUT_SECONDS):
        raise _CodexAppServerError("app server busy")
    executable = (binary or "").strip() or shutil.which("codex")
    if not executable:
        _codex_app_server_lock.release()
        raise _CodexAppServerError("codex unavailable")
    proc = None
    try:
        try:
            proc = subprocess.Popen(
                [executable, "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise _CodexAppServerError("start failed") from exc
        deadline = started_at + _CODEX_APP_SERVER_TIMEOUT_SECONDS
        if time.monotonic() >= deadline:
            raise _CodexAppServerError("timeout")
        reader = _JsonLineReader(proc, deadline)
        _send_app_server_message(proc, {
            "method": "initialize",
            "id": 0,
            "params": {
                "clientInfo": {
                    "name": "agentbar",
                    "title": "AgentBar",
                    "version": __version__,
                }
            },
        })
        initialized = reader.response(0)
        if "error" in initialized or not isinstance(initialized.get("result"), dict):
            raise _CodexAppServerError("initialize failed")
        _send_app_server_message(proc, {"method": "initialized", "params": {}})
        _send_app_server_message(proc, {
            "method": "account/read",
            "id": 1,
            # Keep status checks local: Codex must not refresh or access the
            # network merely because AgentBar renders a credentials badge.
            "params": {"refreshToken": False},
        })
        account_response = reader.response(1)
        if "error" in account_response or not isinstance(account_response.get("result"), dict):
            raise _CodexAppServerError("account read failed")
        raw_account_result = account_response["result"]
        raw_account = raw_account_result.get("account")
        account_result = {
            "account": (
                {
                    "type": _clean_metadata(raw_account.get("type"), max_length=80),
                    "planType": _clean_metadata(raw_account.get("planType"), max_length=160),
                }
                if isinstance(raw_account, dict)
                else None
            ),
            "requiresOpenaiAuth": raw_account_result.get("requiresOpenaiAuth") is True,
        }
        limits_result = None
        if include_rate_limits and _codex_account_is_chatgpt(account_result):
            _send_app_server_message(proc, {
                "method": "account/rateLimits/read",
                "id": 2,
            })
            limits_response = reader.response(2)
            if "error" in limits_response or not isinstance(limits_response.get("result"), dict):
                raise _CodexAppServerError("rate limits read failed")
            limits_result = _sanitize_codex_limits_result(limits_response["result"])
        return account_result, limits_result
    finally:
        try:
            if proc is not None and proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            if proc is not None:
                _terminate_process_group(proc)
            if proc is not None and proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass
        finally:
            _codex_app_server_lock.release()


def _codex_account_is_chatgpt(account_result: dict) -> bool:
    account = account_result.get("account")
    if not isinstance(account, dict):
        return False
    return str(account.get("type") or "").casefold() == "chatgpt"


def _codex_account_resolution(account_result: dict) -> _CredentialResolution:
    account = account_result.get("account")
    if not isinstance(account, dict):
        return _CredentialResolution(
            None,
            "codex_app_server",
            "not_logged_in",
            "未检测到 Codex ChatGPT 登录态，请先运行 codex login",
        )
    account_type = str(account.get("type") or "").casefold()
    if account_type == "apikey":
        return _CredentialResolution(
            None,
            "codex_app_server",
            "api_key_unsupported",
            "Codex 当前使用 API Key 登录；API Key 不适用于 ChatGPT 订阅额度，请运行 codex login 选择 ChatGPT 登录",
        )
    if account_type != "chatgpt":
        return _CredentialResolution(
            None,
            "codex_app_server",
            "unavailable",
            "Codex 当前登录类型不支持 ChatGPT 订阅额度",
        )
    return _CredentialResolution(
        {"plan": _clean_metadata(account.get("planType"))},
        "codex_app_server",
        "available",
        "已通过 Codex App Server 读取 ChatGPT 登录态",
    )


def _resolve_codex_credentials(
    *,
    binary: str = "",
    env: dict | None = None,
) -> _CredentialResolution:
    try:
        account_result, _ = _codex_app_server_session(
            binary=binary,
            env=env,
            include_rate_limits=False,
        )
    except _CodexAppServerError:
        return _CredentialResolution(
            None,
            "codex_app_server",
            "unavailable",
            "无法通过 Codex App Server 读取登录态，请确认 Codex CLI 可用",
        )
    return _codex_account_resolution(account_result)


def credential_status(
    tool: str,
    *,
    settings=None,
    allow_interactive: bool = False,
    refresh: bool = False,
) -> dict:
    """Return cached, non-sensitive CLI credential state for UI/snapshots.

    ``refresh`` bypasses the status cache. ``allow_interactive`` is retained for
    API compatibility but never enables a prompt: AgentBar only invokes the
    CLIs' non-interactive status protocols and never reads OAuth credentials.
    """
    normalized = (tool or "").strip().casefold()
    if normalized not in {"claude", "codex"}:
        return {
            "available": False,
            "source": "none",
            "status": "unavailable",
            "detail": "不支持的凭据来源",
            "needs_authorization": False,
        }
    configured_binary = ""
    if settings is not None:
        configured_binary = str(
            (getattr(settings, "tool_paths", None) or {}).get(normalized) or ""
        )
    cache_key = normalized if not configured_binary else f"{normalized}\0{configured_binary}"
    wait_deadline = time.monotonic() + _CODEX_APP_SERVER_TIMEOUT_SECONDS + 1.0
    with _credential_status_condition:
        cached = _credential_status_cache.get(cache_key)
        if (
            not refresh
            and not allow_interactive
            and cached
            and time.monotonic() - cached[0] < _STATUS_CACHE_TTL_SECONDS
        ):
            return dict(cached[1])
        while cache_key in _credential_status_inflight:
            remaining = wait_deadline - time.monotonic()
            if remaining <= 0:
                return {
                    "available": False,
                    "source": f"{normalized}_cli",
                    "status": "timeout",
                    "detail": "CLI 登录状态检测超时",
                    "needs_authorization": False,
                }
            _credential_status_condition.wait(remaining)
        # Recheck after a concurrent detection finishes. Even explicit refresh
        # callers share that one just-completed probe instead of immediately
        # spawning the same CLI process again.
        cached = _credential_status_cache.get(cache_key)
        if cached and cached[0] >= wait_deadline - (
            _CODEX_APP_SERVER_TIMEOUT_SECONDS + 1.0
        ):
            return dict(cached[1])
        _credential_status_inflight.add(cache_key)
    try:
        binary, env = _tool_runtime(normalized, settings)
        result = (
            _resolve_claude_credentials(binary=binary, env=env)
            if normalized == "claude"
            else _resolve_codex_credentials(binary=binary, env=env)
        )
        return _remember_credential_status(cache_key, result)
    finally:
        with _credential_status_condition:
            _credential_status_inflight.discard(cache_key)
            _credential_status_condition.notify_all()


def _tool_runtime(tool: str, settings=None) -> tuple[str, dict | None]:
    if settings is None:
        return "", None
    try:
        if tool == "claude":
            from .adapters.claude import ClaudeAdapter as AdapterClass
        else:
            from .adapters.codex import CodexAdapter as AdapterClass
        adapter = AdapterClass(settings)
        binary = adapter.binary() or ""
        return binary, adapter.build_env(dict(os.environ))
    except (OSError, ValueError):
        return "", dict(os.environ)


# ================= Claude =================


class ClaudeUsageFetcher:
    tool = "claude"

    def __init__(
        self,
        *,
        binary: str = "",
        env: dict | None = None,
        settings=None,
        status_cache_key: str = "claude",
    ):
        self.binary = (binary or "").strip()
        self.env = env
        self.settings = settings
        self.status_cache_key = status_cache_key

    def _resolve(self) -> _CredentialResolution:
        binary, env = (
            _tool_runtime("claude", self.settings)
            if self.settings is not None
            else (self.binary, self.env)
        )
        result = _resolve_claude_credentials(binary=binary, env=env)
        _remember_credential_status(self.status_cache_key, result)
        return result

    def load_credentials(self) -> dict | None:
        return self._resolve().credentials

    def fetch(self, interactive: bool = False) -> UsageSnapshot | None:
        result = self._resolve()
        if not result.credentials:
            return UsageSnapshot(
                self.tool,
                source="claude_auth_status",
                error=result.detail,
            )
        # Anthropic does not expose a supported third-party subscription quota
        # API. Keep this source status-only; scheduling continues to rely on
        # observed CLI limit events and local ccusage data.
        return UsageSnapshot(
            self.tool,
            windows=[],
            plan=result.credentials.get("plan"),
            source="claude_auth_status",
        )


# ================= Codex =================


class CodexUsageFetcher:
    tool = "codex"

    def __init__(
        self,
        *,
        model: str = "",
        binary: str = "",
        env: dict | None = None,
        settings=None,
        status_cache_key: str = "codex",
    ):
        self.model = (model or "").strip()
        self.binary = (binary or "").strip()
        self.env = env
        self.settings = settings
        self.status_cache_key = status_cache_key

    def load_credentials(self) -> dict | None:
        binary, env = (
            _tool_runtime("codex", self.settings)
            if self.settings is not None
            else (self.binary, self.env)
        )
        result = _resolve_codex_credentials(binary=binary, env=env)
        _remember_credential_status(self.status_cache_key, result)
        return result.credentials

    def fetch(self, interactive: bool = False) -> UsageSnapshot | None:
        binary, env = (
            _tool_runtime("codex", self.settings)
            if self.settings is not None
            else (self.binary, self.env)
        )
        try:
            account_result, limits_result = _codex_app_server_session(
                binary=binary,
                env=env,
                include_rate_limits=True,
            )
        except _CodexAppServerError:
            result = _CredentialResolution(
                None,
                "codex_app_server",
                "unavailable",
                "Codex App Server 额度能力不可用；将继续使用本地观测的限额事件",
            )
            _remember_credential_status(self.status_cache_key, result)
            return UsageSnapshot(
                self.tool,
                source="codex_app_server",
                error=result.detail,
            )
        result = _codex_account_resolution(account_result)
        _remember_credential_status(self.status_cache_key, result)
        if not result.credentials:
            return UsageSnapshot(
                self.tool,
                source="codex_app_server",
                error=result.detail,
            )
        if not isinstance(limits_result, dict):
            return UsageSnapshot(
                self.tool,
                plan=result.credentials.get("plan"),
                source="codex_app_server",
                error="Codex App Server 未返回额度；将继续使用本地观测的限额事件",
            )
        return self.parse(limits_result, plan=result.credentials.get("plan"))

    @staticmethod
    def _model_key(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())

    @staticmethod
    def _windows(
        limits: dict,
        *,
        model: str | None = None,
        limited: bool = False,
        label_prefix: str = "",
    ) -> list[UsageWindow]:
        windows = []
        for keys, fallback_label in (
            (("primary", "primary_window"), "5h"),
            (("secondary", "secondary_window"), "7d"),
        ):
            w = next((limits.get(key) for key in keys if isinstance(limits.get(key), dict)), None)
            if not isinstance(w, dict):
                continue
            used = w.get("used_percent", w.get("usedPercent"))
            if used is None:
                continue
            mins = w.get("window_duration_mins") or w.get("windowDurationMins")
            seconds = w.get("limit_window_seconds") or w.get("limitWindowSeconds")
            label = fallback_label
            if mins:
                if mins < 60:
                    label = f"{round(mins)}m"
                else:
                    label = (
                        f"{round(mins / 60)}h"
                        if mins < 2880
                        else f"{round(mins / 1440)}d"
                    )
            elif seconds:
                label = f"{round(seconds / 3600)}h" if seconds < 2880 * 60 else f"{round(seconds / 86400)}d"
            reset = w.get(
                "resets_at",
                w.get("resetsAt", w.get("reset_at", w.get("resetAt"))),
            )
            if not reset and w.get("reset_after_seconds"):
                reset = time.time() + float(w["reset_after_seconds"])
            windows.append(UsageWindow(
                label=f"{label_prefix}{label}",
                used_percent=max(0.0, min(100.0, float(used))),
                resets_at=float(reset) if reset else None,
                model=model,
                limited=limited,
            ))
        return windows

    @staticmethod
    def _limited(limits: dict) -> bool:
        return (
            bool(limits.get("limit_reached", limits.get("limitReached")))
            or limits.get("allowed") is False
            or bool(limits.get("rateLimitReachedType"))
        )

    def parse(self, data: dict, plan: str | None = None) -> UsageSnapshot:
        """Parse official App Server limits and older saved fixture shapes.

        Live fetching only uses ``rateLimits``/``rateLimitsByLimitId`` from the
        Codex process; the compatibility shape is retained for existing local
        snapshots and parser tests.
        """
        native_by_id = data.get("rateLimitsByLimitId")
        native_base = data.get("rateLimits")
        native_shape = isinstance(native_by_id, dict) or isinstance(native_base, dict)
        if native_shape:
            native_items = []
            for limit_id, limit in (native_by_id or {}).items():
                if isinstance(limit, dict):
                    native_items.append({
                        "limit_name": limit.get("limitName"),
                        "metered_feature": limit.get("limitId") or limit_id,
                        "rate_limit": limit,
                    })
            data = {
                "plan_type": (
                    native_base.get("planType")
                    if isinstance(native_base, dict)
                    else None
                ),
                "rate_limit": native_base or {},
                "additional_rate_limits": native_items,
            }
        additional = [
            item for item in (data.get("additional_rate_limits") or [])
            if isinstance(item, dict)
        ]
        # Persist/select the stable metered_feature whenever the endpoint exposes
        # one. limit_name is presentation text and can be renamed independently.
        available = [
            str(item.get("metered_feature") or item.get("limit_name") or "").strip()
            for item in additional
        ]
        available = [name for name in available if name]
        selected_item = None
        if self.model:
            wanted = self._model_key(self.model)
            for item in additional:
                names = (str(item.get("limit_name") or ""), str(item.get("metered_feature") or ""))
                if wanted and wanted in {self._model_key(name) for name in names}:
                    selected_item = item
                    break
            if selected_item is None:
                choices = "、".join(available[:6]) or "暂无模型专属额度"
                return UsageSnapshot(
                    self.tool,
                    plan=plan or data.get("plan_type"),
                    source="codex_app_server",
                    error=f"未找到模型额度 {self.model!r}；接口可用：{choices}",
                    model=self.model,
                    available_models=available,
                )

        account_limits = data.get("rate_limits") or data.get("rate_limit") or {}
        account_limited = self._limited(account_limits)
        selected_label = (
            str(
                selected_item.get("limit_name")
                or selected_item.get("metered_feature")
                or self.model
            )
            if selected_item is not None
            else (self.model or None)
        )
        if selected_item is not None:
            selected_limits = selected_item.get("rate_limit") or {}
            selected_limited = self._limited(selected_limits)
            selected_id = str(selected_item.get("metered_feature") or "")
            account_id = str(account_limits.get("limitId") or "")
            windows = []
            if not native_shape or not account_id or selected_id != account_id:
                windows = self._windows(
                    account_limits,
                    limited=account_limited,
                    label_prefix="账户 ",
                )
            windows.extend(self._windows(
                selected_limits,
                model=selected_label,
                limited=selected_limited,
                label_prefix=f"{selected_label} ",
            ))
        else:
            selected_limited = False
            windows = self._windows(account_limits, limited=account_limited)
        snap = UsageSnapshot(
            self.tool,
            windows=windows,
            plan=plan or data.get("plan_type"),
            source="codex_app_server",
            # 保留 snapshot 级标记给旧 UI；调度决策应按 window.model
            # 逐窗口判断，避免模型专属限额污染账户级窗口。
            limited=account_limited or selected_limited,
            model=selected_label,
            available_models=available,
        )
        if not windows:
            snap.error = "Codex App Server 未返回可识别的额度窗口"
        return snap


# ================= 快手内部 provider（MyToken / Tokenverse） =================
#
# 两者都是凭 corp SSO cookie 访问的「月度信用额度」接口，响应统一为
# {status, message, data} 信封（status==200 为成功）。诚实原则同上：cookie 缺失
# 或接口失败 → 返回带 error 的 UsageSnapshot，绝不编造额度。
#
# used_percent 始终按「信用额度」算（驱动环形进度 + 限额判定）；window 附带
# used/total/unit 供 UI 按用户选择的 unit（credits/percent/token）展示表头数字。


def _corp_envelope(url: str, cookie: str, extra_headers: dict | None = None) -> dict:
    """请求 corp 接口并校验 {status,message,data} 信封，返回 data；失败抛 RuntimeError。"""
    headers = {
        "Cookie": cookie,
        "Accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if extra_headers:
        headers.update(extra_headers)
    body = _http_get_json(url, headers)
    status = body.get("status")
    if status != 200:
        raise RuntimeError(body.get("message") or f"接口返回 status={status}")
    if body.get("data") is None:
        raise RuntimeError("接口响应缺少 data")
    return body["data"]


def _month_start_str(now: float | None = None) -> str:
    lt = time.localtime(now if now is not None else time.time())
    return time.strftime("%Y-%m-01", lt)


def _next_month_start(now: float | None = None) -> float:
    lt = time.localtime(now if now is not None else time.time())
    year, month = lt.tm_year, lt.tm_mon
    if month == 12:
        year, month = year + 1, 1
    else:
        month += 1
    return time.mktime((year, month, 1, 0, 0, 0, 0, 0, -1))


def _credit_window(used: float, total: float | None, unit: str,
                   resets_at: float | None, token_used: float | None,
                   label: str = "本月") -> UsageWindow:
    """把「信用额度用量」组装成一个展示窗口；used_percent 恒按 credits 算。"""
    if total and total > 0:
        pct = max(0.0, min(100.0, used / total * 100.0))
    else:
        pct = 0.0
    if unit == "token":
        disp_used, disp_total = (token_used if token_used is not None else 0.0), None
    elif unit == "percent":
        disp_used, disp_total = pct, 100.0
    else:  # credits
        disp_used, disp_total = used, total
    return UsageWindow(label=label, used_percent=pct, resets_at=resets_at,
                       used=disp_used, total=disp_total, unit=unit)


class MyTokenUsageFetcher:
    """MyToken（mytoken.corp.kuaishou.com）月度信用额度。"""

    tool = "mytoken"
    BASE = "https://mytoken.corp.kuaishou.com"

    def __init__(self, cookie: str = "", unit: str = "credits",
                 refresh_seconds: int = 300):
        self.cookie = (cookie or "").strip()
        self.unit = unit if unit in ("credits", "percent", "token") else "credits"
        self.refresh_seconds = max(60, int(refresh_seconds or 300))

    def _username(self) -> str:
        data = _corp_envelope(
            f"{self.BASE}/api/auth/sso/user", self.cookie,
            {"Referer": f"{self.BASE}/usage"},
        )
        name = (data.get("name") or data.get("username") or data.get("userName")
                or data.get("loginName") or "").strip()
        if not name:
            raise RuntimeError("SSO 用户接口未返回用户名")
        return name

    def _monthly_tokens(self, username: str) -> float | None:
        month_start_ms = int(time.mktime(time.strptime(_month_start_str(), "%Y-%m-%d")) * 1000)
        now_ms = int(time.time() * 1000)
        url = (f"{self.BASE}/api/v1/billing/usage/token-summary"
               f"?granularity=day&startTime={month_start_ms}&endTime={now_ms}")
        try:
            data = _corp_envelope(url, self.cookie, {"kwaipilot-username": username})
        except (RuntimeError, urllib.error.URLError, OSError):
            return None
        buckets = data.get("buckets") or []
        return float(sum((b.get("totalTokens") or 0) for b in buckets))

    def fetch(self, interactive: bool = False) -> UsageSnapshot | None:
        if not self.cookie:
            return UsageSnapshot(self.tool, source="mytoken_api",
                                 error="未配置 cookie（config.json → providers.mytoken.cookie）")
        try:
            username = self._username()
            data = _corp_envelope(
                f"{self.BASE}/api/v1/billing/account", self.cookie,
                {"kwaipilot-username": username},
            )
        except urllib.error.HTTPError as e:
            return UsageSnapshot(self.tool, source="mytoken_api", error=f"HTTP {e.code}")
        except (RuntimeError, urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            return UsageSnapshot(self.tool, source="mytoken_api", error=str(e))

        summary = data.get("summary") or {}
        account = data.get("account") or {}
        total = summary.get("creditTotal")
        used = summary.get("creditUsed")
        if used is None or total is None:
            return UsageSnapshot(self.tool, source="mytoken_api",
                                 error="account 接口未返回 creditUsed/creditTotal")
        renew_ms = summary.get("renewAt")
        token_used = self._monthly_tokens(username) if self.unit == "token" else None
        window = _credit_window(
            float(used), float(total), self.unit,
            resets_at=(renew_ms / 1000.0) if renew_ms else _next_month_start(),
            token_used=token_used,
        )
        plan = account.get("tierName") or account.get("tierCode")
        return UsageSnapshot(
            self.tool, windows=[window], plan=plan, source="mytoken_api",
            limited=bool(window.used_percent >= 99.9),
        )


class TokenverseUsageFetcher:
    """Tokenverse（tokenverse.corp.kuaishou.com）月度信用额度。"""

    tool = "tokenverse"
    BASE = "https://tokenverse.corp.kuaishou.com"

    def __init__(self, cookie: str = "", unit: str = "credits",
                 refresh_seconds: int = 300):
        self.cookie = (cookie or "").strip()
        self.unit = unit if unit in ("credits", "percent", "token") else "credits"
        self.refresh_seconds = max(60, int(refresh_seconds or 300))

    _PLAN_NAMES = {1: "Standard"}

    def fetch(self, interactive: bool = False) -> UsageSnapshot | None:
        if not self.cookie:
            return UsageSnapshot(self.tool, source="tokenverse_api",
                                 error="未配置 cookie（config.json → providers.tokenverse.cookie）")
        try:
            plan = _corp_envelope(f"{self.BASE}/api/coding-plan/status", self.cookie)
            summary = _corp_envelope(
                f"{self.BASE}/api/coding-plan/usage/summary?startDate={_month_start_str()}",
                self.cookie,
            )
        except urllib.error.HTTPError as e:
            return UsageSnapshot(self.tool, source="tokenverse_api", error=f"HTTP {e.code}")
        except (RuntimeError, urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            return UsageSnapshot(self.tool, source="tokenverse_api", error=str(e))

        monthly = plan.get("monthlyCredits")
        if monthly is None:
            monthly = (plan.get("openModelCreditsPerMon") or 0) + (
                plan.get("closedSourceMonthlyCredits")
                or plan.get("closeModelCreditsPerMon") or 0
            )
        used = summary.get("totalCredits")
        if used is None:
            return UsageSnapshot(self.tool, source="tokenverse_api",
                                 error="usage summary 未返回 totalCredits")
        token_used = (float(summary.get("totalTokens") or 0)
                      if self.unit == "token" else None)
        window = _credit_window(
            float(used), float(monthly or 0), self.unit,
            resets_at=_next_month_start(), token_used=token_used,
        )
        return UsageSnapshot(
            self.tool, windows=[window],
            plan=self._PLAN_NAMES.get(plan.get("planType")),
            source="tokenverse_api",
            limited=bool(window.used_percent >= 99.9),
        )


_CORP_FETCHERS = {
    "mytoken": MyTokenUsageFetcher,
    "tokenverse": TokenverseUsageFetcher,
}


def get_usage_fetchers(settings=None) -> dict[str, object]:
    """只创建用户显式启用的额度/登录状态来源。

    Claude 仅查询官方 CLI 的非敏感登录状态；Codex 仅通过官方
    App Server 查额度。两者都不读取或保存 OAuth token。
    """
    fetchers: dict[str, object] = {}
    if settings is not None:
        sources = getattr(settings, "quota_sources", None) or {}
        claude = sources.get("claude") or {}
        if claude.get("enabled"):
            configured = str((getattr(settings, "tool_paths", None) or {}).get("claude") or "")
            fetchers["claude"] = ClaudeUsageFetcher(
                binary=os.path.expanduser(configured) if configured else "",
                settings=settings,
                status_cache_key=("claude" if not configured else f"claude\0{configured}"),
            )
        codex = sources.get("codex") or {}
        if codex.get("enabled"):
            configured = str((getattr(settings, "tool_paths", None) or {}).get("codex") or "")
            fetchers["codex"] = CodexUsageFetcher(
                model=codex.get("model", ""),
                binary=os.path.expanduser(configured) if configured else "",
                settings=settings,
                status_cache_key=("codex" if not configured else f"codex\0{configured}"),
            )
    providers = getattr(settings, "providers", None) or {}
    for name, cls in _CORP_FETCHERS.items():
        cfg = providers.get(name) or {}
        if cfg.get("enabled") and (cfg.get("cookie") or "").strip():
            fetchers[name] = cls(
                cookie=cfg.get("cookie", ""),
                unit=cfg.get("unit", "credits"),
                refresh_seconds=cfg.get("refresh_seconds", 300),
            )
    return fetchers
