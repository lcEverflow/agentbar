"""Menu content as pure data — no AppKit imports, fully unit-testable.

menubar.py 只负责把这里产出的 spec 渲染成 NSMenu。规则：
- 除分隔线外，每一行要么有 action、要么是带 children 的子菜单入口，
  不存在"点了没反应"的死行（信息行统一 action=open_panel）。
- 节点: {"kind": "action"|"sep"|"submenu", "title": str,
         "action": str|None, "enabled": bool, "children": [...]|None}
"""

from __future__ import annotations

import time

_STATE_GLYPH = {"idle": "◇", "running": "◆", "waiting": "◐", "paused": "Ⅱ"}
_STATE_LABEL = {"idle": "空闲", "running": "运行中", "waiting": "等待中", "paused": "已暂停"}
_TASK_MARK = {"running": "▶", "queued": "·", "waiting_quota": "◐", "paused": "Ⅱ"}

# usage 数据超过该时长视为过期，不再上标题（与 quota.USAGE_STALE_SECONDS 对齐）
TITLE_USAGE_STALE = 15 * 60


def _clock(ts: float | None) -> str:
    if not ts:
        return ""
    if time.localtime(ts).tm_yday != time.localtime().tm_yday:
        return time.strftime("%m-%d %H:%M", time.localtime(ts))
    return time.strftime("%H:%M", time.localtime(ts))


def _action(title: str, action: str = "open_panel", enabled: bool = True) -> dict:
    return {"kind": "action", "title": title, "action": action,
            "enabled": enabled, "children": None}


def _info(title: str) -> dict:
    """A non-clickable informational row (status display)."""
    return {"kind": "action", "title": title, "action": None, "enabled": False, "children": None}


def _sep() -> dict:
    return {"kind": "sep", "title": "", "action": None, "enabled": True, "children": None}


def _submenu(title: str, children: list[dict]) -> dict:
    return {"kind": "submenu", "title": title, "action": None,
            "enabled": True, "children": children}


def build_title(snapshot: dict) -> str:
    """状态栏标题：只放文字（运行数>1 的数字 + Claude 用量）。

    状态图形全部在双环图标里（环心实心点=运行中、双竖条=已暂停），
    标题不再放 ◇/◆/◐/Ⅱ 字符——否则菜单栏看起来像两个图标。
    """
    parts = []
    n_run = len(snapshot.get("running_titles") or [])
    if n_run > 1:
        parts.append(str(n_run))
    claude = (snapshot.get("quota") or {}).get("claude") or {}
    fetched = claude.get("fetched_at")
    windows = claude.get("windows") or []
    state = claude.get("state", "")
    if windows and fetched and time.time() - fetched < TITLE_USAGE_STALE:
        primary = windows[0]
        if primary.get("used_percent") is not None:
            parts.append(f"{primary['used_percent']:.0f}%")
    elif state == "limited":
        parts.append("限额")
    elif state == "ok" and fetched and time.time() - fetched < TITLE_USAGE_STALE:
        parts.append("Claude✓")
    return " ".join(parts)


def _ring_progress(qi: dict | None) -> float | None:
    """单工具环形进度：新鲜 usage 主窗口 used%/100；受限 → 1.0；无数据 → None（只画轨道）。"""
    if not qi:
        return None
    fetched = qi.get("fetched_at")
    windows = qi.get("windows") or []
    if windows and fetched and time.time() - fetched < TITLE_USAGE_STALE:
        up = windows[0].get("used_percent")
        if up is not None:
            return min(1.0, max(0.0, float(up) / 100.0))
    if qi.get("state") == "limited":
        return 1.0
    return None


def build_ring_progress(snapshot: dict) -> tuple[float | None, float | None]:
    """状态栏双环图标进度（外圈 Claude、内圈 Codex）——与 aiusagebar 同款外围圈显示。

    返回 (outer, inner)，每项 0.0-1.0 或 None；None 表示无可信数据，只画底色轨道。
    """
    quota = snapshot.get("quota") or {}
    return _ring_progress(quota.get("claude")), _ring_progress(quota.get("codex"))


def _quota_compact(qi: dict) -> str:
    windows = qi.get("windows") or []
    if windows:
        return " · ".join(
            f"{w['label']} {w['used_percent']:.0f}%" for w in windows[:2]
        )
    return {"ok": "正常", "limited": "受限", "unknown": "未知"}.get(qi.get("state"), "未知")


