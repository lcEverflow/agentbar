#!/usr/bin/env bash
# 卸载 AgentBar 的 LaunchAgent。
set -euo pipefail
LABEL="com.agentbar.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "已卸载 $LABEL"
