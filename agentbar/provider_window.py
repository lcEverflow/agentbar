"""Native quota-source settings window.

Claude/Codex credentials are discovered from their installed CLIs and never
cross the UI boundary. MyToken/Tokenverse keep their explicit, write-only corp
browser-cookie workflow.
"""

from __future__ import annotations

import copy
import logging
import threading

import objc
from AppKit import (
    NSAlert,
    NSAlertFirstButtonReturn,
    NSApp,
    NSBackingStoreBuffered,
    NSButton,
    NSComboBox,
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
    DEFAULT_QUOTA_SOURCES,
    PROVIDER_HOSTS,
    PROVIDER_UNITS,
    save_settings,
)
from .usage import credential_status

log = logging.getLogger("agentbar.provider_window")

W, H = 760, 760
PAD = 18
_QUOTA_SOURCES = ("claude", "codex")
_PROVIDERS = ("mytoken", "tokenverse")
_NAMES = {
    "claude": "Claude",
    "codex": "Codex",
    "mytoken": "MyToken",
    "tokenverse": "Tokenverse",
}
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
        self._source_model = {}
        self._source_refresh_buttons = {}
        self._credential_status = {}
        self._credential_generations = {source: 0 for source in _QUOTA_SOURCES}
        self._credential_lock = threading.Lock()
        self._import_buttons = {}
        self._chrome_buttons = {}
        self._chrome_logins = {}
        self._chrome_login_threads = {}
        self._chrome_login_lock = threading.Lock()
        self._closing_logins = False
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
        self.window.setTitle_("AgentBar 额度来源设置")
        self.window.setReleasedWhenClosed_(False)
        self.window.center()
        view = self.window.contentView()

        view.addSubview_(_label("额度来源设置", PAD, H - 42, 220, 22, bold=True))
        view.addSubview_(_label(
            "默认关闭周期请求；保存时只刷新有变更的来源，也可单独点“刷新此来源”。",
            PAD, H - 65, W - 2 * PAD, 18, dim=True,
        ))

        view.addSubview_(_label("菜单栏数字", PAD, H - 103, 90))
        self.title_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(PAD + 92, H - 109, 180, 27), False
        )
        self.title_popup.addItemsWithTitles_([f"标题：{name}" for name, _ in _TITLE_OPTIONS])
        view.addSubview_(self.title_popup)

        self.auto_refresh_check = NSButton.alloc().initWithFrame_(
            NSMakeRect(PAD + 290, H - 106, 255, 24)
        )
        self.auto_refresh_check.setButtonType_(3)  # NSSwitchButton
        self.auto_refresh_check.setTitle_("周期自动刷新（默认关闭）")
        view.addSubview_(self.auto_refresh_check)

        view.addSubview_(_label("订阅额度", PAD, H - 142, 220, 20, bold=True))
        view.addSubview_(_label(
            "自动使用 Claude Code / Codex CLI 登录态；AgentBar 不接收或保存订阅凭据。",
            PAD + 84, H - 142, W - 2 * PAD - 84, 18, dim=True,
        ))
        self._build_quota_source_row(view, "claude", H - 235)
        self._build_quota_source_row(view, "codex", H - 345)

        view.addSubview_(_label("内部额度", PAD, H - 376, 220, 20, bold=True))
        view.addSubview_(_label(
            "MyToken / Tokenverse 使用企业 SSO Cookie；浏览器登录和导入都只在显式点击后执行。",
            PAD + 84, H - 376, W - 2 * PAD - 84, 18, dim=True,
        ))
        self._build_provider_row(view, "mytoken", H - 525)
        self._build_provider_row(view, "tokenverse", H - 670)

        self._message = _label("", PAD, 23, W - 200, 20, dim=True)
        view.addSubview_(self._message)
        save = _button("保存配置", W - PAD - 176, 18, 176, self, "onSave:", 30)
        save.setKeyEquivalent_("\r")
        view.addSubview_(save)

    def _build_quota_source_row(self, view, source: str, y: int):
        name = _NAMES[source]
        enabled = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, y + 72, 120, 24))
        enabled.setButtonType_(3)  # NSSwitchButton
        enabled.setTitle_(name)
        enabled.setFont_(NSFont.boldSystemFontOfSize_(13))
        view.addSubview_(enabled)
        self._enabled[source] = enabled

        status = _label("", PAD + 126, y + 74, W - PAD * 2 - 126, 20, dim=True)
        view.addSubview_(status)
        self._status[source] = status

        if source == "codex":
            view.addSubview_(_label("limitId", PAD, y + 41, 105))
            model = NSComboBox.alloc().initWithFrame_(
                NSMakeRect(PAD + 108, y + 37, W - 2 * PAD - 108, 24)
            )
            model.setCompletes_(True)
            model.setNumberOfVisibleItems_(8)
            model.setPlaceholderString_(
                "留空=默认账户窗口；或填 App Server 返回的 limitId"
            )
            view.addSubview_(model)
            self._source_model[source] = model
        else:
            view.addSubview_(_label(
                "Anthropic 未提供第三方订阅额度接口；展示任务观测与 ccusage 数据。",
                PAD, y + 41, W - 2 * PAD, 18, dim=True,
            ))

        hint = (
            "仅检测 Claude Code 登录状态，不读取 Keychain 或 OAuth 凭据"
            if source == "claude"
            else "额度由 Codex CLI app-server 自动提供，凭据不会暴露给 AgentBar"
        )
        view.addSubview_(_label(hint, PAD, y + 10, W - 2 * PAD - 190, 18, dim=True))
        refresh = _button(
            "重新检测 / 刷新", W - PAD - 176, y + 4, 176,
            self, "onRefreshSource:", 26,
        )
        refresh.setRepresentedObject_(source)
        view.addSubview_(refresh)
        self._source_refresh_buttons[source] = refresh

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
        # Copy one coherent settings revision, then release the lock before
        # touching AppKit controls. Writers replace/mutate these dictionaries
        # under the same lock.
        with self.settings._lock:
            title_provider = self.settings.title_provider
            usage_auto_refresh = self.settings.usage_auto_refresh
            quota_sources = {
                source: dict((self.settings.quota_sources or {}).get(source) or {})
                for source in _QUOTA_SOURCES
            }
            providers = {
                provider: dict((self.settings.providers or {}).get(provider) or {})
                for provider in _PROVIDERS
            }
        title_values = [value for _, value in _TITLE_OPTIONS]
        try:
            self.title_popup.selectItemAtIndex_(title_values.index(title_provider))
        except ValueError:
            self.title_popup.selectItemAtIndex_(1)

        self.auto_refresh_check.setState_(1 if usage_auto_refresh else 0)
        try:
            available_limit_ids = (
                ((self.core.snapshot().get("quota") or {}).get("codex") or {}).get(
                    "available_models"
                ) or []
            )
        except Exception:
            available_limit_ids = []
        codex_model = self._source_model.get("codex")
        if codex_model is not None:
            codex_model.removeAllItems()
            codex_model.addItemsWithObjectValues_([
                str(value)[:160]
                for value in available_limit_ids
                if isinstance(value, str) and value.strip()
            ])
        for source in _QUOTA_SOURCES:
            defaults = DEFAULT_QUOTA_SOURCES[source]
            cfg = quota_sources[source] or defaults
            self._enabled[source].setState_(1 if cfg.get("enabled") else 0)
            model = self._source_model.get(source)
            if model is not None:
                model.setStringValue_(str(cfg.get("model") or ""))
            self._status[source].setStringValue_("正在静默检测 CLI 登录态…")
            self._source_refresh_buttons[source].setTitle_("检测中…")
            self._source_refresh_buttons[source].setEnabled_(False)
            self._start_credential_check(source)

        for provider in _PROVIDERS:
            defaults = DEFAULT_PROVIDERS[provider]
            cfg = providers[provider] or defaults
            self._enabled[provider].setState_(1 if cfg.get("enabled") else 0)
            unit = cfg.get("unit") if cfg.get("unit") in PROVIDER_UNITS else defaults["unit"]
            self._unit[provider].selectItemWithTitle_(unit)
            self._refresh[provider].setStringValue_(
                str(int(cfg.get("refresh_seconds") or defaults["refresh_seconds"]))
            )
            self._cookie[provider].setStringValue_("")
            cookie = str(cfg.get("cookie") or "")
            if cookie:
                # Cookie is write-only too. Even the text before the first '='
                # can be a pasted JWT/Bearer value rather than a safe cookie name,
                # so the native status never echoes any portion of the secret.
                state = "Cookie 已配置"
            elif cfg.get("enabled"):
                state = "已启用，但缺少 Cookie"
            else:
                state = "未配置 Cookie"
            self._status[provider].setStringValue_(state)

    @objc.python_method
    def _credential_detector(self):
        """Use an injected monitor detector in tests, otherwise the safe resolver."""
        return getattr(self.core.quota, "credential_status", credential_status)

    @objc.python_method
    def _start_credential_check(
        self, source: str, *, refresh: bool = False,
    ) -> None:
        if source not in _QUOTA_SOURCES:
            return
        with self._credential_lock:
            self._credential_generations[source] += 1
            generation = self._credential_generations[source]
        detector = self._credential_detector()

        def work():
            try:
                result = detector(
                    source,
                    settings=self.settings,
                    allow_interactive=False,
                    refresh=bool(refresh),
                )
            except Exception:
                log.exception("%s credential detection failed", source)
                result = {
                    "available": False,
                    "source": "none",
                    "status": "unavailable",
                    "detail": "登录态检测失败，请确认 CLI 已安装并重新登录。",
                    "needs_authorization": False,
                }
            refreshed = False
            if refresh and bool(result.get("available")):
                self.core.quota.reload_fetchers(refresh=False)
                with self.settings._lock:
                    enabled = bool(
                        ((self.settings.quota_sources or {}).get(source) or {}).get(
                            "enabled"
                        )
                    )
                if enabled:
                    self.core.quota.refresh_now(source)
                    refreshed = True
            AppHelper.callAfter(
                self._finish_credential_check,
                source,
                generation,
                result,
                refresh,
                refreshed,
            )

        threading.Thread(
            target=work,
            name=f"agentbar-credential-{source}",
            daemon=True,
        ).start()

    @objc.python_method
    def _finish_credential_check(
        self, source, generation, result, requested_refresh, refreshed,
    ) -> None:
        with self._credential_lock:
            if generation != self._credential_generations.get(source):
                return
        safe = {
            "available": bool(result.get("available")),
            "source": str(result.get("source") or "none")[:80],
            "status": str(result.get("status") or "unavailable")[:80],
            "detail": str(result.get("detail") or "")[:400],
            "needs_authorization": bool(result.get("needs_authorization")),
        }
        self._credential_status[source] = safe
        suffix = " · 已停用" if not self._enabled[source].state() else ""
        self._status[source].setStringValue_(
            (safe["detail"] or "未检测到可用 CLI 登录态") + suffix
        )
        button = self._source_refresh_buttons[source]
        button.setTitle_("重新检测 / 刷新")
        button.setEnabled_(True)
        if requested_refresh:
            if refreshed:
                message = f"{_NAMES[source]}：已重新检测登录态并触发额度刷新。"
            elif safe["available"]:
                message = f"{_NAMES[source]}：已检测到登录态；启用并保存后可刷新额度。"
            else:
                message = safe["detail"] or (
                    f"未检测到 {_NAMES[source]} 登录态，请先在对应 CLI 中登录。"
                )
            self._message.setStringValue_(message)

    def onRefreshSource_(self, sender):
        source = str(sender.representedObject() or "")
        if source not in _QUOTA_SOURCES:
            return
        sender.setEnabled_(False)
        sender.setTitle_("正在检测…")
        self._message.setStringValue_(
            f"正在重新检测 {_NAMES[source]} CLI 登录态…"
        )
        self._start_credential_check(source, refresh=True)

    def onChromeLogin_(self, sender):
        provider = str(sender.representedObject() or "")
        with self._chrome_login_lock:
            if self._closing_logins:
                return
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

        worker = threading.Thread(
            target=work,
            name=f"agentbar-chrome-login-{provider}",
            daemon=True,
        )
        # Publish and start atomically with respect to close_active_logins(). A
        # quit can therefore either miss this attempt entirely (before the click)
        # or cancel and join a real started thread, never an unstarted object.
        with self._chrome_login_lock:
            if self._closing_logins:
                rejected = True
            else:
                rejected = False
                self._chrome_logins[provider] = login
                self._chrome_login_threads[provider] = worker
                worker.start()
        if rejected:
            login.cancel()
            login.close()

    @objc.python_method
    def close_active_logins(self):
        """Cancel active SSO windows and synchronously reclaim Chrome/profile data."""
        with self._chrome_login_lock:
            self._closing_logins = True
            active = list(self._chrome_logins.items())
            threads = dict(self._chrome_login_threads)
            self._chrome_logins.clear()
            self._chrome_login_threads.clear()
        for _provider, login in active:
            try:
                login.cancel()
                login.close()
            except Exception:
                log.exception("failed to close active Chrome login")
        for provider, thread in threads.items():
            if thread is threading.current_thread():
                continue
            try:
                thread.join(timeout=8)
                if thread.is_alive():
                    log.warning("Chrome login worker %s did not stop within 8s", provider)
            except RuntimeError:
                # Defensive for injected/test workers; production threads are
                # started before publication under _chrome_login_lock.
                pass

    @objc.python_method
    def _set_provider_status(self, provider, message):
        self._status[provider].setStringValue_(message)

    @objc.python_method
    def _finish_chrome_login(self, provider, imported, error):
        with self._chrome_login_lock:
            self._chrome_logins.pop(provider, None)
            self._chrome_login_threads.pop(provider, None)
            closing = self._closing_logins
        # A worker may queue this callback just before shutdown cancels it.
        # Once close_active_logins() has linearized, never touch AppKit controls
        # or persist a late credential from the retired login attempt.
        if closing:
            return
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
        try:
            with self.settings._lock:
                old_providers = copy.deepcopy(self.settings.providers)
                try:
                    cfg = self.settings.providers.setdefault(
                        provider, dict(DEFAULT_PROVIDERS[provider])
                    )
                    cfg["enabled"] = True
                    cfg["cookie"] = imported.header
                    save_settings(self.settings)
                except Exception:
                    self.settings.providers = old_providers
                    raise
        except Exception:
            log.exception("failed to save imported %s cookie", provider)
            self._reload_controls()
            self._alert("Cookie 保存失败", "配置文件写入失败，原设置未更改。")
            return
        self.core.quota.reload_fetchers(refresh=False)
        self.core.quota.refresh_now(provider)
        self._reload_controls()
        self._message.setStringValue_(
            f"{_NAMES[provider]}：已从 {imported.source} 导入 {imported.count} 个 Cookie，并触发刷新"
        )

    def onSave_(self, _sender):
        # Read and validate control values before taking the settings lock; AppKit
        # calls must never sit inside a cross-thread configuration transaction.
        source_inputs = {}
        provider_inputs = {}
        try:
            for source in _QUOTA_SOURCES:
                enabled = bool(self._enabled[source].state())
                model_control = self._source_model.get(source)
                model = (
                    self._validated_field(model_control, "Codex limitId", 160)
                    if model_control is not None else ""
                )
                source_inputs[source] = {
                    "enabled": enabled,
                    "model": model,
                }
            for provider in _PROVIDERS:
                defaults = DEFAULT_PROVIDERS[provider]
                unit = str(
                    self._unit[provider].titleOfSelectedItem() or defaults["unit"]
                )
                try:
                    refresh_seconds = max(
                        60,
                        int(str(self._refresh[provider].stringValue()).strip() or 300),
                    )
                except ValueError:
                    refresh_seconds = defaults["refresh_seconds"]
                provider_inputs[provider] = {
                    "enabled": bool(self._enabled[provider].state()),
                    "unit": unit if unit in PROVIDER_UNITS else defaults["unit"],
                    "refresh_seconds": refresh_seconds,
                    "cookie": str(self._cookie[provider].stringValue()).strip(),
                }
        except ValueError as exc:
            self._alert("无法保存额度设置", str(exc))
            return

        title_index = self.title_popup.indexOfSelectedItem()
        selected_title = (
            _TITLE_OPTIONS[title_index][1]
            if 0 <= title_index < len(_TITLE_OPTIONS)
            else None
        )
        selected_auto_refresh = bool(self.auto_refresh_check.state())

        try:
            # One settings transaction covers credential preservation, mutation,
            # persistence and change detection. A concurrent Web PATCH can run
            # wholly before or after this transaction, never interleave with it.
            with self.settings._lock:
                old_sources = copy.deepcopy(self.settings.quota_sources)
                old_providers = copy.deepcopy(self.settings.providers)
                old_title = self.settings.title_provider
                old_auto_refresh = self.settings.usage_auto_refresh
                before_sources = {
                    source: {
                        "enabled": bool(
                            ((old_sources or {}).get(source) or {}).get("enabled")
                        ),
                        **(
                            {"model": str(
                                ((old_sources or {}).get(source) or {}).get("model")
                                or ""
                            )}
                            if source == "codex" else {}
                        ),
                    }
                    for source in _QUOTA_SOURCES
                }
                before_providers = {
                    provider: dict((old_providers or {}).get(provider) or {})
                    for provider in _PROVIDERS
                }
                try:
                    for source, update in source_inputs.items():
                        cfg = self.settings.quota_sources.setdefault(
                            source, dict(DEFAULT_QUOTA_SOURCES[source])
                        )
                        for legacy_secret in ("access_token", "api_key", "account_id"):
                            cfg.pop(legacy_secret, None)
                        cfg["enabled"] = update["enabled"]
                        if source == "codex":
                            cfg["model"] = update["model"]
                        else:
                            cfg.pop("model", None)

                    for provider, update in provider_inputs.items():
                        cfg = self.settings.providers.setdefault(
                            provider, dict(DEFAULT_PROVIDERS[provider])
                        )
                        cfg["enabled"] = update["enabled"]
                        cfg["unit"] = update["unit"]
                        cfg["refresh_seconds"] = update["refresh_seconds"]
                        if update["cookie"]:
                            cfg["cookie"] = update["cookie"]

                    if selected_title is not None:
                        self.settings.title_provider = selected_title
                    self.settings.usage_auto_refresh = selected_auto_refresh
                    save_settings(self.settings)
                except Exception:
                    self.settings.quota_sources = old_sources
                    self.settings.providers = old_providers
                    self.settings.title_provider = old_title
                    self.settings.usage_auto_refresh = old_auto_refresh
                    raise

                changed = [
                    source for source in _QUOTA_SOURCES
                    if before_sources[source]
                    != {
                        "enabled": bool(
                            ((self.settings.quota_sources or {}).get(source) or {}).get(
                                "enabled"
                            )
                        ),
                        **(
                            {"model": str(
                                ((self.settings.quota_sources or {}).get(source) or {}).get(
                                    "model"
                                ) or ""
                            )}
                            if source == "codex" else {}
                        ),
                    }
                ]
                changed.extend(
                    provider for provider in _PROVIDERS
                    if before_providers[provider]
                    != dict((self.settings.providers or {}).get(provider) or {})
                )
                ready_tools = []
                for tool in changed:
                    if tool in _QUOTA_SOURCES:
                        cfg = (self.settings.quota_sources or {}).get(tool) or {}
                        ready = bool(
                            cfg.get("enabled")
                            and (self._credential_status.get(tool) or {}).get(
                                "available"
                            )
                        )
                    else:
                        cfg = (self.settings.providers or {}).get(tool) or {}
                        ready = cfg.get("enabled") and str(
                            cfg.get("cookie") or ""
                        ).strip()
                    if ready:
                        ready_tools.append(tool)
                auto_refresh_enabled = self.settings.usage_auto_refresh
                auto_refresh_changed = old_auto_refresh != auto_refresh_enabled
        except ValueError as exc:
            self._alert("无法保存额度设置", str(exc))
            return
        except Exception:
            log.exception("failed to save quota settings")
            self._reload_controls()
            self._alert("无法保存额度设置", "配置文件写入失败，原设置未更改。")
            return

        self._reload_controls()
        mode = "已开启周期自动刷新" if auto_refresh_enabled else "之后仅手动刷新"
        if not changed and not auto_refresh_changed:
            self._message.setStringValue_(f"配置已保存；没有来源需要刷新；{mode}。")
            return

        self._message.setStringValue_(f"配置已保存；正在后台应用来源变更；{mode}。")

        def apply_runtime_change():
            try:
                self.core.quota.reload_fetchers(refresh=False)
                refreshed = []
                for tool in ready_tools:
                    self.core.quota.refresh_now(tool)
                    refreshed.append(_NAMES[tool])
                error = ""
            except Exception:
                log.exception("failed to apply saved quota-source settings")
                refreshed = []
                error = "配置已保存，但运行时刷新失败；请点“重新检测 / 刷新”重试。"
            AppHelper.callAfter(
                self._finish_saved_runtime_change,
                refreshed,
                mode,
                error,
            )

        threading.Thread(
            target=apply_runtime_change,
            name="agentbar-provider-settings-apply",
            daemon=True,
        ).start()

    @objc.python_method
    def _finish_saved_runtime_change(self, refreshed, mode, error):
        if error:
            self._message.setStringValue_(error)
            return
        refresh_note = (
            f"已刷新变更来源：{'、'.join(refreshed)}"
            if refreshed else "没有来源需要刷新"
        )
        self._message.setStringValue_(f"配置已保存；{refresh_note}；{mode}。")

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
        try:
            with self.settings._lock:
                old_providers = copy.deepcopy(self.settings.providers)
                try:
                    cfg = self.settings.providers.setdefault(
                        provider, dict(DEFAULT_PROVIDERS[provider])
                    )
                    cfg["cookie"] = ""
                    cfg["enabled"] = False
                    save_settings(self.settings)
                except Exception:
                    self.settings.providers = old_providers
                    raise
        except Exception:
            log.exception("failed to clear %s cookie", provider)
            self._reload_controls()
            self._alert("无法清空 Cookie", "配置文件写入失败，原设置未更改。")
            return
        self.core.quota.reload_fetchers(refresh=False)
        self._reload_controls()
        self._message.setStringValue_(f"{_NAMES[provider]} Cookie 已清空并停用。")

    @objc.python_method
    def _validated_field(self, control, label, max_len):
        value = str(control.stringValue() or "").strip()
        if len(value) > max_len:
            raise ValueError(f"{label} 过长")
        if any(char in value for char in "\r\n\x00"):
            raise ValueError(f"{label} 不能包含换行或 NUL")
        return value

    @objc.python_method
    def _alert(self, title, text):
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(text)
        alert.runModal()