def _quota_submenu(tool: str, qi: dict) -> dict:
    dot = {"ok": "🟢", "limited": "🟠"}.get(qi.get("state"), "⚪")
    children: list[dict] = []
    for w in qi.get("windows") or []:
        line = f"{w['label']} 已用 {w['used_percent']:.0f}%"
        if w.get("resets_at"):
            line += f" · {_clock(w['resets_at'])} 重置"
        children.append(_action(line))
    meta = []
    if qi.get("plan"):
        meta.append(f"计划 {qi['plan']}")
    meta.append(f"来源 {qi.get('source') or 'none'}")
    if qi.get("fetched_at"):
        meta.append(f"{_clock(qi['fetched_at'])} 更新")
    children.append(_action(" · ".join(meta)))
    if not (qi.get("windows")):
        children.append(_action(qi.get("detail") or "暂无额度数据"))
    if qi.get("error"):
        children.append(_action(f"⚠ {qi['error'][:70]}"))
    children.append(_sep())
    children.append(_action("↻ 立即刷新额度", "refresh_quota"))
    if tool == "claude" and "Keychain" in (qi.get("error") or ""):
        children.append(_action("🔑 授权读取 Claude Keychain…", "authorize_keychain"))
    return _submenu(f"{dot} {tool.capitalize()} · {_quota_compact(qi)}", children)


def _mobile_submenu(t: dict) -> dict:
    """手机访问子菜单：局域网扫码 + 公网隧道（按隧道状态变形）。"""
    children = [_action("📶 局域网扫码（同一 Wi-Fi）…", "mobile_qr")]
    state = t.get("state", "off")
    if state == "up":
        children.append(_action("🌐 公网扫码（已开通）…", "tunnel_qr"))
        children.append(_action("✕ 停止公网访问", "tunnel_stop"))
    elif state == "starting":
        children.append(_info("⏳ 公网隧道启动中…"))
    else:
        if t.get("installed", True):
            children.append(_action("🌐 开通公网访问（外出可用）…", "tunnel_start"))
        else:
            children.append(_info("需先安装: brew install cloudflared"))
        if state == "error" and t.get("error"):
            children.append(_info(f"⚠ {t['error'][:60]}"))
    label = {"up": "📱 手机访问 · 🌐公网已开通", "starting": "📱 手机访问 · 启动中…"}
    return _submenu(label.get(state, "📱 手机访问"), children)


def build_menu_spec(snapshot: dict) -> list[dict]:
    rows: list[dict] = []
    status = snapshot.get("status", "idle")
    label = _STATE_LABEL.get(status, status)
    queued = snapshot.get("queued", 0)
    waiting = snapshot.get("waiting_quota", 0)

    rows.append(_info(f"{_STATE_GLYPH.get(status, '◇')} AgentBar · {label}"))
    rows.append(_info(f"队列 {queued} 排队 · {waiting} 等额度"))
    rows.append(_sep())

    running = snapshot.get("running_titles") or []
    if running:
        for title in running[:3]:
            rows.append(_action(f"▶ {title[:46]}", action="open_panel"))
    else:
        active = [
            t for t in snapshot.get("tasks") or []
            if t.get("state") in ("queued", "waiting_quota", "paused")
        ]
        if active:
            for t in active[:3]:
                mark = _TASK_MARK.get(t["state"], "·")
                rows.append(_action(f"{mark} {t.get('title', '')[:46]}", action="open_panel"))
        else:
            rows.append(_info("暂无进行中的任务"))
    rows.append(_sep())

    quota = snapshot.get("quota") or {}
    for tool in ("claude", "codex"):
        qi = quota.get(tool)
        if qi:
            rows.append(_quota_submenu(tool, qi))
    if any(tool in quota for tool in ("claude", "codex")):
        rows.append(_sep())

    rows.append(_action("↗ 打开任务面板", "open_panel"))
    rows.append(_action("＋ 快速添加任务…", "quick_add"))
    rows.append(_mobile_submenu(snapshot.get("tunnel") or {}))
    rows.append(_sep())
    if snapshot.get("paused"):
        rows.append(_action("▶ 恢复任务派发", "resume_all"))
    else:
        rows.append(_action("Ⅱ 暂停任务派发", "pause_all"))
    rows.append(_sep())
    rows.append(_action("✕ 退出 AgentBar", "quit"))
    return rows
