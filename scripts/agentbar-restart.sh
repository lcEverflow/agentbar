#!/usr/bin/env bash
# 确定性重启 AgentBar 服务（代码更新后使用）。
# 不用 `launchctl kickstart -k`：它只杀 job 首进程（uv），agentbar 子进程会变成
# 孤儿继续占端口，导致新实例反复"已在运行"退出。
set -euo pipefail

LABEL="com.agentbar.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
pkill -TERM -f "bin/agentbar run" 2>/dev/null || true
for _ in $(seq 1 20); do
  pgrep -f "bin/agentbar run" >/dev/null || break
  sleep 0.5
done
pkill -9 -f "bin/agentbar run" 2>/dev/null || true

if [[ ! -f "$PLIST" ]]; then
  echo "未安装 LaunchAgent，请先运行 scripts/install-launch-agent.sh" >&2
  exit 1
fi
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "已重启 $LABEL"
