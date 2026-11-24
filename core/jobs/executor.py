"""U-10 第三刀：只读执行器——把 `Step.action` 接到工具包的 `observe`。

`core/jobs/runner.py::advance()` 要一个 `execute(step)` 回调；这一层就是那个回调的**只读**实现。
它只做四件事：判定 effect、解析工具名、调一次 ``observe``、把结果压成 ``{"ok": bool, "detail": str}``。

纪律（`docs/decisions/工具包.md` §2 / §7、`docs/decisions/办_在手的事.md` §8）：

* ``effects != "read"`` 的步**根本不进 registry**（写操作只能走批准 → 出口，不许从这里溜过去）；
* 预算按**这件事**数：``turn_id`` 传 `job_id`，一次失败也算一次（registry 自己记）；
* 「没查到」（``no_result``）与「办到了」（``ok``）分开：前者不算这一步成功；
* 工具返回进台账的正文有上限，超了截断并标记——台账是 append-only，塞不进也删不掉；
* registry 抛异常一律变成这一步失败，**不许冒到 `advance()`**（否则一个工具炸掉会让整件事丢掉状态）。

本模块不 import ``core.speech`` / ``core.routing`` / ``agents`` / ``scheduler`` / ``web``
（`scripts/check_layering.py` 规则 11：办与工具都不许自己开口说话）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import structlog
from core.infra.envflags import text as _text

log = structlog.get_logger(__name__)

#: 只有这一种 effect 允许在执行器里跑（其余全部交给批准链）。
READ_EFFECT: str = "read"

#: 只动我们自己那一侧（写本地文件/便签/草稿）。**仍然要批准**，只是不需要出口。
WRITE_EFFECT: str = "write_local"

#: 动别人的界面：**要人逐次批准**（`confirm` 档）+ 显式 `allow_ui=True` 才接。
#: 三判据（前台 / 标题 / ≥100 像素）在工具包 `can_ui_control()`，证据截图由驱动落盘。
UI_EFFECT: str = "ui_control"

#: 动外部世界（发消息、发邮件、下单）：执行器**永远**不接，只走出口注册表（R3）。
EXTERNAL_EFFECT: str = "external_send"

#: 写执行器肯接的 effect（订阅集，别的全部 `blocked:non_writable`）。
WRITABLE_EFFECTS: tuple[str, ...] = (READ_EFFECT, WRITE_EFFECT)

#: 对外发送没接上「谁去发」时的停因（不是拒绝，是「路上没人」）。
NO_SENDER_REASON: str = "no_sender"

#: 对外发送这一步的载荷分隔符：``send:张三|明天见`` → 收件人 + 正文；
#: 没写 ``|`` 时收件人取这一步的 ``target``（默认就是这个会话）。
EXTERNAL_SEP: str = "|"

#: 「更正一条已发的消息」这种动作的前缀：``correct:wechat#89|我说错了，是周四``。
#: 它**仍然是** `external_send`（要人批准），只是落的不是新会话而是"改口"。
CORRECT_PREFIX: str = "correct:"

#: 状态采不到（App 不暴露可比对的字段）时的停因：**不许写**（见 `办_在手的事.md` §17.2）。
NO_STATE_REASON: str = "no_state"

#: 状态采到了、但前后一模一样：什么都没发生，也不算办成。
NO_CHANGE_REASON: str = "no_change"

#: 一步的结果进台账的正文上限（工具包.md §7：工具返回进 prompt 合计 ≤600 字）。
RESULT_MAX_CHARS: int = 600

#: `Step.action` 里的工具名与参数的分隔符：``web_search:杭州机票`` → ``("web_search", "杭州机票")``。
ACTION_SEP: str = ":"


#: 进程内已绑定的工具注册表。没有它，`on` 档不注入执行器（宁可不跑，也不许空转烧预算）。
_registry: Any = None


def set_default_registry(registry: Any) -> None:
    """把工具注册表绑进来（阶段 4 的 `configs/tools/*.yaml` 会在启动时调它）。"""
    global _registry
    _registry = registry


def default_registry() -> Any:
    """当前绑定的注册表；没有就返回 ``None``（执行器不在回合里注入）。"""
    return _registry


def parse_action(action: str) -> tuple[str, str]:
    """``"web_search:杭州机票"`` → ``("web_search", "杭州机票")``；无冒号则 what 为空。"""
    text = str(action or "").strip()
    if ACTION_SEP not in text:
        return (text, "")
    tool, _, what = text.partition(ACTION_SEP)
    return (tool.strip(), what.strip())


#: 动作里那半截 `what` 该落进哪个参数：按工具声明的 `args` 猜，猜不到就不塞。
_PRIMARY_ARGS: tuple[str, ...] = ("selector", "url", "query", "content", "name", "text", "target")


def _primary_arg(registry: Any, tool: str) -> str:
    """`gui_click` → ``selector``，`gui_navigate` → ``url``，`write_note` → ``content``，
    `gui_state` → ``target``（注意 ``target`` 排在最后：执行器的 ``target`` 是**会话**，
    只有当工具真的声明了 ``target`` 参数时才让动作里的那半句覆盖它）。

    为什么需要它：`Step.action` 只有 ``工具名:半句话``，而每个工具的「主参数」名字不一样；
    不猜这一层，`gui_click` 拿到的 selector 会是工具名自己。
    """
    getter = getattr(registry, "get", None)
    spec = getter(tool) if callable(getter) else None
    args = dict(getattr(spec, "args", {}) or {})
    for name in _PRIMARY_ARGS:
        if name in args:
            return name
    return ""


def _act_params(registry: Any, tool: str, what: str, turn_id: str) -> dict[str, Any]:
    """`act` 的载荷：`what` 原样留着（老工具读它），同时按声明的参数名再放一份。"""
    payload: dict[str, Any] = {"tool": tool, "turn_id": turn_id, "what": what}
    primary = _primary_arg(registry, tool)
    if primary and primary != "what":
        payload[primary] = what
    return payload


def _clip(text: str, limit: int = RESULT_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def read_only_executor(
    registry: Any = None,
    *,
    turn_id: str = "",
    target: str = "",
    max_result_chars: int = RESULT_MAX_CHARS,
) -> Callable[[Any], dict[str, Any]]:
    """造一个 ``execute(step)``：只放行 ``effects == "read"``，只调 ``observe``。"""

    def run(step: Any) -> dict[str, Any]:
        effects = str(getattr(step, "effects", "") or "")
        if effects != READ_EFFECT:
            return {"ok": False, "detail": f"blocked:non_read:{effects or 'unknown'}"}
        if registry is None:
            return {"ok": False, "detail": "no_tool:registry 未绑定"}
        tool, what = parse_action(str(getattr(step, "action", "") or ""))
        if not tool:
            return {"ok": False, "detail": "no_tool:动作为空"}
        params: Mapping[str, Any] = {"tool": tool, "turn_id": turn_id}
        try:
            result = registry.observe(target=target, what=what, params=params)
        except Exception as exc:  # noqa: BLE001 - 工具炸了只能是这一步失败
            log.warning("job_step_failed", tool=tool, turn_id=turn_id, error=repr(exc))
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"[:200]}
        status = str(getattr(result, "status", "") or "")
        content = str(getattr(result, "content", "") or "")
        if status != "ok":
            return {"ok": False, "detail": f"{status}:{content}"[:200]}
        detail = _clip(content, max_result_chars)
        if getattr(result, "truncated", False) and not detail.endswith("…"):
            detail += "…"
        return {"ok": True, "detail": detail}

    return run


#: 正文开头的 ``@名字`` 是「要通知谁」，不是正文的一部分（真的 @ 是输入框里的 chip）。
MENTION_PREFIX: str = "@"


def split_mention(text: str) -> tuple[str, str]:
    """``"@小奇 在吗"`` → ``("小奇", "在吗")``；没有前导 @ 就 ``("", 原文)``。

    为什么剥掉：真 @ 由 ``fill_message(mention=…)`` 插成 chip，正文里再留一份
    ``@小奇`` 会变成「chip + 文字」，看起来像 @ 了两次。
    """
    body = str(text or "").strip()
    if not body.startswith(MENTION_PREFIX):
        return ("", body)
    name, sep, tail = body[len(MENTION_PREFIX):].partition(" ")
    name = name.strip()
    if not name:
        return ("", body)
    # 没有分隔符就**不是**「@谁 + 正文」。真机 2026-10-21：
    # `@Elaina，命令已执行` 被整串当成名字，正文变空 → `空文本不发送`。
    # 吞掉正文比多留一个 @ 严重得多。
    if not sep:
        return ("", body)
    return (name, tail.strip())


def _split_external(what: str, target: str) -> tuple[str, str]:
    """把 ``张三|明天见`` 拆成收件人和正文；没写 ``|`` 就发给这个会话。"""
    body = str(what or "").strip()
    if EXTERNAL_SEP in body:
        to, text = body.split(EXTERNAL_SEP, 1)
        return (to.strip() or target), text.strip()
    return target, body


def _run_external(
    step: Any,
    *,
    allow_external: bool,
    external: Callable[..., Any] | None,
    target: str,
    turn_id: str,
) -> dict[str, Any]:
    """对外发送：唯一实现按回车的地方在 ``wechat_send``，这里只认它给的账。"""
    if not allow_external:
        return {"ok": False, "detail": f"blocked:non_writable:{EXTERNAL_EFFECT}"}
    if external is None:
        return {
            "ok": False,
            "detail": f"{NO_SENDER_REASON}:没有能真的发出去的人（WECHAT_SEND=on 且已接发送器才注入）",
        }
    _tool, what = parse_action(str(getattr(step, "action", "") or ""))
    to, text = _split_external(what, target)
    mention, text = split_mention(text)
    if not text and not mention:
        return {"ok": False, "detail": "unsupported:空正文不发送"}
    # 值校验：只问「这个值能不能落地」，不问「参数齐不齐」（没有必填表）。
    # 不可逆动作的兜底是人看具体值（policy 的 confirm 档），这里只挡明显假的地址。
    from core.tools import validate as _validate

    _ok, _to, _why = _validate.check("chat", to)
    if not _ok:
        return {"ok": False, "detail": f"bad_param:chat:{_why}:{str(to)[:40]}"}
    to = str(_to)
    _ok, _text, _why = _validate.check("text", text or mention)
    if not _ok:
        return {"ok": False, "detail": f"bad_param:text:{_why}"}
    text = str(_text) if text else text
    try:
        if mention:
            result = external(to, text, turn_id=turn_id, mention=mention)
        else:
            result = external(to, text, turn_id=turn_id)
    except Exception as exc:  # noqa: BLE001 - 发不出去只能是这一步失败
        log.warning("job_external_failed", to=to, turn_id=turn_id, error=repr(exc))
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"[:200]}

    if isinstance(result, Mapping):
        raw: Mapping[str, Any] = result
    else:
        raw = {"status": str(getattr(result, "status", "") or ""), "detail": str(getattr(result, "detail", "") or "")}
    status = str(raw.get("status") or "")
    detail = str(raw.get("detail") or "")
    local_id = int(raw.get("local_id") or 0)
    server_id = int(raw.get("server_id") or 0)
    if status not in ("sent", "unconfirmed"):
        # 失败就是失败：不重试（重复发送率必须为 0），停在这一步交给人。
        return {"ok": False, "detail": f"{status or 'failed'}:{detail}"[:200]}
    changed: dict[str, Any] = {}
    if local_id:
        changed["local_id"] = local_id
    if server_id:
        changed["server_id"] = server_id
    return {
        "ok": True,
        "detail": f"{status}:{detail}"[:200],
        "state_before": "",
        "state_after": f"wechat#{local_id}" if local_id else "",
        "changed": changed or {"status": status},
    }


def _split_correction(rest: str) -> tuple[str, str]:
    """``wechat#89|我说错了，是周四`` → ``(ref, fix)``；没有 ``|`` 就当"没说清在更正哪条"。"""
    body = str(rest or "").strip()
    if EXTERNAL_SEP not in body:
        return (body, "")
    ref, _, fix = body.partition(EXTERNAL_SEP)
    return (ref.strip(), fix.strip())


