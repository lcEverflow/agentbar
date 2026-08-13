import threading
import time

from agentbar.config import Settings
from agentbar.quota import QuotaMonitor
from agentbar.usage import UsageSnapshot, UsageWindow


def _m(tmp_path):
    return QuotaMonitor(Settings(state_dir=tmp_path))


class _CountingFetcher:
    def __init__(self, tool):
        self.tool = tool
        self.calls = 0

    def fetch(self):
        self.calls += 1
        return UsageSnapshot(self.tool, source="test")


def test_unknown_without_observation(tmp_path):
    st = _m(tmp_path).status("claude")
    assert st.state == "unknown"
    assert st.source == "none"


def test_limited_then_ok(tmp_path):
    m = _m(tmp_path)
    reset = time.time() + 600
    m.record_quota("claude", reset)
    st = m.status("claude")
    assert st.state == "limited"
    assert st.reset_at == reset
    assert m.cooldown_until("claude") == reset

    m.record_success("claude")
    st = m.status("claude")
    assert st.state == "ok"
    assert m.cooldown_until("claude") is None


def test_limited_no_reset_hint(tmp_path):
    m = _m(tmp_path)
    m.record_quota("codex", None)
    st = m.status("codex")
    assert st.state == "limited"
    assert "退避" in st.detail


def test_dump_load_roundtrip(tmp_path):
    m = _m(tmp_path)
    m.record_quota("claude", time.time() + 60)
    data = m.dump()
    m2 = _m(tmp_path)
    m2.load(data)
    assert m2.status("claude").state == "limited"


def test_load_ignores_malformed_observations(tmp_path):
    m = _m(tmp_path)

    m.load({
        "claude": "not-an-object",
        "codex": {
            "last_quota_at": "yesterday",
            "last_success_at": 123,
            "reset_at": float("inf"),
        },
        123: {"last_success_at": 456},
    })

    assert m.dump() == {"codex": {"last_success_at": 123.0}}
    assert m.status("codex").state == "ok"
    m.record_quota("claude", None)
    assert m.status("claude").state == "limited"


def test_task_observations_do_not_schedule_network_refresh(tmp_path):
    m = _m(tmp_path)
    fetcher = _CountingFetcher("claude")
    m._fetchers = {"claude": fetcher}

    m.record_quota("claude", time.time() + 60)
    m.record_success("claude")

    assert fetcher.calls == 0
    assert m._pending_refresh == set()
    assert not m._refresh_evt.is_set()


def test_targeted_manual_refresh_is_coalesced(tmp_path):
    settings = Settings(state_dir=tmp_path, usage_auto_refresh=False)
    m = QuotaMonitor(settings)
    claude = _CountingFetcher("claude")
    codex = _CountingFetcher("codex")
    m._fetchers = {"claude": claude, "codex": codex}
    ccusage_calls = []
    m._ccusage_bin = "/test/ccusage"
    m._refresh_ccusage = lambda: ccusage_calls.append(True)

    # Queue the same request twice before the worker starts. It is absorbed by
    # that source's documented startup fetch rather than causing a second call.
    m.refresh_now("codex")
    m.refresh_now("codex")
    assert m._pending_refresh == {"codex"}

    m.start_background()
    deadline = time.time() + 2
    while codex.calls < 1 and time.time() < deadline:
        time.sleep(0.01)
    m.stop()
    m._thread.join(timeout=2)

    # Both configured sources get exactly one startup fetch; the two already
    # pending Codex clicks do not create a back-to-back duplicate request.
    assert claude.calls == 1
    assert codex.calls == 1
    # ccusage supplements Claude only, so the targeted Codex refresh must not
    # invoke it a second time.
    assert ccusage_calls == [True]
    assert not m._thread.is_alive()


def test_manual_mode_does_not_refresh_on_timer_timeout(tmp_path):
    settings = Settings(state_dir=tmp_path, usage_auto_refresh=False)
    m = QuotaMonitor(settings)
    fetcher = _CountingFetcher("claude")
    m._fetchers = {"claude": fetcher}
    m._ccusage_bin = None

    class _TwoTimeouts:
        def __init__(self):
            self.waits = 0

        def wait(self, _timeout):
            self.waits += 1
            if self.waits == 2:
                m._stop.set()
            return False

        def clear(self):
            pass

    m._refresh_evt = _TwoTimeouts()
    m._loop()

    # The initial configured-source fetch is intentional. The first simulated
    # timer timeout must not add another fetch while manual mode is selected.
    assert fetcher.calls == 1


