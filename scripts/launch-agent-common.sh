#!/usr/bin/env bash
# Shared, side-effect-free helpers for the LaunchAgent lifecycle scripts.
# Callers deliberately choose when to stop/start jobs; this file only provides
# exact-instance ownership checks, plist rendering, and health probes.

agentbar_render_launch_agent_plist() {
  local destination="$1"
  local label="$2"
  local uv_bin="$3"
  local project_dir="$4"
  local state_dir="$5"

  [[ -n "$destination" && -n "$label" && -n "$uv_bin" && -n "$project_dir" && -n "$state_dir" ]] || {
    echo "错误：生成 LaunchAgent plist 时缺少参数" >&2
    return 1
  }

  # plutil serializes each string itself, so paths containing &, <, quotes, or
  # non-ASCII characters cannot corrupt the XML document.
  /usr/bin/plutil -create xml1 "$destination"
  /usr/bin/plutil -insert Label -string "$label" "$destination"
  /usr/bin/plutil -insert ProgramArguments -array "$destination"
  /usr/bin/plutil -insert ProgramArguments -string "$uv_bin" -append "$destination"
  /usr/bin/plutil -insert ProgramArguments -string run -append "$destination"
  /usr/bin/plutil -insert ProgramArguments -string --locked -append "$destination"
  /usr/bin/plutil -insert ProgramArguments -string --project -append "$destination"
  /usr/bin/plutil -insert ProgramArguments -string "$project_dir" -append "$destination"
  /usr/bin/plutil -insert ProgramArguments -string agentbar -append "$destination"
  /usr/bin/plutil -insert ProgramArguments -string run -append "$destination"
  /usr/bin/plutil -insert RunAtLoad -bool true "$destination"
  /usr/bin/plutil -insert KeepAlive -dictionary "$destination"
  /usr/bin/plutil -insert KeepAlive.SuccessfulExit -bool false "$destination"
  /usr/bin/plutil -insert LimitLoadToSessionType -string Aqua "$destination"
  # Give AgentBar enough time to drain HTTP handlers and terminate managed CLI
  # process groups before launchd escalates shutdown.
  /usr/bin/plutil -insert ExitTimeOut -integer 45 "$destination"
  /usr/bin/plutil -insert StandardOutPath -string \
    "$state_dir/launchd.stdout.log" "$destination"
  /usr/bin/plutil -insert StandardErrorPath -string \
    "$state_dir/launchd.stderr.log" "$destination"
  /bin/chmod 600 "$destination"
  /usr/bin/plutil -lint "$destination" >/dev/null
}

agentbar_runtime_pid() {
  local runtime_path="$1"
  local pid=""
  if [[ -f "$runtime_path" ]]; then
    pid=$(/usr/bin/plutil -extract pid raw -o - "$runtime_path" 2>/dev/null || true)
  fi
  if [[ "$pid" =~ ^[0-9]+$ ]] && (( pid > 1 )); then
    printf '%s\n' "$pid"
  fi
}

agentbar_pid_alive() {
  local pid="$1"
  local process_state=""
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  process_state=$(/bin/ps -p "$pid" -o state= 2>/dev/null || true)
  [[ "$process_state" != Z* ]]
}

agentbar_pid_is_agentbar() {
  local pid="$1"
  local command_line=""
  command_line=$(/bin/ps -p "$pid" -o command= 2>/dev/null || true)
  case "$command_line" in
    *"/agentbar run"*|*"/bin/agentbar run"*|*"AgentBar.app/Contents/MacOS/AgentBar"*)
      return 0
      ;;
  esac
  return 1
}

agentbar_remove_runtime_if_owned() {
  local runtime_path="$1"
  local expected_pid="$2"
  local current_pid=""
  current_pid=$(agentbar_runtime_pid "$runtime_path")
  if [[ "$current_pid" == "$expected_pid" ]]; then
    # The path itself is removed, never a resolved target. A malicious/stale
    # symlink therefore cannot turn cleanup into deletion of another file.
    /bin/rm -f -- "$runtime_path"
  fi
}

agentbar_stop_runtime_instance() {
  local runtime_path="$1"
  local timeout_seconds="${2:-30}"
  local expected_pid="${3:-}"
  local pid="$expected_pid"
  local ticks=0
  local max_ticks=$(( timeout_seconds * 2 ))

  if [[ -z "$pid" ]]; then
    pid=$(agentbar_runtime_pid "$runtime_path")
  fi
  [[ -n "$pid" ]] || return 0

  if ! agentbar_pid_alive "$pid"; then
    agentbar_remove_runtime_if_owned "$runtime_path" "$pid"
    return 0
  fi
  if ! agentbar_pid_is_agentbar "$pid"; then
    echo "错误：$runtime_path 指向 PID $pid，但该进程不是可识别的 AgentBar；为避免误杀已中止" >&2
    return 1
  fi

  kill -TERM "$pid" 2>/dev/null || true
  while agentbar_pid_alive "$pid" && (( ticks < max_ticks )); do
    /bin/sleep 0.5
    ticks=$((ticks + 1))
  done
  if agentbar_pid_alive "$pid"; then
    # Never SIGKILL the scheduler: doing so can strand its independently
    # sessioned Claude/Codex children. A failed safe restart is preferable to
    # two agents editing the same workspace concurrently.
    echo "错误：AgentBar PID $pid 在 ${timeout_seconds}s 内未安全退出；未强杀进程" >&2
    return 1
  fi
  agentbar_remove_runtime_if_owned "$runtime_path" "$pid"
}

agentbar_job_loaded() {
  local service_target="$1"
  /bin/launchctl print "$service_target" >/dev/null 2>&1
}

agentbar_bootout_if_loaded() {
  local service_target="$1"
  if agentbar_job_loaded "$service_target"; then
    /bin/launchctl bootout "$service_target"
  fi
}

agentbar_remove_obsolete_log_file() {
  local path="$1"
  if [[ -f "$path" || -L "$path" ]]; then
    /bin/rm -f -- "$path"
  elif [[ -e "$path" ]]; then
    echo "警告：历史日志路径不是普通文件，未删除: $path" >&2
  fi
}

agentbar_wait_healthy() {
  local runtime_path="$1"
  local timeout_seconds="${2:-45}"
  local ticks=0
  local max_ticks=$(( timeout_seconds * 2 ))
  local pid=""
  local port=""
  local response=""

  while (( ticks < max_ticks )); do
    pid=$(agentbar_runtime_pid "$runtime_path")
    port=""
    if [[ -f "$runtime_path" ]]; then
      port=$(/usr/bin/plutil -extract port raw -o - "$runtime_path" 2>/dev/null || true)
    fi
    if [[ -n "$pid" && "$port" =~ ^[0-9]+$ ]] && (( port >= 1 && port <= 65535 )) \
      && agentbar_pid_alive "$pid"; then
      response=$(/usr/bin/curl --disable --noproxy '*' --fail --silent --max-time 2 \
        "http://127.0.0.1:${port}/api/ping" 2>/dev/null || true)
      if printf '%s' "$response" \
        | /usr/bin/grep -Eq '"app"[[:space:]]*:[[:space:]]*"agentbar"'; then
        return 0
      fi
    fi
    /bin/sleep 0.5
    ticks=$((ticks + 1))
  done
  return 1
}
