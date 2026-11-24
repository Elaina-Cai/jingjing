"""参数校验器：只判「这个值能不能落地」，不问「参数齐不齐」。

为什么没有「必填表」：任何有限的必填表对无限的说法都不完备，而不完备的必填表
会把「漏了」变成「猜着办」。所以这里只做两件事——

1. 认得的类型：给出**能不能落地**的判决（会话唯一命中 / 时间可解析 / 域在允许表 …）；
2. 不认得的类型：**不拦**，只记一行 ``param_unknown_type``（将来它可能长成新校验器）。

真正的兜底是「不可逆动作必须人看具体值」（`core/jobs/policy.py` 的 confirm 档），
本模块只负责把「值假得像真的」这一类错挡在执行之前。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable
from safelog import warn

log = logging.getLogger(__name__)

MAX_TEXT_CHARS = 4000
MAX_QUERY_CHARS = 200

Validator = Callable[[Any], tuple[bool, Any, str]]


def _as_str(value: Any) -> str:
    return str(value or "").strip()


def v_text(value: Any) -> tuple[bool, Any, str]:
    text = _as_str(value)
    if not text:
        return False, "", "empty"
    if len(text) > MAX_TEXT_CHARS:
        return False, text, "too_long"
    return True, text, ""


def v_query(value: Any) -> tuple[bool, Any, str]:
    text = _as_str(value)
    if not text:
        return False, "", "empty"
    if len(text) > MAX_QUERY_CHARS:
        return False, text[:MAX_QUERY_CHARS], "too_long"
    return True, text, ""


def v_chat(value: Any) -> tuple[bool, Any, str]:
    """会话地址：长得像 id（wxid_/xxx@chatroom/数字/user:…）或空。

    真正的「唯一命中」要靠联系人库，由 ``core.execution.gui.wechat.display_name_for``
    与 ``_resolve_chat`` 在发送前做——这里只挡明显不是地址的值（防止把整句话当目标）。
    """
    text = _as_str(value)
    if not text:
        return False, "", "empty"
    if len(text) > 120:
        return False, text, "too_long"
    if re.search(r"[\s]{2,}", text) or "\n" in text:
        return False, text, "looks_like_sentence"
    return True, text, ""


_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$|^\d{4}-\d{2}-\d{2}")


def v_time(value: Any) -> tuple[bool, Any, str]:
    text = _as_str(value)
    if not text:
        return False, "", "empty"
    if _TIME_RE.search(text):
        return True, text, ""
    return False, text, "not_absolute"


def v_url(value: Any) -> tuple[bool, Any, str]:
    text = _as_str(value)
    if not text.lower().startswith(("http://", "https://")):
        return False, text, "not_http"
    return True, text, ""


def v_amount(value: Any) -> tuple[bool, Any, str]:
    try:
        amount = float(_as_str(value))
    except (TypeError, ValueError):
        warn("core/tools/validate.py:87 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return False, value, "not_number"
    if amount <= 0:
        return False, amount, "not_positive"
    if amount > 1000:
        return False, amount, "over_limit"
    return True, amount, ""


def v_element(value: Any) -> tuple[bool, Any, str]:
    text = _as_str(value)
    if not text:
        return False, "", "empty"
    return True, text, ""


def v_path(value: Any) -> tuple[bool, Any, str]:
    text = _as_str(value)
    if not text:
        return False, "", "empty"
    if ".." in text:
        return False, text, "traversal"
    return True, text, ""


VALIDATORS: dict[str, Validator] = {
    "chat": v_chat,
    "text": v_text,
    "query": v_query,
    "time": v_time,
    "url": v_url,
    "amount": v_amount,
    "element": v_element,
    "path": v_path,
}


def check(kind: str, value: Any) -> tuple[bool, Any, str]:
    """按类型判一个值。不认得的类型一律放行（只记日志）。"""
    fn = VALIDATORS.get(str(kind or ""))
    if fn is None:
        log.info(f"param_unknown_type kind={kind}")
        return True, value, ""
    return fn(value)


def validate_params(params: Any) -> dict[str, Any]:
    """批量校验：``{"ok": bool, "bad": [...], "unknown": [...], "resolved": {...}}``。"""
    data = params if isinstance(params, dict) else {}
    bad: list[dict[str, str]] = []
    unknown: list[str] = []
    resolved: dict[str, Any] = {}
    for key, value in data.items():
        if key not in VALIDATORS:
            unknown.append(str(key))
            log.info(f"param_unknown_type kind={key}")
            resolved[key] = value
            continue
        ok, out, reason = VALIDATORS[key](value)
        if ok:
            resolved[key] = out
        else:
            bad.append({"name": str(key), "reason": reason})
    return {"ok": not bad, "bad": bad, "unknown": unknown, "resolved": resolved}


def describe_bad(bad: list[dict[str, str]]) -> str:
    """把不合格的参数翻成一句人话（给「问」和「交人」两处共用）。"""
    names = "、".join(f"{item.get('name')}（{item.get('reason')}）" for item in bad)
    return f"这几个值没法用：{names}"
