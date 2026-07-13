"""Real quota/usage fetchers — approach mirrored from ylab/aiusagebar (Swift).

Claude:  GET https://api.anthropic.com/api/oauth/usage
         凭据链: env CLAUDE_CODE_OAUTH_TOKEN → ~/.claude/.credentials.json
                → macOS Keychain "Claude Code-credentials"（默认静默读取，不弹窗）
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
    label: str                 # "5h" | "7d" | "7d Opus" | "7d Sonnet"
    used_percent: float
    resets_at: float | None = None

    def to_dict(self) -> dict:
        return {"label": self.label, "used_percent": round(self.used_percent, 1),
                "resets_at": self.resets_at}


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
        "expires_at": (expires_ms / 1000.0) if expires_ms else None,
        "plan": oauth.get("subscriptionType") or oauth.get("rateLimitTier"),
    }


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
        cred_file = Path.home() / ".claude" / ".credentials.json"
        if cred_file.exists():
            try:
                creds = _parse_claude_credentials(cred_file.read_bytes())
            except OSError:
                creds = None
            if creds:
                return creds
        raw = _keychain_read(interactive=interactive)
        if raw:
            return _parse_claude_credentials(raw)
        return None

    def fetch(self, interactive: bool = False) -> UsageSnapshot | None:
        creds = self.load_credentials(interactive=interactive)
        if not creds:
            return UsageSnapshot(
                self.tool, source="oauth_api",
                error="未读到 Claude 凭据（Keychain 静默读取被拒？菜单里可手动授权）",
            )
        if creds.get("expires_at") and creds["expires_at"] < time.time():
            return UsageSnapshot(self.tool, source="oauth_api",
                                 error="Claude OAuth 凭据已过期，请运行 claude 重新登录")
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


def get_usage_fetchers() -> dict[str, object]:
    return {"claude": ClaudeUsageFetcher(), "codex": CodexUsageFetcher()}
