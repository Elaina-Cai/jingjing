# -*- coding: utf-8 -*-
"""「上一条到没到」（2026-11-01）

她一直只看得到 ``say`` 返回的 "queued"——平台有没有真的送出去、对方那边有没有认，
她不知道。于是 2026-10-30 那天 12 条全失败，她还以为发出去了，继续往下说。

这个模块只做一件事：把**她自己上一条**的 fate 变成一句人话，塞进她这一拍的观测。
口径：
  · 只讲已观察到的证据，不把本地回读当成送达或已读；
  · 超过 ``FRESH_S`` 的不提（旧账翻出来只会让她莫名其妙地道歉）；
  · 拿不到就返回空串，调用方什么都不加——不许因为查不到而让她看见"未知"。
"""
import time

FRESH_S = 3600.0          # 一小时内说过的话才提
_CACHE: dict = {}
_CACHE_S = 5.0
#: 同一条发送、同一个结果只说一次。2026-11-24：主人抱怨「怎么经常给我发这种」——
#: 一条未确认的回执会在随后每个回合被重复端给她，她就一遍遍转告「可能没送到」。
_ANNOUNCED: dict = {}


def _fmt_hm(ts: float) -> str:
    try:
        return time.strftime("%H:%M", time.localtime(float(ts)))
    except Exception:
        return ""


def _one(row: dict) -> str:
    state = str(row.get("state") or "")
    ack = str(row.get("ack") or "")
    if ack == "dry_run":
        return ""
    when = _fmt_hm(row.get("created_ts") or 0)
    why = str(row.get("why") or "").strip()
    if state == "sent":
        if ack == "confirmed":
            return "%s 观察到同账号同会话的同文本地发件记录；单凭它不能证明属于本次发送，也没有对方送达或已读凭据。" % when
        return ("%s 那条发送执行已返回，尚无本地回读确认；送达和已读情况不确定。"
                "这条别再重说一遍，等下一次回读。" % when)
    if state in ("failed", "expired"):
        tail = ("：%s" % why) if why else ""
        return "%s 那条队列报告%s%s；不能仅据此判断对方是否收到。" % (when, "过期" if state == "expired" else "执行失败", tail)
    if state == "unverified":
        return ("%s 那条发送结果尚未核实，是否发出不确定；已暂停自动重试，等待核对。"
                "这条先别重说，有新的内容再开口。" % when)
    if state == "pending":
        return "%s 那条仍在等待执行；没有送达凭据。" % when
    if state == "sending":
        return "%s 那条正在执行发送；结果尚未确认。" % when
    return ""


def _with_reply(line: str, chat_id: str, sent_ts: float, now: float) -> str:
    """兼容旧调用；同会话后续消息和等待超时都不能证明回应关联。"""
    return line


def note(chat_id: str = "", *, now: float | None = None) -> str:
    """返回一句话；没有可说的就返回空串。"""
    now = float(now if now is not None else time.time())
    from .ports import current_read_scope
    scope = current_read_scope()
    if scope is None or not all((scope.platform, scope.account_id, scope.chat_id)) or chat_id != scope.chat_id:
        return ""
    key = (scope.platform, scope.account_id, scope.chat_id)
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _CACHE_S:
        return hit[1]
    out = ""
    try:
        from db import world_db as _w
        rows = _w.last_outbound(chat_id, platform=scope.platform, account_id=scope.account_id, limit=1)
        if rows:
            row = rows[0]
            created = float(row.get("created_ts") or 0)
            if 0 < now - created <= FRESH_S:
                key = (scope.platform, scope.account_id, scope.chat_id,
                       str(row.get("id") or ""), str(row.get("queue_id") or ""), created)
                sig = "%s|%s" % (row.get("state"), row.get("ack"))
                if _ANNOUNCED.get(key) == sig:
                    out = ""
                else:
                    out = _one(row)
                    if out:
                        _ANNOUNCED[key] = sig

    except Exception as exc:  # noqa: BLE001 - 查不到就不提，但要留痕
        # 调用方（loop/brain.py）整段包在 try 里，所以这里 import 失败也不会掀桌子
        from safelog import warn as _warn
        _warn("delivery_note.failed", chat_id=chat_id, err=repr(exc)[:120])
        out = ""
    _CACHE[key] = (now, out)
    return out