def _run_correction(
    rest: str,
    *,
    allow_external: bool,
    correct: Callable[..., Any] | None,
    target: str,
    turn_id: str,
) -> dict[str, Any]:
    """更正：与对外发送同一道闸（`external_send` 永远要人批），只是动作是"改口"。"""
    if not allow_external:
        return {"ok": False, "detail": f"blocked:non_writable:{EXTERNAL_EFFECT}"}
    if correct is None:
        return {
            "ok": False,
            "detail": f"{NO_SENDER_REASON}:没有能更正的人（CORRECTION=on 才注入）",
        }
    ref, fix = _split_correction(rest)
    if not ref or not fix:
        return {"ok": False, "detail": "unsupported:更正要写成 correct:<回执号>|<改口>"}
    try:
        raw = correct(ref, fix, turn_id=turn_id)
    except Exception as exc:  # noqa: BLE001 - 发不出去只能是这一步失败
        log.warning("job_correction_failed", ref=ref, turn_id=turn_id, error=repr(exc))
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"[:200]}
    payload: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    ok = bool(payload.get("ok"))
    status = str(payload.get("status") or "")
    if not ok:
        # **不重试**：更正发两次就是两次打脸。
        return {"ok": False, "detail": f"{status or 'failed'}:{payload.get('reason') or ''}"[:200]}
    return {
        "ok": True,
        "detail": f"{status}:更正 {ref}"[:200],
        "state_before": "",
        "state_after": f"corrected:{ref}",
        "changed": {"ref": ref, "mode": str(payload.get("mode") or ""), "status": status},
    }


