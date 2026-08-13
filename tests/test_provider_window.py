"""Native provider settings smoke tests (real AppKit controls, no event loop)."""

import copy
import threading
from types import SimpleNamespace

from AppKit import NSAlertFirstButtonReturn

from agentbar.config import load_settings
from agentbar.provider_window import ProviderSettingsWindowController


class _Quota:
    def __init__(self):
        self.reloads = 0
        self.reload_args = []
        self.refreshes = []

    def reload_fetchers(self, refresh=True):
        self.reloads += 1
        self.reload_args.append(refresh)

    def refresh_now(self, tool=None):
        self.refreshes.append(tool)


class _Core:
    def __init__(self):
        self.quota = _Quota()


def _controller(tmp_path):
    settings = load_settings(tmp_path / "provider-window-state")
    core = _Core()
    controller = ProviderSettingsWindowController.alloc().initWithCore_settings_(
        core, settings
    )
    controller._build()
    controller._reload_controls()
    return controller, core, settings


def test_native_provider_window_contains_all_quota_sources(tmp_path):
    controller, _, _ = _controller(tmp_path)
    try:
        assert set(controller._enabled) == {"claude", "codex", "mytoken", "tokenverse"}
        assert str(controller._enabled["claude"].title()) == "Claude"
        assert str(controller._enabled["codex"].title()) == "Codex"
        assert str(controller._enabled["mytoken"].title()) == "MyToken"
        assert str(controller._enabled["tokenverse"].title()) == "Tokenverse"
        assert "未配置 Cookie" in str(controller._status["mytoken"].stringValue())
        assert "浏览器登录" in str(controller._chrome_buttons["mytoken"].title())
        assert "已有登录" in str(controller._import_buttons["mytoken"].title())
        assert "OAuth Access Token" in str(controller._status["claude"].stringValue())
        assert str(controller._source_key["claude"].stringValue()) == ""
        assert "metered_feature" in str(
            controller._source_model["codex"].placeholderString()
        )
        assert controller.auto_refresh_check.state() == 0
    finally:
        controller.window.close()


def test_close_active_logins_is_idempotent_and_reclaims_workers(tmp_path):
    controller, _, _ = _controller(tmp_path)
    calls = []

    class FakeLogin:
        def cancel(self):
            calls.append("cancel")

        def close(self):
            calls.append("close")

    class FakeThread:
        def join(self, timeout=None):
            calls.append(("join", timeout))

        def is_alive(self):
            return False

    try:
        with controller._chrome_login_lock:
            controller._chrome_logins["mytoken"] = FakeLogin()
            controller._chrome_login_threads["mytoken"] = FakeThread()

        controller.close_active_logins()
        controller.close_active_logins()

        assert calls == ["cancel", "close", ("join", 8)]
        assert controller._chrome_logins == {}
        assert controller._chrome_login_threads == {}
    finally:
        controller.window.close()


def test_close_active_logins_rejects_a_late_login_publish(tmp_path, monkeypatch):
    controller, _, _ = _controller(tmp_path)
    constructed = threading.Event()
    release_constructor = threading.Event()
    calls = []

    class FakeLogin:
        def __init__(self, _provider):
            constructed.set()
            assert release_constructor.wait(3)

        def cancel(self):
            calls.append("cancel")

        def close(self):
            calls.append("close")

        def run(self, **_kwargs):  # pragma: no cover - must never start
            calls.append("run")

    monkeypatch.setattr("agentbar.provider_window.ChromeCDPLogin", FakeLogin)
    sender = SimpleNamespace(
        representedObject=lambda: "mytoken",
        setTitle_=lambda _title: None,
    )
    starter = threading.Thread(target=lambda: controller.onChromeLogin_(sender))
    starter.start()
    assert constructed.wait(2)

    controller.close_active_logins()
    release_constructor.set()
    starter.join(timeout=2)

    try:
        assert not starter.is_alive()
        assert calls == ["cancel", "close"]
        assert controller._chrome_logins == {}
        assert controller._chrome_login_threads == {}
    finally:
        controller.window.close()


