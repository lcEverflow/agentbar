"""Native MyToken / Tokenverse settings window.

The Web panel still exposes the same settings, but this window is the primary
macOS entry point so users do not need to discover a hidden browser-only page.
Cookie access only happens after an explicit button click.
"""

from __future__ import annotations

import logging
import threading

import objc
from AppKit import (
    NSAlert,
    NSAlertFirstButtonReturn,
    NSApp,
    NSBackingStoreBuffered,
    NSButton,
    NSFont,
    NSMakeRect,
    NSPopUpButton,
    NSSecureTextField,
    NSTextField,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskTitled,
)
from PyObjCTools import AppHelper
from Foundation import NSObject

from .browser_cookies import CookieImportError, import_cookie_header
from .chrome_login import ChromeCDPLogin, ChromeLoginError
from .config import (
    DEFAULT_PROVIDERS,
    PROVIDER_HOSTS,
    PROVIDER_UNITS,
    save_settings,
)

log = logging.getLogger("agentbar.provider_window")

W, H = 720, 500
PAD = 18
_PROVIDERS = ("mytoken", "tokenverse")
_NAMES = {"mytoken": "MyToken", "tokenverse": "Tokenverse"}
_TITLE_OPTIONS = (
    ("Claude", "claude"),
    ("Codex", "codex"),
    ("MyToken", "mytoken"),
    ("Tokenverse", "tokenverse"),
)


def _label(text, x, y, w, h=18, *, bold=False, dim=False):
    label = NSTextField.labelWithString_(text)
    label.setFrame_(NSMakeRect(x, y, w, h))
    label.setFont_(
        NSFont.boldSystemFontOfSize_(12)
        if bold
        else NSFont.systemFontOfSize_(11 if dim else 12)
    )
    if dim:
        label.setTextColor_(label.textColor().colorWithAlphaComponent_(0.58))
    return label


def _button(title, x, y, w, target, selector, h=27):
    button = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
    button.setTitle_(title)
    button.setBezelStyle_(1)
    button.setTarget_(target)
    button.setAction_(selector)
    return button


