"""Open the localhost task panel through macOS LaunchServices.

主线程纪律：菜单栏进程的任何 UI 回调都跑在 Cocoa 主线程，阻塞即整个 App
"卡住"（"打开任务面板/快速添加卡死"事故的根因就在这里）。因此分两个入口：

- open_url_async    — GUI 专用。Popen fire-and-forget，毫秒级返回，绝不等待。
- open_panel_url    — CLI 专用（终端里等结果没问题）。等待 `open` 退出码，
                      失败再退回 webbrowser（其在 macOS 上走 osascript/Apple
                      Events，可能触发 TCC 自动化授权弹窗——只允许 CLI 场景用）。
"""

from __future__ import annotations

import subprocess
import webbrowser

_OPEN = "/usr/bin/open"
_QUIET = {
    "stdin": subprocess.DEVNULL,
    "stdout": subprocess.DEVNULL,
    "stderr": subprocess.DEVNULL,
}


def open_url_async(url: str) -> bool:
    """GUI 主线程安全：把 URL 交给 LaunchServices 后立即返回。

    返回 False 仅代表连 `open` 进程都没起来（极罕见）；`open` 自身失败属于
    后台事件，由调用方决定是否补提示。
    """
    try:
        subprocess.Popen([_OPEN, url], **_QUIET)
        return True
    except OSError:
        return False


def open_panel_url(url: str) -> bool:
    """CLI 阻塞版：等待结果，浏览器关联损坏时兜底 webbrowser。"""
    try:
        result = subprocess.run([_OPEN, url], timeout=8, check=False, **_QUIET)
        if result.returncode == 0:
            return True
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False
