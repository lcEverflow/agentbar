import json
import math
import threading
import time

import pytest

import agentbar.scheduler as scheduler_module
from agentbar.models import TaskState
from agentbar.scheduler import Scheduler
from agentbar.store import StateStore

from conftest import wait_for


def _get(core, tid):
    return next(t for t in core.snapshot()["tasks"] if t["id"] == tid)


def test_success_flow(core, tmp_path):
    t = core.add_task("OK", tool="fake", cwd=str(tmp_path))
    wait_for(lambda: _get(core, t.id)["state"] == "succeeded", desc="succeeded")
    d = _get(core, t.id)
    assert d["session_id"] == "fake-sess-1"
    assert d["attempts"] == 1
    assert "FAKE DONE" in core.store.read_log_tail(t.id)


def test_failure_flow(core, tmp_path):
    t = core.add_task("FAIL", tool="fake", cwd=str(tmp_path))
    wait_for(lambda: _get(core, t.id)["state"] == "failed", desc="failed")
    assert _get(core, t.id)["exit_code"] == 3


def test_quota_wait_then_auto_recover(core, tmp_path):
    """额度耗尽 → WAITING_QUOTA（非 FAILED）→ 退避后自动重试（带 resume）→ 成功。"""
    t = core.add_task("QUOTA_ONCE:k1", tool="fake", cwd=str(tmp_path))
    wait_for(lambda: _get(core, t.id)["quota_waits"] >= 1, desc="quota observed")
    # 中间态不是 failed
    assert _get(core, t.id)["state"] in ("waiting_quota", "queued", "running", "succeeded")
    wait_for(lambda: _get(core, t.id)["state"] == "succeeded", desc="recovered")
    d = _get(core, t.id)
    assert d["quota_waits"] == 1
    assert d["state"] != "failed"


def test_quota_sets_tool_cooldown(core, tmp_path):
    t = core.add_task("QUOTA", tool="fake", cwd=str(tmp_path))
    wait_for(lambda: _get(core, t.id)["state"] == "waiting_quota", desc="waiting")
    d = _get(core, t.id)
    assert d["next_retry_at"] is not None
    # fake_cli 报了 now+3600 的恢复时间戳 → 冷却期生效
    cd = core.quota.cooldown_until("fake")
    assert cd is not None and cd > time.time() + 3000
    q = core.snapshot()["quota"]["fake"]
    assert q["state"] == "limited" and q["source"] == "observed"
    core.act(t.id, "cancel")


def test_cancel_running(core, tmp_path):
    t = core.add_task("SLEEP:30", tool="fake", cwd=str(tmp_path))
    wait_for(lambda: _get(core, t.id)["state"] == "running", desc="running")
    ok, _ = core.act(t.id, "cancel")
    assert ok
    wait_for(lambda: _get(core, t.id)["state"] == "cancelled", desc="cancelled")


def test_cancel_terminates_child_even_when_worker_is_blocked_writing_stdin(
    settings, tmp_path, monkeypatch,
):
    core = Scheduler(settings, StateStore(settings.state_dir))
    adapter = core.registry["fake"]
    monkeypatch.setattr(
        adapter,
        "build_argv",
        lambda _task, resume, binary: [binary, "-c", "import time; time.sleep(30)"],
    )
    monkeypatch.setattr(
        adapter, "stdin_payload", lambda _task, resume: "x" * 2_000_000
    )
    core.start()
    task = core.add_task("OK", "fake", str(tmp_path))

    def child():
        with core._lock:
            run = core._running.get(task.id)
            return run.proc if run and run.proc is not None else None

    proc = wait_for(child, desc="non-reading child launched")
    started = time.monotonic()
    assert core.act(task.id, "cancel")[0]
    wait_for(lambda: _get(core, task.id)["state"] == "cancelled", desc="cancelled")

    try:
        assert time.monotonic() - started < 5
        assert proc.poll() is not None
    finally:
        core.shutdown()


