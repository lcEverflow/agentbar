import json
import os
import stat
import threading

import agentbar.config as config_module
from agentbar.config import load_settings, save_settings


def test_quota_source_defaults_are_explicit_opt_in(tmp_path):
    settings = load_settings(tmp_path / "state")
    assert settings.usage_auto_refresh is False
    assert set(settings.quota_sources) == {"claude", "codex"}
    for source in settings.quota_sources.values():
        assert source == {
            "enabled": False,
            "model": "",
            "access_token": "",
            "account_id": "",
        }


def test_quota_source_model_and_secret_roundtrip_in_private_config(tmp_path):
    state_dir = tmp_path / "state"
    settings = load_settings(state_dir)
    settings.quota_sources["codex"].update({
        "enabled": True,
        "model": "codex_bengalfox",
        "access_token": "oauth-secret",
        "account_id": "account-123",
    })
    save_settings(settings)

    assert stat.S_IMODE(settings.config_path.stat().st_mode) == 0o600
    persisted = json.loads(settings.config_path.read_text(encoding="utf-8"))
    assert persisted["quota_sources"]["codex"] == {
        "enabled": True,
        "model": "codex_bengalfox",
        "access_token": "oauth-secret",
        "account_id": "account-123",
    }

    loaded = load_settings(state_dir)
    assert loaded.quota_sources["codex"] == persisted["quota_sources"]["codex"]
    assert loaded.quota_sources["claude"]["enabled"] is False


def test_config_temp_file_is_private_before_atomic_replace(tmp_path, monkeypatch):
    settings = load_settings(tmp_path / "state")
    seen_modes = []
    real_replace = os.replace

    def checked_replace(source, destination):
        seen_modes.append(stat.S_IMODE(os.stat(source).st_mode))
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", checked_replace)
    settings.quota_sources["claude"]["access_token"] = "private-oauth-token"
    save_settings(settings)

    assert seen_modes == [0o600]
    assert stat.S_IMODE(settings.config_path.stat().st_mode) == 0o600


def test_quota_source_load_discards_unknown_sources_and_fields(tmp_path):
    state_dir = tmp_path / "state"
    settings = load_settings(state_dir)
    payload = json.loads(settings.config_path.read_text(encoding="utf-8"))
    payload["quota_sources"] = {
        "codex": {
            "enabled": True,
            "model": "future-model-id",
            "access_token": "secret",
            "account_id": "account",
            "unexpected": "must-not-survive",
        },
        "unknown-provider": {
            "enabled": True,
            "access_token": "unknown-secret",
        },
    }
    settings.config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_settings(state_dir)
    assert set(loaded.quota_sources) == {"claude", "codex"}
    assert loaded.quota_sources["codex"] == {
        "enabled": True,
        "model": "future-model-id",
        "access_token": "secret",
        "account_id": "account",
    }
    assert "unexpected" not in loaded.quota_sources["codex"]


def test_legacy_api_key_is_migrated_once_and_removed(tmp_path):
    state_dir = tmp_path / "state"
    settings = load_settings(state_dir)
    payload = json.loads(settings.config_path.read_text(encoding="utf-8"))
    payload["quota_sources"]["claude"].pop("access_token")
    payload["quota_sources"]["claude"]["api_key"] = "legacy-oauth-token"
    settings.config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_settings(state_dir)
    assert loaded.quota_sources["claude"]["access_token"] == "legacy-oauth-token"
    persisted = json.loads(loaded.config_path.read_text(encoding="utf-8"))
    assert persisted["quota_sources"]["claude"]["access_token"] == "legacy-oauth-token"
    assert "api_key" not in persisted["quota_sources"]["claude"]


