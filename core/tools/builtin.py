# -*- coding: utf-8 -*-
"""工具注册表的默认装填：把「已经有的实现」接成工具包认的形状。

为什么需要这一层
----------------
`core/tools/registry.py` 是工具包的**契约**（七字段、四 effects、三动词），
`agents/tools/*.py` 是**已有的实现**（异步、面向模型、24 个 schema）。
两边形状不一样：契约要同步回调 + `ToolResult`，实现是 `async def execute(**kwargs)`。

这一层只做三件事，不写任何工具逻辑：

1. **登记**：给每个工具写一份 `ToolSpec`（名/族/effects/args/returns/perms/limits/幂等/owner）；
2. **绑定**：把异步实现包成同步 handler（协程扔进独立线程跑，带硬超时）；
3. **开关**：`read` 默认开；`write_local` 默认**不开**，由人显式 enable；
   `external_send` 一个都不许登记（只走出口注册表，R3）。

三条纪律（`docs/decisions/工具包.md` §5 / §7）
-------------------------------------------
* 未登记的工具有 handler 也调不到（`unknown_tool`）；
* 一次调用算一次，失败也算（计数在 registry 里，按 `turn_id`）；
* 协程超时转成 `status="timeout"` 的 `ToolResult`，**不抛异常**——
  「查不了」和「没查到」必须分开，且都不许把上层打断。

本模块不 import `core.speech` / `core.routing` / `scheduler` / `web`
（`scripts/check_layering.py` 规则 11）。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import structlog

from core.execution.gui.base import MIN_CHANGED_PIXELS
from core.tools.registry import (
    Effect,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    UI_BLOCKED,
    UI_MIN_PIXELS_CHANGED,
    can_ui_control,
)
from safelog import warn
from core.infra.jsonio import atomic_write_text

log = structlog.get_logger(__name__)

#: 同步桥的默认硬上限（工具包 §7：observe 8s / act 20s；这里取写档的上限）。
DEFAULT_TIMEOUT_S: float = 20.0

#: 写本地笔记的落点（相对仓库根）。**只在 data/workspace 下**，出不去。
NOTES_DIR = Path("data") / "workspace" / "notes"

#: `write_note` 的名字上限（防止把一整段正文当文件名）。
NOTE_NAME_MAX_CHARS: int = 60


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def note_path(name: str) -> Path:
    """把「杭州三日」变成 `data/workspace/notes/杭州三日.md`（不信任任何路径分隔符）。"""
    raw = str(name or "").strip()
    for bad in ("/", "\\", "..", ":", "*", "?", '"', "<", ">", "|"):
        raw = raw.replace(bad, "_")
    raw = raw.strip("._ ") or "untitled"
    return _repo_root() / NOTES_DIR / (raw[:NOTE_NAME_MAX_CHARS] + ".md")


def _sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except FileNotFoundError:
        warn("core/tools/builtin.py:80 FileNotFoundError", err=repr(__import__("sys").exc_info()[1]))
        return ""
    except OSError:
        warn("core/tools/builtin.py:82 OSError", err=repr(__import__("sys").exc_info()[1]))
        return ""


def _run_async(make_coro: Callable[[], Any], *, timeout_s: float = DEFAULT_TIMEOUT_S) -> tuple[bool, str]:
    """在**独立线程**里跑一个协程（本进程可能已经在跑事件循环，不能 `asyncio.run`）。"""
    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["r"] = asyncio.run(make_coro())
        except BaseException as exc:  # noqa: BLE001 - 工具炸了只能是这一步失败
            warn("core/tools/builtin.py:93 BaseException", err=repr(exc))
            box["e"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        return (False, f"timeout: {timeout_s:g}s 内没返回")
    if "e" in box:
        exc = box["e"]
        return (False, f"{type(exc).__name__}: {exc}"[:200])
    return (True, str(box.get("r", "") or ""))


# --------------------------- 工具规格（登记七字段） ---------------------------


def read_specs() -> tuple[ToolSpec, ...]:
    """只读工具（默认开）。参数 >4 个的必须拆成两个工具（工具包 §7）。"""
    return (
        ToolSpec(
            name="web_search",
            purpose="要查网上的实时消息时用",
            family="web",
            effects=frozenset({Effect.READ}),
            args={"query": "str"},
            returns="标题 + 3 条要点 + URL + 首段原文（≤600 字，标注「摘要，非原文」）",
            perms=(),
            limits={"timeout_s": 8, "max_chars": 600, "query_max_chars": 120},
            idempotent=True,
            owner="core/tools/builtin.py",
        ),
        ToolSpec(
            name="fetch_url",
            purpose="给你一个网址，读它的正文",
            family="web",
            effects=frozenset({Effect.READ}),
            args={"url": "str"},
            returns="正文纯文本（≤4000 字，超了截断并标记）",
            perms=(),
            limits={"timeout_s": 8, "max_chars": 4000},
            idempotent=True,
            owner="core/tools/builtin.py",
        ),
        ToolSpec(
            name="gui_snapshot",
            purpose="读浏览器当前这一页的文字",
            family="gui",
            effects=frozenset({Effect.READ}),
            args={"target": "str"},
            returns="页面标题 + URL + 正文纯文本（≤4000 字，超了标 truncated）+ 状态摘要 digest",
            perms=(),
            limits={"timeout_s": 8, "max_chars": 4000},
            idempotent=True,
            owner="core/tools/builtin.py",
        ),
        ToolSpec(
            name="get_current_time",
            purpose="问现在几点、今天几号",
            family="system",
            effects=frozenset({Effect.READ}),
            args={},
            returns="ISO8601 本地时间",
            perms=(),
            limits={"timeout_s": 2, "max_chars": 60},
            idempotent=True,
            owner="core/tools/builtin.py",
        ),
    )


def write_specs() -> tuple[ToolSpec, ...]:
    """写本地工具（默认**不**开，由人 `enable()`）。

    只写 `data/workspace/` 下面——不碰账本、不碰配置、不碰别人的 App。
    每一次都要靠**状态差异**回执证明「真的变了」（`changed` 非空），
    没有变化就是 `no_change`，不许当成成功。
    """
    return (
        ToolSpec(
            name="write_note",
            purpose="把东西写进本机文件（可逆）",
            family="file",
            effects=frozenset({Effect.WRITE_LOCAL}),
            args={"content": "str"},
            returns="状态差异：{path, before, after}（before/after 是内容 sha256 前 16 位）",
            perms=("workspace",),
            limits={"timeout_s": 5, "content_max_chars": 8000},
            idempotent=False,
            owner="core/tools/builtin.py",
        ),
        ToolSpec(
            name="send_plan",
            purpose="要真办事（发消息/动手）时用：开一件事等主人批准",
            family="message",
            effects=frozenset({Effect.EXTERNAL_SEND}),
            args={"target": "str", "text": "str"},
            returns="开出来的那件事（停在等主人批准，一个字节都没发）",
            perms=("plan",),
            limits={"timeout_s": 5, "text_max_chars": 400},
            idempotent=True,
            owner="core/tools/builtin.py",
        ),
    )


def ui_specs() -> tuple[ToolSpec, ...]:
    """`ui_control` 类工具（**默认只登记不启用**）：动别人的界面要人显式开。

    为什么单独一组：`read` 默认可跑，`write_local` 由人 `enable()`，
    `ui_control` 则是「碰到别人的界面」——它连登记都要人先点头（`enable_ui_control`）。
    """
    return (
    ToolSpec(
        name="ui_state",
        family="ui",
        effects={Effect.READ},
        args={"action": "state", "target": "str", "what": "element"},
        returns="控件树 / 截图摘要",
        owner="core/tools/builtin.py",
        perms=("ui",),
        limits={"timeout_s": 8},
        idempotent=True,
        purpose="看一眼界面：窗口里有��么控件",
    ),
    ToolSpec(
        name="ui",
        window_title="Edge",
        family="ui",
        effects={Effect.UI_CONTROL},
        args={"action": "click", "target": "str", "what": "element", "text": "str"},
        returns="变没变 / 证据",
        owner="core/tools/builtin.py",
        perms=("ui",),
        limits={"timeout_s": 20},
        idempotent=False,
        purpose="动界面：点一下 / 打字 / 跳转",
    ),
    )


def all_specs() -> tuple[ToolSpec, ...]:
    return read_specs() + write_specs() + ui_specs()


def send_plan_handler(*, target: str = "", what: str = "", params: Mapping[str, Any] | None = None) -> ToolResult:
    """``send_plan``：把「往某处发一句话」开成**一件事**，等主人批准。

    为什么要有这个工具：她的清单里没有「发消息」时，她会老老实实回答「我没有这个途径」——
    真机连着几轮都是这句，而计划器同时又在后台开事，于是话与事自相矛盾。
    把它声明出来，是让「她能办」与「她手里有」这两套事实合一；
    她**依然没有直发的权力**：这里只 ingest + advance，停在 ``need_approval``。
    """
    target_value = " ".join(str(target or params.get("target") or "").split())[:80]
    body = " ".join(str(what or params.get("text") or "").split())[:400]
    if not target_value or not body:
        return ToolResult(status="blocked", content="不能发：target 和 text 都要有")
    try:
        from core.jobs import slots as _slots

        _miss = _slots.missing_slots("往" + target_value + "发一句话：" + body)
        if _miss:
            return ToolResult(status="ok", content="我还得知道：" + _slots.ask_for(_miss))
    except Exception as exc:  # noqa: BLE001 - 槽抽取坏了自己不拦路
        log.warning("send_plan_slots_failed error=" + type(exc).__name__)
    try:
        from core.jobs import plan_block as _pb

        # consume() 收的是**内层 plan**（不是 {"plan": ...} 外壳）——外壳是 ingest_plan 的约定。
        plan = {
            "goal": f"往{target_value}发一句话：{body}",
            "steps": [{"n": 1, "action": f"send:{target_value}|{body}"}],
        }
        job_id = _pb.consume(plan, chat_id=str(params.get("chat_id") or target_value))
    except Exception as exc:  # noqa: BLE001 - 开不出来就说开不出来，不许假称已办
        warn("core/tools/builtin.py:267 Exception", err=repr(exc))
        return ToolResult(status="failed", content=f"没能开成事：{exc}")
    if not job_id:
        return ToolResult(status="failed", content="没能开成事（台账满了或开关没开）")
    return ToolResult(
        status="ok",
        content=f"已开成一件（{job_id}），停在等主人批准；我一个字都没发。",
    )


# ------------------------------- handler（同步回调） -------------------------------


def _handler_from_base_tool(tool: Any, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> Callable[..., ToolResult]:
    """把 `agents/tools/base.py::BaseTool` 的异步实现包成同步 handler。

    参数怎么传：`what` 是工具的主参数（搜索词 / URL），`params` 里其余的按关键字传。
    """

    def run(*, target: str = "", what: str = "", params: Mapping[str, Any] | None = None) -> ToolResult:
        kwargs = dict(params or {})
        kwargs.pop("tool", None)
        kwargs.pop("turn_id", None)
        if what and not kwargs:
            params_decl = getattr(tool, "parameters", {}) or {}
            primary = next((p for p in ("query", "url", "text", "name") if p in params_decl), "")
            if primary:
                kwargs[primary] = what
        ok, out = _run_async(lambda: tool.execute(**kwargs), timeout_s=timeout_s)
        if not ok:
            status = "timeout" if out.startswith("timeout:") else "http_5xx"
            return ToolResult(status=status, content=out[:600], source=getattr(tool, "name", ""))
        return ToolResult(
            status="ok" if out.strip() else "no_result",
            content=out[:4000],
            source=getattr(tool, "name", ""),
        )

    return run


#: GUI 驱动的错误码 -> 工具包的封闭状态码（两套词汇必须能对上，否则下游只能靠猜）。
GUI_STATUS_MAP: dict[str, str] = {
    "blocked": "ui_blocked",        # 开关没开 / 端点不是本机：不是「读不到」，是「不许读」
    "unsupported": "unsupported_type",
    "not_connected": "not_connected",  # 端口没开 / 没有 ws 地址
    "no_target": "no_target",          # 连上了但没有这个页面
    "timeout": "timeout",
}


#: `gui_click` 的声明（`window_title` 是「点之前必须声明点的是哪个窗口」的凭据）。
CLICK_SPEC_NAME = "ui"
CLICK_EVIDENCE_DIR = "data/evidence"


def _click_spec(window_title: str = "") -> ToolSpec:
    """取 `gui_click` 的注册项；调用方给了窗口标题就换成它（换浏览器 = 换这一行，不是猜）。"""
    from dataclasses import replace

    for spec in ui_specs():
        if spec.name == CLICK_SPEC_NAME:
            return replace(spec, window_title=window_title or spec.window_title)
    raise RuntimeError("ui 规格没登记")


def _input_spec(window_title: str = "") -> ToolSpec:
    """`gui_input` 的规格副本：换浏览器 = 换这一行（与 `_click_spec` 同一做法）。"""
    spec = next(item for item in ui_specs() if item.name == "ui")
    if not window_title:
        return spec
    return replace(spec, window_title=window_title)


def ui_handler(*, action: str = "state", target: str = "", what: str = "", text: str = "", url: str = "", params: Mapping[str, Any] | None = None) -> ToolResult:
    """``ui``：界面动作的唯一出口。未知 action 直接 blocked，判据一条不删。"""
    merged = dict(params or {})
    act = str(action or merged.pop("action", "") or "").strip().lower()
    if not act:
        # 老调用点只给 selector / text：按参数推断动作，而不是一律当 state。
        if merged.get("text"):
            act = "type"
        elif merged.get("selector") or merged.get("what"):
            act = "click"
        else:
            act = "state"
    if act in ("state", "snapshot"):
        # 读界面走 ui_state（effects=read，不占授权）；动界面走 ui（ui_control）。
        act = "state" if act == "state" else "snapshot"
    elif act in ("click", "type", "navigate"):
        act = act
    table = {
        "state": gui_state_handler,
        "snapshot": gui_snapshot_handler,
        "click": gui_click_handler,
        "type": gui_input_handler,
        "navigate": gui_navigate_handler,
    }
    fn = table.get(act)
    if fn is None:
        return ToolResult(status="blocked", content="bad_param:action:" + act)
    for key, value in (("target", target), ("text", text), ("url", url)):
        if value:
            merged[key] = value
    what_value = what or str(merged.pop("what", "") or merged.pop("element", "") or "")
    rest = {k: v for k, v in merged.items() if k not in ("action", "what", "element")}
    return fn(target=str(rest.pop("target", "") or ""), what=what_value, params=rest)

def gui_input_handler(
    *,
    target: str = "",
    what: str = "",
    params: Mapping[str, Any] | None = None,
) -> ToolResult:
    """**在网页里打字**（`软件适配器.md` §13.19）。

    判据与点击不同：不是像素差，是**输入框的值变了没有**——打一个字只变十几个像素，
    按像素判会把「打成功」判成失败。前两条（前台 / 标题）照旧在动手之前判。
    唯一裁决者仍是 `can_ui_control()`：这里把 `value_changed` 递进去，让它替第三条。
    """
    payload = dict(params or {})
    selector = str(payload.get("selector") or "").strip()
    text = str(payload.get("text") or what or "")
    window_title = str(payload.get("window_title") or "").strip()
    expect = str(payload.get("expect") or "")
    page = str(payload.get("page") or "")
    try:
        max_chars = int(payload.get("max_chars") or 4000)
    except (TypeError, ValueError):
        warn("core/tools/builtin.py:395 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        max_chars = 4000

    from core.execution.gui.base import GuiError
    from core.execution.gui import backends

    spec = _input_spec(window_title)
    verdict: dict[str, bool] = {}

    def _gate(foreground_ok: bool, title_ok: bool) -> tuple[bool, str]:
        # 前两关先判；第三条（值有没有变）只能事后知道，由下面的 can_ui_control 复查。
        allowed, reason = can_ui_control(
            spec,
            foreground_ok=foreground_ok,
            title_ok=title_ok,
            pixels_changed=UI_MIN_PIXELS_CHANGED,
        )
        verdict["foreground"] = bool(foreground_ok)
        verdict["title"] = bool(title_ok)
        return (allowed, reason)

    try:
        out = backends.reader(str(payload.get("backend") or ""), verb="insert_text", default="cdp").insert_text(
            selector,
            text,
            target=page,
            window_title=spec.window_title,
            expect=expect,
            max_chars=max_chars,
            pre_gate=_gate,
        )
    except GuiError as exc:
        warn("core/tools/builtin.py:426 GuiError", err=repr(exc))
        return ToolResult(
            status=GUI_STATUS_MAP.get(exc.code, "unsupported_type"),
            content="",
            at=_now_iso(),
            reversible=True,
        )
    except Exception as exc:  # noqa: BLE001 - 驱动炸了不许打断回合
        log.warning("gui_input_failed", error=repr(exc))
        return ToolResult(status="unsupported_type", content="", at=_now_iso())

    allowed, reason = can_ui_control(
        spec,
        foreground_ok=bool(verdict.get("foreground")),
        title_ok=bool(verdict.get("title")),
        pixels_changed=0,
        value_changed=out.is_changed(),
    )
    body = (
        f"在 {out.selector} 里输入 {len(out.value_after)} 个字："
        f"前台={'过' if verdict.get('foreground') else '不过'}"
        f" / 标题={'过' if verdict.get('title') else '不过'}"
        f" / 值 {len(out.value_before)}→{len(out.value_after)} 字"
    )
    if out.changed:
        body += "；变化：" + "、".join(sorted(out.changed))
    else:
        body += "；输入框的值没有变化"
    if out.expect_found:
        body += "；目标已出现"
    status = "ok"
    if not allowed:
        # 两关（前台/标题）过了但值没变 = 做完了什么都没变 → `no_change`；
        # 两关本身没过才是 `ui_blocked`（连动手资格都没有）。
        if verdict.get("foreground") and verdict.get("title"):
            status = "no_change"
        else:
            status = reason or UI_BLOCKED
    return ToolResult(
        status=status,
        content=f"{out.title_after or '(无标题)'}\n{body}",
        source=out.url_after,
        at=out.at,
        reversible=True,
        state_before=out.state_before,
        state_after=out.state_after,
        changed=out.changed,
    )


def gui_click_handler(
    *,
    target: str = "",
    what: str = "",
    params: Mapping[str, Any] | None = None,
) -> ToolResult:
    """**真的点一下**（`软件适配器.md` §13.6）：三条判据同时成立才动，动完量像素差。

    effects 是 `ui_control`：默认不启用 + `confirm` 档授权 + 每回合一次，三道闸都要过。
    回执五样：状态差异、三条判据、像素差、证据截图路径、`expect_found`。
    """
    payload = dict(params or {})
    selector = str(payload.get("selector") or what or "").strip()
    window_title = str(payload.get("window_title") or "").strip()
    expect = str(payload.get("expect") or "")
    turn_id = str(payload.get("turn_id") or "")
    page = str(payload.get("page") or "")
    try:
        max_chars = int(payload.get("max_chars") or 4000)
    except (TypeError, ValueError):
        warn("core/tools/builtin.py:495 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        max_chars = 4000

    from core.execution.gui.base import GuiError
    from core.execution.gui import backends
    spec = _click_spec(window_title)
    try:
        out = backends.reader(str(payload.get("backend") or ""), verb="click", default="cdp").click(
            selector,
            target=page,
            window_title=spec.window_title,
            expect=expect,
            max_chars=max_chars,
            evidence_dir=CLICK_EVIDENCE_DIR,
            turn_id=turn_id,
            pre_gate=lambda fg, title: can_ui_control(
                spec,
                foreground_ok=fg,
                title_ok=title,
                pixels_changed=MIN_CHANGED_PIXELS,
            ),
        )
    except GuiError as exc:
        warn("core/tools/builtin.py:517 GuiError", err=repr(exc))
        return ToolResult(
            status=GUI_STATUS_MAP.get(exc.code, "unsupported_type"),
            content="",
            at=_now_iso(),
            reversible=True,
        )
    except Exception as exc:  # noqa: BLE001 - 驱动炸了不许打断回合
        log.warning("gui_click_failed", error=repr(exc))
        return ToolResult(status="unsupported_type", content="", at=_now_iso())

    allowed, reason = can_ui_control(
        spec,
        foreground_ok=out.foreground_ok,
        title_ok=out.title_ok,
        pixels_changed=out.pixels_changed,
    )
    criteria = out.criteria()
    body = (
        f"点 {out.selector}：前台={'过' if criteria['foreground'] else '不过'}"
        f" / 标题={'过' if criteria['title'] else '不过'}"
        f" / 像素差={out.pixels_changed}"
    )
    if out.changed:
        body += "；变化：" + "、".join(sorted(out.changed))
    else:
        body += "；界面没有变化"
    if out.evidence_before:
        body += f"；证据 前 {out.evidence_before}"
    if out.evidence:
        body += f" / 后 {out.evidence}"
    if out.expect_found:
        body += "；目标已出现"
    status = "ok"
    if not allowed:
        status = reason or "ui_blocked"
    elif not out.is_changed():
        status = "no_change"
    return ToolResult(
        status=status,
        content=f"{out.title_after or '(无标题)'}\n{body}",
        source=out.url_after,
        at=out.at,
        evidence=out.evidence,
        reversible=True,
        state_before=out.state_before,
        state_after=out.state_after,
        changed=out.changed,
    )


def gui_navigate_handler(
    *,
    target: str = "",
    what: str = "",
    params: Mapping[str, Any] | None = None,
) -> ToolResult:
    """**第一个写动作**：只跳转，不点不输（`软件适配器.md` §13.5）。

    effects 是 `ui_control`，所以它**默认不启用**：人要显式打开，并且每回合只许一次。
    返回的是状态差异式回执——写动作没有幂等键，只有「动手前后差在哪」能证明它真的动了。
    """
    payload = dict(params or {})
    url = str(payload.get("url") or what or target or "").strip()
    expect = str(payload.get("expect") or "")
    page = str(payload.get("page") or "")
    try:
        max_chars = int(payload.get("max_chars") or 4000)
    except (TypeError, ValueError):
        warn("core/tools/builtin.py:585 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        max_chars = 4000
    from core.execution.gui.base import GuiError
    from core.execution.gui import backends

    try:
        motion = backends.reader(str(payload.get("backend") or ""), verb="navigate", default="cdp").navigate(url, target=page, expect=expect, max_chars=max_chars)
    except GuiError as exc:
        warn("core/tools/builtin.py:592 GuiError", err=repr(exc))
        return ToolResult(
            status=GUI_STATUS_MAP.get(exc.code, "unsupported_type"),
            content="",
            at=_now_iso(),
            reversible=True,
        )
    except Exception as exc:  # noqa: BLE001 - 驱动炸了不许打断回合
        log.warning("gui_navigate_failed", error=repr(exc))
        return ToolResult(status="unsupported_type", content="", at=_now_iso())
    head = f"{motion.title_after or '(无标题)'} — {motion.url_after or '(无地址)'}"
    if motion.is_changed():
        body = "（已跳转；变化：" + "、".join(sorted(motion.changed)) + "）"
    else:
        body = "（地址与界面都没变）"
    if motion.expect_found:
        body += "；目标已出现"
    return ToolResult(
        status="ok",
        content=f"{head}\n{body}",
        source=motion.url_after,
        at=motion.at,
        evidence=motion.state_after,
        reversible=True,
        state_before=motion.state_before,
        state_after=motion.state_after,
        changed=motion.changed,
    )


def gui_snapshot_handler(
    *,
    target: str = "",
    what: str = "",
    params: Mapping[str, Any] | None = None,
) -> ToolResult:
    """读一屏 GUI（只读）：`observe` 的 GUI 版，一次点击都没有。

    `target` 空 = 第一个页面；纯数字 = 下标；其它 = URL / 标题子串。
    **开关没开时返回 `ui_blocked`**：不是「读不到」，是「不许读」，这两件事下游要能分开。
    """
    payload = dict(params or {})
    # `params["target"]` 优先：执行器塞进来的 `target` 是**会话 id**（给工具看的上下文），
    # 不是「读哪个窗口」——它必须能被动作里的那半句话覆盖，否则一律读到会话名。
    key = str(payload.get("target") or what or target or "").strip()
    try:
        max_chars = int(payload.get("max_chars") or 4000)
    except (TypeError, ValueError):
        warn("core/tools/builtin.py:639 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        max_chars = 4000
    from core.execution.gui.base import GuiError
    from core.execution.gui import backends

    try:
        snap = backends.reader(str(payload.get("backend") or ""), verb="snapshot", default="cdp").snapshot(key, max_chars=max_chars)
    except GuiError as exc:
        warn("core/tools/builtin.py:646 GuiError", err=repr(exc))
        return ToolResult(
            status=GUI_STATUS_MAP.get(exc.code, "unsupported_type"),
            content="",
            at=_now_iso(),
            reversible=True,
        )
    except Exception as exc:  # noqa: BLE001 - 驱动炸了不许打断回合
        log.warning("gui_snapshot_failed", error=repr(exc))
        return ToolResult(status="unsupported_type", content="", at=_now_iso())
    head = f"{snap.title or '(无标题)'} — {snap.url or '(无地址)'}"
    if snap.is_empty():
        # 读到页面了，但正文是空的：这是「查了没有」，不是「查不了」。
        return ToolResult(status="no_result", content=head, source=snap.url, at=snap.taken_at)
    return ToolResult(
        status="ok",
        content=f"{head}\n{snap.text}",
        source=snap.url,
        at=snap.taken_at,
        truncated=snap.truncated,
        reversible=True,
    )


def gui_state_handler(
    *,
    target: str = "",
    what: str = "",
    params: Mapping[str, Any] | None = None,
) -> ToolResult:
    """读一屏**原生窗口的控件树**（只读）：OCR 只能看到字，控件树还能看到「哪里能点」。

    与 `gui_snapshot` 的分工：那个读网页（CDP / DOM），这个读桌面应用（UIA / 控件树）。
    `target` 空 = 前台窗口；否则按标题或类名子串找，**只认唯一命中**——两个同名窗口
    就返 `no_target`，宁可不读，也不给模型一份不属于它的屏。
    """
    payload = dict(params or {})
    # `params["target"]` 优先：执行器塞进来的 `target` 是**会话 id**（给工具看的上下文），
    # 不是「读哪个窗口」——它必须能被动作里的那半句话覆盖，否则一律读到会话名。
    key = str(payload.get("target") or what or target or "").strip()
    try:
        max_chars = int(payload.get("max_chars") or 4000)
    except (TypeError, ValueError):
        warn("core/tools/builtin.py:688 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        max_chars = 4000
    from core.execution.gui.base import GuiError
    from core.execution.gui import backends

    try:
        snap = backends.reader(str(payload.get("backend") or ""), verb="state", default="uia").snapshot(key, max_chars=max_chars)
    except GuiError as exc:
        warn("core/tools/builtin.py:695 GuiError", err=repr(exc))
        return ToolResult(
            status=GUI_STATUS_MAP.get(exc.code, "unsupported_type"),
            content="",
            at=_now_iso(),
            reversible=True,
        )
    except Exception as exc:  # noqa: BLE001 - 驱动炸了不许打断回合
        log.warning("gui_state_failed", error=repr(exc))
        return ToolResult(status="unsupported_type", content="", at=_now_iso())
    if snap.is_empty():
        return ToolResult(status="no_result", content=snap.title, source=f"uia:{snap.title}", at=snap.taken_at)
    return ToolResult(
        status="ok",
        content=snap.text,
        source=f"uia:{snap.title}",
        at=snap.taken_at,
        truncated=snap.truncated,
        reversible=True,
    )


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat(sep=" ", timespec="seconds")


def write_note_handler(*, target: str = "", what: str = "", params: Mapping[str, Any] | None = None) -> ToolResult:
    """写一篇笔记，并**用状态差异证明它真的变了**。

    `before` / `after` 是内容 sha256 前 16 位：两者相同 = 什么都没变 =
    `changed` 为空 = 上层必须判 `no_change`（不许当成「写好了」）。
    """
    payload = dict(params or {})
    text = str(payload.get("content", "") or "")[:8000]
    path = note_path(what)
    before = _sha(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not atomic_write_text(path, text if text.endswith("\n") else text + "\n"):
            raise OSError("写不进 %s" % path)
    except OSError as exc:
        warn("core/tools/builtin.py:741 OSError", err=repr(exc))
        return ToolResult(status="http_5xx", content=f"写不进：{exc}"[:200], source="write_note")
    after = _sha(path)
    changed = {}
    if before != after:
        changed = {
            "path": str(path.relative_to(_repo_root())).replace("\\", "/"),
            "before": before,
            "after": after,
        }
    return ToolResult(
        status="ok",
        content=(f"已写 {changed['path']}（{before or '新建'} -> {after}）" if changed else "内容未变"),
        source="write_note",
        state_before=before,
        state_after=after,
        changed=changed,
    )


def get_current_time_handler(
    *, target: str = "", what: str = "", params: Mapping[str, Any] | None = None
) -> ToolResult:
    from datetime import datetime

    return ToolResult(
        status="ok", content=datetime.now().astimezone().isoformat(timespec="seconds"), source="clock"
    )


# ------------------------------- 组装 -------------------------------


def build_registry(
    base_tools: Mapping[str, Any] | None = None,
    *,
    enable_writes: bool = False,
    enable_reads: bool = True,
    enable_ui_control: bool = False,
) -> ToolRegistry:
    """装出一个可用的注册表。`base_tools` 是旧实现（按名字给 `BaseTool` 实例）。

    * `read` 工具默认 `enable()`——与 `DEFAULT_EFFECT_MODE[READ] == "enabled"` 同一口径；
    * `write_local` 工具默认只登记不启用，`enable_writes=True` 才开；
    * `ui_control` 工具（现在只有 `gui_navigate`）**默认只登记不启用**：
      动别人的界面要人显式开（`enable_ui_control=True`），且每回合只许一次；
    * 没有任何 `external_send` 规格能登记进来（`registry.enable()` 也会拒）。
    """
    registry = ToolRegistry()
    tools = dict(base_tools or {})

    for spec in all_specs():
        ok, reason = registry.register(spec)
        if not ok:
            log.warning("tool_spec_rejected", name=spec.name, reason=reason)
            continue
        if spec.name in ("ui", "ui_state"):
            registry.bind(spec.name, ui_handler)
            # 读界面是只读、且执行时仍由 GUI_CHANNEL 判活：登记即可调。
            if spec.effects == frozenset({Effect.READ}):
                registry.enable(spec.name)
        elif spec.name == "gui_click":
            registry.bind(spec.name, gui_click_handler)
        elif spec.name == "send_plan":
            registry.bind(spec.name, send_plan_handler)
        elif spec.name == "gui_snapshot":
            registry.bind(spec.name, gui_snapshot_handler)
        elif spec.name == "write_note":
            registry.bind(spec.name, write_note_handler)
        elif spec.name == "get_current_time":
            registry.bind(spec.name, get_current_time_handler)
        elif spec.name in tools:
            limits = dict(spec.limits or {})
            registry.bind(
                spec.name,
                _handler_from_base_tool(
                    tools[spec.name], timeout_s=float(limits.get("timeout_s", DEFAULT_TIMEOUT_S))
                ),
            )

    if enable_reads:
        for spec in read_specs():
            registry.enable(spec.name)
    if enable_writes:
        for spec in write_specs():
            registry.enable(spec.name)
    if enable_ui_control:
        for spec in ui_specs():
            registry.enable(spec.name)
    # M3：把「模型侧工具的声明」汇入同一份目录（只声明，不实现）
    import logging as _logging

    try:
        from core.tools.declared import declared_specs as _declared_specs

        _known = {_s.name for _s in registry.specs()}
        for _spec in _declared_specs():
            if _spec.name not in _known:
                _ok, _why = registry.register(_spec)
                if not _ok:
                    _logging.getLogger(__name__).info(
                        "declared_spec_rejected name=%s reason=%s", _spec.name, _why)
    except Exception as _declared_exc:  # 声明坏了不许拖垮目录
        _logging.getLogger(__name__).info("declared_specs_failed error=%s", _declared_exc)
    return registry


def bootstrap(
    base_tools: Mapping[str, Any] | None = None,
    *,
    enable_writes: bool = False,
    enable_ui_control: bool = False,
) -> ToolRegistry:
    """启动时调一次：装表 -> 绑进执行器（`core/jobs/executor.py::set_default_registry`）。"""
    from core.jobs.executor import set_default_registry

    registry = build_registry(
        base_tools, enable_writes=enable_writes, enable_ui_control=enable_ui_control
    )
    # 声明式工具（`configs/tools/*.yaml`）：off 时一个文件都不读（默认），
    # shadow 只登记不启用，on 才把文件里写了 `enabled: true` 的那些启用。
    try:
        from core import features as _features
        from core.tools.declarative import bind_declared as _bind_declared

        _decl_mode = _features.mode(_features.DECLARED_TOOLS, _features.MODE_OFF)
        if _decl_mode != _features.MODE_OFF:
            _names = _bind_declared(registry, enable=(_decl_mode == _features.MODE_ON))
            log.info("declared_tools_bound", mode=_decl_mode, count=len(_names))
    except Exception:  # noqa: BLE001 - 声明式工具装不上不许拖垮启动
        log.warning("declared_tools_bind_failed")
    # MCP 工具源：off 一台都不连；shadow 连上列工具但不启用；on 才启用（工具仍需人 enable）。
    try:
        from core import features as _features2
        from core.execution.mcp import bind_servers as _bind_servers

        _mcp_mode = _features2.mode(_features2.MCP_TOOLS, _features2.MODE_OFF)
        if _mcp_mode != _features2.MODE_OFF:
            _mcp_names = _bind_servers(registry, enable=(_mcp_mode == _features2.MODE_ON))
            log.info("mcp_tools_bound", mode=_mcp_mode, count=len(_mcp_names))
    except Exception:  # noqa: BLE001 - 一台 MCP 服务器坏掉不许拖垮启动
        log.warning("mcp_bind_failed")
    set_default_registry(registry)
    log.info(
        "tool_registry_bound",
        specs=len(registry.specs()),
        writes=enable_writes,
        ui=enable_ui_control,
    )
    return registry