def test_monitor_exception_terminates_child_before_task_is_finalized(
    core, tmp_path, monkeypatch,
):
    """A parser/read failure must never orphan an already-launched agent."""
    adapter = core.registry["fake"]

    def fail_session_probe(_output):
        raise RuntimeError("simulated session probe failure")

    monkeypatch.setattr(adapter, "extract_session_id", fail_session_probe)
    task = core.add_task("SLEEP:30", tool="fake", cwd=str(tmp_path))

    def launched_process():
        with core._lock:
            run = core._running.get(task.id)
            return run.proc if run and run.proc is not None else None

    proc = wait_for(launched_process, desc="child launched")
    wait_for(
        lambda: _get(core, task.id)["state"] == "failed",
        timeout=6,
        desc="monitor failure finalized",
    )

    assert "执行监控失败" in _get(core, task.id)["state_reason"]
    assert proc.poll() is not None
    with core._lock:
        assert task.id not in core._running


def test_result_parser_exception_cannot_leave_false_running_task(
    core, tmp_path, monkeypatch,
):
    adapter = core.registry["fake"]
    monkeypatch.setattr(
        adapter, "classify",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("simulated result parse failure")),
    )

    task = core.add_task("OK", tool="fake", cwd=str(tmp_path))
    wait_for(
        lambda: _get(core, task.id)["state"] == "failed",
        desc="result parse failure finalized",
    )

    assert "执行结果处理失败" in _get(core, task.id)["state_reason"]
    with core._lock:
        assert task.id not in core._running


def test_launch_preparation_exception_cannot_leave_false_running_task(
    core, tmp_path, monkeypatch,
):
    adapter = core.registry["fake"]
    monkeypatch.setattr(
        adapter, "stdin_payload",
        lambda _task, resume: (_ for _ in ()).throw(RuntimeError("bad adapter")),
    )

    task = core.add_task("OK", tool="fake", cwd=str(tmp_path))
    wait_for(
        lambda: _get(core, task.id)["state"] == "failed",
        desc="launch preparation failure finalized",
    )

    assert "启动准备失败" in _get(core, task.id)["state_reason"]
    with core._lock:
        assert task.id not in core._running


def test_pause_all_blocks_dispatch(core, tmp_path):
    core.pause_all()
    t = core.add_task("OK", tool="fake", cwd=str(tmp_path))
    time.sleep(0.3)
    assert _get(core, t.id)["state"] == "queued"
    assert core.snapshot()["status"] == "paused"
    core.resume_all()
    wait_for(lambda: _get(core, t.id)["state"] == "succeeded", desc="after resume")


def test_per_task_pause_resume(core, tmp_path):
    core.pause_all()
    t = core.add_task("OK", tool="fake", cwd=str(tmp_path))
    assert core.act(t.id, "pause") == (True, "已暂停")
    core.resume_all()
    time.sleep(0.3)
    assert _get(core, t.id)["state"] == "paused"  # 单任务暂停不受全局恢复影响
    core.act(t.id, "resume")
    wait_for(lambda: _get(core, t.id)["state"] == "succeeded", desc="resumed")


def test_snapshot_exposes_provider_setup_state_without_cookie(core, settings):
    settings.providers["mytoken"].update({
        "enabled": True,
        "cookie": "SESSION=secret",
        "unit": "credits",
    })
    providers = core.snapshot()["provider_config"]
    assert providers["mytoken"] == {
        "enabled": True,
        "cookie_set": True,
        "unit": "credits",
    }
    assert "cookie" not in providers["mytoken"]
    assert providers["tokenverse"]["cookie_set"] is False


def test_serial_fifo(settings, tmp_path):
    settings.max_parallel = 1
    settings.per_tool_limit = 1
    core = Scheduler(settings, StateStore(settings.state_dir))
    core.start()
    try:
        t1 = core.add_task("SLEEP:0.4", tool="fake", cwd=str(tmp_path))
        t2 = core.add_task("OK", tool="fake", cwd=str(tmp_path))
        wait_for(lambda: _get(core, t2.id)["state"] == "succeeded", desc="t2 done")
        d1, d2 = _get(core, t1.id), _get(core, t2.id)
        assert d1["state"] == "succeeded"
        # 串行：t2 必须在 t1 结束后才开始
        assert d2["started_at"] >= d1["finished_at"] - 0.05
    finally:
        core.shutdown()


