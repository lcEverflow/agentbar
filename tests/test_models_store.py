import json

from agentbar.models import Task, TaskState, default_title
from agentbar.store import StateStore


def test_task_roundtrip():
    t = Task(id="abc", title="t", prompt="p", tool="claude", cwd="/tmp",
             state=TaskState.WAITING_QUOTA, next_retry_at=123.0, session_id="s1")
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


def test_store_corrupt_backup(tmp_path):
    st = StateStore(tmp_path)
    st.state_path.write_text("{ not json !!!")
    assert st.load() == {}
    assert list(tmp_path.glob("state.json.corrupt-*")), "corrupt file backed up"


def test_log_tail(tmp_path):
    st = StateStore(tmp_path)
    st.log_path("t1").write_bytes(b"A" * 100 + b"TAIL")
    assert st.read_log_tail("t1", max_bytes=10).endswith("TAIL")
    assert st.read_log_tail("missing") == ""
