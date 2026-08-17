#!/usr/bin/env bash
# Build dist/AgentBar-<version>.dmg from a clean, disposable py2app environment.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/dist"
TEMP_ROOT="${TMPDIR:-/tmp}"
TEMP_ROOT="${TEMP_ROOT%/}"
if [[ -z "$TEMP_ROOT" ]]; then TEMP_ROOT="/"; fi
PY2APP_VERSION="${AGENTBAR_PY2APP_VERSION:-0.28.10}"
UV_BIN="$(command -v uv || true)"

if [[ ! -d "$ROOT/agentbar" || "$ROOT" == "/" ]]; then
  echo "错误：无法确定 AgentBar 项目根目录" >&2
  exit 1
fi
if [[ ! -d "$TEMP_ROOT" ]]; then
  echo "错误：临时目录不存在: $TEMP_ROOT" >&2
  exit 1
fi
if [[ -z "$UV_BIN" || ! -x "$UV_BIN" ]]; then
  echo "错误：找不到 uv，无法从 uv.lock 导出冻结依赖" >&2
  exit 1
fi

if [[ -n "${AGENTBAR_BUILD_PYTHON:-}" ]]; then
  PYFW="$AGENTBAR_BUILD_PYTHON"
elif [[ -x /opt/homebrew/opt/python@3.13/bin/python3.13 ]]; then
  PYFW=/opt/homebrew/opt/python@3.13/bin/python3.13
elif [[ -x /usr/local/opt/python@3.13/bin/python3.13 ]]; then
  PYFW=/usr/local/opt/python@3.13/bin/python3.13
else
  PYFW=""
fi
if [[ -z "$PYFW" || ! -x "$PYFW" ]]; then
  echo "错误：找不到 Homebrew framework Python 3.13" >&2
  echo "请运行 brew install python@3.13，或设置 AGENTBAR_BUILD_PYTHON" >&2
  exit 1
fi
if ! "$PYFW" -c \
  'import sysconfig; raise SystemExit(not bool(sysconfig.get_config_var("PYTHONFRAMEWORK")))'; then
  echo "错误：AGENTBAR_BUILD_PYTHON 必须是 macOS framework Python" >&2
  exit 1
fi

VERSION=$(/usr/bin/sed -n 's/__version__ = "\([^"]*\)"/\1/p' \
  "$ROOT/agentbar/__init__.py")
PROJECT_VERSION=$(/usr/bin/sed -n 's/^version = "\([^"]*\)"/\1/p' \
  "$ROOT/pyproject.toml")
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "错误：agentbar/__init__.py 中的版本号无效: $VERSION" >&2
  exit 1
fi
if [[ "$VERSION" != "$PROJECT_VERSION" ]]; then
  echo "错误：版本号不一致 (__init__=$VERSION, pyproject=$PROJECT_VERSION)" >&2
  exit 1
fi
echo "==> AgentBar v$VERSION"

BUILD_VENV=""
BUILD_LOG=""
REQUIREMENTS=""
SMOKE_DIR=""
STAGE=""
DMG_WORK=""
PUBLISH_STAGE=""
PUBLISH_BACKUP=""
FINAL_APP="$DIST/AgentBar.app"
DMG_NAME="AgentBar-$VERSION.dmg"
FINAL_DMG="$DIST/$DMG_NAME"
OLD_APP_BACKED_UP=false
OLD_DMG_BACKED_UP=false
NEW_APP_PUBLISHED=false
NEW_DMG_PUBLISHED=false
PUBLISH_COMPLETE=false
PRESERVE_PUBLISH_BACKUP=false

