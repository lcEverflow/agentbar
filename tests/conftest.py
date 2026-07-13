import time

import pytest

from agentbar.config import load_settings
from agentbar.scheduler import Scheduler
from agentbar.store import StateStore


def wait_for(pred, timeout=10.0, interval=0.02, desc="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    raise AssertionError(f"timeout waiting for {desc}")


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTBAR_ENABLE_FAKE", "1")
    s = load_settings(tmp_path / "state")
    # 测试专用：加速 tick 与退避
    s.tick_seconds = 0.02
    s.backoff_minutes = [0.002]  # ~0.12s
    s.max_parallel = 2
    s.per_tool_limit = 2
    s.task_timeout_seconds = 60
    s.default_cwd = str(tmp_path)
    return s


@pytest.fixture
def core(settings):
    # Scheduler tests exercise lifecycle/API behavior, never the user's real
    # Keychain or provider network. Keep them deterministic now that the
    # macOS Security bridge is installed in the development environment.
    # (The fetcher/parser itself has dedicated tests.)
    from agentbar import quota as quota_module

    original_fetchers = quota_module.get_usage_fetchers
    quota_module.get_usage_fetchers = lambda: {}
    store = StateStore(settings.state_dir)
    c = Scheduler(settings, store)
    c.start()
    try:
        yield c
    finally:
        c.shutdown()
        quota_module.get_usage_fetchers = original_fetchers
