"""Real quota/usage fetchers — approach mirrored from ylab/aiusagebar (Swift).

Claude:  GET https://api.anthropic.com/api/oauth/usage
         凭据链: env CLAUDE_CODE_OAUTH_TOKEN → 本地缓存 ~/.agentbar/claude_credentials.json
                → ~/.claude/.credentials.json
                → macOS Keychain "Claude Code-credentials"（读到后缓存 access + refresh
                  token，后续自动续期，避免反复弹窗）
         响应: {five_hour|seven_day|seven_day_opus|seven_day_sonnet:
                {utilization: 0-100, resets_at: ISO8601}}

Codex:   GET https://chatgpt.com/backend-api/wham/usage
         凭据: $CODEX_HOME/auth.json（默认 ~/.codex/auth.json）tokens.access_token
               + chatgpt-account-id（tokens.account_id 或 JWT claim）
         响应: {rate_limits: {primary|secondary:
                {used_percent: 0-100, resets_at: epoch_s, window_duration_mins}}}

诚实原则：拿不到就返回带 error 的结果或 None，绝不编造数字。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

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

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "used_percent": round(self.used_percent, 1),
            "resets_at": self.resets_at,
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
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


# ================= Claude =================

KEYCHAIN_SERVICE = "Claude Code-credentials"
CLAUDE_OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
# Claude Code 公开 OAuth client id（与本机 Claude Code CLI 使用的客户端一致）。
CLAUDE_CODE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_REFRESH_SKEW_SECONDS = 60

# 本地凭据缓存：Keychain 成功读到一次后写回这里（0600），包含
# refresh token。后续 access token 过期时 AgentBar 自动续期，不再触碰
# Keychain，从而避免 macOS 反复弹出"Python3 想访问 Claude Code-credentials"。
CLAUDE_CRED_CACHE = Path.home() / ".agentbar" / "claude_credentials.json"
_CLAUDE_REFRESH_LOCK = threading.Lock()


def _cache_read_credentials() -> dict | None:
    """读取本地凭据缓存；过期凭据也保留，供 refresh token 续期。"""
    try:
        creds = json.loads(CLAUDE_CRED_CACHE.read_bytes())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(creds, dict):
        return None
    if not ((creds.get("token") or "").strip()
            or (creds.get("refresh_token") or "").strip()):
        return None
    return creds


def _cache_write_credentials(creds: dict) -> None:
    """把凭据写入本地缓存文件（0600 权限，仅当前用户可读）。"""
    try:
        CLAUDE_CRED_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CLAUDE_CRED_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(creds), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, CLAUDE_CRED_CACHE)
    except OSError:
        log.warning("写入 Claude 凭据缓存失败: %s", CLAUDE_CRED_CACHE, exc_info=True)


def _keychain_read(interactive: bool = False) -> bytes | None:
    """静默读取 Keychain（interactive=True 允许系统弹窗授权，仅由用户显式触发）。"""
    try:
        import Security  # pyobjc-framework-Security
    except ImportError:
        return None
    query = {
        Security.kSecClass: Security.kSecClassGenericPassword,
        Security.kSecAttrService: KEYCHAIN_SERVICE,
        Security.kSecMatchLimit: Security.kSecMatchLimitOne,
        Security.kSecReturnData: True,
        Security.kSecUseAuthenticationUI: (
            Security.kSecUseAuthenticationUIAllow
            if interactive
            else Security.kSecUseAuthenticationUIFail
        ),
    }
    status, data = Security.SecItemCopyMatching(query, None)
    if status != 0 or data is None:
        return None
    return bytes(data)


def _parse_claude_credentials(raw: bytes) -> dict | None:
    try:
        oauth = json.loads(raw.decode("utf-8")).get("claudeAiOauth") or {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    token = (oauth.get("accessToken") or "").strip()
    if not token:
        return None
    expires_ms = oauth.get("expiresAt")
    return {
        "token": token,
        "refresh_token": (oauth.get("refreshToken") or "").strip() or None,
        "expires_at": (expires_ms / 1000.0) if expires_ms else None,
        "plan": oauth.get("subscriptionType") or oauth.get("rateLimitTier"),
        "scopes": oauth.get("scopes"),
    }


def _credentials_expired(creds: dict) -> bool:
    """快过期也视为过期，避免在 usage 请求途中失效。"""
    exp = creds.get("expires_at")
    if not exp:
        return False
    try:
        return float(exp) <= time.time() + CLAUDE_REFRESH_SKEW_SECONDS
    except (TypeError, ValueError):
        return True


def _refresh_claude_credentials(creds: dict) -> dict | None:
    """用 refresh token 换取新 access token；失败时不泄露凭据内容。"""
    refresh_token = (creds.get("refresh_token") or "").strip()
    if not refresh_token:
        return None
    payload = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLAUDE_CODE_CLIENT_ID,
    }).encode("utf-8")
    req = urllib.request.Request(
        CLAUDE_OAUTH_TOKEN_URL,
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "agentbar/claude-oauth-refresh",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError,
            json.JSONDecodeError) as exc:
        log.warning("Claude OAuth 自动续期失败: %s", type(exc).__name__)
        return None
    token = (data.get("access_token") or "").strip()
    expires_in = data.get("expires_in")
    if not token or expires_in is None:
        log.warning("Claude OAuth 自动续期响应缺少 token 或 expires_in")
        return None
    try:
        expires_at = time.time() + float(expires_in)
    except (TypeError, ValueError):
        log.warning("Claude OAuth 自动续期响应的 expires_in 无效")
        return None
    updated = dict(creds)
    updated.update({
        "token": token,
        # Anthropic 可能轮换 refresh token；未返回时沿用旧值。
        "refresh_token": (data.get("refresh_token") or refresh_token).strip(),
        "expires_at": expires_at,
    })
    if data.get("scope") is not None:
        updated["scopes"] = data["scope"]
    return updated


def _ensure_fresh_credentials(creds: dict | None) -> dict | None:
    """返回可用凭据；必要时只刷新一次并原子写回缓存。"""
    if not creds:
        return None
    if not _credentials_expired(creds):
        return creds
    if not (creds.get("refresh_token") or "").strip():
        return None
    with _CLAUDE_REFRESH_LOCK:
        # 其他请求可能已在等锁期间完成续期。
        cached = _cache_read_credentials()
        if cached and not _credentials_expired(cached):
            return cached
        # cached 若仍过期，必须使用调用方刚读到的凭据；它可能
        # 来自 Claude CLI 刚轮换过的 Keychain，比旧缓存更新。
        refreshed = _refresh_claude_credentials(creds)
        if refreshed:
            _cache_write_credentials(refreshed)
        return refreshed


class ClaudeUsageFetcher:
    tool = "claude"
    URL = "https://api.anthropic.com/api/oauth/usage"
    _WINDOW_KEYS = [
        ("five_hour", "5h"),
        ("seven_day", "7d"),
        ("seven_day_opus", "7d Opus"),
        ("seven_day_sonnet", "7d Sonnet"),
    ]

    def load_credentials(self, interactive: bool = False) -> dict | None:
        for env_key in ("CLAUDE_CODE_OAUTH_TOKEN", "CODEXBAR_CLAUDE_OAUTH_TOKEN"):
            token = (os.environ.get(env_key) or "").strip()
            if token:
                return {"token": token, "expires_at": None, "plan": None}
        # 本地缓存优先：过期时用 refresh token 自动续期，不碰 Keychain。
        cached = _cache_read_credentials()
        refreshable = cached if cached and cached.get("refresh_token") else None
        fresh = _ensure_fresh_credentials(cached)
        if fresh:
            return fresh
        cred_file = Path.home() / ".claude" / ".credentials.json"
        if cred_file.exists():
            try:
                creds = _parse_claude_credentials(cred_file.read_bytes())
            except OSError:
                creds = None
            if creds and creds.get("refresh_token"):
                refreshable = creds
            fresh = _ensure_fresh_credentials(creds)
            if fresh:
                return fresh
        raw = _keychain_read(interactive=interactive)
        if raw:
            creds = _parse_claude_credentials(raw)
            if creds:
                # 先落盘 refresh token：即使当下网络不能续期，下次也无需
                # 再读 Keychain。
                _cache_write_credentials(creds)
                return _ensure_fresh_credentials(creds) or creds
        # 有 refresh token 但当下网络续期失败时，返回过期凭据只为
        # 让 fetch() 显示“自动续期重试”，不再误导用户反复授权 Keychain。
        return refreshable

    def fetch(self, interactive: bool = False) -> UsageSnapshot | None:
        creds = self.load_credentials(interactive=interactive)
        if not creds:
            return UsageSnapshot(
                self.tool, source="oauth_api",
                error=("未读到可用的 Claude 凭据（只需手动授权 Keychain 一次，"
                       "后续由 AgentBar 自动续期）"),
            )
        if creds.get("expires_at") and creds["expires_at"] < time.time():
            if creds.get("refresh_token"):
                return UsageSnapshot(
                    self.tool, source="oauth_api",
                    error="Claude OAuth 自动续期暂时失败，AgentBar 将后台重试",
                )
            return UsageSnapshot(
                self.tool, source="oauth_api",
                error="Claude OAuth 凭据已过期，请运行 claude 重新登录",
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
        for key, label in self._WINDOW_KEYS:
            w = data.get(key)
            if not isinstance(w, dict) or w.get("utilization") is None:
                continue
            windows.append(UsageWindow(
                label=label,
                used_percent=max(0.0, min(100.0, float(w["utilization"]))),
                resets_at=_parse_iso(w.get("resets_at")),
            ))
        snap = UsageSnapshot(self.tool, windows=windows, plan=plan, source="oauth_api")
        if not windows:
            snap.error = "usage 接口未返回可识别的额度窗口"
        return snap


# ================= Codex =================


class CodexUsageFetcher:
    tool = "codex"
    URL = "https://chatgpt.com/backend-api/wham/usage"

    @staticmethod
    def _auth_path() -> Path:
        home = (os.environ.get("CODEX_HOME") or "").strip()
        base = Path(home).expanduser() if home else Path.home() / ".codex"
        return base / "auth.json"

    def load_credentials(self) -> dict | None:
        p = self._auth_path()
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        tokens = data.get("tokens") or data
        token = (tokens.get("access_token") or tokens.get("accessToken") or "").strip()
        if not token:
            return None
        id_token = tokens.get("id_token") or ""
        id_payload = _jwt_payload(id_token)
        access_payload = _jwt_payload(token)
        auth_claim = (
            id_payload.get("https://api.openai.com/auth")
            or access_payload.get("https://api.openai.com/auth")
            or {}
        )
        account_id = (
            tokens.get("account_id")
            or auth_claim.get("chatgpt_account_id")
            or access_payload.get("chatgpt_account_id")
            or access_payload.get("account_id")
        )
        plan = auth_claim.get("chatgpt_plan_type") or access_payload.get("chatgpt_plan_type")
        return {"token": token, "account_id": account_id, "plan": plan}

    def fetch(self, interactive: bool = False) -> UsageSnapshot | None:
        creds = self.load_credentials()
        if not creds:
            return UsageSnapshot(self.tool, source="wham_api",
                                 error="未读到 ~/.codex/auth.json（先运行 codex login）")
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

    def parse(self, data: dict, plan: str | None = None) -> UsageSnapshot:
        """Parse both observed WHAM response shapes.

        Older clients expose ``rate_limits.primary`` with minute windows, while
        the current ChatGPT-backed response exposes ``rate_limit.primary_window``
        with second windows and a ``limit_reached`` boolean.
        """
        limits = data.get("rate_limits") or data.get("rate_limit") or {}
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
            reset = w.get("resets_at", w.get("resetsAt"))
            if not reset and w.get("reset_after_seconds"):
                reset = time.time() + float(w["reset_after_seconds"])
            windows.append(UsageWindow(
                label=label,
                used_percent=max(0.0, min(100.0, float(used))),
                resets_at=float(reset) if reset else None,
            ))
        snap = UsageSnapshot(
            self.tool,
            windows=windows,
            plan=plan or data.get("plan_type"),
            source="wham_api",
            limited=bool(limits.get("limit_reached")),
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
        start = _next_month_start() - 1  # 仅用于本月窗口，取月初到现在
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
    """内置 claude/codex，外加 config 里 enabled 且已配 cookie 的 corp provider。"""
    fetchers: dict[str, object] = {
        "claude": ClaudeUsageFetcher(),
        "codex": CodexUsageFetcher(),
    }
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
