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
| 原生任务面板窗口 | 菜单点击直接弹出 AppKit 窗口：添加任务（Prompt/工具/模型/强度/权限/目录）、队列排序、取消/重试/暂停；底部「额度设置」可配置 Claude / Codex / MyToken / Tokenverse |
| 添加任务（Prompt+工具+目录） | 菜单栏快捷添加 / Web 面板 / `agentbar add` CLI |
| 支持 Claude Code、Codex，可扩展 | Adapter 插件制，新 CLI ≈ 60 行代码 |
| 串行 / 有限并行 | `max_parallel`（默认 1 串行）+ `per_tool_limit` |
| 完整生命周期 | queued / running / succeeded / failed / **waiting_quota** / paused / cancelled |
| 额度耗尽不判失败 | 识别限流报错 → `waiting_quota`，解析恢复时间或指数退避，**自动 resume 原会话续跑** |
| 额度状态可见（不伪造） | 额度来源显式 opt-in；手动选择 Claude 模型家族或 Codex `metered_feature`，输入 OAuth Access Token 后才请求 |
| 可控制刷新 | 默认只在启动、保存配置或手动点击时请求一次；任务结束不再强制刷新全部来源，重复点击会合并 |
| 菜单栏双环额度图标 | 外圈 Claude、内圈 Codex 用量一眼可见（同 aiusagebar 的外围圈样式），不用点开菜单；环心徽标显示调度状态（实心点=运行中、双竖条=已暂停），菜单栏只有这一个图标；模板图自适配深浅色；无可信数据只画轨道不编造 |
| 键盘快捷键 | 面板/对话窗口前台时：`⌃W`/`⌘W` 关闭当前窗口，`⌃Q`/`⌘Q` 退出（走完整清理：停隧道/调度器/服务器） |
| 模型 / 强度选择 | 任务级保存模型与强度；Claude 用 `--model` / `--effort`，Codex 用 `--model` / `model_reasoning_effort` 配置覆盖 |
| 实时查看运行/队列/日志/历史 | Web 面板 2s 自刷新 + 日志实时 tail |
| 查看本机其他 CLI | 只读发现正在运行的 Claude/Codex 进程；不读取 Prompt/完整命令，也不会终止外部进程 |
| 重启恢复 | 每次状态变更原子落盘；崩溃遗留的 RUNNING 任务安全暂停，人工确认旧进程后再恢复，避免重复 agent 同时改文件 |
| 默认安全 | 三档权限（默认不开高权限）、仅 127.0.0.1 + token、无 shell 拼接、进程组隔离、超时兜底 |

## 安装 & 运行

```bash
# 依赖: macOS + uv (https://docs.astral.sh/uv/)
cd agentbar
uv sync                         # 安装 macOS 原生 UI 与运行依赖

uv run agentbar run              # 菜单栏模式（推荐）
uv run agentbar run --headless   # 无 GUI（服务器/调试）

bash scripts/install-launch-agent.sh    # 开机自启（登录时拉起）
bash scripts/agentbar-restart.sh        # 只重启默认状态目录对应的实例
bash scripts/uninstall-launch-agent.sh  # 取消自启
```

这三个脚本服务于**源码开发安装**：生成的 LaunchAgent 会记录当前仓库和 `uv`
的绝对路径，并以 `uv run --locked` 启动；移动/删除仓库或让 `uv.lock` 与项目
元数据失配后将无法启动。安装采用候选 plist 校验后原子替换；
若新进程未通过本机 `/api/ping` 健康检查，会恢复旧 plist。重启只向
`~/.agentbar/runtime.json` 所属 PID 发送 `SIGTERM`，不会用进程名批量杀掉其他
`--state-dir` 实例，也不会用 `SIGKILL` 留下正在工作的 Claude/Codex 孤儿进程。
卸载只取消自启并保留 `~/.agentbar` 中的任务、配置和私有日志。

需要脱离源码目录安装时，请构建并拖拽 DMG（依赖 Homebrew framework Python
3.13；Intel Homebrew 与 Apple Silicon 路径都会自动识别）：

