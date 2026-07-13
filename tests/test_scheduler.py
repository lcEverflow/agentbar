import json
import time

import pytest

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


def test_restart_recovers_running_task(settings, tmp_path):
    """模拟崩溃：state.json 里有 RUNNING 任务 → 新调度器把它重新排队并恢复会话。"""
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
        assert d["state"] == "queued"
        assert d["resume_next"] is True
        core.start()
        wait_for(
            lambda: next(t for t in core.snapshot()["tasks"] if t["id"] == "zz1")["state"]
            == "succeeded",
            desc="recovered task ran",
        )
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
