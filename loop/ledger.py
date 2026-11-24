"""账：她的每一次动作都留一行，下一拍先读它。"""
from __future__ import annotations

import time

from core.infra import duration
from core.infra.duration import now_ms, from_epoch_s  # noqa: F401

import json
import os
from typing import Iterable
from datetime import datetime
from .safelog import warn
from core.infra.jsonio import atomic_write_text
from core.infra import actions as _actions  # PIT-111：流水行 I/O 与读判据只有这一处
from core.infra.actions import path, merge_last  # noqa: F401
from core.infra import tempo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 2026-11-22：不再在导入时复制一份。复制之后测试改道 actions.DIR 时，这里和
# loop/ports 读到的还是旧路径（写进临时账本、读却读生产）。用模块级 __getattr__
# 现场取，保证「目录只有一个定义」这件事在运行期也成立。
def __getattr__(name: str):
    if name == "DIR":
        return _actions.DIR
    raise AttributeError(name)
RESPONSE_ACTS = frozenset({"say", "show", "sticker"})


_path = path  # 行路径（PIT-111：唯一流水在 core.infra.actions）


_merge_last = merge_last


def append_receipt(*, act: str, target: str = "", why: str = "", refs=None,
                   result: str = "", blocked_reason: str = "", turn_id: str = "",
                   at: str = "", ack: str = "", text: str = "",
                   extra: dict | None = None) -> dict:
    """Write the receipt with the active conversation coordinates attached."""
    from . import ports

    scope = ports.current_read_scope()
    if scope is not None and all((scope.platform, scope.account_id, scope.chat_id)):
        extra = dict(extra or {})
        extra["scope"] = {
            "platform": str(scope.platform),
            "account_id": str(scope.account_id),
            "chat_id": str(scope.chat_id),
        }
    return _actions.append_receipt(
        act=act, target=target, why=why, refs=refs, result=result,
        blocked_reason=blocked_reason, turn_id=turn_id, at=at, ack=ack,
        text=text, extra=extra,
    )


read_receipts = _actions.read_receipts


def _current_receipt_scope() -> tuple:
    try:
        from . import ports

        scope = ports.current_read_scope()
        if scope is not None and all((scope.platform, scope.account_id, scope.chat_id)):
            return str(scope.platform), str(scope.account_id), str(scope.chat_id)
    except Exception as exc:
        warn("receipt_scope_failed", err=repr(exc)[:120])
    return ""


_receipt_in_scope = _actions.receipt_in_scope


def delivery_fact(receipt: dict) -> str:
    """What this output operation can currently say about its own delivery."""
    result = str(receipt.get("result") or "")
    if receipt.get("act") not in {"say", "show", "sticker"} or result not in {
        "sent", "queued", "rehearsal", "skipped",
    }:
        return ""
    ack = str(receipt.get("ack") or "")
    if result == "rehearsal" or receipt.get("dry_run") or ack in {"dry_run", "rehearsal"}:
        return "这是预演，没有实际发送"
    if result == "skipped" or ack == "skipped":
        return "这次没有提交投递"
    if result == "queued" or ack == "queued":
        return "已排进投递队列，送达还未确认"
    if ack == "confirmed":
        return "本地确认发出，送达还未确认"
    return "已尝试投递，送达还未确认"