def test_concurrent_saves_do_not_share_a_temporary_file(tmp_path):
    state_dir = tmp_path / "state"
    first = load_settings(state_dir)
    second = load_settings(state_dir)
    errors = []
    start = threading.Barrier(3)

    def writer(settings, title):
        try:
            settings.title_provider = title
            start.wait()
            save_settings(settings)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=(first, "claude")),
        threading.Thread(target=writer, args=(second, "codex")),
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=3)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    persisted = json.loads(first.config_path.read_text(encoding="utf-8"))
    assert persisted["title_provider"] in {"claude", "codex"}
    assert list(state_dir.glob(".config.*.tmp")) == []


def test_load_waits_for_in_progress_config_writer(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    settings = load_settings(state_dir)
    replacement_ready = threading.Event()
    release_replace = threading.Event()
    load_done = threading.Event()
    real_replace = os.replace

    def blocked_replace(source, destination):
        if destination == settings.config_path:
            replacement_ready.set()
            assert release_replace.wait(2)
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", blocked_replace)
    settings.title_provider = "codex"
    writer = threading.Thread(target=lambda: save_settings(settings))
    writer.start()
    assert replacement_ready.wait(2)

    loaded = []
    reader = threading.Thread(
        target=lambda: (loaded.append(load_settings(state_dir)), load_done.set())
    )
    reader.start()
    assert not load_done.wait(0.1)

    release_replace.set()
    writer.join(timeout=2)
    reader.join(timeout=2)

    assert load_done.is_set()
    assert loaded[0].title_provider == "codex"


def test_simultaneous_first_loads_share_the_committed_admin_token(
    tmp_path, monkeypatch,
):
    """First-run token generation and persistence are one transaction."""
    state_dir = tmp_path / "state"
    callers = []
    callers_lock = threading.Lock()
    token_rendezvous = threading.Barrier(2)

    def fake_token(_bytes):
        with callers_lock:
            index = len(callers)
            callers.append(threading.current_thread().name)
        try:
            token_rendezvous.wait(timeout=0.25)
        except threading.BrokenBarrierError:
            pass
        return f"first-run-token-{index:016d}"

    monkeypatch.setattr(config_module.secrets, "token_urlsafe", fake_token)
    start = threading.Barrier(3)
    loaded = []
    errors = []

    def reader():
        try:
            start.wait()
            loaded.append(load_settings(state_dir))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=reader, name=f"first-load-{index}")
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert len(callers) == 1
    assert len({settings.token for settings in loaded}) == 1
    persisted = json.loads((state_dir / "config.json").read_text(encoding="utf-8"))
    assert persisted["token"] == loaded[0].token


def test_save_lock_order_cannot_deadlock_read_modify_write(tmp_path, monkeypatch):
    """A settings transaction and a plain save must use one canonical order."""
    settings = load_settings(tmp_path / "state")
    original = settings._lock
    second_attempted_settings = threading.Event()

    class TracedSettingsLock:
        def __enter__(self):
            if threading.current_thread().name == "plain-save":
                second_attempted_settings.set()
            original.acquire()
            return self

        def __exit__(self, *_exc):
            original.release()

    settings._lock = TracedSettingsLock()
    transaction_ready = threading.Event()
    let_transaction_save = threading.Event()
    completed = []

    def transaction():
        with settings._lock:
            transaction_ready.set()
            assert let_transaction_save.wait(2)
            settings.title_provider = "codex"
            save_settings(settings)
        completed.append("transaction")

    owner = threading.Thread(target=transaction, name="settings-transaction", daemon=True)
    owner.start()
    assert transaction_ready.wait(2)

    plain = threading.Thread(
        target=lambda: (save_settings(settings), completed.append("plain")),
        name="plain-save",
        daemon=True,
    )
    plain.start()
    assert second_attempted_settings.wait(2)
    # The plain save is now waiting for settings._lock. It must not already be
    # holding _SAVE_LOCK, or the transaction's nested save would deadlock.
    let_transaction_save.set()
    owner.join(timeout=2)
    plain.join(timeout=2)

    assert not owner.is_alive()
    assert not plain.is_alive()
    assert sorted(completed) == ["plain", "transaction"]


