"""macOS menu-bar frontend — raw AppKit (NSStatusItem + NSMenu), rumps removed.

为什么不用 rumps：在 macOS 26 上出现两类事故——
1. 菜单回调在主线程做阻塞调用（等 `open`、登录或 HTTP）→ 整个 App 卡死；
2. rumps.Timer 每 2s clear+rebuild 打开中的菜单 → 菜单项点击落空。

本实现的纪律：
- 菜单内容只在 menuWillOpen（AppKit 正统时机）重建；NSTimer 只更新标题文本。
- 所有 action 回调毫秒级返回：浏览器用 open_url_async（fire-and-forget），
  登录、Cookie 导入等慢操作都在后台线程执行。
- setAutoenablesItems(False) + 显式 setEnabled，杜绝系统校验导致的置灰。
- 每次重建后把真实 NSMenu 状态导出到 state_dir/menu-debug.json，可实证核查。

菜单逻辑本身在 menu_spec.py（纯函数，可单测）；本文件只做 AppKit 渲染。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from html import escape
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import objc
from AppKit import (
    NSAlert,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBezierPath,
    NSColor,
    NSEventModifierFlagCommand,
    NSEventModifierFlagControl,
    NSImage,
    NSImageLeading,
    NSLineCapStyleRound,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSStatusBar,
    NSVariableStatusItemLength,
)
from Foundation import NSObject, NSTimer
from PyObjCTools import AppHelper

from .config import Settings
from .menu_spec import build_menu_spec, build_ring_progress, build_title
from .scheduler import Scheduler
from .server import ApiServer

log = logging.getLogger("agentbar.menubar")

TITLE_REFRESH_SECONDS = 2.0

# 状态栏双环图标 —— 几何参数与 aiusagebar 的 menuBarImage 对齐（18pt 模板图）。
RING_SIZE = 18.0
_RINGS = (
    # (radius, line_width, track_alpha, fill_alpha)：外圈 Claude、内圈 Codex
    (7.2, 1.75, 0.22, 1.0),
    (4.35, 1.65, 0.18, 0.78),
)


def _atomic_private_write(path, text: str) -> None:
    """Atomically replace a sensitive debug artifact with mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1  # ownership transferred to ``stream``
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _ring_icon(outer: float | None, inner: float | None, status: str = "idle") -> NSImage:
    """画双环额度图标：模板图（黑+透明度），菜单栏深浅色自动适配。

    每环先画整圈淡色轨道，再从 12 点方向顺时针画已用比例的实线弧；
    progress=None 表示无可信数据，只留轨道（诚实：不编造用量）。
    调度状态并入环心徽标（实心点=运行中、双竖条=已暂停），标题不再放
    ◇/◆ 等字符——菜单栏只保留这一个图标。
    """
    img = NSImage.alloc().initWithSize_((RING_SIZE, RING_SIZE))
    img.lockFocus()
    center = (RING_SIZE / 2.0, RING_SIZE / 2.0)
    for (radius, width, track_alpha, fill_alpha), progress in zip(_RINGS, (outer, inner)):
        NSColor.blackColor().colorWithAlphaComponent_(track_alpha).setStroke()
        track = NSBezierPath.bezierPath()
        track.setLineWidth_(width)
        track.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
            center, radius, 0.0, 360.0
        )
        track.stroke()
        if progress is None or progress <= 0:
            continue
        NSColor.blackColor().colorWithAlphaComponent_(fill_alpha).setStroke()
        arc = NSBezierPath.bezierPath()
        arc.setLineWidth_(width)
        arc.setLineCapStyle_(NSLineCapStyleRound)
        arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
            center, radius, 90.0, 90.0 - 360.0 * min(1.0, progress), True
        )
        arc.stroke()
    cx = cy = RING_SIZE / 2.0
    NSColor.blackColor().setFill()
    if status == "running":
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(cx - 2.0, cy - 2.0, 4.0, 4.0)
        ).fill()
    elif status == "paused":
        for x in (cx - 2.1, cx + 0.9):
            NSBezierPath.bezierPathWithRect_(NSMakeRect(x, cy - 2.2, 1.2, 4.4)).fill()
    img.unlockFocus()
    img.setTemplate_(True)
    return img


