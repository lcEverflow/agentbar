"""macOS menu-bar frontend — raw AppKit (NSStatusItem + NSMenu), rumps removed.

为什么不用 rumps：在 macOS 26 上出现两类事故——
1. 菜单回调在主线程做阻塞调用（等 `open`、Keychain+HTTP）→ 整个 App 卡死；
2. rumps.Timer 每 2s clear+rebuild 打开中的菜单 → 菜单项点击落空。

本实现的纪律：
- 菜单内容只在 menuWillOpen（AppKit 正统时机）重建；NSTimer 只更新标题文本。
- 所有 action 回调毫秒级返回：浏览器用 open_url_async（fire-and-forget），
  Keychain 授权丢后台线程，结果经 AppHelper.callAfter 回主线程弹提示。
- setAutoenablesItems(False) + 显式 setEnabled，杜绝系统校验导致的置灰。
- 每次重建后把真实 NSMenu 状态导出到 state_dir/menu-debug.json，可实证核查。

菜单逻辑本身在 menu_spec.py（纯函数，可单测）；本文件只做 AppKit 渲染。
"""

from __future__ import annotations

import json
import logging
import threading
import time

import objc
from AppKit import (
    NSAlert,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSMenu,
    NSMenuItem,
    NSPasteboard,
    NSPasteboardTypeString,
    NSStatusBar,
    NSVariableStatusItemLength,
    NSWorkspace,
    NSWorkspaceOpenConfiguration,
)
from Foundation import NSObject, NSTimer, NSURL
from PyObjCTools import AppHelper

from .browser import open_url_async
from .config import Settings
from .menu_spec import build_menu_spec, build_title
from .scheduler import Scheduler
from .server import ApiServer

log = logging.getLogger("agentbar.menubar")

TITLE_REFRESH_SECONDS = 2.0


