"""Cloudflare Quick Tunnel — 一键公网访问（手机不在同一 Wi-Fi 时用）.

原理：`cloudflared tunnel --url http://127.0.0.1:<port>` 生成一个临时
https://<random>.trycloudflare.com 域名，流量经 Cloudflare 边缘转发到本机。
无需账号、无需自购公网服务器；每次启动域名会变（临时隧道的特性）。

安全：API 仍要求 token；隧道域名启动成功后动态加入 Host 白名单
（其余域名一律 403，DNS-rebinding 防御不受影响）。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
from urllib.parse import urlparse

log = logging.getLogger("agentbar.tunnel")

_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
# launchd 下 PATH 被裁剪，brew 路径直接兜底
_BINARY_CANDIDATES = ("/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared")
START_TIMEOUT = 40.0


class TunnelManager:
    """状态机: off → starting → up → off/error。所有方法线程安全。"""

    def __init__(self, port: int, on_up=None, on_down=None,
                 binary_override: str | None = None):
        self.port = port
        self._on_up = on_up      # callback(hostname): 注册 Host 白名单
        self._on_down = on_down  # callback(hostname): 注销
        self._binary_override = binary_override  # 测试注入
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._url: str | None = None
        self._host: str | None = None
        self._state = "off"      # off | starting | up | error
        self._error = ""

    # ---------- queries ----------

    def binary(self) -> str | None:
        if self._binary_override:
            return self._binary_override if os.path.exists(self._binary_override) else None
        found = shutil.which("cloudflared")
        if found:
            return found
        for c in _BINARY_CANDIDATES:
            if os.path.exists(c):
                return c
        return None

    def status(self) -> dict:
        with self._lock:
            # 进程意外退出 → 降级为 error（reader 线程也会置，这里兜底）
            if self._state == "up" and self._proc and self._proc.poll() is not None:
                self._mark_down_locked("隧道进程已退出")
            return {"state": self._state, "url": self._url, "error": self._error,
                    "installed": self.binary() is not None}

    @property
    def url(self) -> str | None:
        with self._lock:
            return self._url if self._state == "up" else None

    # ---------- lifecycle ----------

    def start(self, timeout: float = START_TIMEOUT) -> bool:
        """阻塞直到隧道可用或失败；调用方负责放到后台线程。"""
        with self._lock:
            if self._state in ("starting", "up"):
                return self._state == "up"
            binary = self.binary()
            if not binary:
                self._state = "error"
                self._error = "未安装 cloudflared（brew install cloudflared）"
                return False
            self._state, self._error = "starting", ""

        try:
            proc = subprocess.Popen(
                [binary, "tunnel", "--no-autoupdate", "--url",
                 f"http://127.0.0.1:{self.port}"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, stdin=subprocess.DEVNULL,
            )
        except OSError as e:
            with self._lock:
                self._state, self._error = "error", f"启动失败: {e}"
            return False

        url_evt = threading.Event()

        def _reader():
            for line in proc.stdout:  # 进程存活期间持续读，EOF = 进程退出
                if not url_evt.is_set():
                    m = _URL_RE.search(line)
                    if m:
                        with self._lock:
                            self._url = m.group(0)
                        url_evt.set()
            with self._lock:
                if self._proc is proc and self._state == "up":
                    self._mark_down_locked("隧道进程已退出")

        threading.Thread(target=_reader, name="agentbar-tunnel-io", daemon=True).start()

        if not url_evt.wait(timeout):
            proc.terminate()
            with self._lock:
                self._state = "error"
                self._error = f"启动超时（{timeout:.0f}s，公司网络可能拦截 Cloudflare）"
            return False

        with self._lock:
            self._proc = proc
            self._host = urlparse(self._url).hostname
            self._state = "up"
            host = self._host
        log.info("tunnel up: %s", self._url)
        if self._on_up and host:
            self._on_up(host)
        return True

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            host = self._host
            self._state, self._url, self._host, self._error = "off", None, None, ""
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._on_down and host:
            self._on_down(host)
        log.info("tunnel stopped")

    # ---------- internal ----------

    def _mark_down_locked(self, reason: str) -> None:
        """caller must hold self._lock"""
        host = self._host
        self._proc, self._url, self._host = None, None, None
        self._state, self._error = "error", reason
        if self._on_down and host:
            threading.Thread(
                target=self._on_down, args=(host,), daemon=True
            ).start()
