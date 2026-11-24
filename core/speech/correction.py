"""发出去之后：能不能撤回，不能就发一条更正（阶段 7 的补偿那一半）。

三件事，全是纯函数，不碰库、不碰窗口：
1. `can_revoke(receipt, *, now, window_s)` —— 微信的撤回窗口是 2 分钟；
   过期就只能更正。**判断只认回执里的时间，不认「我记得是刚才」。**
2. `correction_text(original, fix)` —— 生成那条更正消息：带原话片段，让人知道在更正哪句。
3. `plan_correction(receipt, fix)` —— 把回执 + 更正内容翻成一次投递计划；
   没有 `ref`（不知道在更正哪条）或没有正文，一律 fail closed。

为什么先做「更正」不做「撤回」：撤回要右键菜单 + UI 自动化（脆弱），而更正是
一条普通消息——走的是已经真发通过的 `wechat_send.send_and_verify`，风险归零。
撤回留了探针（`can_revoke`），等菜单这条路验过再接。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable
from safelog import warn

log = logging.getLogger(__name__)

#: 微信撤回窗口（实测规则：发出后 2 分钟内可撤回）。
REVOKE_WINDOW_S = 120

#: 更正消息的前缀。固定成中文冒号，方便以后从账本里再捞出来。
CORRECTION_PREFIX = "更正："

#: 原话片段最长多少字（再多就把更正本身挤没了）。
EXCERPT_MAX_CHARS = 20

#: 一条更正最多多少字（与投递的伴随语同一量级）。
FIX_MAX_CHARS = 200

#: 没有 `ref` 时的拒绝理由（说不清在更正哪一条，就不发）。
REASON_NO_REF = "no_ref"

#: 正文为空时的拒绝理由。
REASON_EMPTY_FIX = "empty_fix"

#: 时间读不出来时按「过期」处理（宁可只能更正，也不要瞎撤回）。
REASON_NO_TIME = "no_time"


@dataclass(frozen=True)
class Correction:
    """一次更正的投递计划。`ref` 指向被更正的那条回执。"""

    ref: str
    target: str
    text: str
    original_excerpt: str
    mode: str  #: "revoke"（还来得及撤回）或 "correct"（只能更正）

    def as_dict(self) -> dict[str, str]:
        return {
            "ref": self.ref,
            "target": self.target,
            "text": self.text,
            "excerpt": self.original_excerpt,
            "mode": self.mode,
        }


def _text(value: Any) -> str:
    return str(value or "").strip()


def _excerpt(original: str) -> str:
    text = " ".join(_text(original).split())
    if len(text) <= EXCERPT_MAX_CHARS:
        return text
    return text[:EXCERPT_MAX_CHARS] + "…"


def correction_text(original: str, fix: str) -> str:
    """`更正：<原话片段> → <改口后的意思>`；缺任一侧就返回空串。"""
    fix_text = " ".join(_text(fix).split())
    if not fix_text or len(fix_text) > FIX_MAX_CHARS:
        return ""
    head = _excerpt(original)
    if not head:
        return f"{CORRECTION_PREFIX}{fix_text}"
    return f"{CORRECTION_PREFIX}{head} → {fix_text}"


def _as_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = _text(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            warn("core/speech/correction.py:97 ValueError", err=repr(__import__("sys").exc_info()[1]))
            continue
    return None


def can_revoke(receipt: Any, *, now: datetime | None = None,
               window_s: int = REVOKE_WINDOW_S) -> tuple[bool, str]:
    """还在撤回窗口内吗？`(能不能, 理由)`。理由为空串表示「能」。"""
    if not _text(_ref(receipt)):
        return False, REASON_NO_REF
    at = _as_time(_field(receipt, "at"))
    if at is None:
        return False, REASON_NO_TIME
    moment = now or datetime.now()
    if moment - at <= timedelta(seconds=max(0, int(window_s))):
        return True, ""
    return False, "expired"


def _field(receipt: Any, name: str) -> Any:
    if isinstance(receipt, dict):
        return receipt.get(name)
    return getattr(receipt, name, None)


def _ref(receipt: Any) -> str:
    return _text(_field(receipt, "ref"))


def plan_correction(receipt: Any, fix: str, *, now: datetime | None = None) -> Correction | None:
    """把「这条回执 + 我想改成什么」翻成一次投递；说不清就返回 None。"""
    ref = _ref(receipt)
    if not ref:
        return None
    text = correction_text(_field(receipt, "detail") or "", fix)
    if not text:
        return None
    target = _text(_field(receipt, "target"))
    if not target:
        return None
    revokable, _reason = can_revoke(receipt, now=now)
    return Correction(
        ref=ref,
        target=target,
        text=text,
        original_excerpt=_excerpt(_field(receipt, "detail") or ""),
        mode="revoke" if revokable else "correct",
    )


# --------------------------------------------------------------- 收口（U-26）
#
# 上面三个纯函数只回答"该不该更正、怎么措辞"，这一半才把话真的发出去。
# 三条 fail closed：开关 off / 说不出在更正哪条（无 `ref`）/ 这条本身就是更正
# （防连环更正：一条更正被再更正，会变成对着自己喊话）。

#: 开关名（`core/features.py::CORRECTION`）。三档：off 不动 / shadow 只算不发 / on 真发。
REASON_OFF = "off"
REASON_NO_PLAN = "no_plan"
REASON_ALREADY = "already_corrected"
REASON_NO_RECEIPT = "no_receipt"
REASON_DELIVER_FAILED = "deliver_failed"


@dataclass(frozen=True)
class CorrectionResult:
    """一次更正的结果。成功失败都要有一条（形状与出口回执同构，好一起落账）。"""

    ok: bool
    status: str
    mode: str
    ref: str
    text: str
    reason: str = ""
    receipt: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "mode": self.mode,
            "ref": self.ref,
            "text": self.text,
            "reason": self.reason,
            "receipt": dict(self.receipt),
        }


def correction_mode() -> str:
    """三态开关的唯一读点（全仓同一个形状）。"""
    from core import features

    return features.mode(features.CORRECTION, features.MODE_OFF)


def is_correction(receipt: Any) -> bool:
    """这条回执本身就是一条更正吗？"""
    return _text(_field(receipt, "detail")).startswith(CORRECTION_PREFIX)


def _status_of(raw: Any) -> str:
    if isinstance(raw, dict):
        return str(raw.get("status") or "")
    return str(getattr(raw, "status", "") or "")


def correct(
    receipt: Any,
    fix: str,
    *,
    deliver: Callable[..., Any] | None = None,
    revoke: Callable[..., Any] | None = None,
    recorder: Callable[..., Any] | None = None,
    turn_id: str = "",
    chat_id: str = "",
    now: datetime | None = None,
    mode: str | None = None,
) -> CorrectionResult:
    """把一条已发回执更正掉：窗口内先撤回再更正，窗口外只更正。

    **撤回失败不阻止更正**——撤回靠右键菜单与 UI，本来就可能失败；而"说错了却
    连更正都没发"是更糟的结果。撤回成不成决定 `status`（`revoked_and_corrected` /
    `corrected`），不决定做不做。
    """
    active = mode if mode is not None else correction_mode()
    ref = _ref(receipt)
    if active == "off":
        return CorrectionResult(False, "off", "", ref, "", REASON_OFF)
    if is_correction(receipt):
        return CorrectionResult(False, "failed", "", ref, "", REASON_ALREADY)
    plan = plan_correction(receipt, fix, now=now)
    if plan is None or deliver is None:
        return CorrectionResult(False, "failed", "", ref, _text(fix), REASON_NO_PLAN)
    if active == "shadow":
        # 影子档：算出该怎么更正、记一行，**一个字节都不发**。
        log.info(
            "correction_shadow ref=%s target=%s mode=%s chars=%d",
            ref, plan.target, plan.mode, len(plan.text),
        )
        return CorrectionResult(True, "shadow", plan.mode, ref, plan.text, "")

    revoked = False
    if plan.mode == "revoke" and revoke is not None:
        try:
            raw = revoke(plan.target, ref)
            revoked = bool(raw.get("ok")) if isinstance(raw, dict) else bool(getattr(raw, "ok", False))
        except Exception as exc:  # noqa: BLE001 - 撤不回来也要继续更正
            log.warning("correction_revoke_failed ref=%s target=%s error=%r", ref, plan.target, exc)

    try:
        sent = deliver(plan.target, plan.text, turn_id=turn_id)
    except Exception as exc:  # noqa: BLE001 - 发不出去只能是这条更正失败
        log.warning("correction_deliver_failed target=%s error=%r", plan.target, exc)
        return CorrectionResult(
            False, "failed", plan.mode, ref, plan.text, f"{type(exc).__name__}: {exc}"[:120]
        )

    status = _status_of(sent)
    ok = status in ("sent", "unconfirmed")
    receipt = {
        "status": status or "failed",
        "kind": "text",
        "target": plan.target,
        "ack": "platform_id" if status == "sent" else ("unconfirmed" if ok else "error"),
        "detail": plan.text,
        "ref": ref,
        "adapter": "wechat",
    }
    if recorder is not None:
        try:
            recorder(receipt, turn_id=turn_id, chat_id=chat_id)
        except Exception as exc:  # noqa: BLE001 - 旁注失败不阻断主链
            log.info("correction_record_failed ref=%s error=%r", ref, exc)
    if not ok:
        return CorrectionResult(False, "failed", plan.mode, ref, plan.text, REASON_DELIVER_FAILED, receipt)
    return CorrectionResult(
        True,
        "revoked_and_corrected" if revoked else "corrected",
        plan.mode,
        ref,
        plan.text,
        "",
        receipt,
    )


def correct_ref(
    ref: str,
    fix: str,
    *,
    ledger: Any = None,
    deliver: Callable[..., Any] | None = None,
    revoke: Callable[..., Any] | None = None,
    recorder: Callable[..., Any] | None = None,
    turn_id: str = "",
    chat_id: str = "",
    now: datetime | None = None,
    mode: str | None = None,
) -> CorrectionResult:
    """从回执号找回那条回执，再 :func:`correct`（执行器手里只有 `ref`，没有回执对象）。"""
    key = _text(ref)
    finder = getattr(ledger, "find_ref", None) if ledger is not None else None
    row = finder(key) if key and callable(finder) else None
    if not row:
        return CorrectionResult(False, "failed", "", key, _text(fix), REASON_NO_RECEIPT)
    return correct(
        row,
        fix,
        deliver=deliver,
        revoke=revoke,
        recorder=recorder,
        turn_id=turn_id,
        chat_id=chat_id,
        now=now,
        mode=mode,
    )
