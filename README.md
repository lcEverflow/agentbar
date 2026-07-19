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
| 常驻 Menu Bar / 开机自启 | 原生 AppKit（NSStatusItem/NSMenu）+ `scripts/install-launch-agent.sh` |
| 可操作的 Menu Bar | 每一行都可点（概览/任务/额度行点击即打开面板）；菜单只在展开时刷新（menuWillOpen）；所有动作毫秒级返回，绝不阻塞主线程；实时菜单状态导出 `menu-debug.json` 可核查 |
| 原生任务面板窗口 | 菜单点击直接弹出 AppKit 窗口（accessory 进程激活自身窗口不受 macOS 26 协作激活限制）：添加任务（Prompt/工具/模型/强度/权限/目录选择器）、队列排优先级（⇧置顶/↑/↓）、取消/重试/暂停派发，进程内直连调度器不经浏览器；web 面板降为次要入口（`🌐 在浏览器中打开面板` / 手机远程） |
| 添加任务（Prompt+工具+目录） | 菜单栏快捷添加 / Web 面板 / `agentbar add` CLI |
| 支持 Claude Code、Codex，可扩展 | Adapter 插件制，新 CLI ≈ 60 行代码 |
| 串行 / 有限并行 | `max_parallel`（默认 1 串行）+ `per_tool_limit` |
| 完整生命周期 | queued / running / succeeded / failed / **waiting_quota** / paused / cancelled |
| 额度耗尽不判失败 | 识别限流报错 → `waiting_quota`，解析恢复时间或指数退避，**自动 resume 原会话续跑** |
| 额度状态可见（不伪造） | 使用当前 CLI 登录态读取的 usage 数据 + 调度观测 + 可选 ccusage；窗口、重置时间和来源都明确标注 |
| 菜单栏双环额度图标 | 外圈 Claude、内圈 Codex 用量一眼可见（同 aiusagebar 的外围圈样式），不用点开菜单；模板图自适配深浅色菜单栏；无可信数据只画轨道不编造 |
| 键盘快捷键 | 面板/对话窗口前台时：`⌃W`/`⌘W` 关闭当前窗口，`⌃Q`/`⌘Q` 退出（走完整清理：停隧道/调度器/服务器） |
| 模型 / 强度选择 | 任务级保存模型与强度；Claude 用 `--model` / `--effort`，Codex 用 `--model` / `model_reasoning_effort` 配置覆盖 |
| 实时查看运行/队列/日志/历史 | Web 面板 2s 自刷新 + 日志实时 tail |
| 查看本机其他 CLI | 只读发现正在运行的 Claude/Codex 进程；不读取 Prompt/完整命令，也不会终止外部进程 |
| 重启恢复 | 每次状态变更原子落盘 `state.json`；崩溃时 RUNNING 任务自动重新入队（续会话） |
| 默认安全 | 三档权限（默认不开高权限）、仅 127.0.0.1 + token、无 shell 拼接、进程组隔离、超时兜底 |

## 安装 & 运行

```bash
# 依赖: macOS + uv (https://docs.astral.sh/uv/)
cd agentbar
uv sync                         # installs the macOS Security bridge for Claude quota access

uv run agentbar run              # 菜单栏模式（推荐）
uv run agentbar run --headless   # 无 GUI（服务器/调试）

bash scripts/install-launch-agent.sh    # 开机自启（登录时拉起）
bash scripts/agentbar-restart.sh        # 代码更新后重启服务（干净杀进程组，避免孤儿占端口）
bash scripts/uninstall-launch-agent.sh  # 取消自启
```

启动后点菜单栏 🤖 →「打开任务面板」，或：

