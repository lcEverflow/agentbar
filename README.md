# AgentBar 🤖

macOS 状态栏（Menu Bar）AI Agent 调度器 —— 让 Claude Code、Codex 等 AI CLI 像后台服务一样持续干活，额度耗尽自动等待恢复后续跑，重启不丢任务。

```
┌─ Menu Bar ──────────────┐        ┌──────────────────────────────┐
│ 🤖▶  状态：运行中        │        │  Web 任务面板 (127.0.0.1:8737)│
│      当前：重构 parser   │        │  添加任务 / 队列 / 日志 / 历史 │
│      队列：2 排队        │◀──────▶│  额度状态 / 暂停恢复          │
│      🟢 Claude：正常     │  同一   └──────────────────────────────┘
│      🟠 Codex：受限      │  进程            ▲ HTTP + token
│      打开任务面板 / 退出  │        ┌─────────┴────────────────────┐
└─────────────────────────┘        │  Scheduler 内核（无 GUI 依赖）  │
                                   │  队列/生命周期/额度退避/持久化   │
                                   │    ├─ ClaudeAdapter  claude -p │
                                   │    ├─ CodexAdapter   codex exec│
                                   │    └─ (你的下一个 CLI)          │
                                   └────────────────────────────────┘
```

## 特性

| 需求 | 实现 |
| ---- | ---- |
| 常驻 Menu Bar / 开机自启 | rumps(NSStatusItem) + `scripts/install-launch-agent.sh` |
| 添加任务（Prompt+工具+目录） | 菜单栏快捷添加 / Web 面板 / `agentbar add` CLI |
| 支持 Claude Code、Codex，可扩展 | Adapter 插件制，新 CLI ≈ 60 行代码 |
| 串行 / 有限并行 | `max_parallel`（默认 1 串行）+ `per_tool_limit` |
| 完整生命周期 | queued / running / succeeded / failed / **waiting_quota** / paused / cancelled |
| 额度耗尽不判失败 | 识别限流报错 → `waiting_quota`，解析恢复时间或指数退避，**自动 resume 原会话续跑** |
| 额度状态可见（不伪造） | 观测事实（最近成功/限流+恢复时间）+ 可选 ccusage；来源明确标注，取不到显示"未知" |
| 实时查看运行/队列/日志/历史 | Web 面板 2s 自刷新 + 日志实时 tail |
| 重启恢复 | 每次状态变更原子落盘 `state.json`；崩溃时 RUNNING 任务自动重新入队（续会话） |
| 默认安全 | 三档权限（默认不开高权限）、仅 127.0.0.1 + token、无 shell 拼接、进程组隔离、超时兜底 |

## 安装 & 运行

```bash
# 依赖: macOS + uv (https://docs.astral.sh/uv/)
cd agentbar
uv sync

uv run agentbar run              # 菜单栏模式（推荐）
uv run agentbar run --headless   # 无 GUI（服务器/调试）

bash scripts/install-launch-agent.sh    # 开机自启（登录时拉起）
bash scripts/uninstall-launch-agent.sh  # 取消自启
```

启动后点菜单栏 🤖 →「打开任务面板」，或：

```bash
uv run agentbar open                                    # 打开 Web 面板（带令牌）
uv run agentbar add --tool claude --cwd ~/proj "重构 utils.py 并补测试"
uv run agentbar add --tool codex --profile readonly "分析这个仓库的架构并写 ARCHITECTURE.md"
uv run agentbar status                                  # 终端看状态
uv run agentbar pause / resume / cancel <id> / log <id>
```

## 任务生命周期

```
                    ┌────────────┐   额度恢复/退避到期(自动)
      add ──▶ queued ──▶ running ──▶ succeeded
        ▲       │  ▲        │ │
        │       │  └────────┼─┼──▶ failed（真实错误才算失败，可手动重试）
  重启恢复│       ▼           │ └──▶ waiting_quota ──（到点自动回 queued，resume 原会话）
  (RUNNING│    paused ◀──────┘          │
   →queued)     │                       ▼
        └───── cancelled ◀──────────────┘（各状态均可取消）
```

- **额度判定**：仅在退出码非 0 时匹配限流特征（`usage limit reached` / `429` / `rate limit` / `overloaded` …），成功输出里出现这些词不会误判。
- **恢复时间**：优先解析报错里的重置时间（如 `limit reached|<epoch>`、`try again in 3 hours`）；解析不到按 5/15/30/60min 退避重试（带抖动）。
- **续跑**：Claude 用 `--resume <session_id>`，Codex 用 `codex exec resume <id>`；旧版 CLI 不支持时自动降级为全新执行。
- **同工具冷却**：一个任务触发限流后，同工具的其他任务也暂缓派发，不空烧重试。

## 安全模型（默认安全）

