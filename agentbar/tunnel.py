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

        # Publish the process immediately. stop() may run while cloudflared is
        # still starting; delaying this assignment until the URL appears leaks
        # an untracked tunnel process during app shutdown.
        with self._lock:
            if self._state != "starting":
                self._terminate_process(proc)
                return False
            self._proc = proc

        url_evt = threading.Event()

        def _reader():
            for line in proc.stdout:  # 进程存活期间持续读，EOF = 进程退出
                if not url_evt.is_set():
                    m = _URL_RE.search(line)
                    if m:
                        with self._lock:
                            accepted = (
                                self._proc is proc and self._state == "starting"
                            )
                            if accepted:
                                self._url = m.group(0)
                        if accepted:
                            url_evt.set()
            # Also wake start() when stop() terminates a still-starting process.
            url_evt.set()
            with self._lock:
                if self._proc is proc and self._state == "up":
                    self._mark_down_locked("隧道进程已退出")

        threading.Thread(target=_reader, name="agentbar-tunnel-io", daemon=True).start()

        if not url_evt.wait(timeout):
            self._terminate_process(proc)
            with self._lock:
                if self._proc is proc:
                    self._proc = None
                if self._state == "starting":
                    self._state = "error"
                    self._error = f"启动超时（{timeout:.0f}s，公司网络可能拦截 Cloudflare）"
            return False

        with self._lock:
            if self._proc is not proc or self._state != "starting" or proc.poll() is not None:
                should_stop = True
                host = None
            else:
                should_stop = False
                self._host = urlparse(self._url).hostname
                self._state = "up"
                host = self._host
        if should_stop:
            self._terminate_process(proc)
            return False
        log.info("tunnel up: %s", self._url)
        notified_up = False
        if self._on_up and host:
            try:
                self._on_up(host)
                notified_up = True
            except Exception as exc:
                # A callback can fail after partially mutating its allow-list.
                # Compensate and retire the process; otherwise start() would
                # raise while leaving an untracked public tunnel alive.
                log.exception("tunnel on_up callback failed")
                with self._lock:
                    if self._proc is proc and self._state == "up":
                        self._proc, self._url, self._host = None, None, None
                        self._state = "error"
                        self._error = f"隧道注册失败: {exc}"
                self._notify_down(host)
                self._terminate_process(proc)
                return False
        # stop() may run after we publish state=up but while an arbitrary on_up
        # callback is still executing. Re-check after notification and compensate
        # a late allow-list add; callbacks must never run while holding _lock.
        with self._lock:
            still_up = (
                self._proc is proc
                and self._state == "up"
                and self._host == host
                and proc.poll() is None
            )
        if not still_up:
            if notified_up and self._on_down and host:
                self._notify_down(host)
            self._terminate_process(proc)
            return False
        return True

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            host = self._host
            self._state, self._url, self._host, self._error = "off", None, None, ""
        if proc and proc.poll() is None:
            self._terminate_process(proc)
        if self._on_down and host:
            self._notify_down(host)
        log.info("tunnel stopped")

    # ---------- internal ----------

    @staticmethod
    def _terminate_process(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                log.warning("cloudflared did not exit after SIGKILL")

    def _mark_down_locked(self, reason: str) -> None:
        """caller must hold self._lock"""
        host = self._host
        self._proc, self._url, self._host = None, None, None
        self._state, self._error = "error", reason
        if self._on_down and host:
            threading.Thread(
                target=self._notify_down, args=(host,), daemon=True
            ).start()

    def _notify_down(self, host: str) -> None:
        """Best-effort compensating callback; never obstruct process cleanup."""
        callback = self._on_down
        if callback is None:
            return
        try:
            callback(host)
        except Exception:
            log.exception("tunnel on_down callback failed for %s", host)