def test_parallel_two(core, tmp_path):
    t1 = core.add_task("SLEEP:0.6", tool="fake", cwd=str(tmp_path))
    t2 = core.add_task("SLEEP:0.6", tool="fake", cwd=str(tmp_path))
    wait_for(
        lambda: _get(core, t1.id)["state"] == "running"
        and _get(core, t2.id)["state"] == "running",
        desc="both running (max_parallel=2)",
    )


def test_retry_finished(core, tmp_path):
    t = core.add_task("FAIL", tool="fake", cwd=str(tmp_path))
    wait_for(lambda: _get(core, t.id)["state"] == "failed", desc="failed")
    ok, _ = core.act(t.id, "retry")
    assert ok
    wait_for(lambda: _get(core, t.id)["attempts"] >= 2, desc="retried")


def test_full_profile_blocked_by_default(core, tmp_path):
    with pytest.raises(ValueError, match="高权限"):
        core.add_task("OK", tool="fake", cwd=str(tmp_path), profile="full")


def test_add_task_validation(core, tmp_path):
    with pytest.raises(ValueError):
        core.add_task("", tool="fake", cwd=str(tmp_path))
    with pytest.raises(ValueError):
        core.add_task("OK", tool="nope", cwd=str(tmp_path))
    with pytest.raises(ValueError):
        core.add_task("OK", tool="fake", cwd="/definitely/not/a/dir")


def test_title_and_schedule_validation(settings, tmp_path):
    core = Scheduler(settings, StateStore(settings.state_dir))
    try:
        with pytest.raises(ValueError, match="标题"):
            core.add_task("OK", "fake", str(tmp_path), title="bad\ntitle")
        with pytest.raises(ValueError, match="标题"):
            core.add_task("OK", "fake", str(tmp_path), title="x" * 201)
        for bad in ("tomorrow", math.nan, math.inf, True):
            with pytest.raises(ValueError, match="Unix 时间戳"):
                core.add_task("OK", "fake", str(tmp_path), scheduled_at=bad)

        future = time.time() + 300
        task = core.add_task(
            "OK", "fake", str(tmp_path), title="valid", scheduled_at=future
        )
        assert task.scheduled_at == pytest.approx(future)
        with pytest.raises(ValueError, match="标题"):
            core.edit_task(task.id, {"title": "bad\rtitle"})
        with pytest.raises(ValueError, match="Unix 时间戳"):
            core.edit_task(task.id, {"scheduled_at": float("-inf")})
        updated = core.edit_task(task.id, {"scheduled_at": None})
        assert updated.scheduled_at is None
    finally:
        core.shutdown()


def test_priority_reorder(core, tmp_path):
    """排队任务可调优先级：move_top / move_up / move_down 只影响 QUEUED 相对顺序。"""
    core.pause_all()  # 保持排队状态
    a = core.add_task("OK a", tool="fake", cwd=str(tmp_path))
    b = core.add_task("OK b", tool="fake", cwd=str(tmp_path))
    c = core.add_task("OK c", tool="fake", cwd=str(tmp_path))

    def queued_ids():
        return [t["id"] for t in core.snapshot()["tasks"] if t["state"] == "queued"]

    assert queued_ids() == [a.id, b.id, c.id]
    ok, _ = core.act(c.id, "move_top")
    assert ok and queued_ids() == [c.id, a.id, b.id]
    ok, _ = core.act(a.id, "move_down")
    assert ok and queued_ids() == [c.id, b.id, a.id]
    ok, _ = core.act(a.id, "move_up")
    assert ok and queued_ids() == [c.id, a.id, b.id]
    # 已在队首继续 move_up：no-op 但不报错
    ok, _ = core.act(c.id, "move_up")
    assert ok and queued_ids() == [c.id, a.id, b.id]
    core.resume_all()


def test_reorder_rejected_for_non_queued(core, tmp_path):
    t = core.add_task("OK", tool="fake", cwd=str(tmp_path))
    wait_for(lambda: _get(core, t.id)["state"] == "succeeded", desc="done")
    ok, msg = core.act(t.id, "move_top")
    assert not ok and "排队" in msg


