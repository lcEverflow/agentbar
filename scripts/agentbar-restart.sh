#!/usr/bin/env bash
# Safely restart the source-based AgentBar LaunchAgent.
# Instance ownership comes from the default state directory's runtime.json;
# unrelated --state-dir instances are never found or signalled.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=launch-agent-common.sh
source "$SCRIPT_DIR/launch-agent-common.sh"

LABEL="com.agentbar.app"
USER_ID="$(id -u)"
SERVICE_TARGET="gui/$USER_ID/$LABEL"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
RUNTIME="$HOME/.agentbar/runtime.json"

if [[ ! -f "$PLIST" || -L "$PLIST" ]]; then
  echo "错误：未找到可信的 LaunchAgent plist，请先运行 scripts/install-launch-agent.sh" >&2
  exit 1
fi
/usr/bin/plutil -lint "$PLIST" >/dev/null

# Capture the owner before bootout: uv can exit while leaving its AgentBar child
# alive, and that child is the only extra process this restart may terminate.
runtime_pid=$(agentbar_runtime_pid "$RUNTIME")
agentbar_bootout_if_loaded "$SERVICE_TARGET"
agentbar_stop_runtime_instance "$RUNTIME" 45 "$runtime_pid"

if ! /bin/launchctl bootstrap "gui/$USER_ID" "$PLIST"; then
  echo "错误：LaunchAgent 加载失败；配置仍保留在 $PLIST" >&2
  exit 1
fi
if ! agentbar_wait_healthy "$RUNTIME" 45; then
  failed_pid=$(agentbar_runtime_pid "$RUNTIME")
  if agentbar_bootout_if_loaded "$SERVICE_TARGET" \
    && agentbar_stop_runtime_instance "$RUNTIME" 45 "$failed_pid"; then
    echo "错误：AgentBar 在 45 秒内未通过健康检查，已停止异常重启循环" >&2
  else
    echo "严重：AgentBar 未通过健康检查，且未能确认异常实例已经停止" >&2
  fi
  echo "请查看 $HOME/.agentbar/launchd.stderr.log" >&2
  exit 1
fi

echo "已安全重启 $LABEL（仅默认状态目录实例）"
