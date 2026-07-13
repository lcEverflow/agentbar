#!/usr/bin/env bash
# 安装 macOS LaunchAgent：登录后自动启动 AgentBar（菜单栏模式）。
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UV_BIN="$(command -v uv || true)"
if [[ -z "$UV_BIN" ]]; then
  echo "错误：未找到 uv。先安装: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

LABEL="com.agentbar.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$UV_BIN</string>
    <string>run</string>
    <string>--project</string>
    <string>$PROJECT_DIR</string>
    <string>agentbar</string>
    <string>run</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>StandardOutPath</key><string>/tmp/agentbar.launchd.log</string>
  <key>StandardErrorPath</key><string>/tmp/agentbar.launchd.err.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "已安装并启动: $PLIST"
echo "日志: /tmp/agentbar.launchd.log"
echo "说明: launchd 环境 PATH 较小，AgentBar 会自动通过登录 shell 解析 claude/codex 路径。"
