"""主人抄送：出了「他该知道」的事，给她主人的私聊发一条短消息。

为什么要有这条：2026-11-04 把「全 C」（外发不再等点头）铺开之后，她手里多了三种
「静悄悄出错」的方式 —— 内容闸拦下了一句、追认五次没找到那条消息、多步任务推到一半
推不动了。三种都属于「她自己知道、主人不知道」。以前这些只写进日志，而主人不看日志。

四条纪律（和这一版其它几个闸一致）：
1. 只报事实，不复述内容：被拦下的那句话**一个字的正文都不许带**（那正是闸要挡的东西，
   抄送里再写一遍等于绕过去；2026-11-04 的 8 条误拦也说明正文会误伤）；
2. 只认 `.env` 里那一个主人（`WECHAT_OWNER_WXIDS` / `YINYUE_OWNER_WXIDS`），
   认不出或认出多个就**不发**（fail-closed，照 BR-069 的老规矩）；
3. 不许刷屏：全局 60 秒一条、同类同会话 10 分钟一条；
4. 抄送自己绝不再走一遍内容闸（直接入 outbound_queue），否则"报错的消息"会被
   "报错机制"再拦一次；同时用 `_IN_NOTE` 防重入。
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from loop.safelog import warn

log = structlog.get_logger(__name__)

#: 会抄送的八类事。新增一类要同时补 `_LINES` 与测试。
KINDS: tuple[str, ...] = (
    "content_blocked",
    "confirm_timeout",
    "order_unverified",
    "job_stuck",
    "confirm_not_started",
    "confirm_stuck",
    "send_failed",
    "quiet_alert",
)

#: 这些事本身就是「发给主人的消息没发出去」；再抄回给主人会自激（2026-11-05 主人「要」加）。
_NEVER_ECHO_KINDS: tuple[str, ...] = ("send_failed",)

#: 全局最小间隔（秒）：任何两条抄送之间至少隔这么久。
GAP_S = 60.0

#: 同一类事 + 同一个会话的最小间隔（秒）。
SAME_KIND_GAP_S = 600.0

#: 一条抄送最多多少字（私聊里一句话，不许变成小作文）。
MAX_CHARS = 160

_LINES: dict[str, str] = {
    "content_blocked": "我在{where}里想发一句「{what}」这类的话，被我自己拦下了，没发。要发得你点头。",
    "confirm_timeout": "我在{where}里说过的一句话，回读五次都没找到，可能没送到。",
    "order_unverified": "我替你在{where}发的话，账上写了发出，微信里查不到那一条。",
    "job_stuck": "有件事我推不动了：{what}。要么你给个说法，要么我放下。",
    "confirm_not_started": "我在{where}里说过的一句话，回执核对压根没跑起来，所以不知道到底送到没。",
    "confirm_stuck": "我在{where}里说过的一句话，回执核对开跑了却一直没结果（卡住了），所以不知道到底送到没。",
    "send_failed": "我在{where}里想说的那句话没发出去（{what}），试了好几次都没成。",
    "quiet_alert": "我已经 {what} 小时没有自己开口说过一句话了：引擎在跑，但话没发出去（或我干脆没说）。"
}

_LAST_ANY = 0.0
_LAST: dict[tuple[str, str], float] = {}
_IN_NOTE = False


def reset() -> None:
    """清掉节流状态（只给测试用）。"""
    global _LAST_ANY
    _LAST_ANY = 0.0
    _LAST.clear()


def owner_chat() -> str:
    """主人的私聊 id；认不出或认出多个都返回空串（不猜）。"""
    try:
        from loop import direct_send as _ds

        ids = sorted(str(x) for x in (_ds.owner_ids() or set()) if str(x).strip())
    except Exception as exc:  # noqa: BLE001 - 认不出主人不是致命的
        warn("owner_copy.owner_ids_failed", err=repr(exc)[:160])
        return ""
    if len(ids) == 1:
        return ids[0]
    if ids:
        warn("owner_copy.owner_ambiguous", n=len(ids))
    return ""


def _clip(text: Any, limit: int = MAX_CHARS) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _label(chat_id: str) -> str:
    """把会话 id 说成人能懂的话；不猜昵称，只说是群还是私聊。"""
    value = str(chat_id or "").strip()
    if not value:
        return "一个会话"
    if value.endswith("@chatroom") or value.startswith("group:"):
        return "一个群（%s）" % value
    return "一个人的私聊（%s）" % value


def text_for(kind: str, *, target: str = "", detail: str = "") -> str:
    """拼这一条抄送的正文。`detail` 里不许出现被拦下的原句（调用方负责）。"""
    line = _LINES.get(str(kind or ""))
    if not line:
        return ""
    where = _label(target)
    what = _clip(detail, 60) if str(detail or "").strip() else "那一条"
    # 用 {where}/{what} 而不是 C 风格 %2$s：2026-11-04 第一版写成 %2$s，
    # Python 的 % 不认这个语法，开局就 ValueError: unsupported format character '$'。
    return _clip(line.format(where=where, what=what))


def note(
    kind: str,
    *,
    target: str = "",
    detail: str = "",
    now: float | None = None,
    conn: Any = None,
    dry_run: bool = False,
) -> dict:
    """给主人发一条抄送。

    返回 `{"sent": bool, "why": str, ...}`；`why` 词表：`unknown_kind` / `busy` /
    `too_soon` / `same_kind_recent` / `no_owner` / `no_text` / `dry_run` /
    `enqueue_failed` / `owner_self`（发给主人的事自己失败了，不再抄回）/ `""`（成功）。
    """
    global _LAST_ANY, _IN_NOTE

    at = float(now if now is not None else time.time())
    key = (str(kind or ""), str(target or ""))
    if str(kind or "") not in KINDS:
        return {"sent": False, "why": "unknown_kind"}
    if _IN_NOTE:
        return {"sent": False, "why": "busy"}
    body = text_for(kind, target=target, detail=detail)
    if not body:
        return {"sent": False, "why": "no_text"}
    if not dry_run:
        if _LAST_ANY and at - _LAST_ANY < GAP_S:
            return {"sent": False, "why": "too_soon"}
        last = _LAST.get(key)
        if last is not None and at - last < SAME_KIND_GAP_S:
            return {"sent": False, "why": "same_kind_recent"}
    chat = owner_chat()
    if not chat:
        return {"sent": False, "why": "no_owner", "text": body}
    # 2026-11-05（主人「要」）：发给主人的那条消息自己失败了，不许再抄回给主人 ——
    # 否则「发不出去 → 抄送 → 也发不出去 → 再抄送」会缓慢自激（600 秒节流只能拖慢它）。
    # 这种失败已经写在 logs/desk.log 与 outbound_queue 里，抄送只会制造更多失败。
    if str(kind or "") in _NEVER_ECHO_KINDS and str(target or "") == str(chat):
        warn("owner_copy.owner_self_skipped", kind=str(kind), chat=str(chat))
        return {"sent": False, "why": "owner_self", "text": body, "chat_id": chat}
    if dry_run:
        return {"sent": False, "why": "dry_run", "text": body, "chat_id": chat}
    # 2026-11-18：客户端没开时，quiet_alert 原来是塞进同一条发不出去的队列，
    # 等客户端回来才一起涌出，反而更像系统播报。没开就只留日志。
    if str(kind or "") == "quiet_alert":
        try:
            from core.execution import wx_health as _wxh

            if _wxh.client_down():
                warn("owner_copy.skipped_client_down", kind=str(kind))
                return {"sent": False, "why": "wechat_client_down", "text": body, "chat_id": chat}
        except Exception as exc:  # noqa: BLE001 - 探测失败按原路走
            warn("owner_copy.health_check_failed", err=repr(exc)[:120])
    ids: list = []
    _IN_NOTE = True
    try:
        from db import world_db as _w

        gid = "owner:%s:%d" % (str(kind), int(at * 1000))
        # 2026-11-18：以前不传 account_id，投递确认线程第一行就 return，
        # 抄送永远停在 unconfirmed（world.db 里 66 条）。这里按会话反查账号；
        # 查不到也要留下名字，别再静默。
        acct = ""
        try:
            from loop import speak_policy as _sp
            acct = str(_sp._account_of("wechat", chat) or "")
        except Exception as exc:  # noqa: BLE001 - 反查失败不阻塞抄送
            warn("owner_copy.account_lookup_failed", err=repr(exc)[:120])
        if not acct:
            try:
                from loop import ports as _ports
                acct = str(_ports._scoped_outbound_account("wechat", chat) or "")
            except Exception:
                acct = ""
        if not acct:
            warn("owner_copy.account_missing", chat=str(chat or ""), kind=str(kind))
        ids = list(_w.enqueue(gid, chat, [{"kind": "text", "text": body}],
                              platform="wechat", account_id=acct,
                              turn_id="owner_copy") or [])
    except Exception as exc:  # noqa: BLE001 - 抄送失败不许影响主流程
        warn("owner_copy.enqueue_failed", err=repr(exc)[:160])
        return {"sent": False, "why": "enqueue_failed"}
    finally:
        _IN_NOTE = False
    _LAST_ANY = at
    _LAST[key] = at
    log.info("owner_copy_sent kind=%s chat=%s queue=%s", str(kind), chat, len(ids))
    return {"sent": True, "why": "", "queue_ids": ids, "chat_id": chat, "text": body}


__all__ = [
    "GAP_S",
    "KINDS",
    "MAX_CHARS",
    "SAME_KIND_GAP_S",
    "note",
    "owner_chat",
    "reset",
    "text_for",
]
