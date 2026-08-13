"""py2app entry point — launches the AgentBar menu-bar scheduler.

Equivalent to `agentbar run` (menu bar mode). Double-clicking AgentBar.app
runs this; a second launch exits quietly because the port instance-check
in cmd_run detects the running copy.
"""

import sys

from agentbar.cli import main


def _bundle_smoke() -> None:
    """Fail the release build if a lazy GUI/runtime dependency is missing.

    ``agentbar --version`` only imports the CLI core. The menu bar, WebKit
    transcript, browser login, QR, and Markdown paths are intentionally lazy,
    so py2app can otherwise produce an app that builds successfully but fails
    on the first click.
    """
    from importlib import resources

    import AppKit  # noqa: F401
    import Foundation  # noqa: F401
    import WebKit  # noqa: F401
    import mistune  # noqa: F401
    import qrcode  # noqa: F401
    import qrcode.image.svg  # noqa: F401
    import websocket  # noqa: F401

    from agentbar import menubar, panel_window, provider_window  # noqa: F401

    for name in ("index.html", "mobile.html"):
        content = (
            resources.files("agentbar")
            .joinpath(f"web/{name}")
            .read_text("utf-8")
        )
        if "<html" not in content.lower():
            raise RuntimeError(f"bundled web resource is invalid: {name}")
    print("AgentBar bundle smoke test passed")

if __name__ == "__main__":
    # 双击启动无参数 → 默认 run；保留 CLI 透传（构建冒烟测试用 --version）。
    args = [a for a in sys.argv[1:] if not a.startswith("-psn")]
    if args == ["--bundle-smoke"]:
        _bundle_smoke()
    else:
        main(args or ["run"])