def write_executor(
    registry: Any = None,
    *,
    turn_id: str = "",
    target: str = "",
    shadow: bool = True,
    allow_ui: bool = False,
    allow_external: bool = False,
    external: Callable[..., Any] | None = None,
    correct: Callable[..., Any] | None = None,
    max_result_chars: int = RESULT_MAX_CHARS,
) -> Callable[[Any], dict[str, Any]]:
    """造一个 ``execute(step)``：放行 ``read`` 与 ``write_local``，其余一律不接。

    与 `read_only_executor()` 的差别只有两点：写操作走 ``registry.act()``（不是 observe）；
    ``shadow=True``（默认）时**只判定不执行**，返回 ``{"shadow": True}``，
    让 `advance()` 以停因 ``shadow`` 停在安全点上——不推进步号、不算失败、不算办完。

    为什么默认影子：写操作没有幂等键（App 点重了就是两单），
    所以在「状态差异式回执」与三级授权落地之前，这条路只许量、不许做。
    `ui_control` 要**两把钥匙同时转**：调用方显式给 ``allow_ui=True``（谁放行谁负责），
    且这一步已经被 `advance()` 的 `_gate()` 放进来过（人逐次批准的那一行 `Approval`）。
    执行器自己不去查批准——它只认「调用方说了允许」，这样「谁能放行」只有一处
    （`core/jobs/policy.py` 的三档 + `runner._gate()` 的顺序）。

    `external_send` 默认也不接（``allow_external=False``）；只有调用方显式给
    ``allow_external=True`` **且**递进一个 ``external(to, text, *, turn_id)`` 时才走那条路，
    而那个回调的实现必须落在 ``core/execution/gui/wechat_send.py``（按回车全仓只有它能按）。
    也就是说：这条路不是绕过 R3，是把 R3 的出口做成一个可注入的回调。
    """

    def run(step: Any) -> dict[str, Any]:
        effects = str(getattr(step, "effects", "") or "")
        tool_head, what_head = parse_action(str(getattr(step, "action", "") or ""))
        if effects == EXTERNAL_EFFECT and tool_head == "correct":
            return _run_correction(
                what_head,
                allow_external=allow_external,
                correct=correct,
                target=target,
                turn_id=turn_id,
            )
        if effects == EXTERNAL_EFFECT:
            return _run_external(
                step,
                allow_external=allow_external,
                external=external,
                target=target,
                turn_id=turn_id,
            )
        if effects == UI_EFFECT and not allow_ui:
            return {"ok": False, "detail": f"blocked:non_writable:{UI_EFFECT}"}
        if effects not in WRITABLE_EFFECTS and effects != UI_EFFECT:
            return {"ok": False, "detail": f"blocked:non_writable:{effects or 'unknown'}"}
        if registry is None:
            return {"ok": False, "detail": "no_tool:registry 未绑定"}
        tool, what = parse_action(str(getattr(step, "action", "") or ""))
        if not tool:
            return {"ok": False, "detail": "no_tool:动作为空"}
        params: Mapping[str, Any] = _act_params(registry, tool, what, turn_id)
        if effects != READ_EFFECT and shadow:
            return {
                "ok": False,
                "shadow": True,
                "detail": f"blocked:shadow:{tool}"[:200],
            }
        try:
            if effects in (WRITE_EFFECT, UI_EFFECT):
                result = registry.act(target=target, action=tool, params=params)
            else:
                result = registry.observe(target=target, what=what, params=params)
        except Exception as exc:  # noqa: BLE001 - 工具炸了只能是这一步失败
            log.warning("job_step_failed", tool=tool, turn_id=turn_id, error=repr(exc))
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"[:200]}
        status = str(getattr(result, "status", "") or "")
        content = str(getattr(result, "content", "") or "")
        if status != "ok":
            return {"ok": False, "detail": f"{status}:{content}"[:200]}
        before = str(getattr(result, "state_before", "") or "")
        after = str(getattr(result, "state_after", "") or "")
        changed = dict(getattr(result, "changed", {}) or {})
        if effects in (WRITE_EFFECT, UI_EFFECT) and not changed:
            # 状态差异式回执：写完跟写前一模一样，不是「办成了」，是「什么都没发生」；
            # 连状态都采不到（前后都是空）更不许当办成——宁可停，也不许拿「点了一下没报错」当成功。
            reason = NO_CHANGE_REASON if (before or after) else NO_STATE_REASON
            advice = "状态没变，不当成写成功" if before or after else "采不到状态，不许写"
            return {
                "ok": False,
                "detail": f"{reason}:{advice}",
                "state_before": before,
                "state_after": after,
            }
        detail = _clip(content, max_result_chars)
        if getattr(result, "truncated", False) and not detail.endswith("…"):
            detail += "…"
        return {
            "ok": True,
            "detail": detail,
            "state_before": before,
            "state_after": after,
            "changed": changed,
        }

    return run