| 档位 | Claude Code | Codex |
| ---- | ---- | ---- |
| 🔒 readonly | `--allowedTools Read,Glob,Grep,…` + 禁 Bash/Edit/Write | `--sandbox read-only` |
| ✏️ edits（默认） | `--permission-mode acceptEdits`（可改文件，Bash 仍被拒） | `--sandbox workspace-write` |
| ⚠️ full | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox` |

- **full 档默认禁用**：需在 `~/.agentbar/config.json` 设 `allow_full_profile: true` 并重启，UI 中也有显式警告。
- API 仅绑定 `127.0.0.1`，所有写操作要求 token（`config.json`，0600），校验 Host 头防 DNS rebinding——防止恶意网页通过浏览器向本机调度器投毒任务。
- 子进程以 argv 数组直接 exec，无 shell 拼接；prompt 走 stdin，杜绝 flag 注入。
- 每个任务独立进程组，取消/超时（默认 2h）时整组终止，不留孤儿进程。

## 额度状态的数据来源（诚实降级）

Anthropic / OpenAI 均未给订阅版 CLI 提供公开的额度查询 API，因此：

1. **observed**（默认）：调度器自身观测——最近一次成功执行时间、最近一次限流及解析出的恢复时间。
2. **ccusage**（可选增强）：`npm i -g ccusage` 后自动启用，解析 `~/.claude` 本地日志展示 5h 窗口用量与重置时间。
3. 无任何数据时显示「未知（尚无执行观测）」。**任何情况下不编造数字**，UI 上标注来源。

## 持久化 & 恢复

`~/.agentbar/`（可用 `AGENTBAR_STATE_DIR` 覆盖）：

```
config.json    # 端口/并行度/权限开关/工具路径/token（用户可编辑）
state.json     # 任务队列+额度观测，每次变更原子写（tmp+rename）
runtime.json   # 实际端口+PID（运行时存在）
logs/<id>.log  # 每任务完整 CLI 输出
agentbar.log   # 调度器日志
```

- 调度器退出（含 SIGTERM）：在跑的 CLI 进程被整组终止，任务放回队列并标记续会话。
- 崩溃/断电：下次启动时 `state.json` 里的 RUNNING 任务自动重新入队。
- `state.json` 损坏：自动备份为 `state.json.corrupt-*` 并从空状态启动，不会起不来。
- launchd 场景 PATH 被裁剪：自动经登录 shell（`zsh -lc`）解析 claude/codex 真实路径，nvm 安装也能找到；亦可在 `config.json` 的 `tool_paths` 手动指定。

## 配置（`~/.agentbar/config.json`）

```jsonc
{
  "port": 8737,
  "max_parallel": 1,        // 全局并行度，1=严格串行
  "per_tool_limit": 1,      // 每个 CLI 的并行上限（claude/codex 额度独立，可各跑一个）
  "default_cwd": "/Users/you",
  "allow_full_profile": false,
  "task_timeout_seconds": 7200,
  "backoff_minutes": [5, 15, 30, 60],
  "tool_paths": {}          // {"claude": "/abs/path"} 手动覆盖
}
```

## 扩展新的 AI CLI

在 `agentbar/adapters/` 加一个文件，实现 4 个方法并注册：

```python
class GeminiAdapter(Adapter):
    name, display_name = "gemini", "Gemini CLI"

    def build_argv(self, task, resume, binary):   # 怎么调
        return [binary, "-p", "--yolo=false"]
    def stdin_payload(self, task, resume):        # prompt 走 stdin
        return task.prompt
    def classify(self, exit_code, output):        # 成功/额度/失败 三分类
        ...
    def extract_session_id(self, output):         # 可选：会话恢复
        ...
```

然后在 `base.py::get_registry()` 注册即可，UI/CLI/调度全部自动生效。

## 测试

```bash
uv run pytest        # 50 个测试：生命周期/额度退避/恢复/取消/API 安全/适配器
```

测试用 `AGENTBAR_ENABLE_FAKE=1` 注册的 fake CLI 模拟成功/失败/限流/慢任务，不消耗真实额度。

## Roadmap：手机远程控制

架构已按 API-first 设计（菜单栏和 Web 面板都是同一 HTTP API 的客户端），远程控制是加通道而非改架构：

1. **内网/Tailscale**（推荐第一步）：Tailscale 组网后手机浏览器直接访问 `http://<mac-tailscale-ip>:8737/?token=…`，Web 面板本身就是响应式的。需把监听地址改为可配置并强化 token 策略。
2. **IM Bot**：Telegram/Slack/Kim bot 进程调用同一 API（add/status/log/pause），推送任务完成/额度恢复通知——适合"下班路上派活"。
3. **PWA + 推送**：面板加 manifest + Web Push，任务完成/失败/等额度主动通知手机。
4. **中继模式**：Mac 出网受限时，经云端轻量 relay（WebSocket 反向连接）转发 API，手机端连 relay。

## 已知限制（v0.1）

- 额度"余量百分比"无官方 API，只能观测/估算（见上文数据来源）。
- 并发修改 `config.json` 需重启生效（无热加载）。
- 任务级依赖（A 完成才跑 B）未实现，当前是 FIFO + 并发上限。
- Menu Bar 图标为 emoji 文本，未做模板图标适配深色菜单栏（功能不受影响）。
