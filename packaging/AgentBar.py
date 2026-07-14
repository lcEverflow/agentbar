"""py2app entry point — launches the AgentBar menu-bar scheduler.

Equivalent to `agentbar run` (menu bar mode). Double-clicking AgentBar.app
runs this; a second launch exits quietly because the port instance-check
in cmd_run detects the running copy.
"""

import sys

from agentbar.cli import main

if __name__ == "__main__":
    # 双击启动无参数 → 默认 run；保留 CLI 透传（构建冒烟测试用 --version）。
    args = [a for a in sys.argv[1:] if not a.startswith("-psn")]
    main(args or ["run"])