```bash
uv run agentbar open                                    # 打开 Web 面板（带令牌）
uv run agentbar add --tool claude --cwd ~/proj "重构 utils.py 并补测试"
uv run agentbar add --tool claude --model opus --effort high "处理一个复杂重构"
uv run agentbar add --tool codex --model <你的模型ID> --effort xhigh --profile readonly "分析这个仓库的架构并写 ARCHITECTURE.md"
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

订阅版 CLI 没有承诺稳定的公开额度查询 API。AgentBar 因此按以下顺序读取，任何一种拿不到都会如实降级：

1. **usage API**（默认、120 秒刷新）：使用本机已有的 Claude OAuth / Codex 登录态读取其当前 usage 响应，显示窗口用量与重置时间。Claude Keychain 默认静默读取，绝不会在后台弹窗；需要时用户可在面板中主动授权。
2. **observed**：调度器自身观测的最近成功执行、真实限流和恢复时间；它会优先标记已确认的限流。
3. **ccusage**（可选增强）：`npm i -g ccusage` 后补充 Claude 本地 5h 成本。
4. 无任何可用数据时显示「未知」，并显示失败原因。**不会估算或编造百分比。**

usage 响应不是稳定的公开契约，接口结构变化时会显示解析错误而非虚构数值。可在 `config.json` 里调整轮询频率：

```jsonc
{ "usage_refresh_seconds": 120 } // 最小 30 秒
```

## 模型与强度

添加任务时，模型和强度均为任务级字段，写入 `state.json` 并在重试/重启恢复时保留。

- **Claude**：模型可填当前 CLI 支持的别名或 ID（例如 `sonnet`、`opus`、`fable`）；强度使用当前 CLI 的 `low`、`medium`、`high`、`xhigh`、`max`。
- **Codex**：模型输入框只接受你账号已经开放的 model ID，留空即沿用本机配置；强度用现有的 `model_reasoning_effort` 配置覆盖，支持 `low` 到 `xhigh`。
- AgentBar 不猜测你的账号有哪些模型，也不展示可能已下线的硬编码模型列表。

## 本机 CLI 观测

面板会列出当前 Mac 上的 Claude/Codex 进程（包括不是由 AgentBar 启动的任务）。为保护工作内容，外部进程只显示工具、PID、状态和已运行时长；不会读取 prompt、完整命令或工作目录，也不会提供取消/暂停操作。由 AgentBar 管理的任务会额外显示保存的标题与工作目录。

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
  "usage_refresh_seconds": 120,
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
uv run pytest        # 59 个测试：生命周期/额度退避/恢复/取消/API 安全/适配器/模型/真实额度解析/进程观测/Menu Bar 子菜单
```

测试用 `AGENTBAR_ENABLE_FAKE=1` 注册的 fake CLI 模拟成功/失败/限流/慢任务，不消耗真实额度。

## Roadmap：手机远程控制

架构已按 API-first 设计（菜单栏和 Web 面板都是同一 HTTP API 的客户端），远程控制是加通道而非改架构：

1. **内网/Tailscale**（推荐第一步）：Tailscale 组网后手机浏览器直接访问 `http://<mac-tailscale-ip>:8737/?token=…`，Web 面板本身就是响应式的。需把监听地址改为可配置并强化 token 策略。
2. **IM Bot**：Telegram/Slack/Kim bot 进程调用同一 API（add/status/log/pause），推送任务完成/额度恢复通知——适合"下班路上派活"。
3. **PWA + 推送**：面板加 manifest + Web Push，任务完成/失败/等额度主动通知手机。
4. **中继模式**：Mac 出网受限时，经云端轻量 relay（WebSocket 反向连接）转发 API，手机端连 relay。

## 已知限制（v0.3）

- usage 响应不是公开契约，接口结构变化时显示解析错误而非虚构数值。
- 修改 `config.json` 需重启生效（无热加载）。
- 任务级依赖（A 完成才跑 B）未实现，当前是 FIFO + 并发上限。
- Menu Bar 标题为文本符号（◇◆◐Ⅱ + 用量百分比），自动适配深浅色菜单栏。

## Menu Bar 架构备注（为什么不用 rumps）

v0.1/v0.2 基于 rumps 时出现两类线上事故，v0.3 改为直接使用 AppKit：

1. **主线程阻塞**：菜单回调里同步等待 `/usr/bin/open`（最长 8s）或 webbrowser
   （macOS 上走 osascript/Apple Events，可能卡在 TCC 授权）→ 整个 App 卡死。
   现在 GUI 一律 `open_url_async`（Popen fire-and-forget，实测 ~3ms 返回），
   Keychain 授权等慢操作全部丢后台线程。
2. **定时重建打开中的菜单**：rumps.Timer 每 2s clear+rebuild 菜单导致点击落空。
   现在菜单内容只在 `menuWillOpen`（AppKit 正统时机）重建，NSTimer 只改标题文本。

回归防线：`tests/test_browser.py`（GUI 打开路径必须 <50ms 且禁用 webbrowser）、
`tests/test_menu_spec.py`（不允许存在无 action 的死行）、运行时 `menu-debug.json`。
