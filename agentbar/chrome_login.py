"""Interactive corp SSO login through an isolated Chrome CDP session.

This mirrors the reliable part of AIUsageBar's setup flow: launch a real Chrome
window with a temporary profile, let Kit/0Pass complete SSO, read the target
host's cookies through Chrome DevTools Protocol, then validate them against the
real provider API. No cookie is persisted until validation succeeds.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

import websocket

from .browser_cookies import ImportedCookie
from .config import PROVIDER_HOSTS, PROVIDER_LOGIN_URLS
from .usage import MyTokenUsageFetcher, TokenverseUsageFetcher


class ChromeLoginError(RuntimeError):
    pass


_READY_COOKIE_NAMES = {
    "mytoken": {"accessproxy_session", "JSESSIONID", "KP_SSO_SID", "ktrace-context"},
    "tokenverse": {"accessproxy_session"},
}


def _chrome_binary() -> Path | None:
    candidates = (
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        Path("/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta"),
    )
    return next((path for path in candidates if path.is_file()), None)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _domain_matches(cookie_host: str, target_host: str) -> bool:
    cookie_host = (cookie_host or "").lstrip(".").lower()
    target_host = target_host.lower()
    return target_host == cookie_host or target_host.endswith("." + cookie_host)


def _cookie_header(raw_cookies: list[dict], target_host: str) -> str:
    """Build a deterministic Cookie header for target_host without logging values."""
    now = time.time()
    candidates = []
    for entry in raw_cookies:
        name = str(entry.get("name") or "").strip()
        value = str(entry.get("value") or "")
        domain = str(entry.get("domain") or "")
        expires = entry.get("expires")
        if not name or not value or not _domain_matches(domain, target_host):
            continue
        if isinstance(expires, (int, float)) and expires > 0 and expires <= now:
            continue
        path = str(entry.get("path") or "/")
        exact = int(domain.lstrip(".").lower() == target_host.lower())
        candidates.append((exact, len(domain.lstrip(".")), len(path), name, value))
    candidates.sort(reverse=True)
    seen = set()
    parts = []
    for _, _, _, name, value in candidates:
        if name in seen:
            continue
        seen.add(name)
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def _validate_cookie(provider: str, header: str) -> bool:
    if provider == "mytoken":
        snap = MyTokenUsageFetcher(cookie=header).fetch()
    elif provider == "tokenverse":
        snap = TokenverseUsageFetcher(cookie=header).fetch()
    else:
        return False
    return bool(snap and not snap.error and snap.windows)


class ChromeCDPLogin:
    def __init__(self, provider: str):
        if provider not in PROVIDER_HOSTS:
            raise ChromeLoginError(f"未知 provider: {provider}")
        self.provider = provider
        self.host = PROVIDER_HOSTS[provider]
        self.login_url = PROVIDER_LOGIN_URLS[provider]
        self.port = _free_port()
        self._cancel = threading.Event()
        self._process: subprocess.Popen | None = None
        self._profile_dir: Path | None = None

    def cancel(self) -> None:
        self._cancel.set()
        # UI 主线程只发终止信号，不在这里 wait/rmtree；run() 的后台线程
        # 会在 finally 里完成回收，避免“取消登录”按钮卡住菜单栏。
        process = self._process
        if process and process.poll() is None:
            process.terminate()

    def close(self) -> None:
        process = self._process
        self._process = None
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        profile_dir = self._profile_dir
        self._profile_dir = None
        if profile_dir:
            shutil.rmtree(profile_dir, ignore_errors=True)

    def run(
        self,
        on_status: Callable[[str], None] | None = None,
        timeout: float = 300,
    ) -> ImportedCookie:
        chrome = _chrome_binary()
        if chrome is None:
            raise ChromeLoginError("未检测到 Google Chrome / Chrome Beta")
        status = on_status or (lambda _message: None)
        self._profile_dir = Path(tempfile.mkdtemp(prefix="agentbar-chrome-login-"))
        args = [
            str(chrome),
            f"--remote-debugging-port={self.port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={self._profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            self.login_url,
        ]
        status("正在启动独立 Chrome 登录窗口…")
        try:
            self._process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._wait_debugger(timeout=20)
            status("请在弹出的 Chrome 中完成企业 SSO 登录…")
            deadline = time.time() + timeout
            last_count = -1
            while time.time() < deadline:
                self._check_cancelled()
                raw = self._fetch_all_cookies()
                header = _cookie_header(raw, self.host)
                fields = {
                    part.split("=", 1)[0].strip()
                    for part in header.split(";")
                    if "=" in part
                }
                count = len(fields)
                if count != last_count:
                    last_count = count
                    status(
                        f"已捕获 {count} 个 Cookie，等待 SSO 完成…"
                        if count
                        else "等待企业登录 Cookie…"
                    )
                ready = _READY_COOKIE_NAMES[self.provider].issubset(fields)
                if ready:
                    status("已捕获完整 Cookie，正在用真实额度接口验证…")
                    if _validate_cookie(self.provider, header):
                        return ImportedCookie(
                            header=header,
                            source="Chrome 企业 SSO 登录",
                            count=count,
                        )
                    status("Cookie 已捕获，但额度接口尚未通过，继续等待登录完成…")
                self._cancel.wait(1.5)
            raise ChromeLoginError("浏览器登录超时（5 分钟）")
        except OSError as exc:
            raise ChromeLoginError(f"启动 Chrome 失败：{exc}") from exc
        finally:
            self.close()

    def _check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise ChromeLoginError("已取消浏览器登录")
        if self._process and self._process.poll() is not None:
            raise ChromeLoginError("Chrome 登录窗口已关闭")

    def _json_get(self, path: str, timeout: float = 5) -> object:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _wait_debugger(self, timeout: float) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._check_cancelled()
            try:
                if isinstance(self._json_get("/json/version", timeout=1), dict):
                    return
            except (OSError, urllib.error.URLError, json.JSONDecodeError):
                pass
            self._cancel.wait(0.5)
        raise ChromeLoginError("Chrome 调试端口启动超时")

    def _fetch_all_cookies(self) -> list[dict]:
        try:
            targets = self._json_get("/json/list")
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return []
        if not isinstance(targets, list):
            return []
        page = next(
            (
                target
                for target in targets
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl")
                and self.host in str(target.get("url") or "")
            ),
            None,
        )
        if page is None:
            page = next(
                (
                    target
                    for target in targets
                    if target.get("type") == "page" and target.get("webSocketDebuggerUrl")
                ),
                None,
            )
        if not page:
            return []
        try:
            connection = websocket.create_connection(
                page["webSocketDebuggerUrl"],
                timeout=6,
                origin=f"http://127.0.0.1:{self.port}",
            )
        except (OSError, ValueError, websocket.WebSocketException):
            return []
        try:
            connection.send(json.dumps({"id": 1, "method": "Network.enable"}))
            connection.send(json.dumps({"id": 2, "method": "Network.getAllCookies"}))
            deadline = time.time() + 6
            while time.time() < deadline:
                message = json.loads(connection.recv())
                if message.get("id") == 2:
                    cookies = (message.get("result") or {}).get("cookies") or []
                    return cookies if isinstance(cookies, list) else []
        except (OSError, ValueError, websocket.WebSocketException):
            return []
        finally:
            connection.close()
        return []
