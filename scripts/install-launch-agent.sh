#!/usr/bin/env bash
# Install the source checkout as a macOS LaunchAgent with atomic rollback.
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=launch-agent-common.sh
source "$SCRIPT_DIR/launch-agent-common.sh"

UV_BIN="$(command -v uv || true)"
if [[ -z "$UV_BIN" || "$UV_BIN" != /* || ! -x "$UV_BIN" ]]; then
  echo "错误：未找到绝对路径可执行的 uv。请先按 https://docs.astral.sh/uv/ 安装 uv" >&2
  exit 1
fi

LABEL="com.agentbar.app"
USER_ID="$(id -u)"
SERVICE_TARGET="gui/$USER_ID/$LABEL"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST="$LAUNCH_AGENTS_DIR/$LABEL.plist"
STATE_DIR="$HOME/.agentbar"
RUNTIME="$STATE_DIR/runtime.json"
STDOUT_LOG="$STATE_DIR/launchd.stdout.log"
STDERR_LOG="$STATE_DIR/launchd.stderr.log"

/bin/mkdir -p "$LAUNCH_AGENTS_DIR" "$STATE_DIR"
/bin/chmod 700 "$STATE_DIR"

for log_path in "$STDOUT_LOG" "$STDERR_LOG"; do
  if [[ -L "$log_path" || ( -e "$log_path" && ! -f "$log_path" ) ]]; then
    echo "错误：拒绝把 launchd 日志写入非普通文件: $log_path" >&2
    exit 1
  fi
  /usr/bin/touch "$log_path"
  /bin/chmod 600 "$log_path"
done

CANDIDATE=$(/usr/bin/mktemp "$LAUNCH_AGENTS_DIR/.${LABEL}.candidate.XXXXXX")
BACKUP_PLIST=""
cleanup_temporary_files() {
  if [[ -n "$CANDIDATE" ]]; then
    /bin/rm -f -- "$CANDIDATE"
  fi
  if [[ -n "$BACKUP_PLIST" ]]; then
    /bin/rm -f -- "$BACKUP_PLIST"
  fi
}
trap cleanup_temporary_files EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

agentbar_render_launch_agent_plist \
  "$CANDIDATE" "$LABEL" "$UV_BIN" "$PROJECT_DIR" "$STATE_DIR"

OLD_PLIST_PRESENT=false
if [[ -e "$PLIST" || -L "$PLIST" ]]; then
  if [[ ! -f "$PLIST" || -L "$PLIST" ]]; then
    echo "错误：拒绝覆盖非普通 LaunchAgent plist: $PLIST" >&2
    exit 1
  fi
  OLD_PLIST_PRESENT=true
  BACKUP_PLIST=$(/usr/bin/mktemp "$LAUNCH_AGENTS_DIR/.${LABEL}.backup.XXXXXX")
  /bin/cp -p "$PLIST" "$BACKUP_PLIST"
fi

OLD_JOB_LOADED=false
if agentbar_job_loaded "$SERVICE_TARGET"; then
  OLD_JOB_LOADED=true
fi
runtime_pid=$(agentbar_runtime_pid "$RUNTIME")

# Stop before replacing the plist. A bootout failure is not ignored: deleting
# the only service definition while launchd still owns it makes later recovery
# needlessly difficult.
agentbar_bootout_if_loaded "$SERVICE_TARGET"
if ! agentbar_stop_runtime_instance "$RUNTIME" 45 "$runtime_pid"; then
  echo "安装已中止；旧 plist 未修改" >&2
  exit 1
fi

if ! /bin/mv -f "$CANDIDATE" "$PLIST"; then
  if [[ "$OLD_JOB_LOADED" == true && "$OLD_PLIST_PRESENT" == true ]]; then
    /bin/launchctl bootstrap "gui/$USER_ID" "$PLIST" 2>/dev/null || true
  fi
  echo "错误：无法原子安装 LaunchAgent plist" >&2
  exit 1
fi
CANDIDATE=""

rollback_install() {
  local restored=false
  local new_pid=""
  local replacement_stopped=true
  new_pid=$(agentbar_runtime_pid "$RUNTIME")
  if ! agentbar_bootout_if_loaded "$SERVICE_TARGET"; then
    echo "严重：新 LaunchAgent 未通过健康检查，且 launchd 拒绝停止它；plist 保持新版本以免破坏已加载服务" >&2
    return 1
  fi
  if ! agentbar_stop_runtime_instance "$RUNTIME" 45 "$new_pid"; then
    replacement_stopped=false
  fi

  if [[ "$OLD_PLIST_PRESENT" == true && -n "$BACKUP_PLIST" ]]; then
    /bin/mv -f "$BACKUP_PLIST" "$PLIST"
    BACKUP_PLIST=""
    restored=true
  else
    /bin/rm -f -- "$PLIST"
  fi

  if [[ "$OLD_JOB_LOADED" == true && "$restored" == true \
    && "$replacement_stopped" == true ]]; then
    if /bin/launchctl bootstrap "gui/$USER_ID" "$PLIST"; then
      echo "新版本启动失败；已恢复并重新加载旧 LaunchAgent" >&2
    else
      echo "严重：新版本启动失败，旧 plist 已恢复但重新加载也失败" >&2
    fi
  elif [[ "$replacement_stopped" == true ]]; then
    echo "新版本启动失败；已撤销新 LaunchAgent 安装" >&2
  else
    echo "新版本启动失败；旧 plist 已恢复，但为避免双实例未重新加载，请先确认残留进程" >&2
  fi
}

if ! /bin/launchctl bootstrap "gui/$USER_ID" "$PLIST"; then
  rollback_install
  exit 1
fi
if ! agentbar_wait_healthy "$RUNTIME" 45; then
  rollback_install
  echo "请查看 $STDERR_LOG" >&2
  exit 1
fi

# Older source installs used public /tmp logs that could contain a historical
# token-bearing startup URL. Remove only those two exact obsolete paths, and
# only after the replacement service is healthy.
agentbar_remove_obsolete_log_file /tmp/agentbar.launchd.log
agentbar_remove_obsolete_log_file /tmp/agentbar.launchd.err.log

echo "已从源码目录安装并启动: $PLIST"
echo "日志: $STDOUT_LOG / $STDERR_LOG（权限 0600）"
echo "提示: 移动或删除源码目录后 LaunchAgent 将无法启动；独立安装请使用 DMG。"