cleanup() {
  # Publishing is a two-artifact transaction. If either final rename failed or
  # the build was interrupted between them, restore the exact previous pair.
  if [[ "$PUBLISH_COMPLETE" != true ]]; then
    if [[ "$NEW_APP_PUBLISHED" == true ]]; then
      /bin/rm -rf -- "$FINAL_APP"
    fi
    if [[ "$NEW_DMG_PUBLISHED" == true ]]; then
      /bin/rm -f -- "$FINAL_DMG"
    fi
    if [[ "$OLD_APP_BACKED_UP" == true \
      && ( -e "$PUBLISH_BACKUP/AgentBar.app" \
        || -L "$PUBLISH_BACKUP/AgentBar.app" ) ]]; then
      if ! /bin/mv "$PUBLISH_BACKUP/AgentBar.app" "$FINAL_APP"; then
        echo "严重：无法恢复上一版 AgentBar.app: $PUBLISH_BACKUP/AgentBar.app" >&2
        PRESERVE_PUBLISH_BACKUP=true
      fi
    fi
    if [[ "$OLD_DMG_BACKED_UP" == true \
      && ( -e "$PUBLISH_BACKUP/$DMG_NAME" \
        || -L "$PUBLISH_BACKUP/$DMG_NAME" ) ]]; then
      if ! /bin/mv "$PUBLISH_BACKUP/$DMG_NAME" "$FINAL_DMG"; then
        echo "严重：无法恢复上一版 DMG: $PUBLISH_BACKUP/$DMG_NAME" >&2
        PRESERVE_PUBLISH_BACKUP=true
      fi
    fi
  fi
  if [[ -n "$BUILD_VENV" ]]; then /bin/rm -rf -- "$BUILD_VENV"; fi
  if [[ -n "$BUILD_LOG" ]]; then /bin/rm -f -- "$BUILD_LOG"; fi
  if [[ -n "$REQUIREMENTS" ]]; then /bin/rm -f -- "$REQUIREMENTS"; fi
  if [[ -n "$SMOKE_DIR" ]]; then /bin/rm -rf -- "$SMOKE_DIR"; fi
  if [[ -n "$STAGE" ]]; then /bin/rm -rf -- "$STAGE"; fi
  if [[ -n "$DMG_WORK" ]]; then /bin/rm -rf -- "$DMG_WORK"; fi
  if [[ -n "$PUBLISH_STAGE" ]]; then /bin/rm -rf -- "$PUBLISH_STAGE"; fi
  if [[ -n "$PUBLISH_BACKUP" && "$PRESERVE_PUBLISH_BACKUP" != true ]]; then
    /bin/rm -rf -- "$PUBLISH_BACKUP"
  fi
  /bin/rm -rf -- "$ROOT/packaging/build" "$ROOT/packaging/dist" \
    "$ROOT/packaging/.eggs" "$ROOT/packaging/__pycache__"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# Install cleanup before the first allocation: if a later mktemp fails, every
# earlier temporary path is still reclaimed by the EXIT trap.
BUILD_VENV=$(/usr/bin/mktemp -d "$TEMP_ROOT/agentbar-build-venv.XXXXXX")
BUILD_LOG=$(/usr/bin/mktemp "$TEMP_ROOT/agentbar-py2app.XXXXXX.log")
REQUIREMENTS=$(/usr/bin/mktemp "$TEMP_ROOT/agentbar-requirements.XXXXXX.txt")

echo "==> 创建隔离构建环境: $PYFW"
"$PYFW" -m venv "$BUILD_VENV"
"$BUILD_VENV/bin/pip" -q install "py2app==$PY2APP_VERSION"

# Runtime dependencies come from the committed lockfile. The project itself is
# copied directly by py2app, so excluding it avoids an unrelated build-isolation
# resolver from silently selecting different packaging dependencies.
"$UV_BIN" export --quiet --locked --no-dev --no-emit-project \
  --format requirements.txt \
  --output-file "$REQUIREMENTS"
"$BUILD_VENV/bin/pip" -q install --requirement "$REQUIREMENTS"

echo "==> py2app 打包"
/bin/rm -rf -- "$ROOT/packaging/build" "$ROOT/packaging/dist" \
  "$ROOT/packaging/.eggs" "$ROOT/packaging/__pycache__"
if ! (cd "$ROOT/packaging" \
  && "$BUILD_VENV/bin/python" py2app_setup.py py2app -q >"$BUILD_LOG" 2>&1); then
  echo "错误：py2app 构建失败（末尾日志如下）" >&2
  /usr/bin/tail -n 100 "$BUILD_LOG" >&2
  exit 1
fi
/usr/bin/tail -n 3 "$BUILD_LOG"

CANDIDATE_APP="$ROOT/packaging/dist/AgentBar.app"
if [[ ! -x "$CANDIDATE_APP/Contents/MacOS/AgentBar" ]]; then
  echo "错误：py2app 未生成可执行 AgentBar.app" >&2
  exit 1
fi

echo "==> ad-hoc 签名并验证"
/usr/bin/codesign --force --deep --sign - "$CANDIDATE_APP"
/usr/bin/codesign --verify --deep --strict "$CANDIDATE_APP"

echo "==> 冒烟测试冻结后的 GUI、WebKit、登录和 Web 资源依赖"
SMOKE_DIR=$(/usr/bin/mktemp -d "$TEMP_ROOT/agentbar-smoke.XXXXXX")
AGENTBAR_STATE_DIR="$SMOKE_DIR" \
  "$CANDIDATE_APP/Contents/MacOS/AgentBar" --version
AGENTBAR_STATE_DIR="$SMOKE_DIR" \
  "$CANDIDATE_APP/Contents/MacOS/AgentBar" --bundle-smoke
/bin/rm -rf -- "$SMOKE_DIR"
SMOKE_DIR=""

echo "==> 生成并校验 DMG"
STAGE=$(/usr/bin/mktemp -d "$TEMP_ROOT/agentbar-dmg-stage.XXXXXX")
/usr/bin/ditto "$CANDIDATE_APP" "$STAGE/AgentBar.app"
/bin/ln -s /Applications "$STAGE/Applications"
/bin/cat > "$STAGE/安装说明.txt" <<'EOF'
AgentBar 安装：
1. 把 AgentBar.app 拖到 Applications 文件夹
2. 首次打开：右键 AgentBar.app → 打开（未公证应用需手动放行一次）
3. 菜单栏出现 AgentBar 图标即已运行；点击可打开任务面板

手机访问：菜单栏 → 手机访问（扫码）；手机与 Mac 需在同一可信网络。

卸载 App：退出 AgentBar 后删除 /Applications/AgentBar.app。
~/.agentbar 中的任务、配置与日志不会自动删除，需由用户另行备份或移除。
EOF

DMG_WORK=$(/usr/bin/mktemp -d "$TEMP_ROOT/agentbar-dmg-output.XXXXXX")
/usr/bin/hdiutil create -volname AgentBar -srcfolder "$STAGE" \
  -format UDZO "$DMG_WORK/$DMG_NAME" -quiet
/usr/bin/hdiutil verify "$DMG_WORK/$DMG_NAME" -quiet

# Stage both verified artifacts on the destination filesystem, then replace the
# public pair together. The EXIT trap rolls back both names after any failure.
/bin/mkdir -p "$DIST"
PUBLISH_STAGE=$(/usr/bin/mktemp -d "$DIST/.agentbar-publish.XXXXXX")
PUBLISH_BACKUP=$(/usr/bin/mktemp -d "$DIST/.agentbar-publish-backup.XXXXXX")
/bin/mv "$CANDIDATE_APP" "$PUBLISH_STAGE/AgentBar.app"
/bin/mv "$DMG_WORK/$DMG_NAME" "$PUBLISH_STAGE/$DMG_NAME"
/usr/bin/codesign --verify --deep --strict "$PUBLISH_STAGE/AgentBar.app"
/usr/bin/hdiutil verify "$PUBLISH_STAGE/$DMG_NAME" -quiet

SIZE=$(/usr/bin/du -h "$PUBLISH_STAGE/$DMG_NAME" | /usr/bin/cut -f1)
SHA256=$(/usr/bin/shasum -a 256 "$PUBLISH_STAGE/$DMG_NAME" | /usr/bin/cut -d ' ' -f1)

if [[ -e "$FINAL_APP" || -L "$FINAL_APP" ]]; then
  OLD_APP_BACKED_UP=true
  /bin/mv "$FINAL_APP" "$PUBLISH_BACKUP/AgentBar.app"
fi
if [[ -e "$FINAL_DMG" || -L "$FINAL_DMG" ]]; then
  OLD_DMG_BACKED_UP=true
  /bin/mv "$FINAL_DMG" "$PUBLISH_BACKUP/$DMG_NAME"
fi

NEW_APP_PUBLISHED=true
/bin/mv "$PUBLISH_STAGE/AgentBar.app" "$FINAL_APP"
NEW_DMG_PUBLISHED=true
/bin/mv "$PUBLISH_STAGE/$DMG_NAME" "$FINAL_DMG"
PUBLISH_COMPLETE=true

echo "==> 完成: $FINAL_DMG ($SIZE)"
echo "SHA-256: $SHA256"