def test_restart_pauses_running_task_until_user_confirms(settings, tmp_path):
    """崩溃时旧 CLI 可能仍活着，恢复时必须先暂停，不能自动重复执行。"""
    store = StateStore(settings.state_dir)
    store.save({
        "tasks": [{
            "id": "zz1", "title": "crashed", "prompt": "OK", "tool": "fake",
            "cwd": str(tmp_path), "state": "running", "session_id": "fake-sess-1",
            "created_at": time.time(), "attempts": 1,
        }],
        "paused": False,
    })
    core = Scheduler(settings, store)
    try:
        d = next(t for t in core.snapshot()["tasks"] if t["id"] == "zz1")
        assert d["state"] == "paused"
        assert d["resume_next"] is True
        assert "避免重复执行" in d["state_reason"]
        core.start()
        time.sleep(0.1)
        assert next(t for t in core.snapshot()["tasks"] if t["id"] == "zz1")["state"] == "paused"
        assert core.act("zz1", "resume")[0]
        wait_for(lambda: next(
            t for t in core.snapshot()["tasks"] if t["id"] == "zz1"
        )["state"] == "succeeded", desc="confirmed recovered task ran")
        # resume 分支被真实走到（fake_cli 收到 --resume 会输出 RESUMED）
        assert "RESUMED" in core.store.read_log_tail("zz1")
    finally:
        core.shutdown()


def test_shutdown_requeues_running(settings, tmp_path):
    core = Scheduler(settings, StateStore(settings.state_dir))
    core.start()
    t = core.add_task("SLEEP:30", tool="fake", cwd=str(tmp_path))
    wait_for(lambda: _get(core, t.id)["state"] == "running", desc="running")
    core.shutdown()
    data = json.loads((settings.state_dir / "state.json").read_text())
    rec = next(x for x in data["tasks"] if x["id"] == t.id)
    assert rec["state"] == "queued"
    assert "回到队列" in rec["state_reason"]


def test_shutdown_terminates_child_even_when_worker_is_blocked_writing_stdin(
    settings, tmp_path, monkeypatch,
):
    core = Scheduler(settings, StateStore(settings.state_dir))
    adapter = core.registry["fake"]
    monkeypatch.setattr(
        adapter, "build_argv",
        lambda _task, resume, binary: [
            binary, "-c", "import time; time.sleep(30)",
        ],
    )
    monkeypatch.setattr(
        adapter, "stdin_payload", lambda _task, resume: "x" * 2_000_000
    )
    core.start()
    task = core.add_task("OK", "fake", str(tmp_path))

    def child():
        with core._lock:
            run = core._running.get(task.id)
            return run.proc if run and run.proc is not None else None

    proc = wait_for(child, desc="non-reading child launched")
    started = time.monotonic()
    core.shutdown()

    assert time.monotonic() - started < 5
    assert proc.poll() is not None
    assert _get(core, task.id)["state"] == "queued"
    assert task.id not in core._running


def test_shutdown_waits_for_tick_and_prevents_post_shutdown_dispatch(settings, tmp_path):
    core = Scheduler(settings, StateStore(settings.state_dir))
    task = core.add_task("SLEEP:2", "fake", str(tmp_path))
    entered = threading.Event()
    release = threading.Event()
    real_tick = core._tick

    def delayed_tick():
        entered.set()
        assert release.wait(2)
        real_tick()

    core._tick = delayed_tick
    core.start()
    assert entered.wait(2)
    stopped = threading.Event()
    stopper = threading.Thread(target=lambda: (core.shutdown(), stopped.set()))
    stopper.start()
    assert not stopped.wait(0.05)
    release.set()
    assert stopped.wait(2)
    stopper.join(timeout=2)

    assert _get(core, task.id)["state"] == "queued"
    assert task.id not in core._running
    assert core._tick_thread is not None and not core._tick_thread.is_alive()
    core.shutdown()  # idempotent


def test_start_is_idempotent(settings):
    core = Scheduler(settings, StateStore(settings.state_dir))
    core.start()
    tick = core._tick_thread
    quota_thread = core.quota._thread
    core.start()
    assert core._tick_thread is tick
    assert core.quota._thread is quota_thread
    core.shutdown()
    assert tick is not None and not tick.is_alive()
    assert quota_thread is not None and not quota_thread.is_alive()


