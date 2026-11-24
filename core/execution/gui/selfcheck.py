"""自检报告：这台机器现在能不能「看、点、发、认人」，一次说清（U-22）。

自检只**读**不**动**：默认不连 CDP、不模拟按键、不写账本，只回答「如果现在让我干，
哪一步会先失败、理由是什么」。所以它可以随时跑，也可以在真发之前跑一次。

七节：

- ``capability``：六项能力（完整性 / 会话 / DPI / 身份可读 / 可写 / 菜单）；
- ``backends``：登记了哪几只手、各自会哪些动词、开关键有没有让它选得中。
  ``probe_network=False``（默认）时 CDP **不连接**，只报「未探测」；
- ``lease``：``input`` 现在归谁；持有者心跳年龄超过 ``STUCK_FACTOR`` 倍 TTL 记为失败；
- ``wechat_db``：解密库在不在、能不能读、最新一行有多少秒前（落盘延迟的经验值）；
- ``flags``：开关组合自相矛盾的地方（``wechat_send=on`` 但 ``wechat_ui=off`` 这类）；
- ``receipts``：回执表按状态计数（没有库时记「未测」）。

``ok is None`` 的含义是**没测**，不是通过——报告里逐条写清「为什么没测」。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from safelog import warn

log = logging.getLogger(__name__)

SECTION_NAMES = ("capability", "backends", "eyes", "lease", "wechat_db", "flags", "receipts")

#: 心跳年龄超过 TTL 的这个倍数，视为「持有者可能僵死」（与 ``wechat.STUCK_FACTOR`` 同值）。
STUCK_FACTOR = 2.0

#: 失败理由 → 人话提示（只解释最常见的几条，不猜别的）
HINTS = {
    "composer_unwritable": "微信窗口可能最小化到托盘，先点开再看",
    "integrity": "我们的进程完整性级别低于微信（UIPI 会静默丢弃 SendInput）",
    "session": "当前不是交互会话（锁屏 / 远程断开都会这样）",
    "dpi": "进程不是 DPI-aware，取到的控件坐标会偏",
    "identity": "读不到微信窗口或会话身份（窗口没开 / 换了账号）",
    "eyes_no_send_plan": "只读视图白名单或清单字数闸又把它挡了，先看 core/tools/prompt.py",
    "menu": "UIA 读不到右键菜单（换肤或版本变化）",
}

#: 会互相矛盾的开关组合：前者开着，后者必须也开着，否则那个开关等于空转。
DEPENDENCIES = (
    ("wechat_send", "wechat_ui", "发消息要有人把字填进输入框"),
    ("wechat_revoke", "wechat_ui", "撤回要先能找到那条消息"),
    ("wechat_ui", "gui_channel", "微信这条路走的是 GUI 通道"),
    ("tool_loop", "tool_package_prompt", "工具清单不进 prompt，判就不知道有工具"),
    ("handoff", "job_ledger", "没有在手的事，就没有可交接的东西"),
    ("correction", "delivery_contract", "更正要靠回执里的 ref 才指得回原文"),
)


@dataclass(frozen=True)
class Section:
    """一节结论。``ok is None`` = 没测（不是通过）。"""

    name: str
    ok: bool | None
    detail: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)

    def line(self) -> str:
        mark = {True: "✅", False: "❌", None: "➖"}[self.ok]
        return f"{mark} {self.name}: {self.detail}"


@dataclass(frozen=True)
class SelfCheckReport:
    sections: tuple[Section, ...] = ()
    at: str = ""

    @property
    def ok(self) -> bool:
        return all(s.ok is not False for s in self.sections)

    def failed(self) -> list[Section]:
        return [s for s in self.sections if s.ok is False]

    def blocked(self) -> list[str]:
        return [s.name for s in self.failed()]

    def section(self, name: str) -> Section | None:
        for s in self.sections:
            if s.name == name:
                return s
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "at": self.at,
            "sections": [
                {"name": s.name, "ok": s.ok, "detail": s.detail, "data": dict(s.data)}
                for s in self.sections
            ],
        }

    def text(self) -> str:
        lines = [f"自检 {self.at}"]
        lines.extend(s.line() for s in self.sections)
        if self.ok:
            lines.append("结论：这台机器现在可以开工。")
        else:
            lines.append("结论：先解决 " + "、".join(self.blocked()) + " 再动手。")
        return "\n".join(lines)


def _now_iso(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() if now is None else now))


def _capability() -> Section:
    from core.execution.gui import capability as cap

    report = cap.probe()
    detail = report.one_line() if report.ok else f"{report.one_line()}（{report.reason}）"
    hint = HINTS.get(report.reason, "")
    if hint and not report.ok:
        detail = f"{detail}——{hint}"
    return Section(name="capability", ok=report.ok, detail=detail, data=report.as_dict())


def _backends(*, probe_network: bool = False) -> Section:
    from core.execution.gui import backends as B

    items: dict[str, Any] = {}
    for info in B.infos():
        items[info.name] = {
            "verbs": sorted(info.verbs),
            "selectable": B.supports(info.name, next(iter(info.verbs))) if info.verbs else False,
        }
    cdp = items.get("cdp")
    if cdp is not None:
        cdp["network"] = "未探测（默认不连）"
        if probe_network:
            try:
                from core.execution.gui.cdp import CdpReader

                targets = CdpReader().list_targets()
                cdp["network"] = f"可连（{len(targets)} 个页面）"
            except Exception as exc:  # noqa: BLE001 - 自检不抛
                warn("core/execution/gui/selfcheck.py:143 Exception", err=repr(exc))
                cdp["network"] = f"连不上：{exc}"
    names = "、".join(items) or "（无）"
    bad = [n for n, v in items.items() if not v.get("selectable")]
    return Section(
        name="backends",
        ok=not bad,
        detail=f"{names}" + (f"；选不中：{'、'.join(bad)}" if bad else ""),
        data=items,
    )


def _eyes(*, specs: Any = None, failures: Any = None, limit: int = 3) -> Section:
    """她这一轮真正会看到的工具清单 + 最近没成的事（`[最近没成]` 那几行）。

    判据（2026-10-21 真机）：她答「我没有往群里发消息的工具」时，`send_plan`
    其实早在注册表里——是清单被整块丢 / 被截断。这一节就是把"她眼睛亮不亮"
    变成一条可读的结论，重启后一条命令就能确认。
    """
    try:
        from core.tools import builtin
        from core.tools.prompt import render_tool_block
        from core.tools.registry import hint_for

        if specs is None:
            specs = builtin.all_specs()
        block = render_tool_block(specs)
        lines = [ln for ln in block.splitlines() if ln.startswith("- ")]
        has_send_plan = any(ln.startswith("- send_plan") for ln in lines)
        data: dict[str, Any] = {
            "tools": len(lines),
            "chars": len(block),
            "has_send_plan": has_send_plan,
        }
        detail = str(len(lines)) + " 个工具 / " + str(len(block)) + " 字"
        if not lines:
            return Section("eyes", False, "清单是空的（她看不见自己会什么）", data)
        if not has_send_plan:
            return Section(
                "eyes",
                False,
                detail + "，但**没有 send_plan**（" + HINTS.get("eyes_no_send_plan", "") + "）",
                data,
            )
        rows: list[dict] = []
        if failures is None:
            try:
                from core.jobs.store import default_store

                rows = list(default_store().recent_failures("", limit=limit) or [])
            except Exception:  # noqa: BLE001 - 台账没开就没有
                warn("core/execution/gui/selfcheck.py:193 Exception", err=repr(__import__("sys").exc_info()[1]))
                rows = []
        else:
            rows = list(failures)[:limit]
        hints = [hint_for(str(r.get("status") or "failed")) for r in rows]
        data["failures"] = len(rows)
        if rows:
            detail += "，最近没成 " + str(len(rows)) + " 件（首条：" + str(rows[0].get("detail", ""))[:40] + "）"
        else:
            detail += "，最近没有失败"
        data["hint_sample"] = next((h for h in hints if h), "")
        return Section("eyes", True, detail, data)
    except Exception as exc:  # noqa: BLE001 - 一节炸了不拖累别的
        warn("core/execution/gui/selfcheck.py:205 Exception", err=repr(exc))
        return Section("eyes", False, "自检本身出错：" + type(exc).__name__)


def _lease(*, now: float | None = None, lock_dir: Any = None) -> Section:
    """手归谁：**锁空闲就是可以开工**，状态文件过期只是残留。

    真机教训（2026-10-21）：一次非正常退出留下 `data/.locks/input.json`，
    只看状态文件会把"锁其实空闲"报成"可能僵死"，于是自检永远红、人开始怀疑
    整套租约。判据要问的是锁，不是那条 json。
    """
    from core.execution.gui import lease as L

    state = L.describe_holder(L.RESOURCE_INPUT, lock_dir=lock_dir, now=now)
    holder = str(state.get("holder") or "")
    age = float(state.get("heartbeat_age_s", -1.0) or -1.0)
    free = L.lock_is_free(L.RESOURCE_INPUT, lock_dir=lock_dir)
    data = dict(state)
    data["lock_free"] = free
    if not holder:
        return Section(name="lease", ok=True, detail="空着（没人拿着手）", data=data)
    if free:
        return Section(
            name="lease",
            ok=True,
            detail=f"锁空闲（{holder} 的状态是残留，pid={state.get('pid')} 心跳 {age:.0f}s 前）",
            data=data,
        )
    stuck = age > STUCK_FACTOR * L.DEFAULT_TTL_S
    detail = f"真被 {holder} 占着（pid={state.get('pid')} 心跳 {age:.0f}s 前）"
    if stuck:
        detail += "——心跳过期，可能僵死（要不要 taskkill 由人定）"
    return Section(name="lease", ok=False, detail=detail, data=data)


def _wechat_db(*, reader: Callable[[], dict[str, Any]] | None = None) -> Section:
    fn = reader or _default_db_reader
    try:
        got = fn()
    except Exception as exc:  # noqa: BLE001 - 自检不抛
        warn("core/execution/gui/selfcheck.py:244 Exception", err=repr(exc))
        return Section(name="wechat_db", ok=False, detail=f"读不了：{exc}", data={})
    if not got.get("path"):
        return Section(name="wechat_db", ok=False, detail="没找到解密库", data=dict(got))
    age = got.get("latest_age_s")
    age_txt = "刚写过" if age is None else f"最新一行 {age:.0f}s 前"
    return Section(
        name="wechat_db",
        ok=True,
        detail=f"{got.get('rows', 0)} 行可读，{age_txt}",
        data=dict(got),
    )


def _default_db_reader() -> dict[str, Any]:
    import time as _t

    from core.execution.gui import wechat as W

    path = W._decrypted_db()
    row = W.latest_row(path) if path else {}
    latest = int(row.get("create_time") or 0)
    age = None if not latest else max(0.0, _t.time() - latest)
    return {"path": str(path or ""), "rows": 1 if row else 0,
            "latest_age_s": age, "chat": row.get("chat", "")}


def _flags(*, reader: Callable[[str], str] | None = None) -> Section:
    from core import features

    get = reader or (lambda name: features.mode(name, features.MODE_OFF))
    modes = {name: get(name) for pair in DEPENDENCIES for name in pair}
    on = {name for name, mode in modes.items() if mode == features.MODE_ON}
    missing = [
        f"{a}=on 但 {b}={modes.get(b, '?')}（{why}）"
        for a, b, why in DEPENDENCIES
        if a in on and modes.get(b) != features.MODE_ON
    ]
    detail = "开关组合自洽" if not missing else "；".join(missing)
    return Section(name="flags", ok=not missing, detail=detail, data=modes)


def _default_receipt_counter() -> Mapping[str, int]:
    from core.terminal import delivery_receipts as R

    return R.count_by_status()


def _receipts(*, counter: Callable[[], Mapping[str, int]] | None = None) -> Section:
    counter = counter or _default_receipt_counter
    try:
        got = dict(counter())
    except Exception as exc:  # noqa: BLE001
        warn("core/execution/gui/selfcheck.py:296 Exception", err=repr(exc))
        return Section(name="receipts", ok=None, detail=f"查不了：{exc}", data={})
    total = sum(int(v) for v in got.values())
    return Section(
        name="receipts",
        ok=None if not total else True,
        detail="还没有回执" if not total else "、".join(f"{k}={v}" for k, v in sorted(got.items())),
        data=got,
    )


def run_selfcheck(
    *,
    capability: Callable[[], Section] | None = None,
    backends: Callable[[], Section] | None = None,
    eyes: Callable[[], Section] | None = None,
    lease: Callable[[], Section] | None = None,
    wechat_db: Callable[[], Section] | None = None,
    flags: Callable[[], Section] | None = None,
    receipts: Callable[[], Section] | None = None,
    probe_network: bool = False,
    now: float | None = None,
) -> SelfCheckReport:
    """跑七节自检。每个探针都可注入（测试不碰真桌面、不碰真库）。"""
    steps = (
        capability or _capability,
        backends or (lambda: _backends(probe_network=probe_network)),
        eyes or (lambda: _eyes()),
        lease or (lambda: _lease(now=now)),
        wechat_db or _wechat_db,
        flags or _flags,
        receipts or (lambda: _receipts()),
    )
    sections: list[Section] = []
    for index, step in enumerate(steps):
        try:
            sections.append(step())
        except Exception as exc:  # noqa: BLE001 - 一节炸了不影响其它节
            warn("core/execution/gui/selfcheck.py:333 Exception", err=repr(exc))
            name = SECTION_NAMES[index] if index < len(SECTION_NAMES) else f"section{index}"
            sections.append(Section(name=name, ok=False, detail=f"探针炸了：{exc}", data={}))
    report = SelfCheckReport(sections=tuple(sections), at=_now_iso(now))
    log.info("selfcheck %s %s", "ok" if report.ok else "blocked", report.blocked())
    return report