def test_auto_mode_wakes_for_nearest_per_source_deadline(tmp_path, monkeypatch):
    settings = Settings(
        state_dir=tmp_path,
        usage_auto_refresh=True,
        usage_refresh_seconds=120,
    )
    m = QuotaMonitor(settings)
    subscription = _CountingFetcher("claude")
    provider = _CountingFetcher("mytoken")
    provider.refresh_seconds = 90
    m._fetchers = {"claude": subscription, "mytoken": provider}
    m._ccusage_bin = None
    now = [1_000.0]
    monkeypatch.setattr("agentbar.quota.time.time", lambda: now[0])

    m._refresh_all()

    assert subscription.calls == 1
    assert provider.calls == 1
    assert m._next_wait_seconds() == 90

    now[0] = 1_090.0
    m._refresh_all(respect_due=True)
    assert subscription.calls == 1
    assert provider.calls == 2
    assert m._next_wait_seconds() == 30

    now[0] = 1_120.0
    m._refresh_all(respect_due=True)
    assert subscription.calls == 2
    assert provider.calls == 2


def test_runtime_global_interval_change_wakes_and_reschedules_subscription(
    tmp_path, monkeypatch,
):
    settings = Settings(
        state_dir=tmp_path,
        usage_auto_refresh=True,
        usage_refresh_seconds=120,
    )
    m = QuotaMonitor(settings)
    fetcher = _CountingFetcher("codex")
    m._fetchers = {"codex": fetcher}
    m._ccusage_bin = None
    now = [2_000.0]
    monkeypatch.setattr("agentbar.quota.time.time", lambda: now[0])
    monkeypatch.setattr(
        "agentbar.quota.get_usage_fetchers",
        lambda _settings: {"codex": fetcher},
    )

    m._refresh_all()
    assert m._next_wait_seconds() == 120
    m._refresh_evt.clear()

    settings.usage_refresh_seconds = 30
    m.reload_fetchers()

    assert m._refresh_evt.is_set()
    assert m._next_wait_seconds() == 30
    now[0] = 2_030.0
    m._refresh_all(respect_due=True)
    assert fetcher.calls == 2


def test_manual_refresh_at_timeout_boundary_is_not_lost(tmp_path):
    """pending is the predicate; an Event race may delay but never lose it."""
    settings = Settings(state_dir=tmp_path, usage_auto_refresh=False)
    m = QuotaMonitor(settings)
    fetcher = _CountingFetcher("codex")
    m._fetchers = {"codex": fetcher}
    m._ccusage_bin = None

    class _TimeoutBoundaryEvent:
        def __init__(self):
            self.waits = 0

        def clear(self):
            pass

        def set(self):
            pass

        def wait(self, _timeout):
            self.waits += 1
            if self.waits == 1:
                # Model refresh_now().set() racing just as wait reports timeout.
                m.refresh_now("codex")
            else:
                m._stop.set()
            return False

    m._refresh_evt = _TimeoutBoundaryEvent()
    m._loop()

    assert fetcher.calls == 2  # startup + the boundary-racing manual request
    assert m._pending_refresh == set()


def test_stop_joins_and_does_not_start_next_provider(tmp_path):
    settings = Settings(state_dir=tmp_path, usage_auto_refresh=False)
    m = QuotaMonitor(settings)

    class _BlockingFetcher(_CountingFetcher):
        def __init__(self, tool):
            super().__init__(tool)
            self.entered = threading.Event()
            self.release = threading.Event()

        def fetch(self):
            self.calls += 1
            self.entered.set()
            assert self.release.wait(2)
            return UsageSnapshot(self.tool, source="test")

    first = _BlockingFetcher("claude")
    second = _CountingFetcher("codex")
    m._fetchers = {"claude": first, "codex": second}
    m._ccusage_bin = None
    m.start_background()
    assert first.entered.wait(2)

    stopped = threading.Event()
    stopper = threading.Thread(target=lambda: (m.stop(), stopped.set()))
    stopper.start()
    assert not stopped.wait(0.05)  # stop waits for the active request to retire
    first.release.set()
    assert stopped.wait(2)
    stopper.join(timeout=2)

    assert second.calls == 0
    assert m._thread is not None and not m._thread.is_alive()


