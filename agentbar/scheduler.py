"""Scheduler core: queue, dispatch, lifecycle, quota-wait, persistence, recovery.

无 GUI 依赖，可独立测试。所有状态变更都在锁内完成并立即落盘（state.json 原子写），
调度器/机器随时挂掉都能从磁盘恢复。
"""

from __future__ import annotations

import logging
import os
import random
import re
import signal
import subprocess
import threading
import time

from . import __version__
from .adapters.base import Outcome, get_registry
from .config import Settings
from .models import (
    EFFORTS,
    FINISHED_STATES,
    PROFILES,
    Task,
    TaskState,
    default_title,
    new_id,
)
from .quota import QuotaMonitor
from .store import StateStore

log = logging.getLogger("agentbar.scheduler")

MAX_FINISHED_KEPT = 200
# codex 旧版本没有 `exec resume` 子命令时的报错特征 → 降级为全新执行
_RESUME_UNSUPPORTED_RE = re.compile(
    r"unrecognized subcommand|unexpected argument", re.IGNORECASE
)
# 子进程环境里剔除嵌套 Claude Code 会话标记（在 Claude Code 里调试本项目时避免干扰）
_ENV_STRIP = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")


def _clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


class _Run:
    """一个正在运行的任务的进程句柄与控制位。"""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.cancel = threading.Event()
        self.mode = "cancel"  # "cancel"(用户取消) | "interrupt"(调度器退出，回队列)
        self.thread: threading.Thread | None = None
        self.started = threading.Event()
        self.was_resume = False


