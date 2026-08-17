"""Quota status — honest, layered; never fabricated.

数据优先级（来源在 UI 明确标注，取不到就降级，绝不编造）：
  1. CLI source  — 用户启用后自动检测 Claude Code 登录态，并通过 Codex
                   App Server 的账户接口读取其可用额度；
                   默认仅启动、保存设置或手动点击时请求，周期刷新需主动开启
  2. observed    — 调度器观测事实：真实任务的限流失败（ground truth，优先于 API 展示）
  3. ccusage     — 本机装了 ccusage 时补充 5h 窗口成本
  4. unknown     — 都取不到，如实显示未知
"""

from __future__ import annotations

import json
import logging
import math
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field

from .config import Settings
from .usage import UsageSnapshot, get_usage_fetchers

log = logging.getLogger("agentbar.quota")

USAGE_STALE_SECONDS = 15 * 60  # 额度来源结果超过该时长视为过期，不再参与判定
# MyToken's token-unit fetch can perform three sequential 12s HTTP calls. Give
# the active request enough time to reach its own bounded timeout before stop()
# reports a lifecycle failure.
STOP_JOIN_SECONDS = 45


@dataclass
class QuotaStatus:
    tool: str
    state: str          # "ok" | "limited" | "unknown"
    detail: str
    source: str         # fetcher source | "observed" | "ccusage" | "none" 或组合
    reset_at: float | None = None
    windows: list = field(default_factory=list)   # [{label, used_percent, resets_at}]
    plan: str | None = None
    fetched_at: float | None = None
    error: str | None = None
    model: str | None = None
    available_models: list[str] = field(default_factory=list)
    stale: bool = False

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "state": self.state,
            "detail": self.detail,
            "source": self.source,
            "reset_at": self.reset_at,
            "windows": self.windows,
            "plan": self.plan,
            "fetched_at": self.fetched_at,
            "error": self.error,
            "model": self.model,
            "available_models": self.available_models,
            "stale": self.stale,
        }


@dataclass(frozen=True)
class _RefreshBatch:
    """An immutable claim on fetchers from one configuration generation."""

    generation: int
    fetchers: dict[str, object]


def _clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _clock_day(ts: float) -> str:
    if time.localtime(ts).tm_yday != time.localtime().tm_yday:
        return time.strftime("%m-%d %H:%M", time.localtime(ts))
    return _clock(ts)


