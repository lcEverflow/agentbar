"""Quota status — honest, layered; never fabricated.

数据优先级（来源在 UI 明确标注，取不到就降级，绝不编造）：
  1. usage API   — aiusagebar 同款官方接口（Claude OAuth usage / Codex wham usage），
                   后台定时刷新（默认 120s），给出各窗口用量百分比 + 重置时间
  2. observed    — 调度器观测事实：真实任务的限流失败（ground truth，优先于 API 展示）
  3. ccusage     — 本机装了 ccusage 时补充 5h 窗口成本
  4. unknown     — 都取不到，如实显示未知
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field

from .config import Settings
from .usage import UsageSnapshot, get_usage_fetchers

log = logging.getLogger("agentbar.quota")

USAGE_STALE_SECONDS = 15 * 60  # usage API 结果超过该时长视为过期，不再参与判定


@dataclass
class QuotaStatus:
    tool: str
    state: str          # "ok" | "limited" | "unknown"
    detail: str
    source: str         # "usage_api" | "observed" | "ccusage" | "none" 或组合
    reset_at: float | None = None
    windows: list = field(default_factory=list)   # [{label, used_percent, resets_at}]
    plan: str | None = None
    fetched_at: float | None = None
    error: str | None = None

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
        }


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
        self._usage: dict[str, UsageSnapshot] = {}
        self._fetchers = get_usage_fetchers()
        self._ccusage: dict | None = None
        self._ccusage_bin = shutil.which("ccusage")
        self._stop = threading.Event()
        self._refresh_evt = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------- persistence (由 scheduler 存进 state.json) ----------

    def load(self, data: dict) -> None:
        with self._lock:
            self._obs = dict(data or {})

    def dump(self) -> dict:
        with self._lock:
            return dict(self._obs)

    # ---------- observations（真实任务结果，ground truth） ----------

    def record_success(self, tool: str) -> None:
        with self._lock:
            o = self._obs.setdefault(tool, {})
            o["last_success_at"] = time.time()
            o.pop("reset_at", None)
        self.refresh_now()

    def record_quota(self, tool: str, reset_at: float | None) -> None:
        with self._lock:
            o = self._obs.setdefault(tool, {})
            o["last_quota_at"] = time.time()
            if reset_at:
                o["reset_at"] = reset_at
        self.refresh_now()

    # ---------- cooldown（调度器据此暂缓派发该工具的任务） ----------

    def cooldown_until(self, tool: str) -> float | None:
        now = time.time()
        candidates: list[float] = []
        with self._lock:
            o = self._obs.get(tool) or {}
            snap = self._usage.get(tool)
        lq, ls = o.get("last_quota_at") or 0, o.get("last_success_at") or 0
        if lq > ls and o.get("reset_at"):
            candidates.append(o["reset_at"])
        # usage API 显示某窗口已打满 → 主动冷却到重置时间（不用真跑一次失败）
        if snap and not snap.error and now - snap.fetched_at < USAGE_STALE_SECONDS:
            for w in snap.windows:
                if (snap.limited or w.used_percent >= 99.9) and w.resets_at and w.resets_at > now:
                    candidates.append(w.resets_at)
        future = [c for c in candidates if c > now]
        return max(future) if future else None

    # ---------- status ----------

    def status(self, tool: str) -> QuotaStatus:
        with self._lock:
            o = dict(self._obs.get(tool) or {})
            snap = self._usage.get(tool)
            cc = dict(self._ccusage) if (self._ccusage and tool == "claude") else None
        now = time.time()
        lq, ls, obs_reset = o.get("last_quota_at"), o.get("last_success_at"), o.get("reset_at")
        observed_limited = bool(lq and (not ls or lq > ls))

        st: QuotaStatus
        if snap and not snap.error and snap.windows and now - snap.fetched_at < USAGE_STALE_SECONDS:
            parts = []
            worst_reset = None
            for w in snap.windows[:3]:
                seg = f"{w.label} {w.used_percent:.0f}%"
                if w.used_percent >= 99.9 and w.resets_at:
                    seg += f"（{_clock_day(w.resets_at)} 重置）"
                    worst_reset = max(worst_reset or 0, w.resets_at)
                parts.append(seg)
            limited = observed_limited or snap.limited or any(w.used_percent >= 99.9 for w in snap.windows)
            st = QuotaStatus(
                tool,
                "limited" if limited else "ok",
                " · ".join(parts),
                "usage_api" + ("+observed" if observed_limited else ""),
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
        else:
            st = QuotaStatus(tool, "unknown", "未知（尚无额度数据）", "none")

        if snap:
            st.windows = [w.to_dict() for w in snap.windows]
            st.plan = snap.plan
            st.fetched_at = snap.fetched_at
            st.error = snap.error
            if snap.error and st.source in ("none",):
                st.detail += f"；usage API: {snap.error}"
        if cc:
            st.detail += f"；5h 已用 ${cc['cost']:.2f}（ccusage）"
            st.source += "+ccusage"
        return st

    # ---------- background refresh ----------

    def start_background(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="agentbar-usage", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._refresh_evt.set()

    def refresh_now(self) -> None:
        """异步触发一次立即刷新（任务结束/用户点菜单时调用）。"""
        self._refresh_evt.set()

    def authorize_claude_keychain(self) -> bool:
        """交互式读取 Keychain（允许系统弹窗；用户点"始终允许"后静默读取即长期可用）。"""
        fetcher = self._fetchers.get("claude")
        if not fetcher:
            return False
        snap = fetcher.fetch(interactive=True)
        ok = bool(snap and not snap.error)
        if snap:
            with self._lock:
                self._usage["claude"] = snap
        return ok

    def _loop(self) -> None:
        self._refresh_all()
        try:
            interval = max(30, float(self.settings.usage_refresh_seconds))
        except (TypeError, ValueError):
            interval = 120
            log.warning("invalid usage_refresh_seconds; falling back to %ss", interval)
        while not self._stop.is_set():
            self._refresh_evt.wait(interval)
            self._refresh_evt.clear()
            if self._stop.is_set():
                return
            self._refresh_all()

    def _refresh_all(self) -> None:
        for tool, fetcher in self._fetchers.items():
            try:
                snap = fetcher.fetch()
            except Exception as e:  # 任何异常都不能带崩后台线程
                log.warning("usage fetch %s failed: %s", tool, e)
                snap = UsageSnapshot(tool, source="usage_api", error=str(e))
            if snap:
                with self._lock:
                    prev = self._usage.get(tool)
                    # 新结果拿不到数据时，保留旧的有效窗口（标过期由 STALE 判定），只更新错误信息
                    if snap.error and prev and prev.windows:
                        prev.error = snap.error
                    else:
                        self._usage[tool] = snap
        if self._ccusage_bin:
            self._refresh_ccusage()

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
