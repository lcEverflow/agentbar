from __future__ import annotations

import os
import json
import plistlib
import re
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


@pytest.mark.parametrize("script", sorted(SCRIPTS.glob("*.sh")))
def test_release_shell_scripts_parse(script: Path):
    subprocess.run(["/bin/bash", "-n", str(script)], check=True)


def test_restart_and_uninstall_never_use_global_process_matchers():
    lifecycle = "\n".join(
        (SCRIPTS / name).read_text(encoding="utf-8")
        for name in (
            "agentbar-restart.sh",
            "uninstall-launch-agent.sh",
            "launch-agent-common.sh",
        )
    )
    assert "pkill" not in lifecycle
    assert "pgrep" not in lifecycle
    assert "kill -9" not in lifecycle
    assert "SIGKILL" in lifecycle  # documented as deliberately forbidden
    assert "runtime.json" in lifecycle


def test_installer_is_atomic_and_has_failure_rollback():
    installer = (SCRIPTS / "install-launch-agent.sh").read_text(encoding="utf-8")
    common = (SCRIPTS / "launch-agent-common.sh").read_text(encoding="utf-8")
    assert "candidate" in installer
    assert "rollback_install" in installer
    assert "agentbar_wait_healthy" in installer
    assert "plutil -create" in common
    assert "cat > \"$PLIST\"" not in installer


def test_dmg_build_uses_locked_dependencies_and_cleans_temporary_outputs():
    build = (SCRIPTS / "build-dmg.sh").read_text(encoding="utf-8")
    assert "uv.lock" in build
    assert '"$UV_BIN" export' in build
    assert "--locked" in build
    assert "trap cleanup EXIT" in build
    assert "--bundle-smoke" in build
    assert "hdiutil verify" in build
    assert "codesign --verify" in build


def test_release_version_sources_match():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    init_text = (ROOT / "agentbar" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__ = "([^"]+)"', init_text)
    assert match is not None
    assert match.group(1) == project["project"]["version"]


def test_py2app_declares_lazy_runtime_dependencies():
    setup_text = (ROOT / "packaging" / "py2app_setup.py").read_text(encoding="utf-8")
    entry_text = (ROOT / "packaging" / "AgentBar.py").read_text(encoding="utf-8")
    for dependency in ("mistune", "qrcode", "websocket", "WebKit"):
        assert f'"{dependency}"' in setup_text
    assert "index.html" in entry_text
    assert "mobile.html" in entry_text
    assert "--bundle-smoke" in entry_text


@pytest.mark.parametrize("source", sorted((ROOT / "packaging").glob("*.py")))
def test_packaging_python_sources_compile(source: Path):
    compile(source.read_text(encoding="utf-8"), str(source), "exec")


@pytest.mark.skipif(sys.platform != "darwin", reason="uses macOS plutil")
def test_plist_renderer_round_trips_special_characters(tmp_path: Path):
    destination = tmp_path / "launch agent.plist"
    uv_bin = str(tmp_path / 'uv & <binary> "quoted"')
    project_dir = str(tmp_path / 'Project & <source> "quoted"')
    state_dir = str(tmp_path / 'State & <private> "quoted"')
    subprocess.run(
        [
            "/bin/bash",
            "-c",
            'source "$1"; agentbar_render_launch_agent_plist "$2" "$3" "$4" "$5" "$6"',
            "bash",
            str(SCRIPTS / "launch-agent-common.sh"),
            str(destination),
            "com.agentbar.app",
            uv_bin,
            project_dir,
            state_dir,
        ],
        check=True,
    )
    with destination.open("rb") as stream:
        plist = plistlib.load(stream)
    assert plist["ProgramArguments"] == [
        uv_bin,
        "run",
        "--locked",
        "--project",
        project_dir,
        "agentbar",
        "run",
    ]
    assert plist["StandardOutPath"] == f"{state_dir}/launchd.stdout.log"
    assert plist["StandardErrorPath"] == f"{state_dir}/launchd.stderr.log"
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["ExitTimeOut"] == 45
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform != "darwin", reason="uses macOS plutil")
def test_runtime_cleanup_unlinks_a_symlink_without_touching_its_target(tmp_path: Path):
    target = tmp_path / "unrelated.json"
    target.write_text('{"pid": 999999, "port": 8737}', encoding="utf-8")
    runtime = tmp_path / "runtime.json"
    runtime.symlink_to(target)
    subprocess.run(
        [
            "/bin/bash",
            "-c",
            'source "$1"; agentbar_remove_runtime_if_owned "$2" 999999',
            "bash",
            str(SCRIPTS / "launch-agent-common.sh"),
            str(runtime),
        ],
        check=True,
    )
    assert not os.path.lexists(runtime)
    assert target.read_text(encoding="utf-8") == '{"pid": 999999, "port": 8737}'


@pytest.mark.skipif(sys.platform != "darwin", reason="uses macOS plutil/ps")
def test_runtime_stop_targets_only_the_recorded_agentbar_pid(tmp_path: Path):
    executable = tmp_path / "agentbar"
    executable.write_text(
        "#!/bin/bash\ntrap 'exit 0' TERM\nwhile true; do sleep 0.1; done\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    process = subprocess.Popen([str(executable), "run"])
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"pid": process.pid, "port": 8737}), encoding="utf-8")
    try:
        result = subprocess.run(
            [
                "/bin/bash",
                "-c",
                'source "$1"; agentbar_stop_runtime_instance "$2" 3 "$3"',
                "bash",
                str(SCRIPTS / "launch-agent-common.sh"),
                str(runtime),
                str(process.pid),
            ],
            timeout=5,
            check=False,
        )
        assert result.returncode == 0
        # TERM may arrive before the tiny helper installs its trap; either a
        # clean zero exit or direct SIGTERM proves the exact PID was stopped.
        assert process.wait(timeout=1) in (0, -15)
        assert not runtime.exists()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)


@pytest.mark.skipif(sys.platform != "darwin", reason="uses macOS plutil/ps")
def test_runtime_stop_refuses_an_unrelated_reused_pid(tmp_path: Path):
    process = subprocess.Popen(["/bin/sleep", "30"])
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"pid": process.pid, "port": 8737}), encoding="utf-8")
    try:
        result = subprocess.run(
            [
                "/bin/bash",
                "-c",
                'source "$1"; agentbar_stop_runtime_instance "$2" 1 "$3"',
                "bash",
                str(SCRIPTS / "launch-agent-common.sh"),
                str(runtime),
                str(process.pid),
            ],
            timeout=3,
            check=False,
            capture_output=True,
        )
        assert result.returncode != 0
        assert b"PID" in result.stderr
        assert process.poll() is None
        assert runtime.exists()
    finally:
        process.terminate()
        process.wait(timeout=2)
