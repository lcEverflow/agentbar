#!/usr/bin/env bash
# Uninstall only AgentBar's source-based LaunchAgent. User tasks/config/logs are
# intentionally preserved in ~/.agentbar.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=launch-agent-common.sh
source "$SCRIPT_DIR/launch-agent-common.sh"

LABEL="com.agentbar.app"
USER_ID="$(id -u)"
SERVICE_TARGET="gui/$USER_ID/$LABEL"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
RUNTIME="$HOME/.agentbar/runtime.json"

if agentbar_job_loaded "$SERVICE_TARGET"; then
  runtime_pid=$(agentbar_runtime_pid "$RUNTIME")
  # Do not ignore a real bootout error and then delete the service definition.
  /bin/launchctl bootout "$SERVICE_TARGET"
  if ! agentbar_stop_runtime_instance "$RUNTIME" 45 "$runtime_pid"; then
    echo "卸载已中止；plist 保留在 $PLIST，便于恢复" >&2
    exit 1
  fi
fi

/bin/rm -f -- "$PLIST"
# Exact historical paths only; ~/.agentbar and its private logs are preserved.
agentbar_remove_obsolete_log_file /tmp/agentbar.launchd.log
agentbar_remove_obsolete_log_file /tmp/agentbar.launchd.err.log
echo "已卸载 $LABEL；任务、配置与私有日志仍保留在 $HOME/.agentbar"