```bash
bash scripts/build-dmg.sh
# 输出 dist/AgentBar.app、dist/AgentBar-<版本>.dmg 和 SHA-256
```

构建使用 `uv.lock` 中的运行依赖和一次性 py2app 环境，随后检查签名、冻结后的
AppKit/WebKit/登录/二维码/Markdown 模块、内置 Web 资源及 DMG 完整性。版本号必须
在 `pyproject.toml` 与 `agentbar/__init__.py` 中一致，否则构建会直接失败。DMG
架构跟随构建机（Apple Silicon 产出 arm64，Intel 产出 x86_64），当前不生成
Universal 2 包。

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
  人工恢复│       ▼           │ └──▶ waiting_quota ──（到点自动回 queued，resume 原会话）
 (崩溃遗留│    paused ◀──────┘          │
 RUNNING)       │                       ▼
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
- API 所有写操作都要求 AgentBar token，并校验 Host 头防 DNS rebinding；手动额度凭据只接受本机回环地址提交，避免经局域网明文传输。
- OAuth Token / Account ID 是只写字段：Web、状态快照、菜单调试文件均只返回“是否已配置”；落盘的 `config.json` 权限固定为 `0600`。
- 子进程以 argv 数组直接 exec，无 shell 拼接；prompt 走 stdin，杜绝 flag 注入。
- 每个任务独立进程组，取消/超时（默认 2h）时整组终止，不留孤儿进程。

## 额度状态的数据来源（诚实降级）

订阅版 CLI 没有承诺稳定的公开额度查询 API。AgentBar 因此只请求用户显式配置的来源，任何一种拿不到都如实降级：

1. **usage API**（显式配置）：Claude / Codex 默认关闭。在「额度设置」中启用来源、选择额度模型，并输入对应的 **OAuth Access Token** 后才会请求。Codex 可选填 Account ID。普通 Anthropic/OpenAI API key 不等于订阅 usage 凭据，接口拒绝时会如实显示错误。
2. **observed**：调度器自身观测的最近成功执行、真实限流和恢复时间；它会优先标记已确认的限流。
3. **ccusage**（可选增强）：`npm i -g ccusage` 后补充 Claude 本地 5h 成本。
4. 无任何可用数据时显示「未知」，并显示失败原因。**不会估算或编造百分比。**

usage 响应不是稳定的公开契约，接口结构变化时会显示解析错误而非虚构数值。默认不周期轮询；如需显式开启：

```jsonc
{ "usage_auto_refresh": true, "usage_refresh_seconds": 120 }
```