class ProviderSettingsWindowController(NSObject):

    def initWithCore_settings_(self, core, settings):
        self = objc.super(ProviderSettingsWindowController, self).init()
        if self is None:
            return None
        self.core = core
        self.settings = settings
        self.window = None
        self._enabled = {}
        self._unit = {}
        self._refresh = {}
        self._cookie = {}
        self._status = {}
        self._import_buttons = {}
        self._chrome_buttons = {}
        self._chrome_logins = {}
        return self

    def show_(self, _sender):
        if self.window is None:
            self._build()
        self._reload_controls()
        try:
            NSApp.activateIgnoringOtherApps_(True)
        except Exception:
            pass
        self.window.makeKeyAndOrderFront_(None)

    def _build(self):
        mask = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
        )
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), mask, NSBackingStoreBuffered, False
        )
        self.window.setTitle_("AgentBar 内部额度设置")
        self.window.setReleasedWhenClosed_(False)
        self.window.center()
        view = self.window.contentView()

        view.addSubview_(_label("内部额度设置", PAD, H - 42, 220, 22, bold=True))
        view.addSubview_(_label(
            "参考 AIUsageBar：点“浏览器登录”，在独立 Chrome 完成 SSO；验证通过后自动保存、刷新并出现在菜单。",
            PAD, H - 65, W - 2 * PAD, 18, dim=True,
        ))

        view.addSubview_(_label("菜单栏数字", PAD, H - 103, 90))
        self.title_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(PAD + 92, H - 109, 180, 27), False
        )
        self.title_popup.addItemsWithTitles_([f"标题：{name}" for name, _ in _TITLE_OPTIONS])
        view.addSubview_(self.title_popup)

        self._build_provider_row(view, "mytoken", H - 250)
        self._build_provider_row(view, "tokenverse", H - 395)

        self._message = _label("", PAD, 23, W - 200, 20, dim=True)
        view.addSubview_(self._message)
        save = _button("保存并刷新", W - PAD - 142, 18, 142, self, "onSave:", 30)
        save.setKeyEquivalent_("\r")
        view.addSubview_(save)

    def _build_provider_row(self, view, provider: str, y: int):
        name = _NAMES[provider]
        enabled = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, y + 92, 150, 24))
        enabled.setButtonType_(3)  # NSSwitchButton
        enabled.setTitle_(name)
        enabled.setFont_(NSFont.boldSystemFontOfSize_(13))
        view.addSubview_(enabled)
        self._enabled[provider] = enabled

        status = _label("", PAD + 158, y + 94, W - PAD * 2 - 158, 20, dim=True)
        view.addSubview_(status)
        self._status[provider] = status

        view.addSubview_(_label("展示单位", PAD, y + 57, 70))
        unit = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(PAD + 72, y + 52, 110, 27), False
        )
        unit.addItemsWithTitles_(list(PROVIDER_UNITS))
        view.addSubview_(unit)
        self._unit[provider] = unit

        view.addSubview_(_label("刷新秒数", PAD + 198, y + 57, 70))
        refresh = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 270, y + 53, 76, 24))
        refresh.setPlaceholderString_("300")
        view.addSubview_(refresh)
        self._refresh[provider] = refresh

        login = _button("🌐 浏览器登录", PAD + 365, y + 51, 122, self, "onChromeLogin:")
        login.setRepresentedObject_(provider)
        view.addSubview_(login)
        self._chrome_buttons[provider] = login

        import_btn = _button("读取已有登录", PAD + 495, y + 51, 122, self, "onImport:")
        import_btn.setRepresentedObject_(provider)
        view.addSubview_(import_btn)
        self._import_buttons[provider] = import_btn

        view.addSubview_(_label("Cookie", PAD, y + 20, 55))
        cookie = NSSecureTextField.alloc().initWithFrame_(
            NSMakeRect(PAD + 55, y + 16, W - 2 * PAD - 55 - 90, 24)
        )
        cookie.setPlaceholderString_("可选：手动粘贴 Request Headers 的 Cookie；留空不修改")
        view.addSubview_(cookie)
        self._cookie[provider] = cookie

        clear = _button("清空", W - PAD - 82, y + 15, 82, self, "onClear:", 26)
        clear.setRepresentedObject_(provider)
        view.addSubview_(clear)

    @objc.python_method
    def _reload_controls(self):
        title_values = [value for _, value in _TITLE_OPTIONS]
        try:
            self.title_popup.selectItemAtIndex_(title_values.index(self.settings.title_provider))
        except ValueError:
            self.title_popup.selectItemAtIndex_(1)

        for provider in _PROVIDERS:
            defaults = DEFAULT_PROVIDERS[provider]
            cfg = (self.settings.providers or {}).get(provider) or defaults
            self._enabled[provider].setState_(1 if cfg.get("enabled") else 0)
            unit = cfg.get("unit") if cfg.get("unit") in PROVIDER_UNITS else defaults["unit"]
            self._unit[provider].selectItemWithTitle_(unit)
            self._refresh[provider].setStringValue_(
                str(int(cfg.get("refresh_seconds") or defaults["refresh_seconds"]))
            )
            self._cookie[provider].setStringValue_("")
            cookie = str(cfg.get("cookie") or "")
            if cookie:
                names = [part.strip().split("=", 1)[0] for part in cookie.split(";") if "=" in part]
                preview = ", ".join(names[:4]) + (" …" if len(names) > 4 else "")
                state = f"Cookie 已配置：{preview}"
            elif cfg.get("enabled"):
                state = "已启用，但缺少 Cookie"
            else:
                state = "未配置 Cookie"
            self._status[provider].setStringValue_(state)

    def onChromeLogin_(self, sender):
        provider = str(sender.representedObject() or "")
        active = self._chrome_logins.get(provider)
        if active is not None:
            active.cancel()
            self._status[provider].setStringValue_("正在取消浏览器登录…")
            return
        try:
            login = ChromeCDPLogin(provider)
        except ChromeLoginError as exc:
            self._alert("无法启动浏览器登录", str(exc))
            return
        self._chrome_logins[provider] = login
        sender.setTitle_("取消登录")
        self._status[provider].setStringValue_("正在启动独立 Chrome 登录窗口…")

        def update_status(message):
            AppHelper.callAfter(self._set_provider_status, provider, message)

        def work():
            try:
                imported = login.run(on_status=update_status)
                error = ""
            except ChromeLoginError as exc:
                imported = None
                error = str(exc)
            except Exception as exc:
                log.exception("Chrome CDP login failed")
                imported = None
                error = str(exc)
            AppHelper.callAfter(self._finish_chrome_login, provider, imported, error)

        threading.Thread(
            target=work,
            name=f"agentbar-chrome-login-{provider}",
            daemon=True,
        ).start()

    @objc.python_method
    def _set_provider_status(self, provider, message):
        self._status[provider].setStringValue_(message)

    @objc.python_method
    def _finish_chrome_login(self, provider, imported, error):
        self._chrome_logins.pop(provider, None)
        self._chrome_buttons[provider].setTitle_("🌐 浏览器登录")
        if error or imported is None:
            message = error or "未捕获到 Cookie"
            self._status[provider].setStringValue_(f"浏览器登录失败：{message}")
            if "已取消" not in message:
                self._alert("浏览器登录失败", message)
            return
        self._save_imported_cookie(provider, imported)

    def onImport_(self, sender):
        provider = str(sender.representedObject() or "")
        host = PROVIDER_HOSTS.get(provider)
        if not host:
            return
        sender.setEnabled_(False)
        self._status[provider].setStringValue_("正在读取 Chrome / Edge / Brave 已有登录态…")

        def work():
            try:
                imported = import_cookie_header(host)
                error = ""
            except CookieImportError as exc:
                imported = None
                error = str(exc)
            except Exception as exc:  # Keychain/SQLite 失败不能带崩 AppKit 主线程
                log.exception("provider cookie import failed")
                imported = None
                error = str(exc)
            AppHelper.callAfter(self._finish_import, provider, imported, error)

        threading.Thread(
            target=work,
            name=f"agentbar-cookie-{provider}",
            daemon=True,
        ).start()

    @objc.python_method
    def _finish_import(self, provider, imported, error):
        self._import_buttons[provider].setEnabled_(True)
        if error or imported is None:
            message = error or "未导入到 Cookie"
            self._status[provider].setStringValue_(f"导入失败：{message}")
            self._alert("Cookie 导入失败", message)
            return
        self._save_imported_cookie(provider, imported)

    @objc.python_method
    def _save_imported_cookie(self, provider, imported):
        cfg = self.settings.providers.setdefault(provider, dict(DEFAULT_PROVIDERS[provider]))
        cfg["enabled"] = True
        cfg["cookie"] = imported.header
        save_settings(self.settings)
        self.core.quota.reload_fetchers()
        self._reload_controls()
        self._message.setStringValue_(
            f"{_NAMES[provider]}：已从 {imported.source} 导入 {imported.count} 个 Cookie，并触发刷新"
        )

    def onSave_(self, _sender):
        for provider in _PROVIDERS:
            defaults = DEFAULT_PROVIDERS[provider]
            cfg = self.settings.providers.setdefault(provider, dict(defaults))
            cfg["enabled"] = bool(self._enabled[provider].state())
            unit = str(self._unit[provider].titleOfSelectedItem() or defaults["unit"])
            cfg["unit"] = unit if unit in PROVIDER_UNITS else defaults["unit"]
            try:
                cfg["refresh_seconds"] = max(
                    60, int(str(self._refresh[provider].stringValue()).strip() or 300)
                )
            except ValueError:
                cfg["refresh_seconds"] = defaults["refresh_seconds"]
            cookie = str(self._cookie[provider].stringValue()).strip()
            if cookie:
                cfg["cookie"] = cookie

        title_index = self.title_popup.indexOfSelectedItem()
        if 0 <= title_index < len(_TITLE_OPTIONS):
            self.settings.title_provider = _TITLE_OPTIONS[title_index][1]
        save_settings(self.settings)
        self.core.quota.reload_fetchers()
        self._reload_controls()
        self._message.setStringValue_("配置已保存；额度刷新已触发，稍后可在菜单中查看。")

    def onClear_(self, sender):
        provider = str(sender.representedObject() or "")
        if provider not in DEFAULT_PROVIDERS:
            return
        alert = NSAlert.alloc().init()
        alert.setMessageText_(f"清空 {_NAMES[provider]} 登录态？")
        alert.setInformativeText_("将删除 AgentBar 保存的 Cookie 并停用该额度来源。")
        alert.addButtonWithTitle_("清空")
        alert.addButtonWithTitle_("取消")
        if alert.runModal() != NSAlertFirstButtonReturn:
            return
        cfg = self.settings.providers.setdefault(provider, dict(DEFAULT_PROVIDERS[provider]))
        cfg["cookie"] = ""
        cfg["enabled"] = False
        save_settings(self.settings)
        self.core.quota.reload_fetchers()
        self._reload_controls()
        self._message.setStringValue_(f"{_NAMES[provider]} Cookie 已清空并停用。")

    @objc.python_method
    def _alert(self, title, text):
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(text)
        alert.runModal()
