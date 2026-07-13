"""Fake AI CLI used by tests / smoke runs.

用法: fake_cli.py [--resume SID] <prompt>
prompt 首个 token 决定行为:
  OK                → 输出结果, 退出 0
  SLEEP:<sec>       → 睡眠后按 OK 处理
  FAIL              → 退出 3
  QUOTA             → 打印 usage-limit 信息, 退出 1
  QUOTA_ONCE:<key>  → 第一次调用走 QUOTA, 之后(以 cwd 下 marker 文件判断)走 OK
"""

import sys
import time
from pathlib import Path


def ok(resumed: bool) -> int:
    if resumed:
        print("RESUMED")
    print("FAKE DONE")
    print("session id: fake-sess-1")
    return 0


def main() -> int:
    args = sys.argv[1:]
    resumed = False
    if args and args[0] == "--resume":
        resumed = True
        args = args[2:]
    prompt = args[0] if args else "OK"
    head = prompt.strip().split()[0] if prompt.strip() else "OK"

    if head.startswith("SLEEP:"):
        time.sleep(float(head.split(":", 1)[1]))
        return ok(resumed)
    if head == "FAIL":
        print("boom: something broke")
        return 3
    if head == "QUOTA":
        print(f"usage limit reached|{int(time.time()) + 3600}")
        return 1
    if head.startswith("QUOTA_ONCE:"):
        key = head.split(":", 1)[1]
        marker = Path.cwd() / f".fake-quota-{key}"
        if not marker.exists():
            marker.write_text("1")
            # 不带可解析的恢复时间 → 走调度器的退避策略
            print("usage limit reached (no reset hint)")
            return 1
        return ok(resumed)
    return ok(resumed)


if __name__ == "__main__":
    sys.exit(main())
