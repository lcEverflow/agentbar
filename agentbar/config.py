"""Settings: config.json in the state dir. User-editable; app writes defaults once."""

from __future__ import annotations

import copy
import fcntl
import json
import math
import os
import secrets
import stat
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PORT = 8737
CONFIG_SCHEMA_VERSION = 3
LEGACY_CLAUDE_CREDENTIAL_CACHE = "claude_credentials.json"
LEGACY_CLAUDE_CREDENTIAL_TEMP = "claude_credentials.tmp"

# 额度来源默认关闭。Claude 只提供 CLI 登录状态（无官方第三方
# 订阅额度 API）；Codex 通过官方 App Server 获取可选 limitId。配置中
# 绝不保存两者的 OAuth token 或 account id。
DEFAULT_QUOTA_SOURCES: dict = {
    "claude": {"enabled": False},
    "codex": {
        "enabled": False,
        "model": "",
    },
}

# 快手内部额度 provider（MyToken / Tokenverse）默认配置。
# 均为凭 corp SSO cookie 访问的信用额度（credits）接口，默认关闭——
# 用户在 config.json 里贴上 cookie 并置 enabled=true 后才会拉取、展示。
# unit: "credits"（信用额度数） | "percent"（已用百分比） | "token"（月度 token 数）
DEFAULT_PROVIDERS: dict = {
    "mytoken": {"enabled": False, "cookie": "", "unit": "credits", "refresh_seconds": 300},
    "tokenverse": {"enabled": False, "cookie": "", "unit": "credits", "refresh_seconds": 300},
}
PROVIDER_HOSTS = {
    "mytoken": "mytoken.corp.kuaishou.com",
    "tokenverse": "tokenverse.corp.kuaishou.com",
}
PROVIDER_LOGIN_URLS = {
    name: f"https://{host}/usage" for name, host in PROVIDER_HOSTS.items()
}
PROVIDER_UNITS = ("credits", "percent", "token")
_SAVE_LOCK = threading.RLock()


def _merge_providers(user: dict | None) -> dict:
    """Return canonical provider config for any JSON-shaped input."""
    merged = copy.deepcopy(DEFAULT_PROVIDERS)
    user = user if isinstance(user, dict) else {}
    for name, defaults in merged.items():
        override = user.get(name)
        if isinstance(override, dict):
            enabled = override.get("enabled")
            defaults["enabled"] = enabled if isinstance(enabled, bool) else False

            cookie = override.get("cookie")
            defaults["cookie"] = (
                cookie.strip()
                if isinstance(cookie, str)
                and len(cookie) <= 65_536
                and not any(char in cookie for char in "\r\n\x00")
                else ""
            )

            unit = override.get("unit")
            if isinstance(unit, str) and unit in PROVIDER_UNITS:
                defaults["unit"] = unit

            seconds = override.get("refresh_seconds", defaults["refresh_seconds"])
            try:
                parsed_seconds = int(seconds) if not isinstance(seconds, bool) else 0
            except (TypeError, ValueError, OverflowError):
                parsed_seconds = defaults["refresh_seconds"]
            defaults["refresh_seconds"] = (
                min(86_400, max(60, parsed_seconds))
                if parsed_seconds > 0
                else defaults["refresh_seconds"]
            )
            if defaults["enabled"] and not defaults["cookie"]:
                # A provider without credentials is not runnable.  Canonicalize
                # the half-configured state instead of leaving a source that can
                # only fail (and used to be retried after every restart).
                defaults["enabled"] = False
    return merged


def _merge_quota_sources(user: dict | None) -> dict:
    """Return the token-free v3 quota-source configuration."""
    merged = copy.deepcopy(DEFAULT_QUOTA_SOURCES)
    user = user if isinstance(user, dict) else {}
    for name, defaults in merged.items():
        override = user.get(name)
        if isinstance(override, dict):
            enabled = override.get("enabled")
            defaults["enabled"] = enabled if isinstance(enabled, bool) else False

            if name == "codex":
                value = override.get("model")
                defaults["model"] = (
                    value.strip()
                    if isinstance(value, str)
                    and len(value) <= 160
                    and not any(char in value for char in "\r\n\x00")
                    else ""
                )
    return merged


