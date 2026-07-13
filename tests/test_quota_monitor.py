import time

from agentbar.config import Settings
from agentbar.quota import QuotaMonitor


def _m(tmp_path):
    return QuotaMonitor(Settings(state_dir=tmp_path))


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
