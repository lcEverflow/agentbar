"""Quota status — honest, observation-based; never fabricated.

Anthropic / OpenAI 都没有为订阅版 CLI 提供公开的额度查询 API，所以采用降级方案：
  1. observed  — 调度器自己观测到的事实（最近一次成功 / 最近一次额度报错 + 恢复时间）
  2. ccusage   — 如果本机装了 ccusage（解析 ~/.claude 本地日志），补充 5h 窗口用量
数据来源在 UI 上明确标注，取不到就显示"未知"，绝不编造。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass

from .config import Settings


@dataclass
class QuotaStatus:
    tool: str
    state: str          # "ok" | "limited" | "unknown"
    detail: str
    source: str         # "observed" | "observed+ccusage" | "none"
    reset_at: float | None = None

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "state": self.state,
            "detail": self.detail,
            "source": self.source,
            "reset_at": self.reset_at,
        }


def _clock(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


class QuotaMonitor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.Lock()
        # {tool: {"last_success_at": float, "last_quota_at": float, "reset_at": float}}
        self._obs: dict[str, dict] = {}
        self._ccusage: dict | None = None
        self._ccusage_bin = shutil.which("ccusage")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------- persistence (由 scheduler 存进 state.json) ----------

    def load(self, data: dict) -> None:
        with self._lock:
            self._obs = dict(data or {})

    def dump(self) -> dict:
        with self._lock:
            return dict(self._obs)

    # ---------- observations ----------

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

    def cooldown_until(self, tool: str) -> float | None:
        """该工具仍处于额度冷却期则返回截止时间，调度器据此不派发同工具任务。"""
        with self._lock:
            o = self._obs.get(tool) or {}
        lq = o.get("last_quota_at") or 0
        ls = o.get("last_success_at") or 0
        if lq <= ls:
            return None
        return o.get("reset_at")

    # ---------- status ----------

    def status(self, tool: str) -> QuotaStatus:
        with self._lock:
            o = dict(self._obs.get(tool) or {})
            cc = dict(self._ccusage) if (self._ccusage and tool == "claude") else None
        now = time.time()
        lq, ls, reset = o.get("last_quota_at"), o.get("last_success_at"), o.get("reset_at")

        if lq and (not ls or lq > ls):
            if reset and reset > now:
                st = QuotaStatus(tool, "limited", f"额度受限，约 {_clock(reset)} 恢复",
                                 "observed", reset)
            else:
                st = QuotaStatus(tool, "limited", "额度受限（恢复时间未知，退避重试中）",
                                 "observed")
        elif ls:
            st = QuotaStatus(tool, "ok", f"正常（{_clock(ls)} 有成功执行）", "observed")
        else:
            st = QuotaStatus(tool, "unknown", "未知（尚无执行观测）", "none")

        if cc:
            st.detail += f"；5h 窗口已用 ${cc['cost']:.2f}，{cc['reset']} 重置"
            st.source = (st.source if st.source != "none" else "") + "+ccusage"
            st.source = st.source.lstrip("+") or "ccusage"
        return st

    # ---------- optional ccusage background refresh ----------

    def start_background(self) -> None:
        if not self._ccusage_bin:
            return
        self._thread = threading.Thread(
            target=self._ccusage_loop, name="agentbar-ccusage", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _ccusage_loop(self) -> None:
        while not self._stop.is_set():
            self._refresh_ccusage()
            self._stop.wait(90)

    def _refresh_ccusage(self) -> None:
        try:
            r = subprocess.run(
                [self._ccusage_bin, "blocks", "--active", "--json"],
                capture_output=True, text=True, timeout=20,
            )
            data = json.loads(r.stdout)
            block = next((b for b in data.get("blocks", []) if b.get("isActive")), None)
            if not block:
                with self._lock:
                    self._ccusage = None
                return
            end = block.get("endTime", "")
            reset = end[11:16] if len(end) >= 16 else "?"
            with self._lock:
                self._ccusage = {"cost": float(block.get("costUSD", 0)), "reset": reset}
        except Exception:
            with self._lock:
                self._ccusage = None  # 取不到就不显示，不伪造