def test_malformed_user_config_is_repaired_before_use(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "config.json").write_text(json.dumps({
        "port": "not-a-port",
        "max_parallel": -4,
        "tick_seconds": float("inf"),
        "backoff_minutes": ["bad", -1],
        "lan_access": "yes",
        "token": 123,
        "providers": [],
        "quota_sources": "bad",
        "title_provider": "unknown",
    }), encoding="utf-8")

    loaded = load_settings(state_dir)
    assert loaded.port == 8737
    assert loaded.max_parallel == 1
    assert loaded.tick_seconds == 1.0
    assert loaded.backoff_minutes == [5, 15, 30, 60]
    assert loaded.lan_access is False
    assert isinstance(loaded.token, str) and loaded.token
    assert loaded.title_provider == "claude"
    assert set(loaded.quota_sources) == {"claude", "codex"}


def test_boolean_numbers_and_non_string_title_cannot_break_startup(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "config.json").write_text(json.dumps({
        "config_schema_version": 2,
        "port": True,
        "max_parallel": False,
        "usage_refresh_seconds": True,
        "tick_seconds": False,
        "backoff_minutes": [True, 7],
        "title_provider": ["codex"],
        "token": "valid-admin-token-value",
    }), encoding="utf-8")

    loaded = load_settings(state_dir)

    assert loaded.port == 8737
    assert loaded.max_parallel == 1
    assert loaded.usage_refresh_seconds == 120
    assert loaded.tick_seconds == 1.0
    assert loaded.backoff_minutes == [7]
    assert loaded.title_provider == "claude"