def render_digest(limit: int = 10, budget: int | None = None) -> str:
    """budget=None（2026-10-31 起默认）：不按字数截——以前按 400 字硬切，最后一行常被砍成半句。"""
    current = _current_receipt_scope()
    rows = read_receipts(min(1000, max(int(limit) * 20, 200)) if current else limit)
    if current:
        scoped = [row for row in rows if _receipt_in_scope(row, current)]
        activity = [row for row in scoped if row.get("kind") != "delivery"][-limit:]
        turn_ids = {str(row.get("turn_id") or "") for row in activity}
        deliveries = [row for row in scoped
                      if row.get("kind") == "delivery"
                      and str(row.get("turn_id") or "") in turn_ids]
        rows = activity + deliveries
    if not rows:
        return "[上一拍] 没有账（这是第一拍）"
    dstat = {r.get("turn_id"): r.get("status") for r in rows if r.get("kind") == "delivery"}
    drefs = {r.get("turn_id"): r.get("ref") for r in rows
             if r.get("kind") == "delivery" and r.get("ref")}
    rows = [r for r in rows if r.get("kind") != "delivery"]
    lines = ["[上一拍 · 最近 %d 行账]" % len(rows)]
    for r in rows:
        _tg = str(r.get("target") or "")
        _tgt = ""
        if _tg:
            try:
                from . import ports as _p
                _tgt = _p.label_for(_tg) or _tg
            except Exception:  # noqa: BLE001 - 翻不出就退回原样
                _tgt = _tg
        seg = "- %s %s%s" % (str(r.get("at") or "")[11:16], r.get("act") or "?",
                             (" " + _tgt) if _tgt else "")
        if r.get("why"):
            seg += "｜%s" % r["why"]
        if r.get("blocked_reason"):
            seg += "｜blocked: %s" % r["blocked_reason"]
        fact = delivery_fact(r)
        if fact:
            seg += "｜%s" % fact
        if r.get("turn_id") in dstat:
            seg += "｜投递:%s" % dstat[r["turn_id"]]
        # 编号必须印出来：不然她"看过什么、回过什么"没法再点开
        _dref = (drefs.get(r.get("turn_id")) or "").strip()
        if _dref:
            seg += "｜回执 msg:%s" % _dref
        _refs = [x for x in (r.get("refs") or []) if x]
        if _refs:
            seg += "｜看过 %s" % ",".join(_refs[:3])
        lines.append(seg)
    _out = "\n".join(lines)
    return _out[:budget] if budget else _out