class QuotaMonitor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.Lock()
        # {tool: {"last_success_at": float, "last_quota_at": float, "reset_at": float}}
        self._obs: dict[str, dict] = {}
        # _usage 只保存最后一次成功结果；刷新错误单独保存，不能用一次临时
        # 网络失败覆盖仍在有效期内的 last-good 数据。
        self._usage: dict[str, UsageSnapshot] = {}
        self._usage_errors: dict[str, UsageSnapshot] = {}
        self._fetchers = get_usage_fetchers(settings)
        self._next_due: dict[str, float] = {}   # 各 provider 的下次到期刷新时刻（per-provider 间隔）
        self._scheduled_intervals: dict[str, float] = {}
        self._pending_refresh: set[str] = set() # 手动刷新来源；Event + set 天然合并快速重复点击
        # tool -> fetch generation。旧请求结束时只能清理自己的 generation，
        # 不能误删 reload 后同名来源的新请求标记。
        # tool -> 当前正在请求的 fetcher 对象。以对象身份而不是全局
        # generation 判重，使 reload 一个来源时不打断其他未变化来源的在途请求。
        self._refreshing: dict[str, object] = {}
        self._fetch_generation = 0
        self._ccusage: dict | None = None
        self._ccusage_bin = shutil.which("ccusage")
        self._stop = threading.Event()
        self._refresh_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()

    def provider_tools(self) -> list[str]:
        """本监视器能上报额度的全部工具名（含 corp provider）。"""
        with self._lock:
            return list(self._fetchers)

    def observed_tools(self) -> list[str]:
        """已有任务成功/限额观测的工具（无需配置上游额度来源）。"""
        with self._lock:
            return list(self._obs)

    @staticmethod
    def _fetcher_signature(fetcher: object) -> tuple:
        """Configuration identity without depending on a fetcher's implementation.

        Production fetchers expose one or more of these immutable constructor
        fields. Unknown/testing fetchers fall back to object identity so replacing
        one is conservatively treated as a real configuration change.
        """
        names = ("binary", "model", "cookie", "unit", "refresh_seconds")
        values = tuple((name, getattr(fetcher, name)) for name in names if hasattr(fetcher, name))
        return (type(fetcher), values if values else id(fetcher))

    def reload_fetchers(self, refresh: bool = True) -> None:
        """Rebuild only changed sources and optionally refresh those sources.

        Saving an unrelated UI preference must not clear every displayed quota or
        cause every configured provider to make another network request.
        """
        # A settings writer mutates credentials and persists them under this
        # lock. Never build fetchers from another thread's uncommitted revision:
        # if that save later rolls back, the monitor could otherwise keep using a
        # credential/model that the UI reported as not saved.
        with self.settings._lock:
            rebuilt = get_usage_fetchers(self.settings)
        wake = False
        with self._lock:
            previous = self._fetchers
            all_tools = set(previous) | set(rebuilt)
            changed = {
                name for name in all_tools
                if name not in previous
                or name not in rebuilt
                or self._fetcher_signature(previous[name])
                != self._fetcher_signature(rebuilt[name])
            }
            if not changed:
                # A settings save may only toggle automatic refresh or change
                # the global interval, neither of which changes fetcher identity.
                # Wake the loop so the new schedule takes effect immediately.
                wake = not self._stop.is_set()
            else:
                self._fetch_generation += 1
                # Preserve the exact old object for unchanged sources. An in-flight
                # result is therefore still valid, while a changed source's old result
                # is rejected by the identity checks in _run_refresh_batch.
                for name in set(previous) & set(rebuilt) - changed:
                    rebuilt[name] = previous[name]
                self._fetchers = rebuilt

                for name in changed:
                    self._next_due.pop(name, None)
                    self._scheduled_intervals.pop(name, None)
                    self._pending_refresh.discard(name)
                    self._usage.pop(name, None)
                    self._usage_errors.pop(name, None)
                if "claude" in changed:
                    self._ccusage = None
                if refresh and not self._stop.is_set():
                    self._pending_refresh.update(changed & set(rebuilt))
                # Even refresh=False must recompute the auto schedule (for
                # example after disabling a source).
                wake = not self._stop.is_set()
        if wake:
            self._refresh_evt.set()

    # ---------- persistence (由 scheduler 存进 state.json) ----------

    def load(self, data: dict) -> None:
        cleaned: dict[str, dict] = {}
        if isinstance(data, dict):
            for tool, raw in data.items():
                if not isinstance(tool, str) or not isinstance(raw, dict):
                    continue
                entry = {}
                for key in ("last_success_at", "last_quota_at", "reset_at"):
                    value = raw.get(key)
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        continue
                    parsed = float(value)
                    if math.isfinite(parsed) and parsed >= 0:
                        entry[key] = parsed
                if entry:
                    cleaned[tool] = entry
        with self._lock:
            self._obs = cleaned

    def dump(self) -> dict:
        with self._lock:
            return dict(self._obs)

    # ---------- observations（真实任务结果，ground truth） ----------

    def record_success(self, tool: str) -> None:
        with self._lock:
            o = self._obs.setdefault(tool, {})
            o["last_success_at"] = time.time()
            o.pop("reset_at", None)

    def record_quota(self, tool: str, reset_at: float | None) -> None:
        with self._lock:
            o = self._obs.setdefault(tool, {})
            o["last_quota_at"] = time.time()
            if reset_at:
                o["reset_at"] = reset_at

    # ---------- cooldown（调度器据此暂缓派发该工具的任务） ----------

    @staticmethod
    def _same_model(configured: str | None, task_model: str | None) -> bool:
        if not configured:
            return True  # 账户级额度适用于该工具的全部模型
        if not task_model:
            return False # 任务用 CLI 默认模型，无法证明它命中已选专属额度
        left = re.sub(r"[^a-z0-9]+", "", configured.casefold())
        right = re.sub(r"[^a-z0-9]+", "", task_model.casefold())
        return bool(left and right and (left == right or left in right or right in left))

    def cooldown_until(self, tool: str, model: str | None = None) -> float | None:
        now = time.time()
        candidates: list[float] = []
        with self._lock:
            o = self._obs.get(tool) or {}
            snap = self._usage.get(tool)
        lq, ls = o.get("last_quota_at") or 0, o.get("last_success_at") or 0
        if lq > ls and o.get("reset_at"):
            candidates.append(o["reset_at"])
        # 额度来源显示某窗口已打满 → 主动冷却到重置时间（不用真跑一次失败）
        if snap and not snap.error and now - snap.fetched_at < USAGE_STALE_SECONDS:
            for w in snap.windows:
                # 一个 snapshot 可能同时含账户通用窗口和多个模型专属窗口。
                # 必须逐窗口匹配，不能用 UI 所选的 snapshot.model 把通用窗口一并过滤。
                if not self._same_model(w.model, model):
                    continue
                if (w.limited or w.used_percent >= 99.9) and w.resets_at and w.resets_at > now:
                    candidates.append(w.resets_at)
        future = [c for c in candidates if c > now]
        return max(future) if future else None

    # ---------- status ----------

    def status(self, tool: str) -> QuotaStatus:
        with self._lock:
            o = dict(self._obs.get(tool) or {})
            snap = self._usage.get(tool)
            refresh_error = self._usage_errors.get(tool)
            cc = dict(self._ccusage) if (self._ccusage and tool == "claude") else None
        # Compatibility for callers/tests that injected an error snapshot directly
        # into _usage before errors were split from last-good data.
        if snap and snap.error:
            refresh_error, snap = snap, None
        now = time.time()
        lq, ls, obs_reset = o.get("last_quota_at"), o.get("last_success_at"), o.get("reset_at")
        observed_limited = bool(lq and (not ls or lq > ls))

        st: QuotaStatus
        if snap and not snap.error and snap.windows and now - snap.fetched_at < USAGE_STALE_SECONDS:
            parts = []
            worst_reset = None
            for w in snap.windows[:3]:
                seg = f"{w.label} {w.used_percent:.0f}%"
                if (w.limited or w.used_percent >= 99.9) and w.resets_at:
                    seg += f"（{_clock_day(w.resets_at)} 重置）"
                    worst_reset = max(worst_reset or 0, w.resets_at)
                parts.append(seg)
            # snapshot.limited 只保留给旧 fetcher / 展示兼容；新 fetcher 在窗口
            # 上标注 limited，供模型级 cooldown 精确判定。
            limited = observed_limited or snap.limited or any(
                w.limited or w.used_percent >= 99.9 for w in snap.windows
            )
            st = QuotaStatus(
                tool,
                "limited" if limited else "ok",
                " · ".join(parts),
                (snap.source or "quota_source")
                + ("+observed" if observed_limited else ""),
                reset_at=worst_reset or (obs_reset if observed_limited else None),
            )
        elif observed_limited:
            if obs_reset and obs_reset > now:
                st = QuotaStatus(tool, "limited", f"额度受限，约 {_clock_day(obs_reset)} 恢复",
                                 "observed", obs_reset)
            else:
                st = QuotaStatus(tool, "limited", "额度受限（恢复时间未知，退避重试中）",
                                 "observed")
        elif ls:
            st = QuotaStatus(tool, "ok", f"正常（{_clock_day(ls)} 有成功执行）", "observed")
        elif snap and not snap.error and snap.source == "claude_auth_status":
            # Claude exposes a supported non-sensitive CLI login-status command,
            # but no supported third-party subscription-usage endpoint. Preserve
            # that useful distinction instead of presenting a logged-in source as
            # an unexplained fetch failure or inventing a percentage.
            st = QuotaStatus(
                tool,
                "unknown",
                "Claude Code 已登录；订阅额度仅展示任务观测与 ccusage",
                "claude_auth_status",
            )
        else:
            st = QuotaStatus(tool, "unknown", "未知（尚无额度数据）", "none")

        if snap:
            st.windows = [w.to_dict() for w in snap.windows]
            st.plan = snap.plan
            st.fetched_at = snap.fetched_at
            st.model = snap.model
            st.available_models = list(snap.available_models)
            st.stale = now - snap.fetched_at >= USAGE_STALE_SECONDS
            if st.stale and st.source == "none":
                # Manual mode deliberately keeps the last-known windows visible,
                # but expired data must never look current or drive cooldown.
                st.detail = "上次额度数据已过期，请手动刷新"
        metadata = snap or refresh_error
        if metadata and not snap:
            st.plan = metadata.plan
            st.fetched_at = metadata.fetched_at
            st.model = metadata.model
            st.available_models = list(metadata.available_models)
        if refresh_error and refresh_error.error:
            st.error = refresh_error.error
            if snap and snap.windows:
                st.detail += f"；上次刷新失败：{refresh_error.error}"
            elif st.source == "none":
                st.detail += f"；额度来源: {refresh_error.error}"
        if cc:
            st.detail += f"；5h 已用 ${cc['cost']:.2f}（ccusage）"
            st.source += "+ccusage"
        return st

    # ---------- background refresh ----------

    def start_background(self) -> None:
        with self._lifecycle_lock:
            if self._stop.is_set():
                return
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._loop, name="agentbar-usage", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._refresh_evt.set()
        with self._lifecycle_lock:
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=STOP_JOIN_SECONDS)
            if thread.is_alive():
                log.warning("usage monitor did not stop within %ss", STOP_JOIN_SECONDS)

    def refresh_now(self, tool: str | None = None) -> None:
        """异步刷新已配置来源。快速重复请求会在 pending set 中合并。"""
        if self._stop.is_set():
            return
        wake = False
        with self._lock:
            candidates: set[str]
            if tool is None:
                candidates = set(self._fetchers)
            elif tool in self._fetchers:
                candidates = {tool}
            else:
                candidates = set()
            # 同一来源正在请求时的额外点击直接合并，不在完成后紧接着再拉一次。
            queued = {
                name for name in candidates
                if self._refreshing.get(name) is not self._fetchers.get(name)
            }
            self._pending_refresh.update(queued)
            wake = bool(queued)
        if wake:
            self._refresh_evt.set()

    def _loop(self) -> None:
        if self._stop.is_set():
            return
        # 启动时有效配置拉取一次；claim 会同时吸收启动前的重复点击。
        self._refresh_all()
        while not self._stop.is_set():
            # pending set 是事实来源，Event 只负责唤醒。必须先 clear 再检查
            # predicate，避免 wait() 超时与 refresh_now().set() 同时发生时丢唤醒。
            self._refresh_evt.clear()
            batch = self._claim_pending_refresh()
            if batch.fetchers:
                self._run_refresh_batch(batch)
                continue
            signaled = self._refresh_evt.wait(self._next_wait_seconds())
            if self._stop.is_set():
                return
            if signaled:
                continue
            elif self.settings.usage_auto_refresh:
                self._refresh_all(respect_due=True)

    def _global_interval(self) -> float:
        try:
            interval = float(self.settings.usage_refresh_seconds)
        except (TypeError, ValueError, OverflowError):
            interval = 120.0
        if not math.isfinite(interval):
            interval = 120.0
        return min(86_400.0, max(30.0, interval))

    def _source_interval(self, fetcher: object) -> float:
        value = getattr(fetcher, "refresh_seconds", None)
        if value is None:
            return self._global_interval()
        try:
            interval = float(value)
        except (TypeError, ValueError, OverflowError):
            return self._global_interval()
        if not math.isfinite(interval):
            return self._global_interval()
        return min(86_400.0, max(30.0, interval))

    def _next_wait_seconds(self) -> float:
        """Return the nearest source deadline without refreshing in manual mode."""
        fallback = self._global_interval()
        if not self.settings.usage_auto_refresh:
            return fallback
        now = time.time()
        with self._lock:
            if not self._fetchers:
                return fallback
            due_times = []
            for name, fetcher in self._fetchers.items():
                interval = self._source_interval(fetcher)
                if self._scheduled_intervals.get(name) != interval:
                    # Runtime interval changes take effect from the settings
                    # revision that woke this loop, not from an obsolete due time.
                    self._scheduled_intervals[name] = interval
                    self._next_due[name] = now + interval
                due_times.append(self._next_due.get(name, now))
        return max(0.0, min(due_times) - now)

    def _claim_locked(
        self,
        candidates: set[str],
        *,
        respect_due: bool,
    ) -> _RefreshBatch:
        """Bind tools to fetchers/generation and mark them in-flight atomically."""
        generation = self._fetch_generation
        if self._stop.is_set():
            return _RefreshBatch(generation, {})
        candidates.intersection_update(self._fetchers)
        # A direct/startup claim absorbs an already pending click for the same
        # source. A current-generation in-flight marker absorbs duplicates too.
        coalesced = {
            name for name in candidates
            if self._refreshing.get(name) is self._fetchers.get(name)
        }
        now = time.time()
        claimed: dict[str, object] = {}
        # Preserve configured/provider insertion order. Besides deterministic UI
        # and tests, this makes stop semantics predictable within a claimed batch.
        for name in (name for name in self._fetchers if name in candidates - coalesced):
            fetcher = self._fetchers[name]
            if (
                respect_due
                and now < self._next_due.get(name, 0)
            ):
                continue
            claimed[name] = fetcher
            self._refreshing[name] = fetcher
        self._pending_refresh.difference_update(set(claimed) | coalesced)
        return _RefreshBatch(generation, claimed)

    def _claim_refresh(
        self,
        tools: set[str] | None = None,
        *,
        respect_due: bool = False,
    ) -> _RefreshBatch:
        with self._lock:
            candidates = set(self._fetchers) if tools is None else set(tools)
            return self._claim_locked(candidates, respect_due=respect_due)

    def _claim_pending_refresh(self) -> _RefreshBatch:
        with self._lock:
            candidates = set(self._pending_refresh)
            # Drop names disabled by a reload so they cannot remain pending
            # forever. Valid names are removed by _claim_locked once claimed or
            # coalesced with an in-flight request.
            self._pending_refresh.intersection_update(self._fetchers)
            return self._claim_locked(candidates, respect_due=False)

    def _refresh_all(
        self,
        tools: set[str] | None = None,
        *,
        respect_due: bool = False,
    ) -> None:
        batch = self._claim_refresh(tools, respect_due=respect_due)
        if batch.fetchers:
            self._run_refresh_batch(batch)

    def _run_refresh_batch(self, batch: _RefreshBatch) -> None:
        """Run one previously claimed batch without changing its identity."""
        try:
            for tool, fetcher in batch.fetchers.items():
                if self._stop.is_set():
                    break
                with self._lock:
                    if self._fetchers.get(tool) is not fetcher:
                        continue
                if self._stop.is_set():
                    break
                try:
                    snap = fetcher.fetch()
                except Exception as e:  # 任何异常都不能带崩后台线程
                    log.warning("usage fetch %s failed: %s", tool, e)
                    snap = UsageSnapshot(tool, source="quota_source", error=str(e))
                if snap:
                    with self._lock:
                        # 配置切换/禁用时丢弃旧请求的迟到结果，避免把 A 模型数字误标为 B。
                        if self._fetchers.get(tool) is not fetcher:
                            continue
                        if snap.error:
                            self._usage_errors[tool] = snap
                        else:
                            self._usage[tool] = snap
                            self._usage_errors.pop(tool, None)
                with self._lock:
                    if self._fetchers.get(tool) is fetcher:
                        interval = self._source_interval(fetcher)
                        self._scheduled_intervals[tool] = interval
                        # Schedule from completion so a slow request cannot make
                        # the same source immediately due again.
                        self._next_due[tool] = time.time() + interval
            with self._lock:
                refresh_ccusage = (
                    self._ccusage_bin
                    and "claude" in batch.fetchers
                    and self._fetchers.get("claude") is batch.fetchers["claude"]
                )
            if refresh_ccusage and not self._stop.is_set():
                self._refresh_ccusage()
        finally:
            with self._lock:
                for tool, fetcher in batch.fetchers.items():
                    if self._refreshing.get(tool) is fetcher:
                        self._refreshing.pop(tool, None)

    def _refresh_ccusage(self) -> None:
        try:
            r = subprocess.run(
                [self._ccusage_bin, "blocks", "--active", "--json"],
                capture_output=True, text=True, timeout=20,
            )
            data = json.loads(r.stdout)
            block = next((b for b in data.get("blocks", []) if b.get("isActive")), None)
            with self._lock:
                self._ccusage = (
                    {"cost": float(block.get("costUSD", 0))} if block else None
                )
        except Exception:
            with self._lock:
                self._ccusage = None  # 取不到就不显示，不伪造