class Scheduler:
    def __init__(self, settings: Settings, store: StateStore):
        self.settings = settings
        self.store = store
        self.registry = get_registry(settings)
        self.quota = QuotaMonitor(settings)
        self._lock = threading.RLock()
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []
        self._running: dict[str, _Run] = {}
        self._paused = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._tick_thread: threading.Thread | None = None
        self._load()

    # ================= persistence & recovery =================

    def _load(self) -> None:
        data = self.store.load()
        for d in data.get("tasks", []):
            try:
                t = Task.from_dict(d)
            except (TypeError, ValueError):
                log.warning("skip unparsable task record: %r", d)
                continue
            if t.state == TaskState.RUNNING:
                # 上个进程死掉时正在运行的任务 → 重新排队（能续会话就续）
                t.state = TaskState.QUEUED
                t.resume_next = bool(t.session_id)
                t.state_reason = "调度器重启，已重新排队" + (
                    "（将恢复会话）" if t.session_id else ""
                )
            self._tasks[t.id] = t
            self._order.append(t.id)
        self._paused = bool(data.get("paused", False))
        self.quota.load(data.get("quota", {}))

    def _persist_locked(self) -> None:
        finished = [
            tid for tid in self._order if self._tasks[tid].state in FINISHED_STATES
        ]
        if len(finished) > MAX_FINISHED_KEPT:
            for tid in finished[: len(finished) - MAX_FINISHED_KEPT]:
                self._order.remove(tid)
                self._tasks.pop(tid, None)
        self.store.save(
            {
                "version": __version__,
                "tasks": [self._tasks[tid].to_dict() for tid in self._order],
                "paused": self._paused,
                "quota": self.quota.dump(),
            }
        )

    # ================= lifecycle =================

    def start(self) -> None:
        self.quota.start_background()
        self._tick_thread = threading.Thread(
            target=self._tick_loop, name="agentbar-tick", daemon=True
        )
        self._tick_thread.start()

    def shutdown(self) -> None:
        """优雅退出：终止在跑的 CLI 进程，把任务放回队列（带 resume），落盘。"""
        self._stop.set()
        self._wake.set()
        with self._lock:
            runs = list(self._running.items())
        for _tid, run in runs:
            run.mode = "interrupt"
            run.cancel.set()
        for _tid, run in runs:
            if run.thread:
                # `_tick` stores the Thread immediately before `.start()`;
                # shutdown can win that tiny race. Wait for the worker's first
                # instruction before joining so Python never sees an unstarted
                # Thread object.
                run.started.wait(timeout=1)
                if run.thread.ident is not None:
                    run.thread.join(timeout=20)
        self.quota.stop()
        with self._lock:
            self._persist_locked()

    # ================= public API =================

    @property
    def paused(self) -> bool:
        return self._paused

    def add_task(
        self,
        prompt: str,
        tool: str,
        cwd: str,
        title: str | None = None,
        profile: str = "edits",
        model: str | None = None,
        effort: str | None = None,
    ) -> Task:
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("prompt 不能为空")
        if len(prompt) > 100_000:
            raise ValueError("prompt 过长（>100KB）")
        if tool not in self.registry:
            raise ValueError(f"未知工具 {tool!r}，可用: {sorted(self.registry)}")
        if profile not in PROFILES:
            raise ValueError(f"未知权限档位 {profile!r}，可用: {PROFILES}")
        if profile == "full" and not self.settings.allow_full_profile:
            raise ValueError(
                "高权限档位默认关闭。如确需开启，编辑 config.json 设置 "
                "allow_full_profile=true 后重启"
            )
        model = (model or "").strip() or None
        if model and (len(model) > 120 or any(c in model for c in "\r\n\x00")):
            raise ValueError("模型名称无效（最多 120 个字符，不能包含换行）")
        effort = (effort or "").strip().lower() or None
        if effort and effort not in EFFORTS:
            raise ValueError(f"未知强度档位 {effort!r}，可用: {EFFORTS}")
        cwd = os.path.abspath(os.path.expanduser(cwd or self.settings.default_cwd))
        if not os.path.isdir(cwd):
            raise ValueError(f"工作目录不存在: {cwd}")
        t = Task(
            id=new_id(),
            title=(title or "").strip() or default_title(prompt),
            prompt=prompt,
            tool=tool,
            cwd=cwd,
            profile=profile,
            model=model,
            effort=effort,
        )
        with self._lock:
            self._tasks[t.id] = t
            self._order.append(t.id)
            self._persist_locked()
        self._wake.set()
        log.info("task %s added (%s, %s)", t.id, tool, t.title)
        return t

    def edit_task(self, task_id: str, changes: dict) -> Task:
        """Edit a task that has not started running.

        A live CLI process is intentionally immutable: changing its prompt or
        safety profile would make the displayed task diverge from the command
        already executing. Users can cancel a running task and create/retry a
        replacement instead.
        """
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                raise ValueError("任务不存在")
            if t.state == TaskState.RUNNING:
                raise ValueError("运行中的任务不能编辑；请先取消，再新建或重试")
            if t.state not in {
                TaskState.QUEUED, TaskState.PAUSED, TaskState.WAITING_QUOTA,
            }:
                raise ValueError(f"状态 {t.state.value} 的任务不能编辑；请使用重试创建新运行")

            prompt = (changes.get("prompt", t.prompt) or "").strip()
            if not prompt:
                raise ValueError("prompt 不能为空")
            if len(prompt) > 100_000:
                raise ValueError("prompt 过长（>100KB）")

            tool = changes.get("tool", t.tool)
            if tool not in self.registry:
                raise ValueError(f"未知工具 {tool!r}，可用: {sorted(self.registry)}")

            profile = changes.get("profile", t.profile)
            if profile not in PROFILES:
                raise ValueError(f"未知权限档位 {profile!r}，可用: {PROFILES}")
            if profile == "full" and not self.settings.allow_full_profile:
                raise ValueError("高权限档位默认关闭。如确需开启，编辑 config.json 设置 allow_full_profile=true 后重启")

            model = (changes.get("model", t.model) or "").strip() or None
            if model and (len(model) > 120 or any(c in model for c in "\r\n\x00")):
                raise ValueError("模型名称无效（最多 120 个字符，不能包含换行）")
            effort = (changes.get("effort", t.effort) or "").strip().lower() or None
            if effort and effort not in EFFORTS:
                raise ValueError(f"未知强度档位 {effort!r}，可用: {EFFORTS}")

            cwd = os.path.abspath(os.path.expanduser(changes.get("cwd", t.cwd) or self.settings.default_cwd))
            if not os.path.isdir(cwd):
                raise ValueError(f"工作目录不存在: {cwd}")

            title = (changes.get("title", t.title) or "").strip() or default_title(prompt)
            t.prompt, t.tool, t.cwd = prompt, tool, cwd
            t.title, t.profile, t.model, t.effort = title, profile, model, effort
            if t.state == TaskState.WAITING_QUOTA:
                t.state = TaskState.QUEUED
                t.next_retry_at = None
                t.state_reason = "编辑后重新入队"
            self._persist_locked()
        self._wake.set()
        log.info("task %s edited", task_id)
        return t

    def act(self, task_id: str, action: str) -> tuple[bool, str]:
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return False, "任务不存在"
            s = t.state
            if action == "cancel":
                if s == TaskState.RUNNING:
                    run = self._running.get(task_id)
                    if run:
                        run.mode = "cancel"
                        run.cancel.set()
                    return True, "正在终止进程…"
                if s in (TaskState.QUEUED, TaskState.PAUSED, TaskState.WAITING_QUOTA):
                    t.state = TaskState.CANCELLED
                    t.finished_at = time.time()
                    t.state_reason = "用户取消"
                    self._persist_locked()
                    return True, "已取消"
                return False, f"状态 {s.value} 不可取消"
            if action == "pause":
                if s in (TaskState.QUEUED, TaskState.WAITING_QUOTA):
                    t.state = TaskState.PAUSED
                    t.state_reason = "人工暂停"
                    self._persist_locked()
                    return True, "已暂停"
                return False, f"状态 {s.value} 不可暂停（运行中请用取消）"
            if action == "resume":
                if s == TaskState.PAUSED:
                    t.state = TaskState.QUEUED
                    t.state_reason = "人工恢复"
                elif s == TaskState.WAITING_QUOTA:
                    t.state = TaskState.QUEUED
                    t.next_retry_at = None
                    t.state_reason = "人工触发立即重试"
                else:
                    return False, f"状态 {s.value} 不可恢复"
                self._persist_locked()
                self._wake.set()
                return True, "已恢复"
            if action == "retry":
                if s in FINISHED_STATES:
                    t.state = TaskState.QUEUED
                    t.finished_at = None
                    t.exit_code = None
                    t.next_retry_at = None
                    t.resume_next = bool(t.session_id)
                    t.state_reason = "人工重试"
                    self._persist_locked()
                    self._wake.set()
                    return True, "已重新入队"
                return False, f"状态 {s.value} 不可重试"
            return False, f"未知操作 {action!r}"

    def pause_all(self) -> None:
        with self._lock:
            self._paused = True
            self._persist_locked()
        log.info("pause_all")

    def resume_all(self) -> None:
        with self._lock:
            self._paused = False
            self._persist_locked()
        self._wake.set()
        log.info("resume_all")

    def snapshot(self) -> dict:
        with self._lock:
            tasks = [self._tasks[tid].to_dict() for tid in self._order]
            running = [
                self._tasks[tid].title
                for tid in self._running
                if tid in self._tasks
            ]
            n_queued = sum(1 for t in tasks if t["state"] == "queued")
            n_waiting = sum(1 for t in tasks if t["state"] == "waiting_quota")
            paused = self._paused
        if paused:
            status = "paused"
        elif running:
            status = "running"
        elif n_waiting or n_queued:
            status = "waiting"
        else:
            status = "idle"
        return {
            "version": __version__,
            "status": status,
            "paused": paused,
            "running_titles": running,
            "queued": n_queued,
            "waiting_quota": n_waiting,
            "tasks": tasks,
            "quota": {name: self.quota.status(name).to_dict() for name in self.registry},
            "cli_processes": self._cli_processes_snapshot(),
            "settings": {
                "max_parallel": self.settings.max_parallel,
                "per_tool_limit": self.settings.per_tool_limit,
                "usage_refresh_seconds": self.settings.usage_refresh_seconds,
                "default_cwd": self.settings.default_cwd,
                "allow_full_profile": self.settings.allow_full_profile,
                "state_dir": str(self.settings.state_dir),
            },
        }

    def _cli_processes_snapshot(self) -> list[dict]:
        """返回本机 Claude/Codex 的脱敏进程观测；扫描失败不能影响调度。"""
        try:
            from .processes import discover_cli_processes

            with self._lock:
                owned = {
                    run.proc.pid: {
                        "tool": self._tasks[tid].tool,
                        "task_id": tid,
                        "title": self._tasks[tid].title,
                        "cwd": self._tasks[tid].cwd,
                    }
                    for tid, run in self._running.items()
                    if run.proc and run.proc.poll() is None and tid in self._tasks
                }
            return discover_cli_processes(owned)
        except Exception:
            log.debug("CLI process scan failed", exc_info=True)
            return []

    # ================= tick loop =================

    def _tick_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("tick failed")
            self._wake.wait(self.settings.tick_seconds)
            self._wake.clear()

    def _iter_tasks(self):
        for tid in list(self._order):
            t = self._tasks.get(tid)
            if t:
                yield t

    def _tool_running(self, tool: str) -> int:
        return sum(
            1
            for tid in self._running
            if tid in self._tasks and self._tasks[tid].tool == tool
        )

    def _tick(self) -> None:
        to_start: list[tuple[str, _Run]] = []
        with self._lock:
            now = time.time()
            dirty = False
            # 1) 额度等待期结束 → 回到队列
            for t in self._iter_tasks():
                if (
                    t.state == TaskState.WAITING_QUOTA
                    and t.next_retry_at
                    and t.next_retry_at <= now
                ):
                    t.state = TaskState.QUEUED
                    t.state_reason = "额度等待结束，重新排队"
                    dirty = True
            # 2) FIFO 派发（跳过被额度冷却/并发上限卡住的任务）
            if not self._paused:
                for t in self._iter_tasks():
                    if t.state != TaskState.QUEUED:
                        continue
                    if len(self._running) >= self.settings.max_parallel:
                        break
                    if self._tool_running(t.tool) >= self.settings.per_tool_limit:
                        continue
                    cd = self.quota.cooldown_until(t.tool)
                    if cd and cd > now:
                        continue
                    run = _Run()
                    run.was_resume = bool(t.resume_next and t.session_id)
                    self._running[t.id] = run
                    t.state = TaskState.RUNNING
                    t.started_at = now
                    t.finished_at = None
                    t.attempts += 1
                    t.state_reason = "执行中"
                    to_start.append((t.id, run))
                    dirty = True
            if dirty:
                self._persist_locked()
        for tid, run in to_start:
            th = threading.Thread(
                target=self._run_task, args=(tid, run), name=f"agentbar-run-{tid}",
                daemon=True,
            )
            run.thread = th
            th.start()

    # ================= task execution =================

    def _run_task(self, task_id: str, run: _Run) -> None:
        run.started.set()
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                self._running.pop(task_id, None)
                return
            adapter = self.registry[t.tool]
            resume = run.was_resume
            timeout = self.settings.task_timeout_seconds
            cwd = t.cwd

        log_path = self.store.log_path(task_id)
        binary = adapter.binary()
        if not binary:
            self._finalize(
                task_id, run,
                Outcome("failure",
                        f"未找到 {t.tool} 可执行文件；请安装，或在 config.json 的 "
                        f"tool_paths 中指定绝对路径"),
                None, None, "",
            )
            return

        argv = adapter.build_argv(t, resume=resume, binary=binary)
        payload = adapter.stdin_payload(t, resume=resume)
        env = os.environ.copy()
        for k in _ENV_STRIP:
            env.pop(k, None)

        rc: int | None = None
        try:
            with open(log_path, "ab") as lf:
                shown = [a if len(a) < 200 else a[:200] + "…" for a in argv]
                header = (
                    f"\n===== attempt {t.attempts} @ "
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"(resume={resume}) =====\n$ {' '.join(shown)}\n"
                )
                lf.write(header.encode())
                lf.flush()
                proc = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    env=env,
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
                    start_new_session=True,  # 独立进程组，方便整组终止
                )
                run.proc = proc
                if payload is not None and proc.stdin:
                    try:
                        proc.stdin.write(payload.encode())
                        proc.stdin.close()
                    except (BrokenPipeError, OSError):
                        pass
                deadline = time.time() + timeout
                while True:
                    rc = proc.poll()
                    if rc is not None:
                        break
                    if run.cancel.is_set():
                        self._terminate(proc)
                        if run.mode == "interrupt":
                            self._finalize(
                                task_id, run,
                                Outcome("interrupted", "调度器退出，任务已回到队列"),
                                None, None, "",
                            )
                        else:
                            self._finalize(
                                task_id, run,
                                Outcome("cancelled", "用户取消，进程已终止"),
                                None, None, "",
                            )
                        return
                    if time.time() > deadline:
                        self._terminate(proc)
                        self._finalize(
                            task_id, run,
                            Outcome("failure", f"超时（>{timeout}s），已终止"),
                            124, None, "",
                        )
                        return
                    time.sleep(0.2)
        except Exception as e:  # Popen 失败等
            self._finalize(
                task_id, run, Outcome("failure", f"启动失败: {e}"), None, None, ""
            )
            return

        tail = self.store.read_log_tail(task_id)
        outcome = adapter.classify(rc, tail)
        sid = adapter.extract_session_id(tail)
        self._finalize(task_id, run, outcome, rc, sid, tail)

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        """SIGTERM 整个进程组，8s 后仍活着则 SIGKILL。"""
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, OSError):
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            return
        deadline = time.time() + 8
        while time.time() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.1)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        proc.wait(timeout=5)

    def _backoff_seconds(self, quota_waits: int) -> float:
        mins = self.settings.backoff_minutes or [5]
        m = mins[min(quota_waits - 1, len(mins) - 1)]
        return m * 60 * (0.9 + 0.2 * random.random())

    def _finalize(
        self,
        task_id: str,
        run: _Run,
        outcome: Outcome,
        rc: int | None,
        sid: str | None,
        tail: str,
    ) -> None:
        with self._lock:
            self._running.pop(task_id, None)
            t = self._tasks.get(task_id)
            if not t:
                return
            now = time.time()
            t.exit_code = rc
            t.finished_at = now
            if sid:
                t.session_id = sid
            if outcome.cost_usd is not None:
                t.cost_usd = outcome.cost_usd

            kind = outcome.kind
            # codex 旧版不支持 exec resume → 放弃会话恢复，降级为全新执行一次
            if (
                kind == "failure"
                and run.was_resume
                and _RESUME_UNSUPPORTED_RE.search(tail or "")
            ):
                t.session_id = None
                t.resume_next = False
                t.state = TaskState.QUEUED
                t.finished_at = None
                t.state_reason = "该 CLI 版本不支持会话恢复，已降级为全新执行"
                self._persist_locked()
                self._wake.set()
                return

            if kind == "success":
                t.state = TaskState.SUCCEEDED
                t.resume_next = False
                t.next_retry_at = None
                t.state_reason = outcome.reason or "完成"
                self.quota.record_success(t.tool)
            elif kind == "quota":
                t.quota_waits += 1
                delay = self._backoff_seconds(t.quota_waits)
                reset = (
                    outcome.reset_at
                    if outcome.reset_at and outcome.reset_at > now
                    else now + delay
                )
                t.state = TaskState.WAITING_QUOTA
                t.next_retry_at = reset
                t.finished_at = None
                t.resume_next = bool(t.session_id)
                t.state_reason = f"额度受限，{_clock(reset)} 自动重试"
                self.quota.record_quota(t.tool, reset)
            elif kind == "cancelled":
                t.state = TaskState.CANCELLED
                t.state_reason = outcome.reason
            elif kind == "interrupted":
                t.state = TaskState.QUEUED
                t.finished_at = None
                t.resume_next = bool(t.session_id)
                t.state_reason = outcome.reason
            else:
                t.state = TaskState.FAILED
                t.state_reason = outcome.reason or f"退出码 {rc}"
            log.info("task %s -> %s (%s)", task_id, t.state.value, t.state_reason)
            self._persist_locked()
        self._wake.set()
