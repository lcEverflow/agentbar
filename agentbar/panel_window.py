"""Native macOS task panel window (AppKit) — the primary control surface.

背景：菜单栏 accessory 进程在 macOS 26 上无权把浏览器拉到前台（cooperative
activation），"打开 web 面板"在用户视角就是没反应。激活自己的窗口则是被允许的，
所以任务的添加/排优先级/取消/重试全部改在本窗口完成——进程内直连 Scheduler，
不经浏览器、不经 HTTP。web 面板保留为次要入口（日志查看/未来手机远程）。

全部代码只在主线程运行（由菜单 action / NSTimer 驱动）。
"""

from __future__ import annotations

import logging
import time

import objc
from AppKit import (
    NSAlert,
    NSApp,
    NSBackingStoreBuffered,
    NSButton,
    NSComboBox,
    NSFont,
    NSMakeRect,
    NSOpenPanel,
    NSPopUpButton,
    NSScrollView,
    NSTableColumn,
    NSTableView,
    NSTextField,
    NSTextView,
    NSViewHeightSizable,
    NSViewMaxYMargin,
    NSViewMinYMargin,
    NSViewWidthSizable,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject, NSTimer

from .models import EFFORTS

log = logging.getLogger("agentbar.panel")

W, H = 820, 580
PAD = 16
STATE_LABEL = {
    "queued": "排队中", "running": "运行中", "waiting_quota": "等额度",
    "paused": "已暂停", "succeeded": "成功", "failed": "失败", "cancelled": "已取消",
}
PROFILE_ITEMS = [("🔒 只读", "readonly"), ("✏️ 可编辑（默认）", "edits"),
                 ("⚠️ 完全权限", "full")]
MODEL_SUGGESTIONS = {"claude": ["opus", "sonnet", "haiku"], "codex": [], "fake": []}


def _label(text, x, y, w, h=18, bold=False, dim=False):
    lb = NSTextField.labelWithString_(text)
    lb.setFrame_(NSMakeRect(x, y, w, h))
    if bold:
        lb.setFont_(NSFont.boldSystemFontOfSize_(12))
    else:
        lb.setFont_(NSFont.systemFontOfSize_(11 if dim else 12))
    if dim:
        lb.setTextColor_(lb.textColor().colorWithAlphaComponent_(0.6))
    lb.setAutoresizingMask_(NSViewMinYMargin)
    return lb


def _button(title, x, y, w, target, sel, h=26):
    b = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
    b.setTitle_(title)
    b.setBezelStyle_(1)  # rounded
    b.setTarget_(target)
    b.setAction_(sel)
    return b


def _pin_top(view):
    view.setAutoresizingMask_(NSViewMinYMargin)
    return view


class PanelWindowController(NSObject):
    # ---------- init ----------

    def initWithCore_settings_server_(self, core, settings, server):
        self = objc.super(PanelWindowController, self).init()
        if self is None:
            return None
        self.core = core
        self.settings = settings
        self.server = server
        self.window = None
        self._rows = []
        self._tools = []
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            2.0, self, "onTick:", None, True
        )
        return self

    # ---------- public ----------

    def show_(self, focus_prompt):
        if self.window is None:
            self._build()
        self.refresh()
        try:
            NSApp.activateIgnoringOtherApps_(True)  # 激活自身：accessory 进程被允许
        except Exception:
            pass
        self.window.makeKeyAndOrderFront_(None)
        if focus_prompt:
            self.window.makeFirstResponder_(self.prompt_view)
        log.info("panel window shown (visible=%s)", bool(self.window.isVisible()))

    # ---------- UI construction ----------

    def _build(self):
        mask = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), mask, NSBackingStoreBuffered, False
        )
        self.window.setTitle_("AgentBar 任务面板")
        self.window.setReleasedWhenClosed_(False)
        self.window.setContentMinSize_((680, 460))
        self.window.center()
        v = self.window.contentView()

        y = H - PAD - 18
        v.addSubview_(_label("新任务 Prompt", PAD, y, 300, bold=True))

        # prompt 多行输入
        y -= 78 + 6
        self.prompt_scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(PAD, y, W - 2 * PAD, 78))
        self.prompt_scroll.setBorderType_(1)
        self.prompt_scroll.setHasVerticalScroller_(True)
        self.prompt_scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        self.prompt_view = NSTextView.alloc().initWithFrame_(
            NSMakeRect(0, 0, W - 2 * PAD - 4, 78))
        self.prompt_view.setFont_(NSFont.systemFontOfSize_(13))
        self.prompt_view.setRichText_(False)
        self.prompt_view.setAutoresizingMask_(NSViewWidthSizable)
        self.prompt_scroll.setDocumentView_(self.prompt_view)
        v.addSubview_(self.prompt_scroll)

        # 参数行：工具 / 模型 / 强度 / 权限 / 添加
        y -= 30 + 8
        x = PAD
        self.tool_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(x, y, 130, 26), False)
        self._tools = [a.availability() for a in self.core.registry.values()]
        for t in self._tools:
            self.tool_popup.addItemWithTitle_(
                t["display_name"] + ("" if t["installed"] else "（未安装）"))
        self.tool_popup.setTarget_(self)
        self.tool_popup.setAction_("onToolChanged:")
        v.addSubview_(_pin_top(self.tool_popup))
        x += 138

        self.model_combo = NSComboBox.alloc().initWithFrame_(NSMakeRect(x, y, 170, 26))
        self.model_combo.setPlaceholderString_("模型（留空=默认）")
        v.addSubview_(_pin_top(self.model_combo))
        x += 178

        self.effort_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(x, y, 120, 26), False)
        self.effort_popup.addItemsWithTitles_(["强度：默认"] + [f"强度：{e}" for e in EFFORTS])
        v.addSubview_(_pin_top(self.effort_popup))
        x += 128

        self.profile_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(x, y, 170, 26), False)
        for title, value in PROFILE_ITEMS:
            if value == "full" and not self.settings.allow_full_profile:
                continue
            self.profile_popup.addItemWithTitle_(title)
        self.profile_popup.selectItemAtIndex_(1)
        v.addSubview_(_pin_top(self.profile_popup))

        self.add_btn = _button("＋ 添加任务", W - PAD - 110, y, 110, self, "onAdd:")
        self.add_btn.setKeyEquivalent_("\r")
        self.add_btn.setAutoresizingMask_(NSViewMinYMargin | 1)  # MinXMargin
        v.addSubview_(self.add_btn)

        # 目录行
        y -= 28 + 6
        v.addSubview_(_label("目录", PAD, y + 4, 34))
        self.cwd_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(PAD + 40, y, W - 2 * PAD - 40 - 90, 24))
        self.cwd_field.setStringValue_(self.settings.default_cwd)
        self.cwd_field.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        v.addSubview_(self.cwd_field)
        choose = _button("选择…", W - PAD - 82, y - 1, 82, self, "onChooseDir:")
        choose.setAutoresizingMask_(NSViewMinYMargin | 1)
        v.addSubview_(choose)

        # 任务表
        table_top = y - 10
        bottom_h = 46
        self.table = NSTableView.alloc().initWithFrame_(NSMakeRect(0, 0, W - 2 * PAD, 100))
        for ident, title, width in (
            ("prio", "#", 36), ("state", "状态", 64), ("title", "任务", 300),
            ("tool", "工具", 64), ("model", "模型", 90), ("reason", "说明", 190),
        ):
            col = NSTableColumn.alloc().initWithIdentifier_(ident)
            col.headerCell().setStringValue_(title)
            col.setWidth_(width)
            self.table.addTableColumn_(col)
        self.table.setDataSource_(self)
        self.table.setDelegate_(self)
        self.table.setUsesAlternatingRowBackgroundColors_(True)
        self.table.setAllowsMultipleSelection_(False)
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(PAD, PAD + bottom_h, W - 2 * PAD, table_top - PAD - bottom_h))
        scroll.setDocumentView_(self.table)
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(1)
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        v.addSubview_(scroll)

        # 底部操作条
        bx = PAD
        for title, sel, w in (
            ("⇧ 置顶", "onMoveTop:", 76), ("↑ 上移", "onMoveUp:", 70),
            ("↓ 下移", "onMoveDown:", 70), ("取消", "onCancelTask:", 60),
            ("重试", "onRetryTask:", 60),
        ):
            b = _button(title, bx, PAD, w, self, sel)
            b.setAutoresizingMask_(NSViewMaxYMargin)
            v.addSubview_(b)
            bx += w + 6
        self.pause_btn = _button("Ⅱ 暂停派发", bx + 8, PAD, 104, self, "onTogglePause:")
        self.pause_btn.setAutoresizingMask_(NSViewMaxYMargin)
        v.addSubview_(self.pause_btn)
        web_btn = _button("🌐 浏览器面板", W - PAD - 118, PAD, 118, self, "onOpenWeb:")
        web_btn.setAutoresizingMask_(NSViewMaxYMargin | 1)
        v.addSubview_(web_btn)
        self.quota_label = _label("", PAD, PAD + 30, W - 2 * PAD, dim=True)
        self.quota_label.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
        v.addSubview_(self.quota_label)

        self.onToolChanged_(None)

    # ---------- data refresh ----------

    def onTick_(self, _timer):
        if self.window is not None and self.window.isVisible():
            self.refresh()

    def refresh(self):
        try:
            s = self.core.snapshot()
        except Exception:
            log.exception("snapshot failed")
            return
        active = [t for t in s["tasks"]
                  if t["state"] in ("running", "queued", "waiting_quota", "paused")]
        done = [t for t in s["tasks"]
                if t["state"] in ("succeeded", "failed", "cancelled")][-15:]
        rows = []
        qpos = 0
        for t in active:
            if t["state"] == "queued":
                qpos += 1
                t = {**t, "_prio": str(qpos)}
            rows.append(t)
        rows += reversed(done)
        selected = self._selected_id()
        self._rows = rows
        self.table.reloadData()
        if selected:
            for i, r in enumerate(rows):
                if r["id"] == selected:
                    self.table.selectRowIndexes_byExtendingSelection_(
                        __import__("Foundation").NSIndexSet.indexSetWithIndex_(i), False)
                    break
        self.pause_btn.setTitle_("▶ 恢复派发" if s["paused"] else "Ⅱ 暂停派发")
        quota_bits = []
        for tool, qi in (s.get("quota") or {}).items():
            quota_bits.append(f"{tool.capitalize()}: {qi['detail']}")
        self.quota_label.setStringValue_("   ".join(quota_bits))

    def _selected_id(self):
        i = self.table.selectedRow() if self.window else -1
        if 0 <= i < len(self._rows):
            return self._rows[i]["id"]
        return None

    # ---------- NSTableViewDataSource ----------

    def numberOfRowsInTableView_(self, _tv):
        return len(self._rows)

    def tableView_objectValueForTableColumn_row_(self, _tv, col, row):
        if not (0 <= row < len(self._rows)):
            return ""
        t = self._rows[row]
        ident = str(col.identifier())
        if ident == "prio":
            return t.get("_prio", "")
        if ident == "state":
            return STATE_LABEL.get(t["state"], t["state"])
        if ident == "title":
            return t["title"]
        if ident == "tool":
            return t["tool"]
        if ident == "model":
            m = t.get("model") or ""
            e = t.get("effort") or ""
            return f"{m} {e}".strip()
        if ident == "reason":
            return t.get("state_reason", "")
        return ""

    # ---------- actions ----------

    def _current_tool(self):
        i = self.tool_popup.indexOfSelectedItem()
        return self._tools[i]["name"] if 0 <= i < len(self._tools) else "claude"

    def onToolChanged_(self, _sender):
        self.model_combo.removeAllItems()
        self.model_combo.addItemsWithObjectValues_(
            MODEL_SUGGESTIONS.get(self._current_tool(), []))

    def onAdd_(self, _sender):
        prompt = str(self.prompt_view.string()).strip()
        effort_i = self.effort_popup.indexOfSelectedItem()
        profile_title = str(self.profile_popup.titleOfSelectedItem())
        profile = next(v for t, v in PROFILE_ITEMS if t == profile_title)
        try:
            self.core.add_task(
                prompt=prompt,
                tool=self._current_tool(),
                cwd=str(self.cwd_field.stringValue()),
                profile=profile,
                model=str(self.model_combo.stringValue()).strip() or None,
                effort=EFFORTS[effort_i - 1] if effort_i > 0 else None,
            )
        except ValueError as e:
            self._alert("无法添加任务", str(e))
            return
        self.prompt_view.setString_("")
        self.refresh()

    def onChooseDir_(self, _sender):
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(False)
        panel.setCanChooseDirectories_(True)
        panel.setAllowsMultipleSelection_(False)
        if panel.runModal():
            urls = panel.URLs()
            if urls and urls.count():
                self.cwd_field.setStringValue_(str(urls.objectAtIndex_(0).path()))

    @objc.python_method
    def _act_selected(self, action):
        tid = self._selected_id()
        if not tid:
            return
        ok, msg = self.core.act(tid, action)
        if not ok:
            self._alert("操作失败", msg)
        self.refresh()

    def onMoveTop_(self, _s):
        self._act_selected("move_top")

    def onMoveUp_(self, _s):
        self._act_selected("move_up")

    def onMoveDown_(self, _s):
        self._act_selected("move_down")

    def onCancelTask_(self, _s):
        self._act_selected("cancel")

    def onRetryTask_(self, _s):
        self._act_selected("retry")

    def onTogglePause_(self, _s):
        if self.core.paused:
            self.core.resume_all()
        else:
            self.core.pause_all()
        self.refresh()

    def onOpenWeb_(self, _s):
        from .browser import open_url_async

        open_url_async(self.server.url(with_token=True))

    @objc.python_method
    def _alert(self, title, text):
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(text)
        alert.runModal()