def test_background_lifecycle_is_idempotent(tmp_path):
    m = QuotaMonitor(Settings(state_dir=tmp_path, usage_auto_refresh=False))
    fetcher = _CountingFetcher("codex")
    m._fetchers = {"codex": fetcher}
    m._ccusage_bin = None

    m.start_background()
    thread = m._thread
    m.start_background()
    assert m._thread is thread
    deadline = time.time() + 2
    while fetcher.calls < 1 and time.time() < deadline:
        time.sleep(0.01)
    m.stop()
    m.stop()
    m.refresh_now("codex")

    assert fetcher.calls == 1
    assert m._pending_refresh == set()
    assert thread is not None and not thread.is_alive()


def test_inflight_refresh_clicks_do_not_queue_a_second_request(tmp_path):
    m = _m(tmp_path)

    class _BlockingFetcher(_CountingFetcher):
        def __init__(self, tool):
            super().__init__(tool)
            self.entered = threading.Event()
            self.release = threading.Event()

        def fetch(self):
            self.calls += 1
            self.entered.set()
            assert self.release.wait(2)
            return UsageSnapshot(self.tool, source="test")

    fetcher = _BlockingFetcher("codex")
    m._fetchers = {"codex": fetcher}
    thread = threading.Thread(target=m._refresh_all, args=({"codex"},))
    thread.start()
    assert fetcher.entered.wait(2)
    m.refresh_now("codex")
    m.refresh_now("codex")
    assert m._pending_refresh == set()
    fetcher.release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert fetcher.calls == 1


def test_dequeued_refresh_is_claimed_before_more_clicks(tmp_path):
    """The worker must not expose a dequeue -> mark-as-refreshing gap."""
    settings = Settings(state_dir=tmp_path, usage_auto_refresh=False)
    m = QuotaMonitor(settings)
    fetcher = _CountingFetcher("codex")
    m._fetchers = {"codex": fetcher}
    m._ccusage_bin = None
    m.start_background()
    deadline = time.time() + 2
    while fetcher.calls < 1 and time.time() < deadline:
        time.sleep(0.01)
    assert fetcher.calls == 1

    claimed = threading.Event()
    release = threading.Event()
    real_run = m._run_refresh_batch

    def pause_after_claim(batch):
        claimed.set()
        assert release.wait(2)
        real_run(batch)

    m._run_refresh_batch = pause_after_claim
    m.refresh_now("codex")
    assert claimed.wait(2)

    # The pending request has already been bound to the current generation and
    # marked in-flight. Clicks in this exact pre-I/O window must be absorbed.
    m.refresh_now("codex")
    m.refresh_now("codex")
    assert m._pending_refresh == set()
    assert m._refreshing.get("codex") is fetcher

    release.set()
    deadline = time.time() + 2
    while fetcher.calls < 2 and time.time() < deadline:
        time.sleep(0.01)
    m.stop()
    m._thread.join(timeout=2)

    assert fetcher.calls == 2  # one startup + one coalesced manual refresh
    assert not m._thread.is_alive()


def test_reload_discards_result_from_previous_model_fetch(tmp_path):
    settings = Settings(state_dir=tmp_path)
    m = QuotaMonitor(settings)

    class _OldModelFetcher:
        tool = "codex"

        def __init__(self):
            self.entered = threading.Event()
            self.release = threading.Event()

        def fetch(self):
            self.entered.set()
            assert self.release.wait(2)
            return UsageSnapshot(
                "codex",
                windows=[UsageWindow("7d", 88, time.time() + 600)],
                source="test",
                model="old-model",
            )

    old = _OldModelFetcher()
    m._fetchers = {"codex": old}
    thread = threading.Thread(target=m._refresh_all, args=({"codex"},))
    thread.start()
    assert old.entered.wait(2)

    settings.quota_sources["codex"].update({
        "enabled": True,
        "model": "new-model",
        "access_token": "new-access-token",
    })
    m.reload_fetchers(refresh=False)
    old.release.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert m.status("codex").windows == []
    assert m.status("codex").model is None