def test_retired_chrome_login_callback_cannot_save_credentials(tmp_path):
    controller, _, _ = _controller(tmp_path)
    saved = []
    controller._save_imported_cookie = lambda *args: saved.append(args)
    try:
        controller.close_active_logins()

        controller._finish_chrome_login(
            "mytoken",
            SimpleNamespace(header="SESSION=late", source="test", count=1),
            "",
        )

        assert saved == []
    finally:
        controller.window.close()


def test_native_provider_save_updates_settings_and_refreshes(tmp_path):
    controller, core, settings = _controller(tmp_path)
    try:
        controller._enabled["mytoken"].setState_(1)
        controller._unit["mytoken"].selectItemWithTitle_("percent")
        controller._refresh["mytoken"].setStringValue_("90")
        controller._cookie["mytoken"].setStringValue_("SESSION=secret")
        controller.title_popup.selectItemAtIndex_(2)  # MyToken

        controller.onSave_(None)

        assert settings.providers["mytoken"]["enabled"] is True
        assert settings.providers["mytoken"]["unit"] == "percent"
        assert settings.providers["mytoken"]["refresh_seconds"] == 90
        assert settings.providers["mytoken"]["cookie"] == "SESSION=secret"
        assert settings.title_provider == "mytoken"
        assert core.quota.reloads == 1
        assert core.quota.reload_args == [False]
        assert core.quota.refreshes == ["mytoken"]
        assert "配置已保存" in str(controller._message.stringValue())
    finally:
        controller.window.close()


def test_native_subscription_sources_save_write_only_tokens(tmp_path):
    controller, core, settings = _controller(tmp_path)
    try:
        controller._enabled["claude"].setState_(1)
        controller._source_model["claude"].setStringValue_("sonnet")
        controller._source_key["claude"].setStringValue_("claude-oauth-secret")

        controller._enabled["codex"].setState_(1)
        controller._source_model["codex"].setStringValue_("codex_other")
        controller._source_key["codex"].setStringValue_("codex-oauth-secret")
        controller._source_account["codex"].setStringValue_("account-123")
        controller.onSave_(None)

        assert settings.quota_sources["claude"] == {
            "enabled": True,
            "model": "sonnet",
            "access_token": "claude-oauth-secret",
            "account_id": "",
        }
        assert settings.quota_sources["codex"] == {
            "enabled": True,
            "model": "codex_other",
            "access_token": "codex-oauth-secret",
            "account_id": "account-123",
        }
        assert core.quota.reloads == 1
        assert core.quota.reload_args == [False]
        assert core.quota.refreshes == ["claude", "codex"]
        # Reload after save must never put a persisted secret back into a control.
        assert str(controller._source_key["claude"].stringValue()) == ""
        assert str(controller._source_key["codex"].stringValue()) == ""
        assert "secret" not in str(controller._status["claude"].stringValue())
    finally:
        controller.window.close()


def test_native_blank_subscription_credentials_preserve_saved_values(tmp_path):
    controller, core, settings = _controller(tmp_path)
    try:
        settings.quota_sources["claude"].update({
            "enabled": True,
            "model": "opus",
            "access_token": "keep-this-token",
        })
        settings.quota_sources["codex"].update({
            "enabled": True,
            "model": "codex",
            "access_token": "keep-codex-token",
            "account_id": "keep-account",
        })
        controller._reload_controls()
        controller._source_model["claude"].setStringValue_("sonnet")
        controller.onSave_(None)

        assert settings.quota_sources["claude"]["access_token"] == "keep-this-token"
        assert settings.quota_sources["claude"]["model"] == "sonnet"
        assert settings.quota_sources["codex"]["access_token"] == "keep-codex-token"
        assert settings.quota_sources["codex"]["account_id"] == "keep-account"
        assert core.quota.reloads == 1
        assert core.quota.reload_args == [False]
        assert core.quota.refreshes == ["claude"]
    finally:
        controller.window.close()


