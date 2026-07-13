"""macOS menu bar frontend (rumps / NSStatusItem).

只做展示与入口，不承载任何调度逻辑；核心状态每 2s 从 Scheduler.snapshot() 拉取。
本模块只在非 headless 模式 import（避免测试/无 GUI 环境依赖 pyobjc）。
"""

from __future__ import annotations

import logging
import webbrowser

import rumps

from .config import Settings
from .scheduler import Scheduler
from .server import ApiServer

log = logging.getLogger("agentbar.menubar")

_STATUS_LABEL = {
    "idle": ("🤖", "空闲"),
    "running": ("🤖▶", "运行中"),
    "waiting": ("🤖⏳", "等待中"),
    "paused": ("🤖⏸", "已暂停"),
}


class AgentBarApp(rumps.App):
    def __init__(self, core: Scheduler, settings: Settings, server: ApiServer):
        super().__init__("AgentBar", title="🤖", quit_button=None)
        self.core = core
        self.settings = settings
        self.server = server

        self.it_status = rumps.MenuItem("状态：启动中…")
        self.it_current = rumps.MenuItem("当前：—")
        self.it_queue = rumps.MenuItem("队列：0")
        self.it_claude = rumps.MenuItem("Claude：未知")
        self.it_codex = rumps.MenuItem("Codex：未知")
        self.menu = [
            self.it_status,
            self.it_current,
            self.it_queue,
            None,
            self.it_claude,
            self.it_codex,
            None,
            rumps.MenuItem("打开任务面板", callback=self.open_panel),
            rumps.MenuItem("快速添加 Claude 任务…", callback=self.quick_add),
            None,
            rumps.MenuItem("全部暂停", callback=self.pause_all),
            rumps.MenuItem("全部恢复", callback=self.resume_all),
            None,
            rumps.MenuItem("退出 AgentBar", callback=self.quit_app),
        ]
        self._timer = rumps.Timer(self._tick, 2)
        self._timer.start()

    # ---------- refresh ----------

    def _tick(self, _sender=None) -> None:
        try:
            s = self.core.snapshot()
        except Exception:
            log.exception("snapshot failed")
            return
        icon, label = _STATUS_LABEL.get(s["status"], ("🤖", s["status"]))
        n_run = len(s["running_titles"])
        self.title = icon + (str(n_run) if n_run > 1 else "")
        self.it_status.title = f"状态：{label}"
        cur = "、".join(s["running_titles"]) or "—"
        self.it_current.title = f"当前：{cur[:60]}"
        self.it_queue.title = f"队列：{s['queued']} 排队 / {s['waiting_quota']} 等额度"
        for tool, item in (("claude", self.it_claude), ("codex", self.it_codex)):
            qi = s["quota"].get(tool)
            if qi:
                mark = {"ok": "🟢", "limited": "🟠"}.get(qi["state"], "⚪")
                item.title = f"{mark} {tool.capitalize()}：{qi['detail'][:48]}"

    # ---------- actions ----------

    def open_panel(self, _sender) -> None:
        webbrowser.open(self.server.url(with_token=True))

    def quick_add(self, _sender) -> None:
        w = rumps.Window(
            title="快速添加 Claude 任务",
            message=f"工作目录：{self.settings.default_cwd}\n（其他工具/目录请用任务面板）",
            default_text="",
            ok="添加",
            cancel="取消",
            dimensions=(420, 120),
        )
        resp = w.run()
        if resp.clicked and resp.text.strip():
            try:
                self.core.add_task(
                    prompt=resp.text, tool="claude", cwd=self.settings.default_cwd
                )
            except ValueError as e:
                rumps.alert("添加失败", str(e))

    def pause_all(self, _sender) -> None:
        self.core.pause_all()
        self._tick()

    def resume_all(self, _sender) -> None:
        self.core.resume_all()
        self._tick()

    def quit_app(self, _sender) -> None:
        log.info("quit from menu")
        try:
            self.core.shutdown()
            self.server.stop()
            self.core.store.clear_runtime()
        finally:
            rumps.quit_application()
