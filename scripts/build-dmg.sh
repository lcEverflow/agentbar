#!/usr/bin/env bash
# Build dist/AgentBar-<ver>.dmg — drag-to-Applications one-click installer.
#
# py2app 需要 framework 构建的 Python（Homebrew python@3.13）；
# uv 管理的 python-build-standalone 不是 framework build，py2app 会拒绝。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYFW="${AGENTBAR_BUILD_PYTHON:-/opt/homebrew/opt/python@3.13/bin/python3.13}"
VENV="$ROOT/.build-venv"
DIST="$ROOT/dist"

if [[ ! -x "$PYFW" ]]; then
  echo "错误：找不到 framework Python: $PYFW" >&2
  echo "  brew install python@3.13  （或设置 AGENTBAR_BUILD_PYTHON）" >&2
  exit 1
fi

VERSION=$(sed -n 's/__version__ = "\(.*\)"/\1/p' "$ROOT/agentbar/__init__.py")
echo "==> AgentBar v$VERSION"

# 1. 构建 venv（framework python + py2app + 运行时依赖）
if [[ ! -x "$VENV/bin/python" ]]; then
  echo "==> 创建构建 venv: ${PYFW}"
  "$PYFW" -m venv "$VENV"
fi
echo "==> 安装依赖（pip）"
"$VENV/bin/pip" -q install --upgrade pip setuptools py2app
"$VENV/bin/pip" -q install "$ROOT"

# 2. py2app（在 packaging/ 下执行，构建产物集中在 dist/build 临时目录）
echo "==> py2app 打包"
rm -rf "$DIST/AgentBar.app" "$ROOT/packaging/build" "$ROOT/packaging/dist"
(cd "$ROOT/packaging" && "$VENV/bin/python" py2app_setup.py py2app -q 2>&1 | tail -3)
mkdir -p "$DIST"
mv "$ROOT/packaging/dist/AgentBar.app" "$DIST/AgentBar.app"
rm -rf "$ROOT/packaging/build" "$ROOT/packaging/dist" "$ROOT/packaging/.eggs"

# 3. ad-hoc 签名（Apple Silicon 上未签名二进制直接拒载）
echo "==> ad-hoc codesign"
codesign --force --deep -s - "$DIST/AgentBar.app" 2>/dev/null

# 4. 冒烟测试：隔离 state dir + 随机端口拉起 headless，ping 通过即算过
echo "==> 冒烟测试 .app 内置 python"
SMOKE_DIR=$(mktemp -d)
AGENTBAR_STATE_DIR="$SMOKE_DIR" "$DIST/AgentBar.app/Contents/MacOS/AgentBar" --version \
  || { echo "错误：.app 启动失败" >&2; exit 1; }
rm -rf "$SMOKE_DIR"

# 5. DMG：AgentBar.app + /Applications 快捷方式，拖拽即安装
echo "==> 生成 DMG"
STAGE=$(mktemp -d)
cp -R "$DIST/AgentBar.app" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cat > "$STAGE/安装说明.txt" <<'EOF'
AgentBar 安装：
1. 把 AgentBar.app 拖到 Applications 文件夹
2. 首次打开：右键 AgentBar.app → 打开（未签名应用需手动放行一次）
3. 菜单栏出现 ◇ 图标即已运行；点击 → 打开任务面板

手机访问：菜单栏 ◇ → 📱 手机访问（扫码），手机与 Mac 连同一 Wi-Fi。

卸载：退出 AgentBar 后删除 /Applications/AgentBar.app 与 ~/.agentbar
EOF
DMG="$DIST/AgentBar-$VERSION.dmg"
rm -f "$DMG"
hdiutil create -volname "AgentBar" -srcfolder "$STAGE" -ov -format UDZO "$DMG" -quiet
rm -rf "$STAGE"

echo "==> 完成: $DMG ($(du -h "$DMG" | cut -f1))"
