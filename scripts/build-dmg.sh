#!/usr/bin/env bash
# Build dist/AgentBar-<version>.dmg from a clean, disposable py2app environment.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/dist"
TEMP_ROOT="${TMPDIR:-/tmp}"
TEMP_ROOT="${TEMP_ROOT%/}"
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

BUILD_VENV=$(/usr/bin/mktemp -d "$TEMP_ROOT/agentbar-build-venv.XXXXXX")
BUILD_LOG=$(/usr/bin/mktemp "$TEMP_ROOT/agentbar-py2app.XXXXXX.log")
REQUIREMENTS=$(/usr/bin/mktemp "$TEMP_ROOT/agentbar-requirements.XXXXXX.txt")
SMOKE_DIR=""
STAGE=""
DMG_WORK=""
APP_BACKUP=""

cleanup() {
  /bin/rm -rf -- "$BUILD_VENV"
  /bin/rm -f -- "$BUILD_LOG" "$REQUIREMENTS"
  if [[ -n "$SMOKE_DIR" ]]; then /bin/rm -rf -- "$SMOKE_DIR"; fi
  if [[ -n "$STAGE" ]]; then /bin/rm -rf -- "$STAGE"; fi
  if [[ -n "$DMG_WORK" ]]; then /bin/rm -rf -- "$DMG_WORK"; fi
  /bin/rm -rf -- "$ROOT/packaging/build" "$ROOT/packaging/dist" \
    "$ROOT/packaging/.eggs" "$ROOT/packaging/__pycache__"
  if [[ -n "$APP_BACKUP" && -d "$APP_BACKUP" ]]; then
    if [[ ! -e "$DIST/AgentBar.app" ]]; then
      /bin/mv "$APP_BACKUP" "$DIST/AgentBar.app" || true
    else
      /bin/rm -rf -- "$APP_BACKUP"
    fi
  fi
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

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

# Publish the already-verified app while retaining the previous known-good app
# until the final rename succeeds.
/bin/mkdir -p "$DIST"
if [[ -e "$DIST/AgentBar.app" ]]; then
  APP_BACKUP="$DIST/.AgentBar.app.previous.$$"
  /bin/rm -rf -- "$APP_BACKUP"
  /bin/mv "$DIST/AgentBar.app" "$APP_BACKUP"
fi
if ! /bin/mv "$CANDIDATE_APP" "$DIST/AgentBar.app"; then
  echo "错误：无法发布新 AgentBar.app" >&2
  exit 1
fi
if [[ -n "$APP_BACKUP" ]]; then
  /bin/rm -rf -- "$APP_BACKUP"
  APP_BACKUP=""
fi

echo "==> 生成并校验 DMG"
STAGE=$(/usr/bin/mktemp -d "$TEMP_ROOT/agentbar-dmg-stage.XXXXXX")
/usr/bin/ditto "$DIST/AgentBar.app" "$STAGE/AgentBar.app"
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
DMG_NAME="AgentBar-$VERSION.dmg"
/usr/bin/hdiutil create -volname AgentBar -srcfolder "$STAGE" \
  -format UDZO "$DMG_WORK/$DMG_NAME" -quiet
/usr/bin/hdiutil verify "$DMG_WORK/$DMG_NAME" -quiet
/bin/mv -f "$DMG_WORK/$DMG_NAME" "$DIST/$DMG_NAME"

SIZE=$(/usr/bin/du -h "$DIST/$DMG_NAME" | /usr/bin/cut -f1)
SHA256=$(/usr/bin/shasum -a 256 "$DIST/$DMG_NAME" | /usr/bin/cut -d ' ' -f1)
echo "==> 完成: $DIST/$DMG_NAME ($SIZE)"
echo "SHA-256: $SHA256"
