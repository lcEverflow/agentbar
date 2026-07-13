"""CLI entry: run the scheduler (menu bar / headless) + client subcommands."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .browser import open_panel_url
from .config import Settings, load_settings
from .models import EFFORTS, PROFILES
from .scheduler import Scheduler
from .server import ApiServer
from .store import StateStore


def _setup_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(settings.state_dir / "agentbar.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


# ---------------- client-side helpers ----------------


def _endpoint(settings: Settings) -> tuple[str, str]:
    store = StateStore(settings.state_dir)
    rt = store.read_runtime()
    port = rt["port"] if rt else settings.port
    return f"http://127.0.0.1:{port}", settings.token


def _request(settings: Settings, method: str, path: str, body: dict | None = None) -> dict:
    base, token = _endpoint(settings)
    req = urllib.request.Request(
        base + path,
        method=method,
        headers={"X-Agentbar-Token": token, "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError):
        print("错误：连不上调度器。请先启动：agentbar run", file=sys.stderr)
        sys.exit(2)


def _instance_alive(settings: Settings) -> bool:
    base, _ = _endpoint(settings)
    try:
        with urllib.request.urlopen(base + "/api/ping", timeout=2) as r:
            return json.loads(r.read().decode()).get("app") == "agentbar"
    except Exception:
        return False


# ---------------- subcommands ----------------


def cmd_run(args, settings: Settings) -> int:
    # 先应用端口覆盖再查重：实例身份 = state dir + 端口，
    # 否则 --port/--state-dir 启动的独立实例会被别的实例误判为"已在运行"
    if args.port is not None:
        settings.port = args.port
    if _instance_alive(settings):
        print("AgentBar 已在运行（用 `agentbar open` 打开面板，或先退出旧实例）")
        return 2
    _setup_logging(settings)
    store = StateStore(settings.state_dir)
    core = Scheduler(settings, store)
    server = ApiServer(core, settings)
    server.start()
    core.start()
    store.write_runtime(server.port)

    print(f"AgentBar v{__version__} 已启动")
    print(f"  状态目录: {settings.state_dir}")
    print(f"  任务面板: {server.url(with_token=True)}")
    print(f"  模式:     {'headless' if args.headless else 'menu bar'}")

    stop_evt = threading.Event()

    def _graceful(*_):
        stop_evt.set()

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)

    if args.headless:
        try:
            while not stop_evt.wait(0.5):
                pass
        finally:
            core.shutdown()
            server.stop()
            store.clear_runtime()
        return 0

    # menu bar 模式：AppKit 事件循环占主线程；信号在后台线程清理后停掉主循环
    from .menubar import AgentBarApp  # 延迟 import，headless/测试不依赖 pyobjc

    app = AgentBarApp(core, settings, server)
    server.hooks["dispatch"] = app.dispatch_async  # /api/debug/dispatch 通道

    def _watch_signal():
        stop_evt.wait()
        core.shutdown()
        server.stop()
        store.clear_runtime()
        app.stop_from_thread()

    threading.Thread(target=_watch_signal, daemon=True).start()
    app.run()
    return 0


def cmd_add(args, settings: Settings) -> int:
    body = {
        "prompt": " ".join(args.prompt),
        "tool": args.tool,
        "cwd": args.cwd,
        "title": args.title,
        "profile": args.profile,
        "model": args.model,
        "effort": args.effort,
    }
    resp = _request(settings, "POST", "/api/tasks", body)
    if not resp.get("ok"):
        print(f"添加失败: {resp.get('error')}", file=sys.stderr)
        return 1
    t = resp["task"]
    choice = f"model={t.get('model') or 'default'}, effort={t.get('effort') or 'default'}"
    print(f"已入队 [{t['id']}] {t['title']}  (tool={t['tool']}, profile={t['profile']}, {choice})")
    return 0


_STATE_ICON = {
    "queued": "·", "running": "▶", "succeeded": "✔", "failed": "✘",
    "waiting_quota": "⏳", "paused": "⏸", "cancelled": "⊘",
}


def cmd_status(args, settings: Settings) -> int:
    s = _request(settings, "GET", "/api/state")
    if not s.get("ok"):
        print(f"错误: {s.get('error')}", file=sys.stderr)
        return 1
    print(f"状态: {s['status']}   排队 {s['queued']}  等额度 {s['waiting_quota']}"
          f"  运行中 {len(s['running_titles'])}")
    for name, qi in s["quota"].items():
        print(f"  {name}: {qi['detail']} (来源: {qi['source']})")
    print()
    for t in s["tasks"][-20:]:
        icon = _STATE_ICON.get(t["state"], "?")
        line = f"  {icon} [{t['id']}] {t['state']:<13} {t['tool']:<6} {t['title']}"
        if t.get("state_reason"):
            line += f"  — {t['state_reason']}"
        print(line)
    return 0


def cmd_open(args, settings: Settings) -> int:
    base, token = _endpoint(settings)
    url = f"{base}/?token={token}"
    print(url)
    if _instance_alive(settings):
        if not open_panel_url(url):
            print("无法调用 macOS 浏览器；请复制上面的链接手动打开。", file=sys.stderr)
            return 1
    else:
        print("（调度器未运行，先执行 agentbar run）")
    return 0


def cmd_pause(args, settings: Settings) -> int:
    _request(settings, "POST", "/api/pause-all")
    print("已暂停全部任务派发")
    return 0


def cmd_resume(args, settings: Settings) -> int:
    _request(settings, "POST", "/api/resume-all")
    print("已恢复任务派发")
    return 0


def cmd_cancel(args, settings: Settings) -> int:
    resp = _request(settings, "POST", f"/api/tasks/{args.task_id}/cancel")
    print(resp.get("message") or resp.get("error"))
    return 0 if resp.get("ok") else 1


def cmd_log(args, settings: Settings) -> int:
    resp = _request(settings, "GET", f"/api/tasks/{args.task_id}/log")
    print(resp.get("log") or "(空)")
    return 0


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="agentbar", description="macOS 状态栏 AI Agent 调度器"
    )
    p.add_argument("--state-dir", help="状态目录（默认 ~/.agentbar，或环境变量 AGENTBAR_STATE_DIR）")
    p.add_argument("--version", action="version", version=f"agentbar {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("run", help="启动调度器（默认带菜单栏）")
    sp.add_argument("--headless", action="store_true", help="无菜单栏模式（服务器/调试）")
    sp.add_argument("--port", type=int, help=f"API 端口（默认取配置）")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("add", help="添加任务")
    sp.add_argument("--tool", default="claude", help="claude | codex（默认 claude）")
    sp.add_argument("--cwd", default=os.getcwd(), help="工作目录（默认当前目录）")
    sp.add_argument("--profile", default="edits", choices=PROFILES)
    sp.add_argument("--model", help="模型 ID；不填时使用对应 CLI 默认模型")
    sp.add_argument("--effort", choices=EFFORTS, help="推理强度；不填时使用 CLI 默认")
    sp.add_argument("--title", default=None)
    sp.add_argument("prompt", nargs="+", help="任务 prompt")
    sp.set_defaults(fn=cmd_add)

    for name, fn, help_ in (
        ("status", cmd_status, "查看调度器状态与任务列表"),
        ("open", cmd_open, "打开任务面板（带令牌）"),
        ("pause", cmd_pause, "暂停全部"),
        ("resume", cmd_resume, "恢复全部"),
    ):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)

    sp = sub.add_parser("cancel", help="取消任务")
    sp.add_argument("task_id")
    sp.set_defaults(fn=cmd_cancel)

    sp = sub.add_parser("log", help="查看任务日志")
    sp.add_argument("task_id")
    sp.set_defaults(fn=cmd_log)

    args = p.parse_args(argv)
    settings = load_settings(Path(args.state_dir) if args.state_dir else None)
    sys.exit(args.fn(args, settings) or 0)
