# -*- coding: utf-8 -*-
"""三级授权：一件事往前推，什么时候可以自己动手、什么时候必须问人。

出处：`docs/decisions/办_在手的事.md` §8（三条硬红线）与 §5 槽 ②（`policy`）。

三档
----
| 档 | 谁能给 | 覆盖什么 | 凭据 |
|---|---|---|---|
| `auto` | 设计本身 | `read`（看一眼、查一下） | 无需凭据 |
| `grant` | **人**，一次授权长期有效 | `write_local` 这类「只动我们这一侧」的事 | `Policy.approved_by` 非空 + 未过期 + 在 scope 内 |
| `confirm` | 人，逐次 | `ui_control`（动别人的界面）、`external_send`（对外说话，只走出口） | 一行 `Approval(job_id, step_n, action_hash)` |

三条纪律
--------
1. **档位由 `effects` 决定，不由工具名决定**：换个名字不许绕开档位（改名叫 `read` 也还是 `read`）。
2. **`grant` 必须有凭据**：`Policy.approved_by` 空 = 没人批过 = 退化成 `confirm`。
   这是「默认拒绝」：没批过的长期授权不存在。
3. **档位只决定「要不要问」，不决定「能不能做」**：`external_send` 即使拿到批准也不许从
   执行器走，它只走出口注册表（R3）。

本模块不 import `core.speech` / `core.routing` / `agents` / `scheduler` / `web`（规则 11）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from safelog import warn

#: 三档（不许出现第四档）。
TIER_AUTO: str = "auto"
TIER_GRANT: str = "grant"
TIER_CONFIRM: str = "confirm"
TIERS: tuple[str, ...] = (TIER_AUTO, TIER_GRANT, TIER_CONFIRM)

#: effect → 档位。未知 effect 一律按 `confirm`（宁可多问一句）。
EFFECT_TIERS: dict[str, str] = {
    "read": TIER_AUTO,
    "write_local": TIER_GRANT,
    "ui_control": TIER_CONFIRM,
    "external_send": TIER_CONFIRM,
}

#: 拒绝原因（回执里的 `reason`，一句话说清卡在哪）。
REASON_NO_GRANT: str = "no_grant"
REASON_GRANT_EXPIRED: str = "grant_expired"
REASON_OUT_OF_SCOPE: str = "out_of_scope"
REASON_OVER_AMOUNT: str = "over_amount"
REASON_NEEDS_CONFIRM: str = "needs_confirm"

#: 授权范围里表示「所有 effect」的通配符。
SCOPE_ALL: str = "*"


def effect_tier(effect: str) -> str:
    """这一步属于哪一档。未知 = 最严那档。"""
    return EFFECT_TIERS.get(str(effect or "").strip(), TIER_CONFIRM)


def describe_tier(tier: str) -> str:
    """给回执/日志的一句话（≤20 字，不带情绪词）。"""
    return {
        TIER_AUTO: "可以直接做",
        TIER_GRANT: "有长期授权",
        TIER_CONFIRM: "要你点头",
    }.get(str(tier or ""), "要你点头")


def _now(now: Any = None) -> datetime:
    if isinstance(now, datetime):
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        warn("core/jobs/policy.py:81 ValueError", err=repr(__import__("sys").exc_info()[1]))
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _tool_of(action: str) -> str:
    return str(action or "").split(":", 1)[0].strip()


def grant_allows(
    policy: Any,
    *,
    step: Any,
    amount: float = 0.0,
    now: Any = None,
) -> tuple[bool, str]:
    """长期授权能不能覆盖这一步：`(允许, 拒绝原因)`。

    `scope` 里可以写 effect 名、工具名或 `*`；空 scope 什么都不覆盖（默认拒绝）。
    """
    if policy is None or not str(getattr(policy, "approved_by", "") or "").strip():
        return (False, REASON_NO_GRANT)
    expires_at = _parse_time(getattr(policy, "expires_at", None))
    if expires_at is not None and _now(now) > expires_at:
        return (False, REASON_GRANT_EXPIRED)
    raw_scope = str(getattr(policy, "scope", "") or "")
    terms = {part.strip() for part in raw_scope.replace("，", ",").split(",") if part.strip()}
    if not terms:
        return (False, REASON_OUT_OF_SCOPE)
    effect = str(getattr(step, "effects", "") or "")
    tool = _tool_of(getattr(step, "action", ""))
    if not (SCOPE_ALL in terms or effect in terms or (tool and tool in terms)):
        return (False, REASON_OUT_OF_SCOPE)
    max_amount = getattr(policy, "max_amount", None)
    if max_amount is not None and float(amount) > float(max_amount):
        return (False, REASON_OVER_AMOUNT)
    return (True, "")


def authorize_step(
    step: Any,
    *,
    policy: Any = None,
    amount: float = 0.0,
    now: Any = None,
) -> tuple[bool, str, str]:
    """这一步能不能自己动手：`(允许, 档位, 拒绝原因)`。

    注意这只回答「要不要再问一句」，**不**回答「能不能对外说话」——
    后者永远只有出口一条路（R3）。
    """
    tier = effect_tier(getattr(step, "effects", ""))
    if tier == TIER_AUTO:
        return (True, tier, "")
    if tier == TIER_GRANT:
        ok, reason = grant_allows(policy, step=step, amount=amount, now=now)
        return (ok, tier, "" if ok else reason)
    return (False, tier, REASON_NEEDS_CONFIRM)


def needs_human(step: Any, *, policy: Any = None, amount: float = 0.0, now: Any = None) -> bool:
    """这一步是不是非得人点头（`abstain` 的反面：不许自己决定的事）。"""
    ok, _tier, _reason = authorize_step(step, policy=policy, amount=amount, now=now)
    return not ok


# ── 「主人自己点名」= 授权已经给过 ────────────────────────────────────────
# 真机 2026-10-21 业主原话：「我都在群里说了还要她说什么」。
# 规则只放行两类效果，且**必须逐字命中**主人这句话里出现过的那段正文：
#   * external_send / write_local —— 主人说了"发这句/写这个"，就是许可；
#   * ui_control 永不自动放行（点哪里、点几下不是一句话能说清的）；
#   * read 不需要批准，用不着这条。
# 宁可多问一次，也不许把"她推断出来的部分"当成主人点过名。
ORDER_EFFECTS: tuple[str, ...] = ("external_send", "write_local")
ORDER_MIN_CHARS = 4


def _norm(text: str) -> str:
    """只留「字」：去空白、去标点。

    真机 2026-10-21：主人说「今天下雨记得带伞」，她写成「今天下雨，记得带伞」
    （多一个逗号）→ 逐字判据判 False → 又被问了一遍。标点不是语义，不该挡住授权。
    """
    return "".join(ch for ch in str(text or "") if ch.isalnum())


def dictated_payload(action: str) -> str:
    """``send:目标|正文`` / ``write:路径|内容`` 里那段「正文/内容」。"""
    raw = str(action or "")
    _, sep, tail = raw.partition(":")
    if not sep:
        return ""
    if "|" in tail:
        return tail.split("|", 1)[1]
    return ""


def is_owner_ordered(step: Any, text: str) -> bool:
    """这一步是不是「主人这句原话里已经点名」的那一步。"""
    raw_effects = getattr(step, "effects", "") or ""
    # 真机坑：Step.effects 是**单个字符串**（"external_send"），不是元组；
    # 按可迭代写会逐字符比较 → 永远 False，于是「主人点名也不批」。
    if isinstance(raw_effects, str):
        effects = (raw_effects,) if raw_effects else ()
    else:
        effects = tuple(raw_effects)
    if not any(effect in ORDER_EFFECTS for effect in effects):
        return False
    payload = _norm(dictated_payload(getattr(step, "action", "")))
    said = _norm(text)
    if len(payload) < ORDER_MIN_CHARS or not said:
        return False
    return payload in said