def test_non_object_json_config_is_replaced_without_crashing(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "config.json").write_text("[]", encoding="utf-8")

    loaded = load_settings(state_dir)

    assert loaded.port == 8737
    persisted = json.loads(loaded.config_path.read_text(encoding="utf-8"))
    assert isinstance(persisted, dict)
    assert persisted["quota_sources"]["claude"]["enabled"] is False
    backup = state_dir / "config.json.corrupt"
    assert backup.read_text(encoding="utf-8") == "[]"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_malformed_config_is_preserved_before_repair(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    original = b'{"token":"recover-me", invalid json'
    (state_dir / "config.json").write_bytes(original)

    loaded = load_settings(state_dir)

    assert loaded.token != "recover-me"
    assert (state_dir / "config.json.corrupt").read_bytes() == original
    assert json.loads(loaded.config_path.read_text(encoding="utf-8"))["token"] == loaded.token


def test_invalid_admin_tokens_are_rotated_on_load(tmp_path):
    invalid_tokens = [
        "too-short",
        "a" * 20 + "\n",
        "b" * 513,
    ]
    for index, invalid in enumerate(invalid_tokens):
        state_dir = tmp_path / f"state-{index}"
        state_dir.mkdir()
        (state_dir / "config.json").write_text(json.dumps({
            "config_schema_version": 2,
            "token": invalid,
        }), encoding="utf-8")

        loaded = load_settings(state_dir)

        assert loaded.token != invalid
        assert 16 <= len(loaded.token) <= 512
        assert not any(ord(char) < 32 or ord(char) == 127 for char in loaded.token)


def test_nested_provider_and_quota_values_are_canonicalized(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "config.json").write_text(json.dumps({
        "config_schema_version": 2,
        "providers": {
            "mytoken": {
                "enabled": True,
                "cookie": [],
                "unit": {},
                "refresh_seconds": "not-an-int",
            },
            "tokenverse": {
                "enabled": True,
                "cookie": " session=value ",
                "unit": "percent",
                "refresh_seconds": 1,
            },
        },
        "quota_sources": {
            "claude": {
                "enabled": True,
                "access_token": [],
                "model": {},
                "account_id": None,
            },
            "codex": {
                "enabled": True,
                "access_token": " oauth-token ",
                "model": " model-id ",
                "account_id": 123,
            },
        },
    }), encoding="utf-8")

    loaded = load_settings(state_dir)

    assert loaded.providers["mytoken"] == {
        "enabled": False,
        "cookie": "",
        "unit": "credits",
        "refresh_seconds": 300,
    }
    assert loaded.providers["tokenverse"] == {
        "enabled": True,
        "cookie": "session=value",
        "unit": "percent",
        "refresh_seconds": 60,
    }
    assert loaded.quota_sources["claude"] == {
        "enabled": False,
        "model": "",
        "access_token": "",
        "account_id": "",
    }
    assert loaded.quota_sources["codex"] == {
        "enabled": True,
        "model": "model-id",
        "access_token": "oauth-token",
        "account_id": "",
    }
    persisted = json.loads(loaded.config_path.read_text(encoding="utf-8"))
    assert persisted["providers"] == loaded.providers
    assert persisted["quota_sources"] == loaded.quota_sources


def test_legacy_lan_default_is_migrated_to_loopback_once(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "config.json").write_text(json.dumps({
        "lan_access": True,
        "token": "old-token",
    }), encoding="utf-8")

    loaded = load_settings(state_dir)
    assert loaded.lan_access is False
    assert loaded.token != "old-token"
    persisted = json.loads(loaded.config_path.read_text(encoding="utf-8"))
    assert persisted["config_schema_version"] == 2
    assert persisted["lan_access"] is False
    assert persisted["token"] == loaded.token

    persisted["lan_access"] = True
    loaded.config_path.write_text(json.dumps(persisted), encoding="utf-8")
    assert load_settings(state_dir).lan_access is True


def test_non_migrating_client_load_does_not_rotate_live_legacy_token(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    legacy = {
        "lan_access": True,
        "token": "legacy-short",
    }
    config_path = state_dir / "config.json"
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    client = load_settings(state_dir, migrate=False)

    assert client.token == legacy["token"]
    assert client.lan_access is True
    assert json.loads(config_path.read_text(encoding="utf-8")) == legacy

    server = load_settings(state_dir)
    assert server.token != legacy["token"]
    assert server.lan_access is False
    assert json.loads(config_path.read_text(encoding="utf-8"))["config_schema_version"] == 2


def test_non_migrating_client_load_does_not_rewrite_or_backup_damage(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config_path = state_dir / "config.json"
    damaged = b'{"token": invalid'
    config_path.write_bytes(damaged)

    load_settings(state_dir, migrate=False)

    assert config_path.read_bytes() == damaged
    assert list(state_dir.glob("config.json.corrupt*")) == []


def test_valid_config_permissions_are_repaired_without_content_change(tmp_path):
    state_dir = tmp_path / "state"
    settings = load_settings(state_dir)
    settings.config_path.chmod(0o644)

    loaded = load_settings(state_dir)

    assert stat.S_IMODE(loaded.config_path.stat().st_mode) == 0o600


def test_obsolete_claude_credential_cache_is_removed_on_upgrade(
    tmp_path, monkeypatch,
):
    fake_home = tmp_path / "home"
    default_state_dir = fake_home / ".agentbar"
    custom_state_dir = tmp_path / "custom-state"
    default_state_dir.mkdir(parents=True)
    custom_state_dir.mkdir()
    monkeypatch.setattr(
        "agentbar.config.Path.home",
        staticmethod(lambda: fake_home),
    )
    legacy_paths = [
        directory / name
        for directory in (default_state_dir, custom_state_dir)
        for name in ("claude_credentials.json", "claude_credentials.tmp")
    ]
    for legacy in legacy_paths:
        legacy.write_text('{"token":"obsolete-secret"}', encoding="utf-8")
    unrelated = default_state_dir / "claude_credentials.backup"
    unrelated.write_text("must survive exact cleanup", encoding="utf-8")

    load_settings(custom_state_dir)

    assert all(not legacy.exists() for legacy in legacy_paths)
    assert unrelated.read_text(encoding="utf-8") == "must survive exact cleanup"
