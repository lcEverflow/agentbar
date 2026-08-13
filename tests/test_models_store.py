import json
import os
import stat
import threading

import pytest

from agentbar.models import Task, TaskState, default_title
from agentbar.store import StateStore


def test_task_roundtrip():
    t = Task(id="abc", title="t", prompt="p", tool="claude", cwd="/tmp",
             state=TaskState.WAITING_QUOTA, next_retry_at=123.0, session_id="s1",
             model="opus", effort="high")
    d = t.to_dict()
    assert d["state"] == "waiting_quota"
    t2 = Task.from_dict(json.loads(json.dumps(d)))
    assert t2 == t


def test_task_unknown_state_degrades():
    t = Task.from_dict({"id": "x", "title": "t", "prompt": "p", "tool": "claude",
                        "cwd": "/tmp", "state": "banana"})
    assert t.state == TaskState.FAILED
    assert "banana" in t.state_reason


def test_default_title():
    assert default_title("hello\nworld") == "hello"
    assert len(default_title("x" * 200)) <= 49


def test_store_roundtrip(tmp_path):
    st = StateStore(tmp_path)
    st.save({"tasks": [1, 2], "paused": True})
    assert st.load() == {"tasks": [1, 2], "paused": True}
    assert stat.S_IMODE(st.state_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(st.logs_dir.stat().st_mode) == 0o700


def test_store_repairs_legacy_private_file_permissions_without_following_symlinks(
    tmp_path,
):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    private_names = (
        "state.json",
        "runtime.json",
        "menu-debug.json",
        "agentbar.log",
        "launchd.stdout.log",
        "launchd.stderr.log",
        "state.json.corrupt-123",
    )
    private_paths = [tmp_path / name for name in private_names]
    private_paths.append(logs_dir / "valid_task-1.log")
    for path in private_paths:
        path.write_text("keep me", encoding="utf-8")
        path.chmod(0o644)

    unrelated = tmp_path / "notes.txt"
    unrelated.write_text("public", encoding="utf-8")
    unrelated.chmod(0o644)
    invalid_log = logs_dir / "not a task!.log"
    invalid_log.write_text("public", encoding="utf-8")
    invalid_log.chmod(0o644)
    symlink_target = tmp_path.parent / f"{tmp_path.name}-outside.log"
    symlink_target.write_text("outside", encoding="utf-8")
    symlink_target.chmod(0o644)
    (logs_dir / "linked.log").symlink_to(symlink_target)

    StateStore(tmp_path)

    for path in private_paths:
        assert path.read_text(encoding="utf-8") == "keep me"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(unrelated.stat().st_mode) == 0o644
    assert stat.S_IMODE(invalid_log.stat().st_mode) == 0o644
    assert stat.S_IMODE(symlink_target.stat().st_mode) == 0o644


def test_store_corrupt_backup(tmp_path):
    st = StateStore(tmp_path)
    st.state_path.write_text("{ not json !!!")
    assert st.load() == {}
    assert list(tmp_path.glob("state.json.corrupt-*")), "corrupt file backed up"


def test_store_non_object_json_is_backed_up_and_ignored(tmp_path):
    st = StateStore(tmp_path)
    st.state_path.write_text("[]", encoding="utf-8")

    assert st.load() == {}
    assert not st.state_path.exists()
    assert list(tmp_path.glob("state.json.corrupt-*")), "non-object state backed up"


def test_log_tail(tmp_path):
    st = StateStore(tmp_path)
    st.log_path("t1").write_bytes(b"A" * 100 + b"TAIL")
    assert st.read_log_tail("t1", max_bytes=10).endswith("TAIL")
    assert st.read_log_tail("missing") == ""


def test_private_log_open_and_task_id_traversal_guard(tmp_path):
    st = StateStore(tmp_path)
    with st.open_log_append("safe_task-1") as handle:
        handle.write(b"secret transcript")
    assert stat.S_IMODE(st.log_path("safe_task-1").stat().st_mode) == 0o600
    with pytest.raises(ValueError):
        st.log_path("../../outside")
    with pytest.raises(ValueError):
        st.log_path(123)


def test_task_log_reads_and_appends_never_follow_symlinks(tmp_path):
    st = StateStore(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("do not expose or modify", encoding="utf-8")
    st.log_path("linked_task").symlink_to(outside)

    assert st.read_log_head("linked_task") == ""
    assert st.read_log_tail("linked_task") == ""
    with pytest.raises(OSError):
        st.open_log_append("linked_task")
    assert outside.read_text(encoding="utf-8") == "do not expose or modify"


def test_delete_log_unlinks_only_the_task_entry(tmp_path):
    st = StateStore(tmp_path / "state")
    outside = tmp_path / "outside.log"
    outside.write_text("keep", encoding="utf-8")
    linked = st.log_path("linked_task")
    linked.symlink_to(outside)

    st.delete_log("linked_task")

    assert not linked.exists()
    assert outside.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    "payload",
    [[], "text", {}, {"port": True}, {"port": "8737"}, {"port": 0}, {"port": 65_536}],
)
def test_runtime_reader_rejects_non_object_or_invalid_port(tmp_path, payload):
    st = StateStore(tmp_path)
    st.runtime_path.write_text(json.dumps(payload), encoding="utf-8")

    assert st.read_runtime() is None


def test_runtime_reader_accepts_valid_port(tmp_path):
    st = StateStore(tmp_path)
    st.runtime_path.write_text(json.dumps({"port": 8737, "pid": 123}), encoding="utf-8")

    assert st.read_runtime() == {"port": 8737, "pid": 123}


def test_runtime_clear_only_removes_the_calling_instance_record(tmp_path):
    st = StateStore(tmp_path)
    st.runtime_path.write_text(
        json.dumps({"port": 9001, "pid": 222, "started_at": 1}),
        encoding="utf-8",
    )

    st.clear_runtime(owner_pid=111)
    assert st.read_runtime()["pid"] == 222

    st.clear_runtime(owner_pid=222)
    assert st.read_runtime() is None


def test_old_instance_clear_cannot_erase_concurrent_replacement_runtime(tmp_path):
    old = StateStore(tmp_path)
    replacement = StateStore(tmp_path)
    old.runtime_path.write_text(
        json.dumps({"port": 8737, "pid": 111, "started_at": 1}),
        encoding="utf-8",
    )
    old_holds_lock = threading.Event()
    release_old = threading.Event()
    real_lock = old._runtime_file_lock

    class PausedLock:
        def __enter__(self):
            self.held = real_lock()
            self.held.__enter__()
            old_holds_lock.set()
            assert release_old.wait(2)
            return self

        def __exit__(self, *exc):
            return self.held.__exit__(*exc)

    old._runtime_file_lock = PausedLock
    clearer = threading.Thread(target=lambda: old.clear_runtime(owner_pid=111))
    writer = threading.Thread(target=lambda: replacement.write_runtime(9002))
    clearer.start()
    assert old_holds_lock.wait(2)
    writer.start()
    assert writer.is_alive()  # blocked behind the old process's check+unlink

    release_old.set()
    clearer.join(timeout=2)
    writer.join(timeout=2)

    runtime = replacement.read_runtime()
    assert runtime is not None
    assert runtime["port"] == 9002
    assert runtime["pid"] == os.getpid()
