"""Real quota/usage fetchers — approach mirrored from ylab/aiusagebar (Swift).

Claude:  GET https://api.anthropic.com/api/oauth/usage
         AgentBar 运行时只使用用户在额度设置中显式保存的 OAuth Access Token；
         响应: {five_hour|seven_day|seven_day_opus|seven_day_sonnet:
                {utilization: 0-100, resets_at: ISO8601}}

Codex:   GET https://chatgpt.com/backend-api/wham/usage
         AgentBar 运行时使用显式保存的 OAuth Access Token，以及可选 Account ID；
         响应: {rate_limits: {primary|secondary:
                {used_percent: 0-100, resets_at: epoch_s, window_duration_mins}}}

诚实原则：拿不到就返回带 error 的结果或 None，绝不编造数字。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime

log = logging.getLogger("agentbar.usage")

HTTP_TIMEOUT = 12


@dataclass
class UsageWindow:
    label: str                 # "5h" | "7d" | "7d Opus" | "7d Sonnet" | "本月"
    used_percent: float
    resets_at: float | None = None
    # 信用额度类 provider（MyToken / Tokenverse）附带原始额度值，供 UI 按 unit 展示。
    used: float | None = None       # 已用（credits 或 token 数）
    total: float | None = None      # 总额度
    unit: str | None = None         # "credits" | "percent" | "token"
    # None 表示账户级通用窗口；有值时只限对应模型家族/额度桶。
    model: str | None = None
    limited: bool = False

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "used_percent": round(self.used_percent, 1),
            "resets_at": self.resets_at,
            "model": self.model,
            "limited": self.limited,
            "used": self.used,
            "total": self.total,
            "unit": self.unit,
        }


@dataclass
class UsageSnapshot:
    tool: str
    windows: list[UsageWindow] = field(default_factory=list)
    plan: str | None = None
    source: str = ""
    fetched_at: float = field(default_factory=time.time)
    error: str | None = None
    limited: bool = False
    model: str | None = None
    available_models: list[str] = field(default_factory=list)

    @property
    def primary(self) -> UsageWindow | None:
        return self.windows[0] if self.windows else None

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "windows": [w.to_dict() for w in self.windows],
            "plan": self.plan,
            "source": self.source,
            "fetched_at": self.fetched_at,
            "error": self.error,
            "limited": self.limited,
            "model": self.model,
            "available_models": self.available_models,
        }


def _http_get_json(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _parse_iso(value) -> float | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _jwt_payload(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


# ================= Claude =================


class ClaudeUsageFetcher:
    tool = "claude"
    URL = "https://api.anthropic.com/api/oauth/usage"
    _WINDOW_KEYS = [
        ("five_hour", "5h"),
        ("seven_day", "7d"),
        ("seven_day_opus", "7d Opus"),
        ("seven_day_sonnet", "7d Sonnet"),
    ]

    def __init__(self, access_token: str = "", model: str = ""):
        self.access_token = (access_token or "").strip()
        self.model = (model or "").strip()

    def load_credentials(self) -> dict | None:
        if not self.access_token:
            return None
        return {"token": self.access_token, "plan": None}

    def fetch(self, interactive: bool = False) -> UsageSnapshot | None:
        creds = self.load_credentials()
        if not creds:
            return UsageSnapshot(
                self.tool, source="oauth_api",
                error="未配置 Claude OAuth Access Token",
            )
        headers = {
            "Authorization": f"Bearer {creds['token']}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code/2.1.0",
        }
        try:
            data = _http_get_json(self.URL, headers)
        except urllib.error.HTTPError as e:
            return UsageSnapshot(self.tool, source="oauth_api",
                                 error=f"usage 接口 HTTP {e.code}")
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            return UsageSnapshot(self.tool, source="oauth_api", error=f"网络错误: {e}")
        return self.parse(data, plan=creds.get("plan"))

    def parse(self, data: dict, plan: str | None = None) -> UsageSnapshot:
        windows = []
        selected = self.model.casefold()
        selected_family = (
            "opus" if "opus" in selected else "sonnet" if "sonnet" in selected else ""
        )
        for key, label in self._WINDOW_KEYS:
            # 选中 Opus/Sonnet 时，保留账户通用窗口，只隐藏另一个
            # 模型家族的专属周窗口。其他模型仍展示账户通用额度。
            if key.startswith("seven_day_") and (
                not selected_family or not key.endswith(selected_family)
            ):
                continue
            w = data.get(key)
            if not isinstance(w, dict) or w.get("utilization") is None:
                continue
            windows.append(UsageWindow(
                label=label,
                used_percent=max(0.0, min(100.0, float(w["utilization"]))),
                resets_at=_parse_iso(w.get("resets_at")),
                model=selected_family if key.startswith("seven_day_") else None,
            ))
        snap = UsageSnapshot(
            self.tool,
            windows=windows,
            plan=plan,
            source="oauth_api",
            model=self.model or None,
            available_models=["opus", "sonnet"],
        )
        if not windows:
            snap.error = "usage 接口未返回可识别的额度窗口"
        return snap


# ================= Codex =================


class CodexUsageFetcher:
    tool = "codex"
    URL = "https://chatgpt.com/backend-api/wham/usage"

    def __init__(self, access_token: str = "", account_id: str = "", model: str = ""):
        self.access_token = (access_token or "").strip()
        self.account_id = (account_id or "").strip()
        self.model = (model or "").strip()

    def load_credentials(self) -> dict | None:
        if not self.access_token:
            return None
        access_payload = _jwt_payload(self.access_token)
        auth_claim = access_payload.get("https://api.openai.com/auth") or {}
        account_id = (
            self.account_id
            or auth_claim.get("chatgpt_account_id")
            or access_payload.get("chatgpt_account_id")
            or access_payload.get("account_id")
        )
        plan = auth_claim.get("chatgpt_plan_type") or access_payload.get("chatgpt_plan_type")
        return {"token": self.access_token, "account_id": account_id, "plan": plan}

    def fetch(self, interactive: bool = False) -> UsageSnapshot | None:
        creds = self.load_credentials()
        if not creds:
            return UsageSnapshot(self.tool, source="wham_api",
                                 error="未配置 Codex OAuth Access Token")
        headers = {
            "Authorization": f"Bearer {creds['token']}",
            "Accept": "*/*",
            "Referer": "https://chatgpt.com/codex/cloud/settings/analytics",
            "x-openai-target-path": "/backend-api/wham/usage",
            "x-openai-target-route": "/backend-api/wham/usage",
            "User-Agent": "agentbar/0.2",
        }
        if creds.get("account_id"):
            headers["chatgpt-account-id"] = creds["account_id"]
        try:
            data = _http_get_json(self.URL, headers)
        except urllib.error.HTTPError as e:
            return UsageSnapshot(self.tool, source="wham_api",
                                 error=f"usage 接口 HTTP {e.code}")
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            return UsageSnapshot(self.tool, source="wham_api", error=f"网络错误: {e}")
        return self.parse(data, plan=creds.get("plan"))

    @staticmethod
    def _model_key(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())

    @staticmethod
    def _windows(
        limits: dict,
        *,
        model: str | None = None,
        limited: bool = False,
        label_prefix: str = "",
    ) -> list[UsageWindow]:
        windows = []
        for keys, fallback_label in (
            (("primary", "primary_window"), "5h"),
            (("secondary", "secondary_window"), "7d"),
        ):
            w = next((limits.get(key) for key in keys if isinstance(limits.get(key), dict)), None)
            if not isinstance(w, dict):
                continue
            used = w.get("used_percent", w.get("usedPercent"))
            if used is None:
                continue
            mins = w.get("window_duration_mins") or w.get("windowDurationMins")
            seconds = w.get("limit_window_seconds") or w.get("limitWindowSeconds")
            label = fallback_label
            if mins:
                label = f"{round(mins / 60)}h" if mins < 2880 else f"{round(mins / 1440)}d"
            elif seconds:
                label = f"{round(seconds / 3600)}h" if seconds < 2880 * 60 else f"{round(seconds / 86400)}d"
            reset = w.get(
                "resets_at",
                w.get("resetsAt", w.get("reset_at", w.get("resetAt"))),
            )
            if not reset and w.get("reset_after_seconds"):
                reset = time.time() + float(w["reset_after_seconds"])
            windows.append(UsageWindow(
                label=f"{label_prefix}{label}",
                used_percent=max(0.0, min(100.0, float(used))),
                resets_at=float(reset) if reset else None,
                model=model,
                limited=limited,
            ))
        return windows

    @staticmethod
    def _limited(limits: dict) -> bool:
        return (
            bool(limits.get("limit_reached", limits.get("limitReached")))
            or limits.get("allowed") is False
        )

    def parse(self, data: dict, plan: str | None = None) -> UsageSnapshot:
        """Parse both observed WHAM response shapes.

        Older clients expose ``rate_limits.primary`` with minute windows, while
        the current ChatGPT-backed response exposes ``rate_limit.primary_window``
        with second windows and a ``limit_reached`` boolean.
        """
        additional = [
            item for item in (data.get("additional_rate_limits") or [])
            if isinstance(item, dict)
        ]
        # Persist/select the stable metered_feature whenever the endpoint exposes
        # one. limit_name is presentation text and can be renamed independently.
        available = [
            str(item.get("metered_feature") or item.get("limit_name") or "").strip()
            for item in additional
        ]
        available = [name for name in available if name]
        selected_item = None
        if self.model:
            wanted = self._model_key(self.model)
            for item in additional:
                names = (str(item.get("limit_name") or ""), str(item.get("metered_feature") or ""))
                if wanted and wanted in {self._model_key(name) for name in names}:
                    selected_item = item
                    break
            if selected_item is None:
                choices = "、".join(available[:6]) or "暂无模型专属额度"
                return UsageSnapshot(
                    self.tool,
                    plan=plan or data.get("plan_type"),
                    source="wham_api",
                    error=f"未找到模型额度 {self.model!r}；接口可用：{choices}",
                    model=self.model,
                    available_models=available,
                )

        account_limits = data.get("rate_limits") or data.get("rate_limit") or {}
        account_limited = self._limited(account_limits)
        selected_label = (
            str(
                selected_item.get("limit_name")
                or selected_item.get("metered_feature")
                or self.model
            )
            if selected_item is not None
            else (self.model or None)
        )
        if selected_item is not None:
            selected_limits = selected_item.get("rate_limit") or {}
            selected_limited = self._limited(selected_limits)
            windows = self._windows(
                account_limits,
                limited=account_limited,
                label_prefix="账户 ",
            )
            windows.extend(self._windows(
                selected_limits,
                model=selected_label,
                limited=selected_limited,
                label_prefix=f"{selected_label} ",
            ))
        else:
            selected_limited = False
            windows = self._windows(account_limits, limited=account_limited)
        snap = UsageSnapshot(
            self.tool,
            windows=windows,
            plan=plan or data.get("plan_type"),
            source="wham_api",
            # 保留 snapshot 级标记给旧 UI；调度决策应按 window.model
            # 逐窗口判断，避免模型专属限额污染账户级窗口。
            limited=account_limited or selected_limited,
            model=selected_label,
            available_models=available,
        )
        if not windows:
            snap.error = "usage 接口未返回 rate_limits"
        return snap


# ================= 快手内部 provider（MyToken / Tokenverse） =================
#
# 两者都是凭 corp SSO cookie 访问的「月度信用额度」接口，响应统一为
# {status, message, data} 信封（status==200 为成功）。诚实原则同上：cookie 缺失
# 或接口失败 → 返回带 error 的 UsageSnapshot，绝不编造额度。
#
# used_percent 始终按「信用额度」算（驱动环形进度 + 限额判定）；window 附带
# used/total/unit 供 UI 按用户选择的 unit（credits/percent/token）展示表头数字。


def _corp_envelope(url: str, cookie: str, extra_headers: dict | None = None) -> dict:
    """请求 corp 接口并校验 {status,message,data} 信封，返回 data；失败抛 RuntimeError。"""
    headers = {
        "Cookie": cookie,
        "Accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if extra_headers:
        headers.update(extra_headers)
    body = _http_get_json(url, headers)
    status = body.get("status")
    if status != 200:
        raise RuntimeError(body.get("message") or f"接口返回 status={status}")
    if body.get("data") is None:
        raise RuntimeError("接口响应缺少 data")
    return body["data"]


def _month_start_str(now: float | None = None) -> str:
    lt = time.localtime(now if now is not None else time.time())
    return time.strftime("%Y-%m-01", lt)


def _next_month_start(now: float | None = None) -> float:
    lt = time.localtime(now if now is not None else time.time())
    year, month = lt.tm_year, lt.tm_mon
    if month == 12:
        year, month = year + 1, 1
    else:
        month += 1
    return time.mktime((year, month, 1, 0, 0, 0, 0, 0, -1))


def _credit_window(used: float, total: float | None, unit: str,
                   resets_at: float | None, token_used: float | None,
                   label: str = "本月") -> UsageWindow:
    """把「信用额度用量」组装成一个展示窗口；used_percent 恒按 credits 算。"""
    if total and total > 0:
        pct = max(0.0, min(100.0, used / total * 100.0))
    else:
        pct = 0.0
    if unit == "token":
        disp_used, disp_total = (token_used if token_used is not None else 0.0), None
    elif unit == "percent":
        disp_used, disp_total = pct, 100.0
    else:  # credits
        disp_used, disp_total = used, total
    return UsageWindow(label=label, used_percent=pct, resets_at=resets_at,
                       used=disp_used, total=disp_total, unit=unit)


class MyTokenUsageFetcher:
    """MyToken（mytoken.corp.kuaishou.com）月度信用额度。"""

    tool = "mytoken"
    BASE = "https://mytoken.corp.kuaishou.com"

    def __init__(self, cookie: str = "", unit: str = "credits",
                 refresh_seconds: int = 300):
        self.cookie = (cookie or "").strip()
        self.unit = unit if unit in ("credits", "percent", "token") else "credits"
        self.refresh_seconds = max(60, int(refresh_seconds or 300))

    def _username(self) -> str:
        data = _corp_envelope(
            f"{self.BASE}/api/auth/sso/user", self.cookie,
            {"Referer": f"{self.BASE}/usage"},
        )
        name = (data.get("name") or data.get("username") or data.get("userName")
                or data.get("loginName") or "").strip()
        if not name:
            raise RuntimeError("SSO 用户接口未返回用户名")
        return name

    def _monthly_tokens(self, username: str) -> float | None:
        month_start_ms = int(time.mktime(time.strptime(_month_start_str(), "%Y-%m-%d")) * 1000)
        now_ms = int(time.time() * 1000)
        url = (f"{self.BASE}/api/v1/billing/usage/token-summary"
               f"?granularity=day&startTime={month_start_ms}&endTime={now_ms}")
        try:
            data = _corp_envelope(url, self.cookie, {"kwaipilot-username": username})
        except (RuntimeError, urllib.error.URLError, OSError):
            return None
        buckets = data.get("buckets") or []
        return float(sum((b.get("totalTokens") or 0) for b in buckets))

    def fetch(self, interactive: bool = False) -> UsageSnapshot | None:
        if not self.cookie:
            return UsageSnapshot(self.tool, source="mytoken_api",
                                 error="未配置 cookie（config.json → providers.mytoken.cookie）")
        try:
            username = self._username()
            data = _corp_envelope(
                f"{self.BASE}/api/v1/billing/account", self.cookie,
                {"kwaipilot-username": username},
            )
        except urllib.error.HTTPError as e:
            return UsageSnapshot(self.tool, source="mytoken_api", error=f"HTTP {e.code}")
        except (RuntimeError, urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            return UsageSnapshot(self.tool, source="mytoken_api", error=str(e))

        summary = data.get("summary") or {}
        account = data.get("account") or {}
        total = summary.get("creditTotal")
        used = summary.get("creditUsed")
        if used is None or total is None:
            return UsageSnapshot(self.tool, source="mytoken_api",
                                 error="account 接口未返回 creditUsed/creditTotal")
        renew_ms = summary.get("renewAt")
        token_used = self._monthly_tokens(username) if self.unit == "token" else None
        window = _credit_window(
            float(used), float(total), self.unit,
            resets_at=(renew_ms / 1000.0) if renew_ms else _next_month_start(),
            token_used=token_used,
        )
        plan = account.get("tierName") or account.get("tierCode")
        return UsageSnapshot(
            self.tool, windows=[window], plan=plan, source="mytoken_api",
            limited=bool(window.used_percent >= 99.9),
        )


class TokenverseUsageFetcher:
    """Tokenverse（tokenverse.corp.kuaishou.com）月度信用额度。"""

    tool = "tokenverse"
    BASE = "https://tokenverse.corp.kuaishou.com"

    def __init__(self, cookie: str = "", unit: str = "credits",
                 refresh_seconds: int = 300):
        self.cookie = (cookie or "").strip()
        self.unit = unit if unit in ("credits", "percent", "token") else "credits"
        self.refresh_seconds = max(60, int(refresh_seconds or 300))

    _PLAN_NAMES = {1: "Standard"}

    def fetch(self, interactive: bool = False) -> UsageSnapshot | None:
        if not self.cookie:
            return UsageSnapshot(self.tool, source="tokenverse_api",
                                 error="未配置 cookie（config.json → providers.tokenverse.cookie）")
        try:
            plan = _corp_envelope(f"{self.BASE}/api/coding-plan/status", self.cookie)
            summary = _corp_envelope(
                f"{self.BASE}/api/coding-plan/usage/summary?startDate={_month_start_str()}",
                self.cookie,
            )
        except urllib.error.HTTPError as e:
            return UsageSnapshot(self.tool, source="tokenverse_api", error=f"HTTP {e.code}")
        except (RuntimeError, urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            return UsageSnapshot(self.tool, source="tokenverse_api", error=str(e))

        monthly = plan.get("monthlyCredits")
        if monthly is None:
            monthly = (plan.get("openModelCreditsPerMon") or 0) + (
                plan.get("closedSourceMonthlyCredits")
                or plan.get("closeModelCreditsPerMon") or 0
            )
        used = summary.get("totalCredits")
        if used is None:
            return UsageSnapshot(self.tool, source="tokenverse_api",
                                 error="usage summary 未返回 totalCredits")
        token_used = (float(summary.get("totalTokens") or 0)
                      if self.unit == "token" else None)
        window = _credit_window(
            float(used), float(monthly or 0), self.unit,
            resets_at=_next_month_start(), token_used=token_used,
        )
        return UsageSnapshot(
            self.tool, windows=[window],
            plan=self._PLAN_NAMES.get(plan.get("planType")),
            source="tokenverse_api",
            limited=bool(window.used_percent >= 99.9),
        )


_CORP_FETCHERS = {
    "mytoken": MyTokenUsageFetcher,
    "tokenverse": TokenverseUsageFetcher,
}


def get_usage_fetchers(settings=None) -> dict[str, object]:
    """只创建用户显式启用且已配凭据的额度来源。

    没有 Settings 也不创建隐式来源，确保任何调用路径都不会读取 CLI
    登录文件或系统凭据存储。
    """
    fetchers: dict[str, object] = {}
    if settings is not None:
        sources = getattr(settings, "quota_sources", None) or {}
        claude = sources.get("claude") or {}
        if claude.get("enabled") and (claude.get("access_token") or "").strip():
            fetchers["claude"] = ClaudeUsageFetcher(
                access_token=claude.get("access_token", ""),
                model=claude.get("model", ""),
            )
        codex = sources.get("codex") or {}
        if codex.get("enabled") and (codex.get("access_token") or "").strip():
            fetchers["codex"] = CodexUsageFetcher(
                access_token=codex.get("access_token", ""),
                account_id=codex.get("account_id", ""),
                model=codex.get("model", ""),
            )
    providers = getattr(settings, "providers", None) or {}
    for name, cls in _CORP_FETCHERS.items():
        cfg = providers.get(name) or {}
        if cfg.get("enabled") and (cfg.get("cookie") or "").strip():
            fetchers[name] = cls(
                cookie=cfg.get("cookie", ""),
                unit=cfg.get("unit", "credits"),
                refresh_seconds=cfg.get("refresh_seconds", 300),
            )
    return fetchers