def default_state_dir() -> Path:
    env = os.environ.get("AGENTBAR_STATE_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".agentbar"


@dataclass
class Settings:
    state_dir: Path
    config_schema_version: int = CONFIG_SCHEMA_VERSION
    port: int = DEFAULT_PORT
    max_parallel: int = 1          # 全局并行度，1 = 串行
    per_tool_limit: int = 1        # 每个 AI CLI 的并行上限
    default_cwd: str = str(Path.home())
    allow_full_profile: bool = False   # 高权限档位默认关闭
    task_timeout_seconds: int = 7200   # 单任务运行上限
    backoff_minutes: list[float] = field(default_factory=lambda: [5, 15, 30, 60])
    usage_refresh_seconds: int = 120    # 订阅额度接口轮询间隔（最小 30 秒）
    # 默认仅启动时、保存设置后或用户手动点击时刷新；避免后台反复请求。
    usage_auto_refresh: bool = False
    tick_seconds: float = 1.0
    tool_paths: dict = field(default_factory=dict)  # 手动指定 CLI 路径: {"claude": "/path"}
    lan_access: bool = False       # 默认只绑定本机；显式开启后才允许局域网手机访问
    token: str = ""
    # 快手内部额度 provider 配置（见 DEFAULT_PROVIDERS）；cookie 空 / enabled=False 则不拉取。
    providers: dict = field(default_factory=lambda: copy.deepcopy(DEFAULT_PROVIDERS))
    # Claude CLI 登录状态 / Codex App Server 额度；不持久化 OAuth 凭据。
    quota_sources: dict = field(default_factory=lambda: copy.deepcopy(DEFAULT_QUOTA_SOURCES))
    # 菜单栏标题显示哪个 provider 的用量百分比：claude / codex / mytoken / tokenverse
    title_provider: str = "claude"
    _lock: threading.RLock = field(
        default_factory=threading.RLock,
        repr=False,
        compare=False,
    )

    @property
    def config_path(self) -> Path:
        return self.state_dir / "config.json"


_PERSISTED_KEYS = (
    "config_schema_version",
    "port",
    "max_parallel",
    "per_tool_limit",
    "default_cwd",
    "allow_full_profile",
    "task_timeout_seconds",
    "backoff_minutes",
    "usage_refresh_seconds",
    "usage_auto_refresh",
    "tool_paths",
    "lan_access",
    "token",
    "providers",
    "quota_sources",
    "title_provider",
)


def _normalize_loaded_settings(s: Settings) -> bool:
    """Repair malformed user-edited values before worker threads consume them."""
    before = {key: copy.deepcopy(getattr(s, key)) for key in _PERSISTED_KEYS}

    def bounded_int(value, default: int, low: int, high: int) -> int:
        if isinstance(value, bool):
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            return default
        return parsed if low <= parsed <= high else default

    def bounded_float(value, default: float, low: float, high: float) -> float:
        if isinstance(value, bool):
            return default
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return default
        return parsed if math.isfinite(parsed) and low <= parsed <= high else default

    s.port = bounded_int(s.port, DEFAULT_PORT, 1, 65_535)
    s.config_schema_version = CONFIG_SCHEMA_VERSION
    s.max_parallel = bounded_int(s.max_parallel, 1, 1, 64)
    s.per_tool_limit = bounded_int(s.per_tool_limit, 1, 1, 64)
    s.task_timeout_seconds = bounded_int(s.task_timeout_seconds, 7200, 1, 604_800)
    s.usage_refresh_seconds = bounded_int(s.usage_refresh_seconds, 120, 30, 86_400)
    s.tick_seconds = bounded_float(s.tick_seconds, 1.0, 0.05, 60.0)
    s.allow_full_profile = s.allow_full_profile if isinstance(s.allow_full_profile, bool) else False
    s.usage_auto_refresh = s.usage_auto_refresh if isinstance(s.usage_auto_refresh, bool) else False
    s.lan_access = s.lan_access if isinstance(s.lan_access, bool) else False
    s.default_cwd = s.default_cwd if isinstance(s.default_cwd, str) and s.default_cwd else str(Path.home())
    s.token = (
        s.token
        if isinstance(s.token, str)
        and 16 <= len(s.token) <= 512
        and not any(ord(char) < 32 or ord(char) == 127 for char in s.token)
        else ""
    )
    s.tool_paths = (
        {str(k): str(v) for k, v in s.tool_paths.items() if isinstance(k, str) and isinstance(v, str)}
        if isinstance(s.tool_paths, dict)
        else {}
    )
    raw_backoff = s.backoff_minutes if isinstance(s.backoff_minutes, list) else []
    backoff = []
    for value in raw_backoff:
        parsed = bounded_float(value, -1, 0, 10_080)
        if parsed >= 0:
            backoff.append(parsed)
    s.backoff_minutes = backoff or [5, 15, 30, 60]
    allowed_titles = {*DEFAULT_QUOTA_SOURCES, *DEFAULT_PROVIDERS}
    if not isinstance(s.title_provider, str) or s.title_provider not in allowed_titles:
        s.title_provider = "claude"
    return before != {key: getattr(s, key) for key in _PERSISTED_KEYS}


def load_settings(
    state_dir: Path | None = None,
    *,
    migrate: bool = True,
) -> Settings:
    sd = Path(state_dir).expanduser() if state_dir else default_state_dir()
    sd.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(sd, stat.S_IRWXU)  # 0700：state 目录含 token 与任务日志
    except OSError:
        pass

    cfg = sd / "config.json"
    lock_path = cfg.with_name(".config.lock")
    # A migrating load is a read/modify/write transaction: a missing or legacy
    # config generates a token and commits it below. Keep an exclusive lock
    # throughout. CLI client commands use migrate=False and a shared lock so
    # merely querying an older, still-running AgentBar never rotates the token
    # in its config behind the process's in-memory credentials.
    with _SAVE_LOCK:
        lock_fd = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        try:
            os.fchmod(lock_fd, stat.S_IRUSR | stat.S_IWUSR)
            fcntl.flock(lock_fd, fcntl.LOCK_EX if migrate else fcntl.LOCK_SH)
            return _load_settings_holding_file_lock(sd, cfg, migrate=migrate)
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)


