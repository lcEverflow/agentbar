"""py2app build config — produces dist/AgentBar.app (standalone bundle).

Run via scripts/build-dmg.sh, NOT directly: it needs a framework-build
Python (Homebrew python@3.13) — uv-managed python-build-standalone
interpreters are not framework builds and py2app refuses them.
"""

import pathlib
import re
import sys

from setuptools import setup

HERE = pathlib.Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

VERSION = re.search(
    r'__version__ = "([^"]+)"',
    (ROOT / "agentbar" / "__init__.py").read_text(),
)
if VERSION is None:
    raise RuntimeError("cannot read AgentBar version from agentbar/__init__.py")
VERSION = VERSION.group(1)

setup(
    name="AgentBar",
    app=[str(HERE / "AgentBar.py")],
    options={
        "py2app": {
            # 不打 zip：agentbar 需要 importlib.resources 读 web/*.html，
            # qrcode 需要读包内数据文件，散装进 Resources/lib 最稳。
            "packages": ["agentbar", "qrcode", "mistune", "websocket"],
            # These imports are lazy in normal startup and therefore easy for a
            # freezer to miss. Keep them explicit and verify them from the
            # finished .app in scripts/build-dmg.sh.
            "includes": [
                "AppKit",
                "Foundation",
                "PyObjCTools.AppHelper",
                "WebKit",
                "qrcode.image.svg",
            ],
            "plist": {
                "CFBundleName": "AgentBar",
                "CFBundleDisplayName": "AgentBar",
                "CFBundleIdentifier": "com.agentbar.app",
                "CFBundleShortVersionString": VERSION,
                "CFBundleVersion": VERSION,
                "LSUIElement": True,  # 菜单栏 accessory：无 Dock 图标
                "LSMinimumSystemVersion": "13.0",
            },
        }
    },
)
