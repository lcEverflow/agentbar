"""Scheduler core: queue, dispatch, lifecycle, quota-wait, persistence, recovery.

无 GUI 依赖，可独立测试。所有状态变更都在锁内完成并立即落盘（state.json 原子写），
调度器/机器随时挂掉都能从磁盘恢复。
"""

from __future__ import annotations

import logging
import math
import os
import random
import re
import signal
import subprocess
import threading
import time

from . import __version__
from .adapters.base import Outcome, get_registry
from .config import DEFAULT_PROVIDERS, DEFAULT_QUOTA_SOURCES, Settings
from .models import (
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


def _validated_title(value, prompt: str) -> str:
    if value is None or value == "":
        return default_title(prompt)
    if not isinstance(value, str):
        raise ValueError("标题必须是字符串")
    if len(value) > 200 or any(char in value for char in "\r\n\x00"):
        raise ValueError("标题无效（最多 200 个字符，不能包含换行或 NUL）")
    return value.strip() or default_title(prompt)


def _validated_schedule(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("定时执行时间必须是有限的 Unix 时间戳")
    scheduled = float(value)
    if not math.isfinite(scheduled):
        raise ValueError("定时执行时间必须是有限的 Unix 时间戳")
    return scheduled if scheduled > time.time() else None


def _optional_text(value, label: str, max_len: int) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label}必须是字符串")
    cleaned = value.strip()
    if len(cleaned) > max_len or any(char in cleaned for char in "\r\n\x00"):
        raise ValueError(f"{label}无效（最多 {max_len} 个字符，不能包含换行或 NUL）")
    return cleaned or None


def _valid_optional_number(value) -> bool:
    return value is None or (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) >= 0
    )


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
        # A user-triggered "立即重试" bypasses the shared tool cooldown exactly
        # once, without clearing the observation for other queued tasks.
        self._cooldown_bypass_once: set[str] = set()
        self._paused = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._tick_thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()
        self._started = False
        self._shutdown_complete = False
        self._load()

    # ================= persistence & recovery =================

    def _load(self) -> None:
        data = self.store.load()
        raw_tasks = data.get("tasks", [])
        if not isinstance(raw_tasks, list):
            raw_tasks = []
        for d in raw_tasks:
            if not isinstance(d, dict):
                log.warning("skip non-object task record")
                continue
            try:
                t = Task.from_dict(d)
                # IDs become log filenames and API path components. Reject
                # traversal/control characters before they enter scheduler state.
                StateStore._safe_task_id(t.id)
                if not all(
                    isinstance(value, str) and value
                    for value in (t.title, t.prompt, t.tool, t.cwd, t.profile)
                ):
                    raise ValueError("invalid core task fields")
                if t.model is not None and not isinstance(t.model, str):
                    raise ValueError("invalid task model")
                if t.effort is not None and not isinstance(t.effort, str):
                    raise ValueError("invalid task effort")
                if not isinstance(t.resume_next, bool):
                    raise ValueError("invalid task resume flag")
                if any(
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                    for value in (t.attempts, t.quota_waits)
                ):
                    raise ValueError("invalid task counters")
                if not all(_valid_optional_number(value) for value in (
                    t.created_at,
                    t.scheduled_at,
                    t.started_at,
                    t.finished_at,
                    t.next_retry_at,
                    t.cost_usd,
                )):
                    raise ValueError("invalid task timestamps/cost")
                if t.exit_code is not None and (
                    isinstance(t.exit_code, bool) or not isinstance(t.exit_code, int)
                ):
                    raise ValueError("invalid task exit code")
                if t.session_id is not None and (
                    not isinstance(t.session_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", t.session_id)
                ):
                    raise ValueError("invalid task session id")
                if not isinstance(t.state_reason, str):
                    raise ValueError("invalid task reason")
            except (TypeError, ValueError):
                log.warning("skip unparsable task record: %r", d)
                continue
            if t.id in self._tasks:
                log.warning("skip duplicate task id: %s", t.id)
                continue
            if t.tool not in self.registry:
                t.state = TaskState.FAILED
                t.finished_at = time.time()
                t.state_reason = f"恢复失败：未知工具 {t.tool!r}"
            elif t.profile not in PROFILES:
                t.state = TaskState.FAILED
                t.finished_at = time.time()
                t.state_reason = f"恢复失败：未知权限档位 {t.profile!r}"
            if t.state == TaskState.RUNNING:
                # AgentBar 与 CLI 使用独立进程组。若 AgentBar 崩溃，旧 CLI
                # 可能仍在运行；自动重派会产生两个 agent 同时改代码/发消息。
                # 因此恢复为人工暂停，用户确认现场后再显式恢复。
                t.state = TaskState.PAUSED
                t.resume_next = bool(t.session_id)
                t.state_reason = "检测到上次异常退出，为避免重复执行已暂停；请确认旧进程后手动恢复" + (
                    "（可尝试恢复会话）" if t.session_id else ""
                )
            self._tasks[t.id] = t
            self._order.append(t.id)
        paused = data.get("paused", False)
        self._paused = paused if isinstance(paused, bool) else False
        self.quota.load(data.get("quota", {}))

    def _persist_locked(self) -> None:
        finished = [
            tid for tid in self._order if self._tasks[tid].state in FINISHED_STATES
        ]
        prune: set[str] = set()
        if len(finished) > MAX_FINISHED_KEPT:
            prune = set(finished[: len(finished) - MAX_FINISHED_KEPT])
        persisted_order = [tid for tid in self._order if tid not in prune]
        self.store.save(
            {
                "version": __version__,
                "tasks": [self._tasks[tid].to_dict() for tid in persisted_order],
                "paused": self._paused,
                "quota": self.quota.dump(),
            }
        )
        # Pruning is part of the same commit: a failed save must not silently
        # delete completed tasks from the live scheduler.
        if prune:
            self._order[:] = persisted_order
            for tid in prune:
                self._tasks.pop(tid, None)
                try:
                    self.store.delete_log(tid)
                except OSError as exc:
                    # The state commit already retired this task. A filesystem
                    # cleanup error must not turn the successful transaction
                    # into a caller-visible failure, but should remain visible
                    # for diagnostics and a later manual cleanup.
                    log.warning("failed to delete retired task log %s: %s", tid, exc)

    def _ensure_mutable_locked(self) -> None:
        if self._stop.is_set():
            raise ValueError("调度器正在停止，拒绝修改任务")

    @staticmethod
    def _restore_task_locked(task: Task, checkpoint: dict) -> None:
        """Restore a Task in place so any live references remain valid."""
        restored = Task.from_dict(checkpoint)
        vars(task).clear()
        vars(task).update(vars(restored))

    # ================= lifecycle =================

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started or self._shutdown_complete:
                return
            self.quota.start_background()
            self._tick_thread = threading.Thread(
                target=self._tick_loop, name="agentbar-tick", daemon=True
            )
            self._started = True
            self._tick_thread.start()

    def shutdown(self) -> None:
        """优雅退出：终止在跑的 CLI 进程，把任务放回队列（带 resume），落盘。"""
        with self._lifecycle_lock:
            if self._shutdown_complete:
                return
            # Linearize shutdown with public mutations and dispatch. Once this
            # block completes, no later mutating API can commit new state.
            with self._lock:
                self._stop.set()
                self._wake.set()

            # First retire the dispatcher. _tick also re-checks _stop while it
            # holds the scheduler lock, so a tick that was already waiting on the
            # lock cannot launch work after shutdown has begun.
            tick = self._tick_thread
            if tick and tick is not threading.current_thread():
                tick.join(timeout=5)
                if tick.is_alive():
                    log.warning("scheduler tick thread did not stop within 5s")

            with self._lock:
                runs = list(self._running.items())
            for _tid, run in runs:
                run.mode = "interrupt"
                run.cancel.set()
            # Do not rely solely on workers polling the cancel flag. A CLI that
            # never reads stdin can leave its worker blocked in pipe.write(), so
            # shutdown itself must retire every already-published process.
            for _tid, run in runs:
                proc = run.proc
                if proc is not None and proc.poll() is None:
                    self._terminate(proc)
            for _tid, run in runs:
                if run.thread and run.thread is not threading.current_thread():
                    # `_tick` stores the Thread immediately before `.start()`;
                    # wait for the worker's first instruction before joining so
                    # Python never sees an unstarted Thread object.
                    run.started.wait(timeout=1)
                    if run.thread.ident is not None:
                        # Binary discovery may make two 15s login-shell probes
                        # before it observes cancellation; allow that bounded
                        # path to retire too.
                        run.thread.join(timeout=35)
                        if run.thread.is_alive():
                            log.warning("task worker %s did not stop within 35s", _tid)
            self.quota.stop()
            try:
                with self._lock:
                    self._persist_locked()
            finally:
                # The scheduler cannot be restarted after its threads/processes
                # have been retired. A disk failure is still reported, but repeat
                # shutdown calls must be idempotent and all mutations stay closed.
                self._shutdown_complete = True

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
        scheduled_at: float | None = None,
        before_commit=None,
    ) -> Task:
        if not isinstance(prompt, str):
            raise ValueError("prompt 必须是字符串")
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt 不能为空")
        if len(prompt) > 100_000:
            raise ValueError("prompt 过长（>100KB）")
        if not isinstance(tool, str) or tool not in self.registry:
            raise ValueError(f"未知工具 {tool!r}，可用: {sorted(self.registry)}")
        if not isinstance(profile, str) or profile not in PROFILES:
            raise ValueError(f"未知权限档位 {profile!r}，可用: {PROFILES}")
        if profile == "full" and not self.settings.allow_full_profile:
            raise ValueError(
                "高权限档位默认关闭。如确需开启，编辑 config.json 设置 "
                "allow_full_profile=true 后重启"
            )
        model = _optional_text(model, "模型名称", 120)
        effort = _optional_text(effort, "推理强度", 32)
        effort = effort.lower() if effort else None
        adapter = self.registry[tool]
        if effort and adapter.effort_choices and effort not in adapter.effort_choices:
            raise ValueError(
                f"{tool} 不支持强度 {effort!r}，可用: {adapter.effort_choices}"
            )
        if cwd is not None and not isinstance(cwd, str):
            raise ValueError("工作目录必须是字符串")
        cwd = os.path.abspath(os.path.expanduser(cwd or self.settings.default_cwd))
        if not os.path.isdir(cwd):
            raise ValueError(f"工作目录不存在: {cwd}")
        clean_title = _validated_title(title, prompt)
        clean_schedule = _validated_schedule(scheduled_at)
        t = Task(
            id=new_id(),
            title=clean_title,
            prompt=prompt,
            tool=tool,
            cwd=cwd,
            profile=profile,
            model=model,
            effort=effort,
            scheduled_at=clean_schedule,
        )
        with self._lock:
            if before_commit is not None:
                before_commit()
            self._ensure_mutable_locked()
            self._tasks[t.id] = t
            self._order.append(t.id)
            try:
                self._persist_locked()
            except Exception:
                # A caller that saw a persistence error must not leave a task
                # behind that a later tick can execute.
                self._tasks.pop(t.id, None)
                try:
                    self._order.remove(t.id)
                except ValueError:
                    pass
                raise
        self._wake.set()
        log.info("task %s added (%s, %s)", t.id, tool, t.title)
        return t

    def edit_task(self, task_id: str, changes: dict, before_commit=None) -> Task:
        """Edit a task that has not started running.

        A live CLI process is intentionally immutable: changing its prompt or
        safety profile would make the displayed task diverge from the command
        already executing. Users can cancel a running task and create/retry a
        replacement instead.
        """
        if not isinstance(changes, dict):
            raise ValueError("任务修改必须是 JSON object")
        with self._lock:
            if before_commit is not None:
                before_commit()
            self._ensure_mutable_locked()
            t = self._tasks.get(task_id)
            if not t:
                raise ValueError("任务不存在")
            if t.state == TaskState.RUNNING:
                raise ValueError("运行中的任务不能编辑；请先取消，再新建或重试")
            if t.state not in {
                TaskState.QUEUED, TaskState.PAUSED, TaskState.WAITING_QUOTA,
            }:
                raise ValueError(f"状态 {t.state.value} 的任务不能编辑；请使用重试创建新运行")

            prompt_value = changes.get("prompt", t.prompt)
            if not isinstance(prompt_value, str):
                raise ValueError("prompt 必须是字符串")
            prompt = prompt_value.strip()
            if not prompt:
                raise ValueError("prompt 不能为空")
            if len(prompt) > 100_000:
                raise ValueError("prompt 过长（>100KB）")

            tool = changes.get("tool", t.tool)
            if not isinstance(tool, str) or tool not in self.registry:
                raise ValueError(f"未知工具 {tool!r}，可用: {sorted(self.registry)}")

            profile = changes.get("profile", t.profile)
            if not isinstance(profile, str) or profile not in PROFILES:
                raise ValueError(f"未知权限档位 {profile!r}，可用: {PROFILES}")
            if profile == "full" and not self.settings.allow_full_profile:
                raise ValueError("高权限档位默认关闭。如确需开启，编辑 config.json 设置 allow_full_profile=true 后重启")

            model = _optional_text(changes.get("model", t.model), "模型名称", 120)
            effort = _optional_text(changes.get("effort", t.effort), "推理强度", 32)
            effort = effort.lower() if effort else None
            adapter = self.registry[tool]
            if effort and adapter.effort_choices and effort not in adapter.effort_choices:
                raise ValueError(
                    f"{tool} 不支持强度 {effort!r}，可用: {adapter.effort_choices}"
                )

            cwd_value = changes.get("cwd", t.cwd)
            if cwd_value is not None and not isinstance(cwd_value, str):
                raise ValueError("工作目录必须是字符串")
            cwd = os.path.abspath(os.path.expanduser(cwd_value or self.settings.default_cwd))
            if not os.path.isdir(cwd):
                raise ValueError(f"工作目录不存在: {cwd}")

            title = _validated_title(changes.get("title", t.title), prompt)
            scheduled_at = (
                _validated_schedule(changes.get("scheduled_at"))
                if "scheduled_at" in changes
                else t.scheduled_at
            )
            checkpoint = t.to_dict()
            t.prompt, t.tool, t.cwd = prompt, tool, cwd
            t.title, t.profile, t.model, t.effort = title, profile, model, effort
            t.scheduled_at = scheduled_at
            if t.state == TaskState.WAITING_QUOTA:
                t.state = TaskState.QUEUED
                t.next_retry_at = None
                t.state_reason = "编辑后重新入队"
            try:
                self._persist_locked()
            except Exception:
                self._restore_task_locked(t, checkpoint)
                raise
        self._wake.set()
        log.info("task %s edited", task_id)
        return t

    def act(self, task_id: str, action: str, before_commit=None) -> tuple[bool, str]:
        with self._lock:
            if before_commit is not None:
                before_commit()
            if self._stop.is_set():
                return False, "调度器正在停止，拒绝修改任务"
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
                        proc = run.proc
                        if proc is not None and proc.poll() is None:
                            threading.Thread(
                                target=self._terminate,
                                args=(proc,),
                                name=f"agentbar-cancel-{task_id}",
                                daemon=True,
                            ).start()
                    return True, "正在终止进程…"
                if s in (TaskState.QUEUED, TaskState.PAUSED, TaskState.WAITING_QUOTA):
                    checkpoint = t.to_dict()
                    cooldown_before = set(self._cooldown_bypass_once)
                    self._cooldown_bypass_once.discard(task_id)
                    t.state = TaskState.CANCELLED
                    t.finished_at = time.time()
                    t.state_reason = "用户取消"
                    try:
                        self._persist_locked()
                    except Exception:
                        self._restore_task_locked(t, checkpoint)
                        self._cooldown_bypass_once.clear()
                        self._cooldown_bypass_once.update(cooldown_before)
                        raise
                    return True, "已取消"
                return False, f"状态 {s.value} 不可取消"
            if action == "pause":
                if s in (TaskState.QUEUED, TaskState.WAITING_QUOTA):
                    checkpoint = t.to_dict()
                    cooldown_before = set(self._cooldown_bypass_once)
                    self._cooldown_bypass_once.discard(task_id)
                    t.state = TaskState.PAUSED
                    t.state_reason = "人工暂停"
                    try:
                        self._persist_locked()
                    except Exception:
                        self._restore_task_locked(t, checkpoint)
                        self._cooldown_bypass_once.clear()
                        self._cooldown_bypass_once.update(cooldown_before)
                        raise
                    return True, "已暂停"
                return False, f"状态 {s.value} 不可暂停（运行中请用取消）"
            if action == "resume":
                checkpoint = t.to_dict()
                cooldown_before = set(self._cooldown_bypass_once)
                if s == TaskState.PAUSED:
                    t.state = TaskState.QUEUED
                    t.state_reason = "人工恢复"
                elif s == TaskState.WAITING_QUOTA:
                    t.state = TaskState.QUEUED
                    t.next_retry_at = None
                    t.state_reason = "人工触发立即重试"
                    self._cooldown_bypass_once.add(task_id)
                else:
                    return False, f"状态 {s.value} 不可恢复"
                try:
                    self._persist_locked()
                except Exception:
                    self._restore_task_locked(t, checkpoint)
                    self._cooldown_bypass_once.clear()
                    self._cooldown_bypass_once.update(cooldown_before)
                    raise
                self._wake.set()
                return True, "已恢复"
            if action == "retry":
                if s in FINISHED_STATES:
                    checkpoint = t.to_dict()
                    t.state = TaskState.QUEUED
                    t.finished_at = None
                    t.exit_code = None
                    t.next_retry_at = None
                    t.resume_next = bool(t.session_id)
                    t.state_reason = "人工重试"
                    try:
                        self._persist_locked()
                    except Exception:
                        self._restore_task_locked(t, checkpoint)
                        raise
                    self._wake.set()
                    return True, "已重新入队"
                return False, f"状态 {s.value} 不可重试"
            if action in ("move_top", "move_up", "move_down"):
                return self._reorder_locked(t, action)
            return False, f"未知操作 {action!r}"

    def _reorder_locked(self, t: Task, action: str) -> tuple[bool, str]:
        """调整排队任务的优先级（仅影响 QUEUED 任务间的相对顺序，FIFO 派发即优先级）。"""
        if t.state != TaskState.QUEUED:
            return False, "仅排队中的任务可调整优先级"
        queued = [
            tid for tid in self._order
            if self._tasks[tid].state == TaskState.QUEUED
        ]
        order_before = list(self._order)
        i = queued.index(t.id)
        if action == "move_up" and i > 0:
            self._swap_order(queued[i], queued[i - 1])
        elif action == "move_down" and i < len(queued) - 1:
            self._swap_order(queued[i], queued[i + 1])
        elif action == "move_top":
            while i > 0:  # 逐位前移，保持其余任务相对顺序
                self._swap_order(queued[i], queued[i - 1])
                queued[i], queued[i - 1] = queued[i - 1], queued[i]
                i -= 1
        else:
            return True, "已在该位置"
        try:
            self._persist_locked()
        except Exception:
            self._order[:] = order_before
            raise
        self._wake.set()
        return True, "已调整优先级"

    def _swap_order(self, a: str, b: str) -> None:
        ia, ib = self._order.index(a), self._order.index(b)
        self._order[ia], self._order[ib] = self._order[ib], self._order[ia]

    def pause_all(self, before_commit=None) -> None:
        with self._lock:
            if before_commit is not None:
                before_commit()
            self._ensure_mutable_locked()
            paused_before = self._paused
            self._paused = True
            try:
                self._persist_locked()
            except Exception:
                self._paused = paused_before
                raise
        log.info("pause_all")

    def resume_all(self, before_commit=None) -> None:
        with self._lock:
            if before_commit is not None:
                before_commit()
            self._ensure_mutable_locked()
            paused_before = self._paused
            self._paused = False
            try:
                self._persist_locked()
            except Exception:
                self._paused = paused_before
                raise
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
        provider_config = {}
        for name in DEFAULT_PROVIDERS:
            cfg = (self.settings.providers or {}).get(name) or {}
            provider_config[name] = {
                "enabled": bool(cfg.get("enabled")),
                "cookie_set": bool(str(cfg.get("cookie") or "").strip()),
                "unit": cfg.get("unit") or DEFAULT_PROVIDERS[name]["unit"],
            }
        quota_source_config = {}
        for name in DEFAULT_QUOTA_SOURCES:
            cfg = (self.settings.quota_sources or {}).get(name) or {}
            quota_source_config[name] = {
                "enabled": bool(cfg.get("enabled")),
                "model": str(cfg.get("model") or ""),
                # CLI capability/login detection can spawn a subprocess. Keep
                # this 2-second snapshot path non-blocking; settings/API views
                # detect status on a worker instead.
                "credential_mode": "auto",
            }
        return {
            "version": __version__,
            "status": status,
            "paused": paused,
            "running_titles": running,
            "queued": n_queued,
            "waiting_quota": n_waiting,
            "tasks": tasks,
            "quota": {
                name: self.quota.status(name).to_dict()
                # 展示已启用的上游来源 + 已有真实任务观测的工具。
                for name in dict.fromkeys(
                    self.quota.provider_tools() + self.quota.observed_tools()
                )
            },
            "title_provider": self.settings.title_provider,
            # 菜单只需要无敏感信息的配置摘要，用于展示“未配置/待刷新”入口。
            # Cookie 永远不进入 snapshot、日志或前端状态转储。
            "provider_config": provider_config,
            # CLI 登录凭据及派生账户信息永不进入 snapshot。
            "quota_source_config": quota_source_config,
            "cli_processes": self._cli_processes_snapshot(),
            "settings": {
                "max_parallel": self.settings.max_parallel,
                "per_tool_limit": self.settings.per_tool_limit,
                "usage_refresh_seconds": self.settings.usage_refresh_seconds,
                "usage_auto_refresh": self.settings.usage_auto_refresh,
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
            if self._stop.is_set():
                return
            # Task fields are mutated before persistence so state.json records
            # RUNNING before a worker can launch. Keep an in-place checkpoint so
            # a failed save can roll the tentative transition back completely.
            changed_tasks: dict[str, dict] = {}
            cooldown_before = set(self._cooldown_bypass_once)

            def checkpoint(task: Task) -> None:
                changed_tasks.setdefault(task.id, task.to_dict())

            now = time.time()
            dirty = False
            # 1) 额度等待期结束 → 回到队列
            for t in self._iter_tasks():
                if (
                    t.state == TaskState.WAITING_QUOTA
                    and t.next_retry_at
                    and t.next_retry_at <= now
                ):
                    checkpoint(t)
                    t.state = TaskState.QUEUED
                    t.state_reason = "额度等待结束，重新排队"
                    dirty = True
            # 2) FIFO 派发（跳过被额度冷却/并发上限/定时卡住的任务）
            if not self._paused:
                for t in self._iter_tasks():
                    if self._stop.is_set():
                        break
                    if t.state != TaskState.QUEUED:
                        continue
                    if t.scheduled_at and t.scheduled_at > now:
                        continue  # 定时任务：时间未到
                    if len(self._running) >= self.settings.max_parallel:
                        break
                    if self._tool_running(t.tool) >= self.settings.per_tool_limit:
                        continue
                    bypass_cooldown = t.id in self._cooldown_bypass_once
                    if not bypass_cooldown:
                        cd = self.quota.cooldown_until(t.tool, t.model)
                        if cd and cd > now:
                            continue
                    run = _Run()
                    run.was_resume = bool(t.resume_next and t.session_id)
                    checkpoint(t)
                    self._running[t.id] = run
                    t.state = TaskState.RUNNING
                    t.started_at = now
                    t.finished_at = None
                    t.attempts += 1
                    t.state_reason = "执行中"
                    self._cooldown_bypass_once.discard(t.id)
                    to_start.append((t.id, run))
                    dirty = True
            if dirty:
                try:
                    self._persist_locked()
                except Exception:
                    for tid, previous in changed_tasks.items():
                        live = self._tasks.get(tid)
                        if live is not None:
                            self._restore_task_locked(live, previous)
                    for tid, run in to_start:
                        if self._running.get(tid) is run:
                            self._running.pop(tid, None)
                    self._cooldown_bypass_once.clear()
                    self._cooldown_bypass_once.update(cooldown_before)
                    raise
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

        if self._stop.is_set() or run.cancel.is_set():
            self._finalize(
                task_id, run,
                Outcome("interrupted", "调度器退出，任务已回到队列"),
                None, None, "",
            )
            return

        try:
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

            if self._stop.is_set() or run.cancel.is_set():
                self._finalize(
                    task_id, run,
                    Outcome("interrupted", "调度器退出，任务已回到队列"),
                    None, None, "",
                )
                return

            argv = adapter.build_argv(t, resume=resume, binary=binary)
            payload = adapter.stdin_payload(t, resume=resume)
            env = os.environ.copy()
            for k in _ENV_STRIP:
                env.pop(k, None)
            env = adapter.build_env(env)  # ensure tool binary dir is on PATH
        except Exception as e:
            self._finalize(
                task_id, run,
                Outcome("failure", f"启动准备失败: {e}"),
                None, None, "",
            )
            return

        rc: int | None = None
        proc: subprocess.Popen | None = None
        try:
            with self.store.open_log_append(task_id) as lf:
                shown = [a if len(a) < 200 else a[:200] + "…" for a in argv]
                header = (
                    f"\n===== attempt {t.attempts} @ "
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"(resume={resume}) =====\n$ {' '.join(shown)}\n"
                )
                lf.write(header.encode())
                lf.flush()
                # Pair the final stop check and Popen registration under the
                # scheduler lock. Shutdown can then either prevent the launch or
                # see the process in _running and cancel it; there is no invisible
                # post-shutdown child-process window.
                with self._lock:
                    if self._stop.is_set() or run.cancel.is_set():
                        proc = None
                    else:
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
                if proc is None:
                    self._finalize(
                        task_id, run,
                        Outcome("interrupted", "调度器退出，任务已回到队列"),
                        None, None, "",
                    )
                    return
                if payload is not None and proc.stdin:
                    try:
                        proc.stdin.write(payload.encode())
                        proc.stdin.close()
                    except (BrokenPipeError, OSError):
                        pass
                deadline = time.time() + timeout
                next_sid_probe = time.time() + 2.0
                sid_captured = False
                while True:
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
                    rc = proc.poll()
                    if rc is not None:
                        break
                    # 运行中即捕获 session id（codex 打在输出 header）：
                    # 「查看对话」对运行中任务立即可用，也防长输出把 header 挤出 tail
                    if not sid_captured and time.time() >= next_sid_probe:
                        next_sid_probe = time.time() + 2.0
                        sid_early = adapter.extract_session_id(
                            self.store.read_log_head(task_id)
                        )
                        if sid_early:
                            sid_captured = True
                            with self._lock:
                                t_live = self._tasks.get(task_id)
                                if t_live and not t_live.session_id:
                                    t_live.session_id = sid_early
                                    self._persist_locked()
                    if time.time() > deadline:
                        self._terminate(proc)
                        self._finalize(
                            task_id, run,
                            Outcome("failure", f"超时（>{timeout}s），已终止"),
                            124, None, "",
                        )
                        return
                    time.sleep(0.2)
        except Exception as e:  # Popen 或运行中日志/会话探测失败
            # Once Popen succeeds, every exit path must retire the process before
            # _finalize removes it from _running. Otherwise an agent can continue
            # editing after the UI has already reported the task as failed, and
            # shutdown no longer has a handle with which to stop it.
            if proc is not None and proc.poll() is None:
                try:
                    self._terminate(proc)
                except Exception:
                    log.exception("failed to terminate task %s after monitor error", task_id)
            phase = "执行监控失败" if proc is not None else "启动失败"
            self._finalize(
                task_id, run, Outcome("failure", f"{phase}: {e}"), None, None, ""
            )
            return

        try:
            tail = self.store.read_log_tail(task_id)
            outcome = adapter.classify(rc, tail)
            # 头+尾都扫：codex 的 session id 在输出开头，长输出下 tail 看不到它
            head = self.store.read_log_head(task_id)
            sid = adapter.extract_session_id(head + "\n" + tail)
        except Exception as e:
            self._finalize(
                task_id, run,
                Outcome("failure", f"执行结果处理失败: {e}"), rc, None, "",
            )
            return
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
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("process group %s did not exit after SIGKILL", pgid)

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