def _load_settings_holding_file_lock(
    sd: Path,
    cfg: Path,
    *,
    migrate: bool,
) -> Settings:
    """Load, normalize, and if needed commit while ``.config.lock`` is held."""
    s = Settings(state_dir=sd)
    data: dict = {}
    if cfg.exists():
        raw = cfg.read_bytes()
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            data = parsed
        elif migrate:
            # Preserve the exact damaged input before replacing it with a
            # canonical config.  This also covers valid JSON scalars/arrays,
            # which are not valid AgentBar configurations.
            _backup_corrupt_config(cfg, raw)

    for key in _PERSISTED_KEYS:
        if key in data:
            setattr(s, key, data[key])

    # v1 exposed the admin API on the LAN by default. Because every old config
    # persisted that default, merely changing the dataclass would leave existing
    # installations exposed. Secure the one-time migration; users who really
    # want LAN access can explicitly re-enable it afterwards.
    try:
        old_schema = int(data.get("config_schema_version", 1))
    except (TypeError, ValueError, OverflowError):
        old_schema = 1
    if migrate and old_schema < 2:
        s.lan_access = False
        # Older releases printed token-bearing panel URLs to launchd stdout.
        # Rotate once so a value copied into browser history or legacy /tmp
        # logs cannot continue administering the upgraded instance.
        s.token = secrets.token_urlsafe(24)

    # providers 始终与内置默认深合并：老 config 缺字段时补齐，未知 provider 丢弃。
    s.providers = _merge_providers(data.get("providers"))
    s.quota_sources = _merge_quota_sources(data.get("quota_sources"))
    normalized = _normalize_loaded_settings(s)
    if not migrate and isinstance(data.get("token"), str):
        # The provisional CLI client must authenticate exactly as an already
        # running older process does, even if its manually edited token would be
        # rotated by today's stricter length/control-character validation.
        s.token = data["token"]

    raw_sources = data.get("quota_sources")
    legacy_quota_key = isinstance(raw_sources, dict) and any(
        isinstance(source, dict)
        and bool({"api_key", "access_token", "account_id"} & set(source))
        for source in raw_sources.values()
    )
    changed = bool(
        not cfg.exists()
        or set(_PERSISTED_KEYS) - set(data)
        or legacy_quota_key
        or normalized
        or data.get("providers") != s.providers
        or data.get("quota_sources") != s.quota_sources
    )
    if not s.token:
        s.token = secrets.token_urlsafe(24)
        changed = True
    if changed and migrate:
        _write_settings_file_holding_lock(s, cfg)
    elif not changed:
        # A user may have hand-edited or restored a valid config with a loose
        # umask. Permissions are an invariant, not merely a side effect of writes.
        try:
            os.chmod(cfg, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    # Releases before v0.10.4 hard-coded this cache under ~/.agentbar even when
    # AgentBar itself used a custom state directory. Delete only the two exact
    # historical names in both locations; the .tmp variant can remain after an
    # interrupted legacy write and contains the same OAuth token.
    if migrate:
        legacy_default_dir = Path.home() / ".agentbar"
        legacy_paths = {
            directory / name
            for directory in (sd, legacy_default_dir)
            for name in (
                LEGACY_CLAUDE_CREDENTIAL_CACHE,
                LEGACY_CLAUDE_CREDENTIAL_TEMP,
            )
        }
        for legacy_path in legacy_paths:
            try:
                legacy_path.unlink(missing_ok=True)
            except OSError:
                pass
    return s


def _backup_corrupt_config(cfg: Path, raw: bytes) -> Path:
    """Create a private, non-overwriting forensic copy of a damaged config."""
    base = cfg.with_name(f"{cfg.name}.corrupt")
    candidate = base
    index = 0
    while True:
        fd = -1
        try:
            fd = os.open(
                candidate,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                stat.S_IRUSR | stat.S_IWUSR,
            )
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            return candidate
        except FileExistsError:
            index += 1
            candidate = cfg.with_name(f"{cfg.name}.corrupt.{index}")
        except Exception:
            # Never replace the original after creating only a partial backup.
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        finally:
            if fd >= 0:
                os.close(fd)


def _write_settings_file_holding_lock(s: Settings, cfg: Path) -> None:
    """Atomically write ``s``; caller owns the process and config-file locks."""
    payload = {k: getattr(s, k) for k in _PERSISTED_KEYS}
    tmp_path: str | None = None
    fd = -1
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=".config.", suffix=".tmp", dir=str(cfg.parent)
        )
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, cfg)
        tmp_path = None
        # The replacement already inherits the temp file's fchmod(0600).  A
        # redundant chmod must not turn a committed save into an apparent
        # failure that makes callers roll their in-memory transaction back.
        try:
            os.chmod(cfg, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def save_settings(s: Settings) -> None:
    """Atomically persist settings without exposing or racing secret data.

    ``Settings._lock`` lets callers hold a read/modify/write transaction.  The
    module lock covers multiple Settings instances in one process; ``flock``
    covers accidental concurrent AgentBar processes.
    """
    # Every settings read/modify/write path acquires the per-instance lock
    # first.  Keep that order here as well: callers are allowed to already hold
    # ``s._lock``, and reversing the order would deadlock against such a caller
    # while another thread is doing a plain save.
    with s._lock, _SAVE_LOCK:
        # Never persist legacy credentials even if an older UI/client mutates a
        # Settings object in memory before calling save.
        s.quota_sources = _merge_quota_sources(s.quota_sources)
        cfg = s.config_path
        cfg.parent.mkdir(parents=True, exist_ok=True)
        lock_path = cfg.with_name(".config.lock")
        lock_fd = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        try:
            os.fchmod(lock_fd, stat.S_IRUSR | stat.S_IWUSR)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            _write_settings_file_holding_lock(s, cfg)
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