class _Bridge(NSObject):
    """Objective-C 桥：菜单 delegate + action/timer target（只在主线程被调用）。"""

    def initWithOwner_(self, owner):
        self = objc.super(_Bridge, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def menuWillOpen_(self, menu):
        self.owner._rebuild_menu()

    def onAction_(self, sender):
        self.owner._dispatch(str(sender.representedObject() or ""))

    def onTimer_(self, timer):
        self.owner._update_title()


class AgentBarApp:
    def __init__(self, core: Scheduler, settings: Settings, server: ApiServer):
        self.core = core
        self.settings = settings
        self.server = server
        self._nsapp = None
        self._bridge = None
        self._item = None
        self._menu = None
        self._timer = None
        self._panel = None  # 原生任务面板窗口（懒加载）

    # ---------- lifecycle ----------

    def run(self) -> None:
        self._nsapp = NSApplication.sharedApplication()
        self._nsapp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        self._bridge = _Bridge.alloc().initWithOwner_(self)
        self._item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        self._menu = NSMenu.alloc().init()
        self._menu.setAutoenablesItems_(False)
        self._menu.setDelegate_(self._bridge)
        self._item.setMenu_(self._menu)
        self._update_title()
        self._rebuild_menu()
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            TITLE_REFRESH_SECONDS, self._bridge, "onTimer:", None, True
        )
        log.info("menu bar up (AppKit)")
        AppHelper.runEventLoop()

    def stop_from_thread(self) -> None:
        """信号处理线程调用：清理已在外部完成，只负责停掉主事件循环。"""
        AppHelper.callAfter(self._terminate)

    def dispatch_async(self, action: str) -> None:
        """任意线程安全：把动作转投主线程，与真实菜单点击走同一 _dispatch。"""
        AppHelper.callAfter(self._dispatch, action)

    def _terminate(self) -> None:
        self._nsapp.terminate_(None)

    # ---------- rendering (main thread only) ----------

    def _snapshot(self) -> dict | None:
        try:
            return self.core.snapshot()
        except Exception:
            log.exception("snapshot failed")
            return None

    def _update_title(self) -> None:
        if self._item is None:  # run() 之前（含单测）不触碰 AppKit
            return
        snap = self._snapshot()
        if snap is None:
            return
        self._item.button().setTitle_(build_title(snap))

    def _rebuild_menu(self) -> None:
        if self._menu is None:
            return
        snap = self._snapshot()
        if snap is None:
            return
        self._render(self._menu, build_menu_spec(snap))
        self._write_debug_dump()

    def _render(self, menu, nodes: list[dict]) -> None:
        menu.removeAllItems()
        for node in nodes:
            if node["kind"] == "sep":
                menu.addItem_(NSMenuItem.separatorItem())
                continue
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                node["title"], None, ""
            )
            if node.get("children") is not None:
                sub = NSMenu.alloc().init()
                sub.setAutoenablesItems_(False)
                self._render(sub, node["children"])
                item.setSubmenu_(sub)
            elif node.get("action"):
                item.setTarget_(self._bridge)
                item.setAction_("onAction:")
                item.setRepresentedObject_(node["action"])
            item.setEnabled_(bool(node.get("enabled", True)))
            menu.addItem_(item)

    # ---------- actions（必须毫秒级返回，禁止任何阻塞） ----------

    def _dispatch(self, action: str) -> None:
        log.info("menu action: %s", action)
        try:
            if action == "open_panel":
                self._show_panel(False)
            elif action == "quick_add":
                self._show_panel(True)
            elif action == "open_web_panel":
                self._open(self.server.url(with_token=True))
            elif action == "refresh_quota":
                self.core.quota.refresh_now()  # 异步：只置事件
            elif action == "authorize_keychain":
                self._authorize_keychain_bg()
            elif action == "pause_all":
                self.core.pause_all()
            elif action == "resume_all":
                self.core.resume_all()
            elif action == "quit":
                self._quit()
        except Exception:
            log.exception("menu action %s failed", action)
        self._update_title()

    def _show_panel(self, focus_prompt: bool) -> None:
        """原生任务面板窗口——激活自身窗口是 accessory 进程被允许的，必定前台。"""
        if self._panel is None:
            from .panel_window import PanelWindowController

            self._panel = PanelWindowController.alloc().initWithCore_settings_server_(
                self.core, self.settings, self.server
            )
        self._panel.show_(focus_prompt)

    def _open(self, url: str) -> None:
        """浏览器打开（次要入口）：NSWorkspace 原生异步优先，失败绝不静默。"""
        if self._open_native(url):
            return
        if not open_url_async(url, on_result=self._on_open_result):
            self._open_failed(url, "无法启动 /usr/bin/open")

    def _open_native(self, url: str) -> bool:
        try:
            ns_url = NSURL.URLWithString_(url)
            cfg = NSWorkspaceOpenConfiguration.configuration()

            def done(app, err):
                if err is not None:
                    reason = str(err.localizedDescription())
                    log.warning("NSWorkspace open failed: %s", reason)
                    AppHelper.callAfter(self._open_failed, url, reason)
                else:
                    log.info("NSWorkspace opened panel ok")

            NSWorkspace.sharedWorkspace().openURL_configuration_completionHandler_(
                ns_url, cfg, done
            )
            return True
        except Exception:
            log.exception("NSWorkspace path unavailable, fallback to /usr/bin/open")
            return False

    def _on_open_result(self, url: str, rc: int, err: str) -> None:
        """open_url_async 的后台回调（非主线程）。"""
        if rc != 0:
            AppHelper.callAfter(self._open_failed, url, f"open 退出码 {rc} {err[:120]}")

    def _open_failed(self, url: str, reason: str) -> None:
        copied = False
        try:
            pb = NSPasteboard.generalPasteboard()
            pb.clearContents()
            pb.setString_forType_(url, NSPasteboardTypeString)
            copied = True
        except Exception:
            log.exception("pasteboard write failed")
        hint = "面板地址已复制到剪贴板，直接粘贴到浏览器即可。" if copied else ""
        self._alert("无法自动打开浏览器", f"原因：{reason}\n{hint}\n{url}")

    def _authorize_keychain_bg(self) -> None:
        def work():
            try:
                ok = self.core.quota.authorize_claude_keychain()
            except Exception:
                log.exception("keychain authorize failed")
                ok = False
            msg = (
                "已取得 Keychain 授权，额度数据已刷新。"
                if ok
                else "未能读取 Keychain；请确认已登录 Claude Code 后重试。"
            )
            AppHelper.callAfter(self._alert, "Claude 额度", msg)

        threading.Thread(target=work, name="agentbar-keychain", daemon=True).start()

    def _alert(self, title: str, text: str) -> None:
        self._nsapp.activateIgnoringOtherApps_(True)
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(text)
        alert.runModal()

    def _quit(self) -> None:
        log.info("quit from menu")
        try:
            self.core.shutdown()
            self.server.stop()
            self.core.store.clear_runtime()
        finally:
            self._terminate()

    # ---------- evidence ----------

    def _write_debug_dump(self) -> None:
        try:
            payload = {"ts": time.time(), "title": str(self._item.button().title()),
                       "items": self._dump(self._menu)}
            path = self.settings.state_dir / "menu-debug.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        except Exception:
            log.debug("menu debug dump failed", exc_info=True)

    def _dump(self, menu) -> list[dict]:
        out = []
        for i in range(menu.numberOfItems()):
            it = menu.itemAtIndex_(i)
            if it.isSeparatorItem():
                out.append({"sep": True})
                continue
            row = {
                "title": str(it.title()),
                "enabled": bool(it.isEnabled()),
                "action": str(it.representedObject() or ""),
            }
            if it.submenu() is not None:
                row["children"] = self._dump(it.submenu())
            out.append(row)
        return out