def test_native_codex_account_id_can_be_cleared_without_deleting_token(
    tmp_path, monkeypatch,
):
    controller, core, settings = _controller(tmp_path)
    settings.quota_sources["codex"].update({
        "enabled": True,
        "access_token": "keep-codex-token",
        "account_id": "remove-account",
    })

    class ConfirmAlert:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

        def setMessageText_(self, _text):
            pass

        def setInformativeText_(self, _text):
            pass

        def addButtonWithTitle_(self, _title):
            pass

        def runModal(self):
            return NSAlertFirstButtonReturn

    monkeypatch.setattr("agentbar.provider_window.NSAlert", ConfirmAlert)
    try:
        controller._reload_controls()
        assert controller._source_account_clear["codex"].isEnabled()

        controller.onClearAccount_(
            SimpleNamespace(representedObject=lambda: "codex")
        )

        assert settings.quota_sources["codex"]["account_id"] == ""
        assert settings.quota_sources["codex"]["access_token"] == "keep-codex-token"
        assert settings.quota_sources["codex"]["enabled"] is True
        assert core.quota.reload_args == [False]
        assert core.quota.refreshes == ["codex"]
        assert not controller._source_account_clear["codex"].isEnabled()
    finally:
        controller.window.close()


def test_native_manual_refresh_is_source_scoped(tmp_path):
    controller, core, settings = _controller(tmp_path)
    try:
        settings.quota_sources["claude"].update({
            "enabled": True,
            "access_token": "configured-token",
        })
        controller._reload_controls()
        controller.onRefreshSource_(controller._source_refresh_buttons["claude"])
        assert core.quota.refreshes == ["claude"]
        assert "手动额度刷新" in str(controller._message.stringValue())
    finally:
        controller.window.close()


def test_native_enabled_subscription_requires_access_token(tmp_path):
    controller, core, settings = _controller(tmp_path)
    try:
        alerts = []
        controller._alert = lambda title, text: alerts.append((title, text))
        controller._enabled["claude"].setState_(1)
        controller.onSave_(None)

        assert core.quota.reloads == 0
        assert settings.quota_sources["claude"]["enabled"] is False
        assert alerts and "OAuth Access Token" in alerts[0][1]
    finally:
        controller.window.close()


def test_native_non_source_setting_does_not_refresh_any_provider(tmp_path):
    controller, core, settings = _controller(tmp_path)
    try:
        controller.title_popup.selectItemAtIndex_(1)  # Codex
        controller.onSave_(None)

        assert settings.title_provider == "codex"
        assert core.quota.reloads == 0
        assert core.quota.refreshes == []
    finally:
        controller.window.close()


def test_native_auto_refresh_toggle_reschedules_without_fetching(tmp_path):
    controller, core, settings = _controller(tmp_path)
    try:
        controller.auto_refresh_check.setState_(1)

        controller.onSave_(None)

        assert settings.usage_auto_refresh is True
        assert core.quota.reload_args == [False]
        assert core.quota.refreshes == []
    finally:
        controller.window.close()


def test_native_cookie_import_refreshes_only_imported_provider(tmp_path):
    controller, core, settings = _controller(tmp_path)
    try:
        imported = SimpleNamespace(
            header="session=secret",
            source="Chrome / Default",
            count=1,
        )
        controller._save_imported_cookie("mytoken", imported)

        assert settings.providers["mytoken"]["cookie"] == "session=secret"
        assert core.quota.reload_args == [False]
        assert core.quota.refreshes == ["mytoken"]
    finally:
        controller.window.close()


def test_native_cookie_status_never_previews_secret_text(tmp_path):
    controller, _, settings = _controller(tmp_path)
    try:
        secret = "SENTINEL_RAW_COOKIE_SECRET=private-value"
        settings.providers["mytoken"].update({
            "enabled": True,
            "cookie": secret,
        })
        controller._reload_controls()

        status = str(controller._status["mytoken"].stringValue())
        assert status == "Cookie 已配置"
        assert "SENTINEL" not in status
        assert "private-value" not in status
    finally:
        controller.window.close()