def test_shutdown_persist_failure_still_completes_lifecycle(
    settings, monkeypatch,
):
    core = Scheduler(settings, StateStore(settings.state_dir))
    core.start()
    tick = core._tick_thread
    quota_thread = core.quota._thread
    monkeypatch.setattr(
        core.store, "save", lambda _data: (_ for _ in ()).throw(OSError("disk full"))
    )

    with pytest.raises(OSError, match="disk full"):
        core.shutdown()

    assert core._shutdown_complete is True
    assert core._stop.is_set()
    assert tick is not None and not tick.is_alive()
    assert quota_thread is not None and not quota_thread.is_alive()
    core.shutdown()  # idempotent even though the final save failed
    with pytest.raises(ValueError, match="正在停止"):
        core.pause_all()


def test_post_shutdown_mutating_apis_are_rejected(settings, tmp_path):
    core = Scheduler(settings, StateStore(settings.state_dir))
    task = core.add_task("OK", "fake", str(tmp_path))
    core.shutdown()

    with pytest.raises(ValueError, match="正在停止"):
        core.add_task("OK", "fake", str(tmp_path))
    with pytest.raises(ValueError, match="正在停止"):
        core.edit_task(task.id, {"title": "must not change"})
    with pytest.raises(ValueError, match="正在停止"):
        core.pause_all()
    with pytest.raises(ValueError, match="正在停止"):
        core.resume_all()
    ok, message = core.act(task.id, "cancel")
    assert not ok and "正在停止" in message
    assert len(core.snapshot()["tasks"]) == 1
    assert _get(core, task.id)["title"] != "must not change"


def test_damaged_state_records_do_not_leave_fake_running_tasks(settings, tmp_path):
    state = {
        "tasks": [
            "not-an-object",
            {
                "id": "../../unsafe",
                "title": "unsafe id",
                "prompt": "p",
                "tool": "fake",
                "cwd": str(tmp_path),
                "state": "running",
            },
            {
                "id": "bad-fields",
                "title": [],
                "prompt": "p",
                "tool": "fake",
                "cwd": str(tmp_path),
                "state": "running",
            },
            {
                "id": "unknown-tool",
                "title": "unknown adapter",
                "prompt": "p",
                "tool": "removed-adapter",
                "cwd": str(tmp_path),
                "state": "running",
            },
            {
                "id": "bad-time",
                "title": "bad timestamp",
                "prompt": "p",
                "tool": "fake",
                "cwd": str(tmp_path),
                "scheduled_at": ["tomorrow"],
            },
            {
                "id": 123,
                "title": "numeric id",
                "prompt": "must not run",
                "tool": "fake",
                "cwd": str(tmp_path),
                "state": "queued",
            },
            {
                "id": "wild-session",
                "title": "glob session",
                "prompt": "must not run",
                "tool": "fake",
                "cwd": str(tmp_path),
                "state": "queued",
                "session_id": "*",
            },
        ],
        "quota": ["not", "a", "mapping"],
        "paused": "false",
    }
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    StateStore(settings.state_dir).save(state)

    core = Scheduler(settings, StateStore(settings.state_dir))
    try:
        tasks = core.snapshot()["tasks"]
        assert len(tasks) == 1
        assert tasks[0]["id"] == "unknown-tool"
        assert tasks[0]["state"] == "failed"
        assert "未知工具" in tasks[0]["state_reason"]
        assert core._running == {}
        assert core.quota.dump() == {}
        assert core.paused is False
    finally:
        core.shutdown()


def test_add_task_persistence_failure_leaves_no_executable_ghost(
    settings, tmp_path, monkeypatch,
):
    core = Scheduler(settings, StateStore(settings.state_dir))
    launched = threading.Event()
    monkeypatch.setattr(core, "_run_task", lambda *_args: launched.set())
    monkeypatch.setattr(
        core.store, "save", lambda _data: (_ for _ in ()).throw(OSError("disk full"))
    )

    with pytest.raises(OSError, match="disk full"):
        core.add_task("OK", "fake", str(tmp_path))
    assert core.snapshot()["tasks"] == []

    core.start()
    try:
        time.sleep(0.1)
        assert not launched.is_set()
        assert core._running == {}
    finally:
        # Restore persistence so shutdown itself can complete normally.
        monkeypatch.undo()
        core.shutdown()


