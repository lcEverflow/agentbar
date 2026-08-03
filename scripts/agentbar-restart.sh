#!/usr/bin/env bash
# 确定性重启 AgentBar 服务（代码更新后使用）。
# 不用 `launchctl kickstart -k`：它只杀 job 首进程（uv），agentbar 子进程会变成
# 孤儿继续占端口，导致新实例反复"已在运行"退出。同时根据
# runtime.json 精确停止 DMG/AgentBar.app 实例，避免它阻塞源码版接管。
set -euo pipefail

LABEL="com.agentbar.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
RUNTIME="$HOME/.agentbar/runtime.json"

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true

# runtime.json 是单实例锁的事实来源。只在命令行明确属于
# AgentBar 时停止该 PID，避免 stale PID 复用后误杀其他进程。
runtime_pid=""
if [[ -f "$RUNTIME" ]]; then
  runtime_pid=$(/usr/bin/plutil -extract pid raw -o - "$RUNTIME" 2>/dev/null || true)
fi
if [[ "$runtime_pid" =~ ^[0-9]+$ ]] && kill -0 "$runtime_pid" 2>/dev/null; then
  runtime_cmd=$(ps -p "$runtime_pid" -o command= 2>/dev/null || true)
  case "$runtime_cmd" in
    *"/bin/agentbar run"*|*"AgentBar.app/Contents/MacOS/AgentBar"*)
      kill -TERM "$runtime_pid" 2>/dev/null || true
      ;;
  esac
fi

pkill -TERM -f "bin/agentbar run" 2>/dev/null || true
for _ in $(seq 1 20); do
  runtime_alive=false
  if [[ "$runtime_pid" =~ ^[0-9]+$ ]] && kill -0 "$runtime_pid" 2>/dev/null; then
    runtime_alive=true
  fi
  if [[ "$runtime_alive" == false ]] && ! pgrep -f "bin/agentbar run" >/dev/null; then
    break
  fi
  sleep 0.5
done
if [[ "$runtime_pid" =~ ^[0-9]+$ ]] && kill -0 "$runtime_pid" 2>/dev/null; then
  runtime_cmd=$(ps -p "$runtime_pid" -o command= 2>/dev/null || true)
  case "$runtime_cmd" in
    *"/bin/agentbar run"*|*"AgentBar.app/Contents/MacOS/AgentBar"*)
      kill -9 "$runtime_pid" 2>/dev/null || true
      ;;
  esac
fi
pkill -9 -f "bin/agentbar run" 2>/dev/null || true

if [[ ! -f "$PLIST" ]]; then
  echo "未安装 LaunchAgent，请先运行 scripts/install-launch-agent.sh" >&2
  exit 1
fi
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "已重启 $LABEL"
