"""Native provider settings smoke tests (real AppKit controls, no event loop)."""

from agentbar.config import load_settings
from agentbar.provider_window import ProviderSettingsWindowController


class _Quota:
    def __init__(self):
        self.reloads = 0

    def reload_fetchers(self):
        self.reloads += 1


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


def test_native_provider_window_contains_both_providers(tmp_path):
    controller, _, _ = _controller(tmp_path)
    try:
        assert set(controller._enabled) == {"mytoken", "tokenverse"}
        assert str(controller._enabled["mytoken"].title()) == "MyToken"
        assert str(controller._enabled["tokenverse"].title()) == "Tokenverse"
        assert "未配置 Cookie" in str(controller._status["mytoken"].stringValue())
        assert "浏览器登录" in str(controller._chrome_buttons["mytoken"].title())
        assert "已有登录" in str(controller._import_buttons["mytoken"].title())
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
        assert "配置已保存" in str(controller._message.stringValue())
    finally:
        controller.window.close()
