"""Open the localhost task panel through macOS LaunchServices.

主线程纪律：菜单栏进程的任何 UI 回调都跑在 Cocoa 主线程，阻塞即整个 App
"卡住"。同时 `open` 的失败绝不允许静默——必须监听退出码并回报。

- open_url_async  — GUI 备选路径。Popen 立即返回；后台线程等子进程退出，
                    把 rc/stderr 记日志并回调 on_result（菜单栏据此弹兜底提示）。
                    （GUI 首选路径是 menubar 里的 NSWorkspace 原生打开。）
- open_panel_url  — CLI 专用（终端里等结果没问题）。等待 `open` 退出码，
                    失败再退回 webbrowser（macOS 上走 osascript/Apple Events，
                    可能触发 TCC 授权弹窗——只允许 CLI 场景用）。
"""

from __future__ import annotations

import logging
import subprocess
import threading
import webbrowser
from typing import Callable

log = logging.getLogger("agentbar.browser")

_OPEN = "/usr/bin/open"

# on_result(url, exit_code, stderr_text)；exit_code -1=没起来, -2=超时
OnResult = Callable[[str, int, str], None]


def _sanitized(url: str) -> str:
    return url.split("?")[0]  # 日志里不落 token


def open_url_async(url: str, on_result: OnResult | None = None) -> bool:
    """GUI 主线程安全：立即返回；子进程结果由后台线程记录/回调。"""
    try:
        proc = subprocess.Popen(
            [_OPEN, url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except OSError as e:
        log.error("spawn %s failed: %s", _OPEN, e)
        if on_result:
            on_result(url, -1, str(e))
        return False

    def watch():
        try:
            _, err = proc.communicate(timeout=15)
            rc = proc.returncode
            err_text = (err or b"").decode("utf-8", errors="replace").strip()
        except subprocess.TimeoutExpired:
            rc, err_text = -2, "open 超过 15s 未退出"
        log.info("open %s -> rc=%s%s", _sanitized(url), rc,
                 f" stderr={err_text[:200]}" if err_text else "")
        if on_result:
            on_result(url, rc, err_text)

    threading.Thread(target=watch, name="agentbar-open-watch", daemon=True).start()
    return True


def open_panel_url(url: str) -> bool:
    """CLI 阻塞版：等待结果，浏览器关联损坏时兜底 webbrowser。"""
    try:
        result = subprocess.run(
            [_OPEN, url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
        )
        if result.returncode == 0:
            return True
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False