手动模式下，任务成功/限额观测只更新本地状态，不会触发上游网络刷新。

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
- 崩溃/断电：旧 CLI 进程可能仍在运行，因此下次启动会把 RUNNING 任务转为 `paused`；确认现场后手动恢复，避免重复执行有副作用的任务。
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
  "usage_auto_refresh": false, // false=默认手动；true=按上方间隔轮询
  "tool_paths": {},         // {"claude": "/abs/path"} 手动覆盖
  "title_provider": "claude",   // 状态栏标题显示哪个 provider 的用量：claude/codex/mytoken/tokenverse
  "quota_sources": {       // 订阅额度：默认关闭，必须手动输入凭据
    "claude": {
      "enabled": false,
      "model": "sonnet", // opus / sonnet；留空=账户通用窗口
      "access_token": "", // OAuth Access Token，非普通 API key
      "account_id": ""
    },
    "codex": {
      "enabled": false,
      "model": "",       // metered_feature；留空=账户总额度
      "access_token": "", // ChatGPT OAuth Access Token
      "account_id": ""    // 可选；JWT 不含账号时填写
    }
  },
  "providers": {            // 快手内部额度 provider（默认关闭，可在面板一键导入 Cookie）
    "mytoken": {
      "enabled": false,
      "cookie": "",         // corp SSO cookie 头（面板导入或手动复制整行 Cookie）
      "unit": "credits",    // 展示单位：credits / percent / token
      "refresh_seconds": 300
    },
    "tokenverse": {
      "enabled": false,
      "cookie": "",
      "unit": "credits",
      "refresh_seconds": 300
    }
  }
}
```

### 快手内部额度：MyToken / Tokenverse

除订阅版 Claude / Codex 外，AgentBar 支持展示两个快手内部工具的月度信用额度（credits）：

| provider | 接口 | 鉴权 |
| ---- | ---- | ---- |
| **MyToken** | `mytoken.corp.kuaishou.com` — `/api/auth/sso/user` → `/api/v1/billing/account` | corp SSO cookie + `kwaipilot-username` 头（自动带） |
| **Tokenverse** | `tokenverse.corp.kuaishou.com` — `/api/coding-plan/status` + `/api/coding-plan/usage/summary` | corp SSO cookie |

启用方式：点击菜单栏 AgentBar 图标 → **额度设置…**（或原生任务面板底部「额度设置」）。MyToken / Tokenverse 即使尚未配置也会以“未配置”状态出现在菜单中。点「浏览器登录」后 AgentBar 启动临时 Chrome 会话；完成企业 SSO 后捕获并校验 Cookie，成功后保存、启用并刷新一次。「读取已有登录」与手动粘贴整行 `Cookie` 仍作为兜底。凭据缺失或接口失败时如实显示错误，不编造额度。

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
uv run pytest        # 生命周期/刷新竞态/凭据脱敏/模型额度/API 安全/原生 UI/Menu Bar
bash -n scripts/*.sh # 安装、重启、卸载与 DMG 脚本语法
```

测试用 `AGENTBAR_ENABLE_FAKE=1` 注册的 fake CLI 模拟成功/失败/限流/慢任务，不消耗真实额度。

## Roadmap：手机远程控制

架构已按 API-first 设计（菜单栏和 Web 面板都是同一 HTTP API 的客户端），远程控制是加通道而非改架构：

1. **内网/Tailscale**：默认只监听 `127.0.0.1`。明确开启 `lan_access` 后，从菜单生成一次带 `#token=…` fragment 的二维码；凭据不会进入 HTTP 请求日志。局域网仍是明文 HTTP，不应提交额度凭据。
2. **IM Bot**：Telegram/Slack/Kim bot 进程调用同一 API（add/status/log/pause），推送任务完成/额度恢复通知——适合"下班路上派活"。
3. **PWA + 推送**：面板加 manifest + Web Push，任务完成/失败/等额度主动通知手机。
4. **中继模式**：Mac 出网受限时，经云端轻量 relay（WebSocket 反向连接）转发 API，手机端连 relay。

## 已知限制（当前版本）

- usage 响应不是公开契约，接口结构变化时显示解析错误而非虚构数值。
- 修改 `config.json` 需重启生效（无热加载）。
- 任务级依赖（A 完成才跑 B）未实现，当前是 FIFO + 并发上限。
- Menu Bar 使用原生矢量双环图标，自动适配深浅色；没有可信额度数据时只画轨道。

## Menu Bar 架构备注（为什么不用 rumps）

v0.1/v0.2 基于 rumps 时出现两类线上事故，v0.3 改为直接使用 AppKit：

1. **主线程阻塞**：菜单回调里同步等待 `/usr/bin/open`（最长 8s）或 webbrowser
   （macOS 上走 osascript/Apple Events，可能卡在 TCC 授权）→ 整个 App 卡死。
   现在 GUI 一律 `open_url_async`（Popen fire-and-forget，实测 ~3ms 返回），
   浏览器登录等慢操作全部丢后台线程。
2. **定时重建打开中的菜单**：rumps.Timer 每 2s clear+rebuild 菜单导致点击落空。
   现在菜单内容只在 `menuWillOpen`（AppKit 正统时机）重建，NSTimer 只改标题文本。

回归防线：`tests/test_browser.py`（GUI 打开路径必须 <50ms 且禁用 webbrowser）、
`tests/test_menu_spec.py`（不允许存在无 action 的死行）、运行时 `menu-debug.json`。