def test_tick_persistence_failure_rolls_back_running_without_worker(
    settings, tmp_path, monkeypatch,
):
    core = Scheduler(settings, StateStore(settings.state_dir))
    task = core.add_task("OK", "fake", str(tmp_path))
    launched = threading.Event()
    monkeypatch.setattr(core, "_run_task", lambda *_args: launched.set())
    monkeypatch.setattr(
        core.store, "save", lambda _data: (_ for _ in ()).throw(OSError("disk full"))
    )

    with pytest.raises(OSError, match="disk full"):
        core._tick()

    current = _get(core, task.id)
    assert current["state"] == "queued"
    assert current["attempts"] == 0
    assert task.id not in core._running
    assert not launched.is_set()

    monkeypatch.undo()
    core.shutdown()


@pytest.mark.parametrize(
    "operation",
    ["edit", "cancel", "pause", "resume", "retry", "reorder", "pause_all", "resume_all"],
)
def test_public_mutation_persistence_failure_rolls_back_memory(
    settings, tmp_path, monkeypatch, operation,
):
    core = Scheduler(settings, StateStore(settings.state_dir))
    first = core.add_task("OK first", "fake", str(tmp_path))
    second = None

    if operation == "resume":
        with core._lock:
            first.state = TaskState.WAITING_QUOTA
            first.next_retry_at = time.time() + 3600
            core._persist_locked()
    elif operation == "retry":
        with core._lock:
            first.state = TaskState.FAILED
            first.finished_at = time.time()
            core._persist_locked()
    elif operation == "reorder":
        second = core.add_task("OK second", "fake", str(tmp_path))
    elif operation == "resume_all":
        core.pause_all()

    def memory_state():
        with core._lock:
            return {
                "tasks": [core._tasks[tid].to_dict() for tid in core._order],
                "order": list(core._order),
                "paused": core._paused,
                "cooldown_bypass": set(core._cooldown_bypass_once),
            }

    before = memory_state()
    monkeypatch.setattr(
        core.store, "save", lambda _data: (_ for _ in ()).throw(OSError("disk full"))
    )

    with pytest.raises(OSError, match="disk full"):
        if operation == "edit":
            core.edit_task(first.id, {"title": "must roll back", "prompt": "changed"})
        elif operation in {"cancel", "pause", "resume", "retry"}:
            core.act(first.id, operation)
        elif operation == "reorder":
            assert second is not None
            core.act(first.id, "move_down")
        elif operation == "pause_all":
            core.pause_all()
        else:
            core.resume_all()

    assert memory_state() == before

    monkeypatch.undo()
    core.shutdown()


def test_waiting_quota_manual_retry_bypasses_cooldown_once(settings, tmp_path):
    core = Scheduler(settings, StateStore(settings.state_dir))
    task = core.add_task("OK", "fake", str(tmp_path))
    reset = time.time() + 3600
    with core._lock:
        task.state = TaskState.WAITING_QUOTA
        task.next_retry_at = reset
        core.quota.record_quota("fake", reset)
        core._persist_locked()

    assert core.act(task.id, "resume")[0]
    core._tick()
    wait_for(lambda: _get(core, task.id)["state"] == "succeeded", desc="forced retry")
    assert _get(core, task.id)["attempts"] == 1
    assert task.id not in core._cooldown_bypass_once
    core.shutdown()


def test_finished_task_pruning_removes_only_retired_logs(
    settings, tmp_path, monkeypatch,
):
    monkeypatch.setattr(scheduler_module, "MAX_FINISHED_KEPT", 2)
    core = Scheduler(settings, StateStore(settings.state_dir))
    tasks = [core.add_task(f"OK {index}", "fake", str(tmp_path)) for index in range(3)]
    for task in tasks:
        with core.store.open_log_append(task.id) as handle:
            handle.write(task.id.encode())

    with core._lock:
        for task in tasks:
            task.state = TaskState.SUCCEEDED
            task.finished_at = time.time()
        core._persist_locked()

    assert tasks[0].id not in core._tasks
    assert not core.store.log_path(tasks[0].id).exists()
    assert core.store.log_path(tasks[1].id).exists()
    assert core.store.log_path(tasks[2].id).exists()
    core.shutdown()