def record_delivery(turn_id: str, status: str, ref: str = "") -> dict:
    """补账：先说 unconfirmed，后台确认后追加一行 confirmed。

    账是追加式（只增不改），所以状态用"再写一行"表达；读的时候后写的盖前面。
    """
    row = {"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "kind": "delivery", "turn_id": turn_id, "status": status, "ref": ref}
    _append_row(row)  # 唯一写口（PIT-111）
    return row


def blocked_targets(window_min: int = tempo.minutes("ledger.blocked")) -> dict[str, str]:
    """最近被拦过的靶 -> 原因。同一处 30 分钟内不再试，避免反复撞墙白烧 token。"""
    import datetime as _dt
    from pathlib import Path as _P

    cutoff = (_dt.datetime.now() - _dt.timedelta(minutes=window_min)).strftime("%Y-%m-%d %H:%M:%S")
    out: dict[str, str] = {}
    # 文件名只有一个来源（PIT-111）：这里不再自己拼 ledger-<日期>.jsonl
    today = _P(_actions.path())
    if not today.exists():
        return out
    current = _current_receipt_scope()
    for line in today.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            warn("ledger:102 Exception", err=repr(__import__("sys").exc_info()[1]))
            continue
        if not _receipt_in_scope(row, current):
            continue
        if row.get("act") != "say" or row.get("result") != "blocked":
            continue
        # 2026-10-24：只有"墙"（硬规则）才配进 30 分钟负缓存。
        # 之前把所有 blocked 都算进去，结果「刚跟这个人说过话」这种**瞬时**理由
        # 也会被缓存 30 分钟，而且每被拦一次就刷新窗口 ⇒ 她永远说不了话
        # （16:48 一次冷却拦截，把她锁到 17:18，中间问"吃什么"四次全被自己的缓存拦掉）。
        # 「刚被拦过…」这种自己生成的包裹文案也一律不算（否则自我续期）。
        _why = str(row.get("blocked_reason") or "")
        if "墙" not in _why:
            continue
        if "刚被拦过" in _why:
            continue
        if (row.get("at") or "") < cutoff:
            continue
        target = str(row.get("target") or "")
        if target:
            out[target] = str(row.get("blocked_reason") or "被拦过")
    return out


def last_say_at(target: str) -> float:
    return _actions.last_say_at(target, scope=_current_receipt_scope())


def said_this_turn(target: str, turn_id: str) -> bool:
    return _actions.said_this_turn(target, turn_id, scope=_current_receipt_scope())


def quiet_ms(target: str) -> int | None:
    return _actions.quiet_ms(target, scope=_current_receipt_scope())


def speaking_facts(target: str, *, window_min: int = tempo.minutes("ledger.facts")) -> dict:
    """我最近在这人这儿开口了几次（事实，不是闸）。

    2026-10-24：原来这里叫 last_say_at，被当成门（150 秒不连说）。
    用户要求「不要代码控制、要模型控制」⇒ 门撤掉，这个数据改成给她看的事实。
    """
    out = {"says": 0, "last_at": 0.0, "gap_s": None}
    if not target:
        return out
    try:
        rows = read_receipts(limit=300)
    except Exception:  # noqa: BLE001
        warn("ledger:168 Exception", err=repr(__import__("sys").exc_info()[1]))
        return out
    current = _current_receipt_scope()
    now = time.time()
    for d in rows:
        if not _receipt_in_scope(d, current):
            continue
        if d.get("kind") == "delivery":
            continue
        if d.get("act") != "say" or d.get("result") != "sent":
            continue
        if str(d.get("target") or "") != target:
            continue
        try:
            ts = datetime.strptime(d.get("at", ""), "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:  # noqa: BLE001
            warn("ledger:180 Exception", err=repr(__import__("sys").exc_info()[1]))
            continue
        if now - ts <= window_min * 60:
            out["says"] += 1
        if ts > out["last_at"]:
            out["last_at"] = ts
    if out["last_at"]:
        out["gap_s"] = int(now - out["last_at"])
    return out


def said_since(at: str, *, exclude: str = "", limit: int | None = None,
               exclude_chat: str = "") -> list[dict]:
    """这一拍（``at`` 之后）真正发出去的话（target / text）——跨窗去重的唯一来源。

    2026-10-28 ③：原来由 ``loop/run.py`` 攒字符串塞进 brain，等于把台账复制一份到内存，
    同一件事两个来源就一定分叉（还有 80 字截断）。现在只有台账；
    ``at`` 只是"从哪一刻开始算"，格式与 ``append_receipt`` 写下的 ``at`` 一致，
    同格式字符串比较即时间序。

    2026-10-31 C7：取消 60 字截断与 limit=4（主人要求不留上限；截半句会把"说过什么"读成另一句）；
    新增 ``exclude_chat``——台账的 target 是 chat_id，而 focus 往往是会话名，只排除一个会漏掉本窗。
    """
    out: list[dict] = []
    try:
        rows = read_receipts(limit=200)
    except Exception:  # noqa: BLE001
        warn("ledger_says_unreadable", err=repr(__import__("sys").exc_info()[1]))
        return out
    current = _current_receipt_scope()
    _skip = {x for x in (exclude, exclude_chat) if x}
    for d in rows:
        if not _receipt_in_scope(d, current):
            continue
        if d.get("act") != "say" or d.get("result") != "sent":
            continue
        if at and str(d.get("at") or "") < at:
            continue
        _tgt = str(d.get("target") or "")
        if _tgt and _tgt in _skip:
            continue
        out.append({"target": _tgt, "text": str(d.get("text") or d.get("why") or "")})
        if limit and len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 真凭据对账
# 2026-11-04：账本里 18:46:52 有 4 条 `say`/`sent` 回执，而 `messages` 同期一条都没有、
# `outbound_queue` 也没有对应行（那 4 条的 `ack` 是空串）。她照着这本账以为自己
# 「连发了三遍伪造的报到通知」，下一拍就去「撤回」一件没发生的事。
# 所以凡「我说过」都必须有凭据：回执本身进过投递队列（`ack='queued'` / `queue_ids` 非空），
# 或者队列表 / 消息表里真能查到同一句。
SAY_EVIDENCE_DAYS = 7
SAY_EVIDENCE_TTL_S = 60.0
_EVID_CACHE = {"at": 0.0, "keys": set(), "ok": False}

#: 2026-11-04：**测试/接线脚本把自己发的消息写进了生产账本**。
#: `tests/test_claim_ledger_wiring.py:83` 的 DEFAULT_CHAT 就是 "wiring_10000000001@chatroom"，
#: 冒烟脚本还用了 `c1` 和 `via="speech"`。这些回执**永远不是她说过的话**，
#: 不管凭据在不在 —— 这是源头过滤，不是「证不出来」那种无奈丢弃。
TEST_TARGET_PREFIXES = ("wiring_", "test_", "smoke_")
TEST_TARGETS = ("c1",)
TEST_VIAS = ("speech",)


def _is_test_receipt(d: dict) -> bool:
    """这条回执是不是测试/接线脚本写的（她的记忆里不该有）。"""
    d = d or {}
    t = str(d.get("target") or "")
    if t in TEST_TARGETS or any(t.startswith(x) for x in TEST_TARGET_PREFIXES):
        return True
    return str(d.get("via") or "") in TEST_VIAS
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _real_say_keys(days: int = SAY_EVIDENCE_DAYS):
    """(chat_id, 归一正文) 的真凭据集合；第二个返回值＝至少一个来源读成功。

    读不到不等于「她没说过」，所以调用方拿到 False 时按降级处理（照旧展示 + 打 warn），
    而不是把所有历史一句话都抹掉。
    """
    keys = set()
    ok = False
    try:
        import time as _t

        from db import world_db as _w   # 懒导入：这个模块导入期不许开库
        con = _w.conn()
        cut = _t.time() - days * 86400
        for chat_id, text in con.execute(
                "select chat_id, coalesce(json_extract(payload, '$.text'), '') "
                "from outbound_queue where coalesce(created_ts, 0) >= ?", (cut,)):
            k = _say_key(text)
            if k:
                keys.add((str(chat_id or ""), k))
        ok = True
    except Exception as exc:  # noqa: BLE001
        warn("ledger.evidence_queue_failed", err=repr(exc)[:80])
    try:
        import glob as _g
        import sqlite3 as _s

        for db in _g.glob(os.path.join(_ROOT, "data", "platforms", "*", "*", "account.db")):
            try:
                c = _s.connect("file:" + str(db).replace(chr(92), "/") + "?mode=ro", uri=True)
                try:
                    rows = c.execute(
                        "select chat_id, content from messages where is_self = 1 "
                        "and created_at >= datetime('now', 'localtime', ?)",
                        ("-%d day" % int(days),)).fetchall()
                finally:
                    c.close()
            except Exception as exc:  # noqa: BLE001
                warn("ledger.evidence_messages_failed", db=os.path.basename(str(db)),
                     err=repr(exc)[:60])
                continue
            for chat_id, text in rows:
                k = _say_key(text)
                if k:
                    keys.add((str(chat_id or ""), k))
            ok = True
    except Exception as exc:  # noqa: BLE001
        warn("ledger.evidence_messages_scan_failed", err=repr(exc)[:80])
    return keys, ok


def _evidence():
    """带 60 秒缓存：一拍里 `recent_says` / `recent_says_in` 会各调一次，别每拍开两遍库。"""
    import time as _t
    now = _t.time()
    if now - float(_EVID_CACHE["at"] or 0.0) < SAY_EVIDENCE_TTL_S:
        return _EVID_CACHE["keys"], bool(_EVID_CACHE["ok"])
    keys, ok = _real_say_keys()
    _EVID_CACHE.update({"at": now, "keys": keys, "ok": ok})
    return keys, ok


def verified_says(items, *, limit=None):
    """只留下有真凭据的「我说过」。没有凭据的一条都不留 —— 宁可少说，不许假账。"""
    rows = list(items or [])
    # 先摘掉测试写的，再谈凭据：降级放行也不能把测试的话放回来。
    n_test = 0
    pure = []
    for d in rows:
        if _is_test_receipt(d):
            n_test += 1
            continue
        pure.append(d)
    if n_test:
        warn("ledger.says_test_harness", dropped=n_test)
    rows = pure
    keys, ok = _evidence()
    if not ok:
        warn("ledger.evidence_unavailable", n=len(rows))
        return rows[:limit] if limit else rows
    kept = []
    for d in rows:
        d = d or {}
        ack = str(d.get("ack") or "")
        if ack == "confirmed" or ack == "queued" or d.get("queue_ids"):
            kept.append(d)
            continue
        chat_id = str(d.get("chat_id") or d.get("target") or "")
        if (chat_id, _say_key(d.get("text"))) in keys:
            kept.append(d)
    dropped = len(rows) - len(kept)
    if dropped:
        warn("ledger.says_unverified", dropped=dropped, kept=len(kept))
    return kept[:limit] if limit else kept


def _say_key(text: str) -> str:
    """把一句话归一成「是不是同一句」的键：折叠空白，去掉结尾的标点。"""
    t = " ".join(str(text or "").split())
    return t.rstrip("。.…、，,~～ ")


def uniq_says(items, *, limit: int = 3) -> list:
    """同一句话只说一次（保留最新那条），新的在前。

    2026-11-04 真机事故：「[这一拍为什么醒]」里同一句「18:46 同学你好，关于报到这件事我再说
    一遍…」连着出现 2 次，「[这个会话我最近说过]」里出现 3 次 —— 都是账本里连着三条一模一样
    的 say 回执。她看到自己被复制了三遍，就更容易把同一件事再讲一遍。
    去重放在账本这一层，两个块的调用方都不用改（recent_says / recent_says_in 都走它）。
    """
    out: list = []
    seen: set = set()
    for d in reversed(list(items or [])):
        row = d or {}
        text_key = _say_key(row.get("text"))
        k = (str(row.get("act") or "say"), text_key)
        if not text_key or k in seen:
            continue
        seen.add(k)
        out.append(row)
        if limit and len(out) >= int(limit):
            break
    return out


def recent_says(limit: int = 3) -> list[dict]:
    """最近几条真正发出去的 say（at / target / text），**新的在前**。

    2026-11-02：原来从头遍历账本（文件里旧的在前），拿到的其实是**最旧**的几条——
    「我最近开口」一直在告诉她今天早上说过什么，而不是刚刚说过什么。
    和 `brief._hits` 是同一个病根：把「按行读」当成了「按时间读」。
    """
    hits: list[dict] = []
    try:
        rows = read_receipts(limit=300)
    except Exception:  # noqa: BLE001
        warn("ledger.recent_says.Exception", err=repr(__import__("sys").exc_info()[1]))
        return []
    for d in rows:
        if d.get("kind") == "delivery":
            continue
        if d.get("act") != "say" or d.get("result") != "sent":
            continue
        hits.append({"ack": str(d.get("ack") or ""), "queue_ids": d.get("queue_ids"), "via": str(d.get("via") or ""), "at": str(d.get("at") or ""), "target": str(d.get("target") or ""),
                     "text": str(d.get("text") or d.get("why") or "")})
    return verified_says(uniq_says(hits, limit=None), limit=limit)


def recent_says_in(target: str, limit: int = 3) -> list[dict]:
    """我在某个会话里最近的回应动作（at / act / text / delivery），新的在前。

    2026-11-02：自发醒来那一拍她看不到自己说过什么，于是同一句反复说。
    实测「在。实验室我去不了，我没腿」出现过几十次、「12:25 那份记录我打不开」
    从中午追问到晚上。这里只给原话，不评判、不拦——让她自己看出重复。
    """
    _t = str(target or "").strip()
    if not _t:
        return []
    hits: list[dict] = []
    try:
        rows = read_receipts(limit=600)
    except Exception:  # noqa: BLE001
        warn("ledger_recent_says_failed", err=repr(__import__("sys").exc_info()[1]))
        return []
    current = _current_receipt_scope()
    for d in rows:
        if not _receipt_in_scope(d, current):
            continue
        if d.get("kind") == "delivery":
            continue
        if d.get("act") not in RESPONSE_ACTS or d.get("result") not in {"sent", "queued"}:
            continue
        if str(d.get("target") or "") != _t:
            continue
        _txt = str(d.get("text") or "").strip()
        if not _txt:
            continue
        hits.append({
            "act": str(d.get("act") or ""), "ack": str(d.get("ack") or ""),
            "queue_ids": d.get("queue_ids"),
            "via": str(d.get("via") or ""), "at": str(d.get("at") or ""),
            "target": _t, "scope": d.get("scope"), "turn_id": str(d.get("turn_id") or ""),
            "timing": d.get("timing"), "result": str(d.get("result") or ""), "text": _txt,
        })
    # read_receipts 是正序（旧→新），要的是最新的几条，所以从尾部取再翻回来。
    out = verified_says(uniq_says(hits, limit=None), limit=limit)
    for row in out:
        row["delivery"] = _delivery_fact_for_response(row)
    return out


def _complete_send_scope(scope) -> tuple[str, str, str] | None:
    """平台/账号/会话三件套齐全，才谈得上本地发送凭据。"""
    if isinstance(scope, dict):
        platform = scope.get("platform")
        account_id = scope.get("account_id")
        chat_id = scope.get("chat_id")
    elif isinstance(scope, (tuple, list)) and len(scope) == 3:
        platform, account_id, chat_id = scope
    else:
        return None
    values = (str(platform or ""), str(account_id or ""), str(chat_id or ""))
    return values if all(values) else None


def confirmed_say_at(row: dict, scope, at: float) -> float | None:
    """本地能确认这句真的发出时的落定时间；确认不了就 None。

    直发只认 ``ack='confirmed'``；带 ``queue_ids`` 的回执必须让世界库按同一组
    platform/account_id/chat_id/turn_id 核完全部队列号，并核对气泡数与队列号
    数量一致。返回的只是本地发送凭据，不等于对方已经收到或已读。
    """
    row = row if isinstance(row, dict) else {}
    send_scope = (_complete_send_scope(scope) if scope is not None
                  else _complete_send_scope(row.get("scope")))
    if send_scope is None or str(row.get("target") or "") != send_scope[2]:
        return None
    qids = row.get("queue_ids")
    if qids is None:
        return float(at) if str(row.get("ack") or "") == "confirmed" else None
    if not isinstance(qids, list) or not qids:
        return None
    timing = row.get("timing")
    bubbles = timing.get("bubbles") if isinstance(timing, dict) else None
    if bubbles != len(qids):
        return None
    turn_id = str(row.get("turn_id") or "")
    if not turn_id:
        return None
    try:
        from db.world_db import confirmed_queue_settled_ts

        settled = confirmed_queue_settled_ts(
            qids, platform=send_scope[0], account_id=send_scope[1],
            chat_id=send_scope[2], turn_id=turn_id,
        )
    except Exception:  # noqa: BLE001 - 世界库读不到就不算本地确认
        return None
    return max(float(at), float(settled)) if settled is not None else None


def _delivery_fact_for_response(row: dict) -> str:
    """把同一个回应回执的内容与它当前能证明的发送状态绑在一起。"""
    try:
        at = datetime.strptime(str(row.get("at") or ""), "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:  # noqa: BLE001
        at = 0.0
    if at and confirmed_say_at(row, row.get("scope"), at) is not None:
        return "本地确认发出，送达还未确认"
    return delivery_fact(row)


def send_state_of(receipts) -> dict:
    """这一拍回来的回执里，哪些回应是真出去了、哪些还只是排进队列。

    只认本地发送凭据（``confirmed_say_at``）：直发要有 ack=confirmed，走队列的
    必须让世界库按同一组 platform/account_id/chat_id/turn_id 核完全部队列号。
    排进队列只是"还在路上"，不算说过——否则她会把没出去的话当成"我说了、
    他没接"，然后自己把话收回去。这里返回的也只是本地发送凭据，不等于对方
    已经收到或看到。
    """
    out = {"confirmed": 0, "queued": 0}
    for row in receipts or []:
        if not isinstance(row, dict):
            continue
        if row.get("act") not in RESPONSE_ACTS or row.get("result") not in {"sent", "queued"}:
            continue
        try:
            at = datetime.strptime(str(row.get("at") or ""), "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:  # noqa: BLE001 - 时间读不出来就当没有本地发送凭据
            at = 0.0
        if at and confirmed_say_at(row, row.get("scope"), at) is not None:
            out["confirmed"] += 1
        else:
            out["queued"] += 1
    return out


def recent_failed_says(window_min: int = tempo.minutes("ledger.recent"), limit: int = 3) -> list[dict]:
    """最近 window 分钟内**想发但没发出去**的句子。

    2026-11-22：16:06 那条隐私提醒写得好，但投递端在标题闸中止了。下一拍她只看到
    「最近开口」里已确认的那几句，失败的那条完全不可见——她可能把没说出去的话当成说过了。
    这里把失败/被拦的 say 单独取出来，让提示里能说一句「这句没发出去，对方可能没看到」。
    """
    from datetime import datetime as _dt

    try:
        rows = read_receipts(limit=300)
    except Exception:  # noqa: BLE001
        warn("ledger.recent_failed_says_read_failed", err=repr(__import__("sys").exc_info()[1]))
        return []
    current = _current_receipt_scope()
    now = time.time()
    out: list[dict] = []
    for row in rows:
        if not _receipt_in_scope(row, current):
            continue
        if row.get("act") not in {"say", "say_failed"}:
            continue
        if str(row.get("result") or "") not in {"failed", "blocked", "skipped"}:
            continue
        try:
            at = _dt.strptime(str(row.get("at") or ""), "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            from core.infra.warn_guard import once as _once
            from safelog import warn as _warn
            if _once("ledger_recent_failed_says_bad_at"):
                _warn("ledger_recent_failed_says_bad_at", at=str(row.get("at") or "")[:40])
            continue
        if now - at > window_min * 60:
            continue
        out.append({
            "target": str(row.get("target") or ""),
            "at": at,
            "why": str(row.get("blocked_reason") or row.get("why") or "")[:80],
            "text": str(row.get("text") or "")[:60],
        })
    out.sort(key=lambda item: -item["at"])
    return out[:limit]


def recent_say_targets(window_min: int = tempo.minutes("ledger.recent"),
                       limit: int | None = None) -> list[dict]:
    """最近 window 分钟内我在哪些会话开过口（target 是 chat_id，含句数/最后一次）。

    2026-10-24 事故：[我最近开口] 原来只认「★ 私聊格」，而 ★ 在"有人在等我"时才出现——
    她一开口 after_mine 就清空、★ 消失，于是恰好在"我刚连说几句没人回"的时刻，
    唯一那句刹车事实被断供。改成按账里的会话取，不再依赖 ★。
    """
    now = time.time()
    acc: dict[str, dict] = {}
    try:
        rows = read_receipts(limit=300)
    except Exception:  # noqa: BLE001
        warn("ledger.recent_say_targets.Exception", err=repr(__import__("sys").exc_info()[1]))
        return []
    current = _current_receipt_scope()
    for d in rows:
        if not _receipt_in_scope(d, current):
            continue
        if d.get("kind") == "delivery":
            continue
        if d.get("act") != "say" or d.get("result") != "sent":
            continue
        t = str(d.get("target") or "")
        if not t:
            continue
        try:
            at = datetime.strptime(str(d.get("at") or ""), "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:  # noqa: BLE001
            continue
        # 排队回执只是「已排进队列」；最近开口只认本地确认落定的时间。
        confirmed_at = confirmed_say_at(d, d.get("scope"), at)
        if confirmed_at is None:
            continue
        if now - confirmed_at > window_min * 60:
            continue
        e = acc.setdefault(t, {"target": t, "says": 0, "says_10m": 0, "last_at": 0.0, "last_text": ""})
        e["says"] += 1
        if now - confirmed_at <= 600:
            e["says_10m"] += 1
        if confirmed_at >= e["last_at"]:
            e["last_at"] = confirmed_at
            e["last_text"] = str(d.get("text") or d.get("why") or "")
    out = sorted(acc.values(), key=lambda x: -x["last_at"])
    if limit:
        out = out[:limit]
    for e in out:
        e["gap_s"] = int(now - e["last_at"])
    return out


# ---------------- 承诺：我答应过的事（2026-10-26 加） ----------------
# 之前没有这条路：19:44 她在群里说"我问了一句，她还没说清楚"，之后就再没有
# 任何地方记得这件事——只能靠她自己偶然想起来。现在答应一句就落一行账，
# 下一拍回到「我还欠着的」里；读取不销账，完成只认显式关闭记录。
# 承诺有效期由关闭事件决定，不设日期查询窗口。


_append_row = _actions.append_row


def record_promise(*, who: str, what: str, chat_id: str = "", due: str = "",
                   turn_id: str = "") -> dict:
    """记一条"我答应过的事"（我回头问 / 我等着 / 等我看到再告诉你）。"""
    what = str(what or "").strip()
    if not what:
        return {}
    from loop.ports import current_read_scope
    active = current_read_scope()
    coordinates = {"platform": active.platform, "account_id": active.account_id, "chat_id": str(chat_id or "")} if active is not None and chat_id else {}
    return _append_row({"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "kind": "promise", "turn_id": turn_id, "scope": coordinates,
                        "id": "%d" % int(time.time() * 1000),
                        "who": str(who or "")[:40], "chat_id": str(chat_id or ""),
                        "target": str(chat_id or ""), "act": "promise",
                        "what": what, "due": str(due or "")[:40]})


def close_promise(pid: str, *, reason: str = "", chat_id: str = "",
                  evidence_ref: str = "", scope=None) -> dict:
    """关闭一条承诺：账本记审计，目标侧同步关闭（目标才是真源）。

    ``chat_id`` 不给就从账本行反查；账本只写一行关闭记录，不再单独决定
    「这件事算不算完成」。目标不存在（旧账本行还没映射）时只记审计。
    """
    row = _append_row({"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "kind": "promise_closed", "ref": str(pid or ""),
                       "why": str(reason or "")})
    chat = str(chat_id or "").strip()
    if not chat:
        for item in _promise_rows(("promise",)):
            if str(item.get("id")) == str(pid):
                chat = str(item.get("chat_id") or item.get("target") or "").strip()
                break
    if chat:
        try:
            from core.context.promise_goal import close_promise_goal

            terminal = (
                "abandoned"
                if str(reason or "").startswith(("expired", "cancel", "gave_up"))
                else "completed"
            )
            close_promise_goal(
                chat,
                promise_id=pid,
                at=str(row.get("at") or ""),
                result=str(reason or "closed"),
                evidence_ref=evidence_ref,
                scope=scope,
                state=terminal,
            )
        except Exception as exc:  # noqa: BLE001 - 目标失败不丢审计行
            warn("promise_goal_close_failed", err=repr(exc)[:120])
    return row


_promise_rows = _actions.rows_for_kinds


def open_promises(limit: int | None = None) -> list[dict]:
    """只读查询保留流水中没有显式关闭记录的承诺，不按天数过期。"""
    rows = _promise_rows(("promise", "promise_closed"))
    closed = {r.get("ref") for r in rows if r.get("kind") == "promise_closed"}
    out = [r for r in rows if r.get("kind") == "promise" and r.get("id") not in closed]
    return (out[-limit:] if limit else out)[::-1]


def render_promises(limit: int | None = None, budget: int | None = None,
                    exclude_ids: Iterable[str] = (), chat_id: str | None = None, scope=None) -> str:
    """承诺块；``exclude_ids`` = 已经有目标映射的账本行（避免与目标重复展示）。"""
    from core.context.cognitive_commit import _scope_identity
    identity = _scope_identity(scope)
    excluded = {str(item) for item in exclude_ids}
    rows = [row for row in open_promises() if str(row.get("id")) not in excluded and (chat_id is None or str(row.get("chat_id") or "") == chat_id) and (identity is None or _scope_identity(row.get("scope")) == identity)]
    if limit:
        rows = rows[:limit]
    if not rows:
        return ""
    lines = ["[我还欠着的]（我自己答应过的，还没销账）"]
    for r in rows:
        lines.append("- %s｜%s：%s" % (str(r.get("at") or "")[5:16],
                                      r.get("who") or r.get("chat_id") or "",
                                      r.get("what")))
    _out = "\n".join(lines)
    return _out[:budget] if budget else _out
