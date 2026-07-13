from agentbar.adapters.claude import ClaudeAdapter
from agentbar.adapters.codex import CodexAdapter
from agentbar.config import Settings
from agentbar.models import Task


def _task(tool, profile="edits", session=None):
    return Task(id="t1", title="t", prompt="do things", tool=tool, cwd="/tmp",
                profile=profile, session_id=session)


def test_claude_default_is_safe(tmp_path):
    a = ClaudeAdapter(Settings(state_dir=tmp_path))
    argv = a.build_argv(_task("claude"), resume=False, binary="claude")
    assert "--dangerously-skip-permissions" not in argv
    assert "--permission-mode" in argv and "acceptEdits" in argv
    # prompt 不在 argv 里（走 stdin，防 flag 注入）
    assert "do things" not in argv
    assert a.stdin_payload(_task("claude"), resume=False) == "do things"


def test_claude_readonly_blocks_write_tools(tmp_path):
    a = ClaudeAdapter(Settings(state_dir=tmp_path))
    argv = a.build_argv(_task("claude", "readonly"), resume=False, binary="claude")
    i = argv.index("--disallowedTools")
    assert "Bash" in argv[i + 1] and "Write" in argv[i + 1]
    assert "--dangerously-skip-permissions" not in argv


def test_claude_full_only_when_asked(tmp_path):
    a = ClaudeAdapter(Settings(state_dir=tmp_path))
    argv = a.build_argv(_task("claude", "full"), resume=False, binary="claude")
    assert "--dangerously-skip-permissions" in argv


def test_claude_resume(tmp_path):
    a = ClaudeAdapter(Settings(state_dir=tmp_path))
    t = _task("claude", session="sess-9")
    argv = a.build_argv(t, resume=True, binary="claude")
    assert argv[argv.index("--resume") + 1] == "sess-9"
    assert "原始任务" in a.stdin_payload(t, resume=True)


def test_codex_sandbox_flags(tmp_path):
    a = CodexAdapter(Settings(state_dir=tmp_path))
    ro = a.build_argv(_task("codex", "readonly"), resume=False, binary="codex")
    assert ["--sandbox", "read-only"] == ro[ro.index("--sandbox"):ro.index("--sandbox") + 2]
    ed = a.build_argv(_task("codex"), resume=False, binary="codex")
    assert "workspace-write" in ed
    assert "--dangerously-bypass-approvals-and-sandbox" not in ed
    full = a.build_argv(_task("codex", "full"), resume=False, binary="codex")
    assert "--dangerously-bypass-approvals-and-sandbox" in full


def test_codex_resume_subcommand(tmp_path):
    a = CodexAdapter(Settings(state_dir=tmp_path))
    t = _task("codex", session="0199-abc")
    argv = a.build_argv(t, resume=True, binary="codex")
    assert argv[1:4] == ["exec", "resume", "0199-abc"]
    assert argv[-1] == "-"  # prompt 走 stdin