def test_reload_claims_new_generation_and_old_finally_keeps_its_marker(
    tmp_path, monkeypatch
):
    settings = Settings(state_dir=tmp_path)
    m = QuotaMonitor(settings)

    class _BlockingModelFetcher(_CountingFetcher):
        def __init__(self, model):
            super().__init__("codex")
            self.model = model
            self.entered = threading.Event()
            self.release = threading.Event()

        def fetch(self):
            self.calls += 1
            self.entered.set()
            assert self.release.wait(2)
            return UsageSnapshot("codex", source="test", model=self.model)

    old = _BlockingModelFetcher("old-model")
    new = _BlockingModelFetcher("new-model")
    m._fetchers = {"codex": old}

    old_thread = threading.Thread(target=m._refresh_all, args=({"codex"},))
    old_thread.start()
    assert old.entered.wait(2)

    monkeypatch.setattr(
        "agentbar.quota.get_usage_fetchers", lambda _settings: {"codex": new}
    )
    m.reload_fetchers(refresh=True)
    new_batch = m._claim_pending_refresh()
    assert new_batch.fetchers == {"codex": new}
    assert new_batch.generation == m._fetch_generation

    new_thread = threading.Thread(target=m._run_refresh_batch, args=(new_batch,))
    new_thread.start()
    assert new.entered.wait(2)

    # Finishing generation 0 must not erase generation 1's in-flight marker.
    old.release.set()
    old_thread.join(timeout=2)
    assert not old_thread.is_alive()
    assert m._refreshing.get("codex") is new
    m.refresh_now("codex")
    assert m._pending_refresh == set()

    new.release.set()
    new_thread.join(timeout=2)
    assert not new_thread.is_alive()
    assert old.calls == 1
    assert new.calls == 1
    assert m.status("codex").model == "new-model"


def test_reload_refreshes_only_changed_sources_and_preserves_other_snapshot(
    tmp_path, monkeypatch
):
    m = _m(tmp_path)

    class _ConfiguredFetcher(_CountingFetcher):
        def __init__(self, tool, access_token):
            super().__init__(tool)
            self.access_token = access_token

    claude_old = _ConfiguredFetcher("claude", "same-token")
    codex_old = _ConfiguredFetcher("codex", "old-token")
    m._fetchers = {"claude": claude_old, "codex": codex_old}
    claude_snapshot = UsageSnapshot(
        "claude", windows=[UsageWindow("5h", 10)], source="test"
    )
    m._usage = {
        "claude": claude_snapshot,
        "codex": UsageSnapshot("codex", windows=[UsageWindow("5h", 20)], source="test"),
    }
    claude_rebuilt = _ConfiguredFetcher("claude", "same-token")
    codex_new = _ConfiguredFetcher("codex", "new-token")
    monkeypatch.setattr(
        "agentbar.quota.get_usage_fetchers",
        lambda _settings: {"claude": claude_rebuilt, "codex": codex_new},
    )

    m.reload_fetchers(refresh=True)

    assert m._fetchers["claude"] is claude_old
    assert m._usage["claude"] is claude_snapshot
    assert "codex" not in m._usage
    assert m._pending_refresh == {"codex"}
    batch = m._claim_pending_refresh()
    m._run_refresh_batch(batch)
    assert claude_old.calls == 0
    assert claude_rebuilt.calls == 0
    assert codex_new.calls == 1


def test_reload_never_builds_fetcher_from_uncommitted_settings(tmp_path):
    settings = Settings(state_dir=tmp_path)
    settings.quota_sources["codex"].update({
        "enabled": True,
        "access_token": "stable-token",
    })
    m = QuotaMonitor(settings)
    transaction_open = threading.Event()
    release_transaction = threading.Event()
    reload_done = threading.Event()

    def rolled_back_writer():
        with settings._lock:
            settings.quota_sources["codex"]["access_token"] = "transient-token"
            transaction_open.set()
            assert release_transaction.wait(2)
            settings.quota_sources["codex"]["access_token"] = "stable-token"

    writer = threading.Thread(target=rolled_back_writer)
    writer.start()
    assert transaction_open.wait(2)

    reloader = threading.Thread(
        target=lambda: (m.reload_fetchers(refresh=False), reload_done.set())
    )
    reloader.start()
    assert not reload_done.wait(0.1)

    release_transaction.set()
    writer.join(timeout=2)
    reloader.join(timeout=2)

    assert reload_done.is_set()
    assert m._fetchers["codex"].access_token == "stable-token"


def test_refresh_error_keeps_fresh_last_good_snapshot(tmp_path):
    m = _m(tmp_path)
    good = UsageSnapshot(
        "codex", windows=[UsageWindow("5h", 37)], source="test"
    )
    m._usage["codex"] = good

    class _ErrorFetcher:
        tool = "codex"

        def fetch(self):
            return UsageSnapshot("codex", source="test", error="temporary outage")

    m._fetchers = {"codex": _ErrorFetcher()}
    m._refresh_all({"codex"})
    status = m.status("codex")

    assert status.state == "ok"
    assert status.windows[0]["used_percent"] == 37
    assert status.error == "temporary outage"
    assert "上次刷新失败" in status.detail