def test_native_read_modify_save_paths_hold_settings_lock(tmp_path, monkeypatch):
    controller, _, settings = _controller(tmp_path)
    lock_states = []

    def assert_locked(saved_settings):
        assert saved_settings is settings
        lock_states.append(settings._lock._is_owned())

    class ConfirmAlert:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

        def setMessageText_(self, _text):
            pass

        def setInformativeText_(self, _text):
            pass

        def addButtonWithTitle_(self, _title):
            pass

        def runModal(self):
            return NSAlertFirstButtonReturn

    monkeypatch.setattr("agentbar.provider_window.save_settings", assert_locked)
    monkeypatch.setattr("agentbar.provider_window.NSAlert", ConfirmAlert)
    try:
        controller.onSave_(None)
        controller._save_imported_cookie(
            "mytoken",
            SimpleNamespace(header="session=secret", source="test", count=1),
        )
        controller.onClearSource_(
            SimpleNamespace(representedObject=lambda: "claude")
        )
        controller.onClear_(
            SimpleNamespace(representedObject=lambda: "mytoken")
        )

        assert lock_states == [True, True, True, True]
    finally:
        controller.window.close()


def test_native_save_failure_rolls_back_all_settings(tmp_path, monkeypatch):
    controller, core, settings = _controller(tmp_path)
    before = {
        "providers": copy.deepcopy(settings.providers),
        "quota_sources": copy.deepcopy(settings.quota_sources),
        "title_provider": settings.title_provider,
        "usage_auto_refresh": settings.usage_auto_refresh,
    }
    alerts = []
    controller._alert = lambda title, text: alerts.append((title, text))
    controller._enabled["claude"].setState_(1)
    controller._source_key["claude"].setStringValue_("new-secret")
    controller._enabled["mytoken"].setState_(1)
    controller._cookie["mytoken"].setStringValue_("SESSION=new-secret")
    controller.title_popup.selectItemAtIndex_(2)
    controller.auto_refresh_check.setState_(1)

    def fail_save(_settings):
        raise OSError("disk full")

    monkeypatch.setattr("agentbar.provider_window.save_settings", fail_save)
    try:
        controller.onSave_(None)

        assert settings.providers == before["providers"]
        assert settings.quota_sources == before["quota_sources"]
        assert settings.title_provider == before["title_provider"]
        assert settings.usage_auto_refresh == before["usage_auto_refresh"]
        assert core.quota.reloads == 0
        assert core.quota.refreshes == []
        assert alerts == [("无法保存额度设置", "配置文件写入失败，原设置未更改。")]
    finally:
        controller.window.close()


def test_native_destructive_save_failure_keeps_credentials(tmp_path, monkeypatch):
    controller, core, settings = _controller(tmp_path)
    settings.quota_sources["claude"].update({
        "enabled": True,
        "access_token": "keep-token",
    })
    settings.providers["mytoken"].update({
        "enabled": True,
        "cookie": "SESSION=keep-cookie",
    })
    alerts = []
    controller._alert = lambda title, text: alerts.append((title, text))

    class ConfirmAlert:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

        def setMessageText_(self, _text):
            pass

        def setInformativeText_(self, _text):
            pass

        def addButtonWithTitle_(self, _title):
            pass

        def runModal(self):
            return NSAlertFirstButtonReturn

    monkeypatch.setattr("agentbar.provider_window.NSAlert", ConfirmAlert)
    monkeypatch.setattr(
        "agentbar.provider_window.save_settings",
        lambda _settings: (_ for _ in ()).throw(OSError("read only")),
    )
    try:
        controller.onClearSource_(SimpleNamespace(representedObject=lambda: "claude"))
        controller.onClear_(SimpleNamespace(representedObject=lambda: "mytoken"))
        controller._save_imported_cookie(
            "mytoken",
            SimpleNamespace(header="SESSION=replacement", source="test", count=1),
        )

        assert settings.quota_sources["claude"]["access_token"] == "keep-token"
        assert settings.quota_sources["claude"]["enabled"] is True
        assert settings.providers["mytoken"]["cookie"] == "SESSION=keep-cookie"
        assert settings.providers["mytoken"]["enabled"] is True
        assert core.quota.reloads == 0
        assert core.quota.refreshes == []
        assert len(alerts) == 3
    finally:
        controller.window.close()
