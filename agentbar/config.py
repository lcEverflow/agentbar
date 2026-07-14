"""Settings: config.json in the state dir. User-editable; app writes defaults once."""

from __future__ import annotations

import json
import os
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PORT = 8737


def default_state_dir() -> Path:
    env = os.environ.get("AGENTBAR_STATE_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".agentbar"


@dataclass
class Settings:
    state_dir: Path
    port: int = DEFAULT_PORT
    max_parallel: int = 1          # 全局并行度，1 = 串行
    per_tool_limit: int = 1        # 每个 AI CLI 的并行上限
    default_cwd: str = str(Path.home())
    allow_full_profile: bool = False   # 高权限档位默认关闭
    task_timeout_seconds: int = 7200   # 单任务运行上限
    backoff_minutes: list[float] = field(default_factory=lambda: [5, 15, 30, 60])
    usage_refresh_seconds: int = 120    # 订阅额度接口轮询间隔（最小 30 秒）
    tick_seconds: float = 1.0
    tool_paths: dict = field(default_factory=dict)  # 手动指定 CLI 路径: {"claude": "/path"}
    lan_access: bool = True        # 绑定 0.0.0.0 供同一局域网的手机访问（API 仍需 token）
    token: str = ""

    @property
    def config_path(self) -> Path:
        return self.state_dir / "config.json"


_PERSISTED_KEYS = (
    "port",
    "max_parallel",
    "per_tool_limit",
    "default_cwd",
    "allow_full_profile",
    "task_timeout_seconds",
    "backoff_minutes",
    "usage_refresh_seconds",
    "tool_paths",
    "lan_access",
    "token",
)


def load_settings(state_dir: Path | None = None) -> Settings:
    sd = Path(state_dir).expanduser() if state_dir else default_state_dir()
    sd.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(sd, stat.S_IRWXU)  # 0700：state 目录含 token 与任务日志
    except OSError:
        pass

    s = Settings(state_dir=sd)
    cfg = sd / "config.json"
    data: dict = {}
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}

    for key in _PERSISTED_KEYS:
        if key in data:
            setattr(s, key, data[key])

    changed = not cfg.exists() or set(_PERSISTED_KEYS) - set(data)
    if not s.token:
        s.token = secrets.token_urlsafe(24)
        changed = True
    if changed:
        save_settings(s)
    return s


def save_settings(s: Settings) -> None:
    cfg = s.config_path
    payload = {k: getattr(s, k) for k in _PERSISTED_KEYS}
    tmp = cfg.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, cfg)
    try:
        os.chmod(cfg, stat.S_IRUSR | stat.S_IWUSR)  # 0600：含 API token
    except OSError:
        pass