def test_stale_manual_snapshot_remains_visible_but_is_explicitly_untrusted(tmp_path):
    m = _m(tmp_path)
    reset = time.time() + 3600
    m._usage["codex"] = UsageSnapshot(
        "codex",
        windows=[UsageWindow("5h", 100, reset)],
        source="test",
        fetched_at=time.time() - 3600,
    )

    status = m.status("codex")

    assert status.state == "unknown"
    assert status.stale is True
    assert status.windows[0]["used_percent"] == 100
    assert "已过期" in status.detail
    assert m.cooldown_until("codex", "gpt-5.4") is None


def test_startup_reload_same_tool_fetches_new_generation_once(tmp_path, monkeypatch):
    settings = Settings(state_dir=tmp_path, usage_auto_refresh=False)
    m = QuotaMonitor(settings)
    old = _CountingFetcher("codex")
    new = _CountingFetcher("codex")
    m._fetchers = {"codex": old}
    m._ccusage_bin = None
    monkeypatch.setattr(
        "agentbar.quota.get_usage_fetchers", lambda _settings: {"codex": new}
    )

    before_startup_claim = threading.Event()
    release_startup = threading.Event()
    pending_event_consumed = threading.Event()
    real_refresh_all = m._refresh_all
    real_claim_pending = m._claim_pending_refresh
    first = True

    def pause_startup_before_claim(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            before_startup_claim.set()
            assert release_startup.wait(2)
        real_refresh_all(*args, **kwargs)

    def observe_pending_event():
        batch = real_claim_pending()
        pending_event_consumed.set()
        return batch

    m._refresh_all = pause_startup_before_claim
    m._claim_pending_refresh = observe_pending_event
    m.start_background()
    assert before_startup_claim.wait(2)

    # Reload wins before startup has claimed a fetcher. Startup must bind only
    # the new fetcher and absorb reload's pending request into that same call.
    m.reload_fetchers(refresh=True)
    release_startup.set()
    assert pending_event_consumed.wait(2)
    m.stop()
    m._thread.join(timeout=2)

    assert old.calls == 0
    assert new.calls == 1
    assert not m._thread.is_alive()


def test_model_specific_usage_only_cools_matching_tasks(tmp_path):
    m = _m(tmp_path)
    reset = time.time() + 600
    m._usage["codex"] = UsageSnapshot(
        "codex",
        windows=[UsageWindow("7d", 100, reset, model="GPT-5.3-Codex-Spark")],
        source="test",
        # Snapshot.model describes the manually selected display bucket. The
        # cooldown scope comes from each window instead.
        model="GPT-5.3-Codex-Spark",
    )

    assert m.cooldown_until("codex", "gpt-5.3-codex-spark") == reset
    assert m.cooldown_until("codex", "gpt-5.4") is None
    assert m.cooldown_until("codex", None) is None


def test_generic_usage_window_cools_every_task_model(tmp_path):
    m = _m(tmp_path)
    reset = time.time() + 600
    m._usage["codex"] = UsageSnapshot(
        "codex",
        windows=[UsageWindow("5h", 100, reset, model=None)],
        source="test",
        model="GPT-5.3-Codex-Spark",
    )

    assert m.cooldown_until("codex", "gpt-5.3-codex-spark") == reset
    assert m.cooldown_until("codex", "gpt-5.4") == reset
    assert m.cooldown_until("codex", None) == reset


def test_window_limited_flag_drives_only_its_model_cooldown(tmp_path):
    m = _m(tmp_path)
    reset = time.time() + 600
    m._usage["claude"] = UsageSnapshot(
        "claude",
        windows=[
            UsageWindow(
                "7d Opus",
                42,
                reset,
                model="opus",
                limited=True,
            )
        ],
        source="test",
        model="opus",
    )

    assert m.cooldown_until("claude", "claude-opus-4-6") == reset
    assert m.cooldown_until("claude", "claude-sonnet-4-6") is None


def test_snapshot_limited_is_display_compatibility_not_model_cooldown(tmp_path):
    m = _m(tmp_path)
    reset = time.time() + 600
    m._usage["codex"] = UsageSnapshot(
        "codex",
        windows=[
            UsageWindow(
                "Spark",
                42,
                reset,
                model="GPT-5.3-Codex-Spark",
                limited=False,
            )
        ],
        source="test",
        limited=True,
        model="GPT-5.3-Codex-Spark",
    )

    # The aggregate flag still supports old UI snapshots, but cannot prove
    # which model window is exhausted and therefore must not block scheduling.
    assert m.status("codex").state == "limited"
    assert m.cooldown_until("codex", "gpt-5.3-codex-spark") is None