def _qr_page_html(url: str, mode: str = "局域网", note: str = "") -> str:
    """Generate the QR window HTML (SVG QR, no PIL dependency)."""
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage)
    svg = img.to_string().decode()
    safe_url = escape(url)
    safe_mode = escape(mode)
    safe_note = escape(note or "链接含访问令牌，勿外传")
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{{font-family:-apple-system,sans-serif;margin:0;padding:20px;text-align:center;
     background:#fff;color:#1c1c1e}}
svg{{width:280px;height:280px;display:block;margin:8px auto}}
h3{{margin:4px 0 2px;font-size:15px}}
p{{font-size:12px;color:#8e8e93;margin:4px 0}}
.url{{font-size:11px;font-family:Menlo,monospace;word-break:break-all;
     background:#f2f2f7;border-radius:8px;padding:8px;margin-top:10px;
     user-select:all;-webkit-user-select:all}}
</style></head><body>
<h3>手机扫码 · 查看/提交任务（{safe_mode}）</h3>
<p>{safe_note}</p>
{svg}
<div class="url">{safe_url}</div>
</body></html>"""


def _token_fragment_url(url: str, token: str) -> str:
    """Put the bootstrap token in a fragment, outside HTTP requests and logs."""
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "token"]
    fragment = [
        (key, value) for key, value in parse_qsl(parts.fragment) if key != "token"
    ]
    fragment.append(("token", token))
    return urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(query),
        urlencode(fragment),
    ))


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
        self._provider_panel = None  # MyToken / Tokenverse 原生设置窗口（懒加载）
        self._ring_key = None    # 双环图标缓存键（进度没变不重画，避免 2s 一次的无谓刷新）
        self._qr_window = None   # 手机访问二维码窗口（懒加载）
        self._qr_webview = None  # 二维码窗口里的 WKWebView（内容可切换 LAN/公网）
        self._debug_lock = threading.Lock()
        self._debug_pending: tuple[object, str] | None = None
        self._debug_worker_running = False
        self._closing = threading.Event()
        from .tunnel import TunnelManager
        self.tunnel = TunnelManager(
            server.port, on_up=server.allow_host, on_down=server.disallow_host
        )
        server.hooks["tunnel_status"] = self.tunnel.status  # /api/state 附带隧道状态

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
        self._item.button().setImagePosition_(NSImageLeading)
        self._update_title()
        self._rebuild_menu()
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            TITLE_REFRESH_SECONDS, self._bridge, "onTimer:", None, True
        )
        # Install main menu so text fields in our windows get Cmd+C/V/X/Z/A,
        # and Cmd+W closes the front window.
        self._install_main_menu()
        log.info("menu bar up (AppKit)")
        AppHelper.runEventLoop()

    def stop_from_thread(self) -> None:
        """信号处理线程调用：清理已在外部完成，只负责停掉主事件循环。"""
        AppHelper.callAfter(self._terminate)

    def begin_shutdown(self) -> None:
        """Close menu/debug action admission before retiring external resources."""
        self._closing.set()

    def close_active_provider_logins(self) -> None:
        """Safe from shutdown threads; no-op until the provider window is opened."""
        panel = self._provider_panel
        if panel is not None:
            panel.close_active_logins()

    def dispatch_async(self, action: str) -> None:
        """任意线程安全：把动作转投主线程，与真实菜单点击走同一 _dispatch。"""
        if self._closing.is_set() or getattr(self.server, "stopping", False):
            return
        AppHelper.callAfter(self._dispatch, action)

    def _terminate(self) -> None:
        self._nsapp.terminate_(None)

    def _install_main_menu(self) -> None:
        """Install a minimal main menu so Cmd+C/V/X/Z work in our NSTextView/NSTextField,
        Cmd/Ctrl+W closes the front window, and Cmd/Ctrl+Q quits with full cleanup.

        快捷键只在本 App 处于激活态（面板/对话窗口在前台）时生效——accessory
        进程收不到其他 App 前台时的按键，这是 macOS 的路由规则。
        """
        main = NSMenu.alloc().init()

        # App menu: Quit 走 bridge 的 "quit" 动作（停隧道/调度器/服务器再退出），
        # 不能用 NSApp.terminate:（跳过清理会留下 cloudflared 孤儿进程）。
        app_menu = NSMenu.alloc().initWithTitle_("AgentBar")
        for mask in (NSEventModifierFlagCommand, NSEventModifierFlagControl):
            quit_it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Quit AgentBar", "onAction:", "q")
            quit_it.setKeyEquivalentModifierMask_(mask)
            quit_it.setTarget_(self._bridge)
            quit_it.setRepresentedObject_("quit")
            app_menu.addItem_(quit_it)
        app_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("AgentBar", None, "")
        app_item.setSubmenu_(app_menu)
        main.addItem_(app_item)

        edit_menu = NSMenu.alloc().initWithTitle_("Edit")
        for title, sel, key in [
            ("Undo", "undo:", "z"),
            ("Redo", "redo:", "Z"),
            (None, None, None),
            ("Cut", "cut:", "x"),
            ("Copy", "copy:", "c"),
            ("Paste", "paste:", "v"),
            ("Select All", "selectAll:", "a"),
        ]:
            if title is None:
                edit_menu.addItem_(NSMenuItem.separatorItem())
            else:
                it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, key)
                edit_menu.addItem_(it)
        edit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Edit", None, "")
        edit_item.setSubmenu_(edit_menu)
        main.addItem_(edit_item)

        win_menu = NSMenu.alloc().initWithTitle_("Window")
        for mask in (NSEventModifierFlagCommand, NSEventModifierFlagControl):
            close_it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Close Window", "performClose:", "w")
            close_it.setKeyEquivalentModifierMask_(mask)
            win_menu.addItem_(close_it)
        win_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Window", None, "")
        win_item.setSubmenu_(win_menu)
        main.addItem_(win_item)

        self._nsapp.setMainMenu_(main)

    # ---------- rendering (main thread only) ----------

    def _snapshot(self) -> dict | None:
        try:
            snap = self.core.snapshot()
            snap["tunnel"] = self.tunnel.status()
            return snap
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
        outer, inner = build_ring_progress(snap)
        status = snap.get("status", "idle")
        key = (
            None if outer is None else round(outer, 2),
            None if inner is None else round(inner, 2),
            status,
        )
        if key != self._ring_key:
            self._ring_key = key
            self._item.button().setImage_(_ring_icon(outer, inner, status))

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
        # An action may already be queued on AppKit when SIGTERM begins. Drop it
        # at execution time too, so a late tunnel_start cannot recreate a child
        # after shutdown has stopped and cleared the current tunnel.
        if action != "quit" and (
            self._closing.is_set() or getattr(self.server, "stopping", False)
        ):
            return
        log.info("menu action: %s", action)
        try:
            if action == "open_panel":
                self._show_panel(False)
            elif action == "quick_add":
                self._show_panel(True)
            elif action == "provider_settings":
                self._show_provider_settings()
            elif action.startswith("refresh_quota:"):
                tool = action.split(":", 1)[1].strip()
                if tool:
                    self.core.quota.refresh_now(tool)
            elif action == "pause_all":
                self.core.pause_all()
            elif action == "resume_all":
                self.core.resume_all()
            elif action == "mobile_qr":
                self._show_mobile_qr()
            elif action == "tunnel_start":
                self._start_tunnel_bg()
            elif action == "tunnel_qr":
                self._show_tunnel_qr()
            elif action == "tunnel_stop":
                threading.Thread(target=self.tunnel.stop, daemon=True).start()
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

    def _show_provider_settings(self) -> None:
        """原生内部额度设置：菜单里始终可发现，不再藏在 Web 面板。"""
        if self._provider_panel is None:
            from .provider_window import ProviderSettingsWindowController

            self._provider_panel = (
                ProviderSettingsWindowController.alloc().initWithCore_settings_(
                    self.core, self.settings
                )
            )
        self._provider_panel.show_(None)

    def _show_mobile_qr(self) -> None:
        """局域网扫码；访问令牌只放 URL fragment。"""
        url = self.server.mobile_url()
        if not url:
            self._alert(
                "手机访问",
                "未获取到局域网 IP（Mac 未联网？），或 config.json 中 lan_access 已关闭。",
            )
            return
        self._show_qr_window(
            _token_fragment_url(url, self.settings.token),
            "局域网",
            "手机需与 Mac 连同一 Wi-Fi",
        )

    def _start_tunnel_bg(self) -> None:
        """开通公网隧道（cloudflared，阻塞 ~5-15s → 后台线程），成功后自动弹二维码。"""
        def work():
            ok = self.tunnel.start()
            if ok:
                AppHelper.callAfter(self._show_tunnel_qr)
            else:
                err = self.tunnel.status().get("error") or "未知错误"
                AppHelper.callAfter(self._alert, "公网访问", f"隧道启动失败：{err}")

        threading.Thread(target=work, name="agentbar-tunnel", daemon=True).start()

    def _show_tunnel_qr(self) -> None:
        url = self.tunnel.url
        if not url:
            self._alert("公网访问", "隧道未开通（先点「开通公网访问」）。")
            return
        self._show_qr_window(
            _token_fragment_url(f"{url.rstrip('/')}/m", self.settings.token),
            "公网（Cloudflare Tunnel）",
            "任何网络可达 · 链接含令牌切勿外传 · 每次开通域名会变",
        )

    def _show_qr_window(self, url: str, mode: str, note: str) -> None:
        try:
            page = _qr_page_html(url, mode, note)
            from AppKit import (
                NSBackingStoreBuffered,
                NSMakeRect,
                NSWindow,
                NSWindowStyleMaskClosable,
                NSWindowStyleMaskTitled,
            )
            from WebKit import WKWebView

            if self._qr_window is None:
                win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                    NSMakeRect(0, 0, 360, 470),
                    NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
                    NSBackingStoreBuffered, False,
                )
                win.setReleasedWhenClosed_(False)
                win.center()
                wv = WKWebView.alloc().initWithFrame_(NSMakeRect(0, 0, 360, 470))
                win.contentView().addSubview_(wv)
                self._qr_window, self._qr_webview = win, wv
            self._qr_window.setTitle_(f"手机访问 AgentBar · {mode}")
            self._qr_webview.loadHTMLString_baseURL_(page, None)
            self._qr_window.makeKeyAndOrderFront_(None)
            self._nsapp.activateIgnoringOtherApps_(True)
        except Exception:
            log.exception("qr window failed")
            self._alert("手机访问", f"二维码窗口创建失败；手机浏览器直接打开：\n{url}")

    def _alert(self, title: str, text: str) -> None:
        self._nsapp.activateIgnoringOtherApps_(True)
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(text)
        alert.runModal()

    def _quit(self) -> None:
        log.info("quit from menu")
        self.begin_shutdown()
        first_error = None
        try:
            for label, action in (
                # Stop admission first. Scheduler shutdown may wait for active
                # CLI processes; HTTP cannot remain open during that window.
                ("HTTP server", self.server.stop),
                ("public tunnel", self.tunnel.stop),
                ("provider login", self.close_active_provider_logins),
                ("scheduler", self.core.shutdown),
                ("runtime file", self.core.store.clear_runtime),
            ):
                try:
                    action()
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
                    log.exception("quit cleanup failed: %s", label)
        finally:
            self._terminate()
        if first_error is not None:
            raise first_error

    # ---------- evidence ----------

    def _write_debug_dump(self) -> None:
        try:
            payload = {"ts": time.time(), "title": str(self._item.button().title()),
                       "items": self._dump(self._menu)}
            path = self.settings.state_dir / "menu-debug.json"
            text = json.dumps(payload, ensure_ascii=False, indent=1)
        except Exception:
            log.debug("menu debug dump failed", exc_info=True)
            return
        with self._debug_lock:
            # Menu open is a UI path. Coalesce rapid rebuilds and keep fsync off
            # the AppKit thread; the newest snapshot always drains last.
            self._debug_pending = (path, text)
            if self._debug_worker_running:
                return
            self._debug_worker_running = True
        threading.Thread(
            target=self._drain_debug_dump,
            name="agentbar-menu-debug",
            daemon=True,
        ).start()

    def _drain_debug_dump(self) -> None:
        while True:
            with self._debug_lock:
                job = self._debug_pending
                self._debug_pending = None
                if job is None:
                    self._debug_worker_running = False
                    return
            path, text = job
            try:
                _atomic_private_write(path, text)
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
