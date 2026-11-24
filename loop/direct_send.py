"""点名即发：主人在私聊里点名「在×群里说一句：…」—— 直接投递，不等模型自觉调工具。

真机 2026-10-21：同一句「在银月的测试群里说一句：今天下雨记得带伞」在 13:10→16:36
被重复 **16 次**，她每回都答「计划挂在那儿了…我按不到那个键，真发还得你自己敲」。
话没错、也给了替代，但那 16 次最后是**放弃** —— 事没办成。

这条路原来挂在 `core/jobs/direct.py` 上（认出句式 → 开一件 job → 由 `_job_tick` 推进），
而整条 jobs 执行链（`core/routing/turn_pipeline.py` / `_job_tick`）已经删掉了：
那个模块现在**没有任何生产调用方**，`jobs` 表最新一条停在 10-21。

所以这里不复活 jobs，直接写第二条路：

* 判据复用 `core.jobs.direct.parse_order`（同一份正则，不另造一套分词）；
* 只认**私聊**里的指令（群里别人的话不猜）；
* 只认**主人**（`.env` 的 `WECHAT_OWNER_WXIDS` / `YINYUE_OWNER_WXIDS`；空则一律不认，
  fail-closed —— 不知道谁是主人时，一句话都不发）；
* 每条入站消息只发一次（`msg_id` 记在 `data/loop/direct_sent.json`，重启也在）；
* 三态开关 `YINYUE_FEATURE_DIRECT_SEND`：`off`（默认）/ `shadow`（只记不发）/ `on`（真投递）；
* 投递走 `loop/ports.say_many` —— 和她说 `say` 是**同一条队列**（desk 认领、outbox 落盘）。
  拿到 `queue_ids` 后每拍用 `check_deliveries()` 读 `outbound_queue.state` 反查，
  于是「发没发出去」不再只是 `ack=queued` 的猜测。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Iterable

import structlog

from core import features
from core.infra import jsonio
from core.infra import envflags
from core.jobs.direct import parse_order
from loop import ports
from loop.safelog import warn

log = structlog.get_logger(__name__)

MODE_OFF = features.MODE_OFF
MODE_SHADOW = features.MODE_SHADOW
MODE_ON = features.MODE_ON

STATE_REL = os.path.join("data", "loop", "direct_sent.json")
MAX_KEPT = 500
DONE_STATES = ("sent",)
DEAD_STATES = ("failed", "expired", "skipped")


@dataclass(frozen=True)
class Order:
    """一条「主人点名让我往哪儿发一句」。"""

    target: str
    body: str
    msg_id: str
    at: str = ""
    speaker: str = ""


def mode() -> str:
    """三态档位。默认 off：这条路上线第一个月只许人影看，不许自己开。"""
    return features.mode(features.DIRECT_SEND, features.MODE_OFF)


def owner_ids() -> set[str]:
    """主人 id 集合。来源与 `loop/brief.py::_owner_ids` 同（那边实测 `room_member.is_owner`
    7 个群标错 6 个，所以只信 `.env`）。"""
    raw = " ".join((envflags.text("WECHAT_OWNER_WXIDS"),
                    envflags.text("YINYUE_OWNER_WXIDS")))
    return {x.strip() for x in re.split(r"[,;，；\s]+", raw) if x.strip()}


def is_group(chat_id: str) -> bool:
    c = str(chat_id or "")
    return c.endswith("@chatroom") or c.startswith("group:")


def sender_ids(db_path: str, rows) -> dict[str, str]:
    """按 (会话, 正文) 回账号库查真实 sender_id（wxid）。

    为什么不能用 msg_id：`Row` 来自 `data/world.db` 的 `inbound_event`，`msg_id` 是
    「平台:账号:事件号」里的**事件号**，和 account.db `messages.msg_id` 不是同一个值
    （2026-11-04 实测：同一条私聊消息两边取值完全不同）；而 `inbound_event.who_id`
    目前是空的，`who` 只有显示名「Elaina」。所以唯一稳的键是 (chat_id, 正文)。

    为什么必须回查：`loop/snapshot.py` 会把 `row.speaker` 改写成**显示名**
    （`:428` `r.speaker = name or "（来源不明）"`、`:402` 自己写的改 `"我"`），
    到 `loop/run.py` 手里只剩「Elaina」这种昵称。而主人识别只认 wxid
    （`WECHAT_OWNER_WXIDS`）——2026-11-04 第一次真机点名就是这样被静默丢掉的
    （`window_done ... orders=0`，日志里一条 `direct_send_*` 都没有）。
    """
    pairs = []
    for r in rows or ():
        cid = str(getattr(r, "chat_id", "") or "")
        txt = str(getattr(r, "text", "") or getattr(r, "raw", "") or "")
        mid = str(getattr(r, "msg_id", "") or "")
        if cid and txt and not getattr(r, "is_self", False):
            pairs.append((cid, txt, mid))
    if not pairs or not db_path:
        return {}
    out: dict[str, str] = {}
    try:
        con = ports._connect(db_path)
        for cid, txt, mid in pairs:
            row = con.execute(
                "select sender_id from messages where chat_id = ? and content = ? "
                "order by created_at desc limit 1",
                (cid, txt),
            ).fetchone()
            if row and str(row[0] or ""):
                out[mid] = str(row[0])
        con.close()
    except Exception as exc:            # 回查失败就退回显示名比对，不许拖垮这一拍
        warn("direct_send.sender_lookup_failed", err=repr(exc)[:80], db=str(db_path)[:80])
    return out


def find_orders(rows, *, chat_id: str, owners: set[str] | None = None,
                seen: Iterable[str] = (), sender_ids_map: dict[str, str] | None = None,
                ) -> tuple[Order, ...]:
    """从这一拍新到的话里挑出「主人点名让我发一句」。纯判断：不发、不写盘。

    `sender_ids_map` 是 `sender_ids()` 的回查结果（msg_id -> wxid）；没有就退回
    比 `row.speaker`——但那多半是昵称，只在测试/离线场景够用。
    """
    ids = owner_ids() if owners is None else {str(x) for x in owners}
    if not ids:
        return ()
    if is_group(chat_id):
        return ()
    done = {str(x) for x in (seen or ())}
    found: list[Order] = []
    for row in rows or ():
        if bool(getattr(row, "is_self", 0)):
            continue
        mid = str(getattr(row, "msg_id", "") or "")
        if mid and mid in done:
            continue
        # 2026-11-04：优先用行上带下来的真 sender_id（精确、不查库）；
        # 没有才退回按正文本地回查（sender_ids_map），最后才是显示名。
        who = str(getattr(row, "sender_id", "")
                  or (sender_ids_map or {}).get(mid)
                  or getattr(row, "speaker", "") or "")
        if who not in ids:
            continue
        text = str(getattr(row, "text", "") or getattr(row, "raw", "") or "")
        hit = parse_order(text)
        if hit is None:
            continue
        target, body = hit
        found.append(Order(target=target, body=body, msg_id=mid,
                           at=str(getattr(row, "at", "") or ""), speaker=who))
    return tuple(found)


def _root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def state_path() -> str:
    return os.path.join(_root(), STATE_REL)


def load_state() -> dict:
    """读状态。文件不存在＝第一次跑，不是错误。"""
    p = state_path()
    if not os.path.exists(p):
        return {"items": {}}
    data, why = jsonio.read_json_checked(p, default=None)
    # 2026-11-22：`why` 是状态名（"ok" / "missing" / "corrupt"），不是布尔。
    # 原来把 "ok" 也当成读坏了，于是每次加载都返回空表并写一条告警：
    # 已处理过的直发订单不记账，重启后会被当成新订单再发一遍（实测 2592 条告警）。
    if why != "ok":
        warn("direct_send.state_unreadable", path=p, reason=str(why)[:120])
        return {"items": {}}
    if not isinstance(data, dict):
        return {"items": {}}
    data.setdefault("items", {})
    return data


def save_state(state: dict) -> None:
    """写状态。必须走 `core.infra.jsonio.atomic_write_json`：

    自己拼一个固定的临时文件名再改名，并发写会交织成坏文件——
    `tests/pytest_style/test_jsonio_atomic.py` 两条守卫就是拦这个的（2026-11-04 实测踩到）。
    """
    p = state_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
    except Exception as exc:
        warn("direct_send.state_unwritable", path=p, error=repr(exc)[:120])
        return
    try:
        if not jsonio.atomic_write_json(p, state, indent=1):
            warn("direct_send.state_unwritable", path=p, reason="atomic_write_json 返回 False")
    except Exception as exc:
        warn("direct_send.state_unwritable", path=p, error=repr(exc)[:120])


def _trim(items: dict) -> None:
    if len(items) <= MAX_KEPT:
        return
    for key in list(items.keys())[: len(items) - MAX_KEPT]:
        items.pop(key, None)


async def deliver(order: Order, *, platform: str = "wechat", turn_id: str = "",
                  dry_run: bool = False) -> dict:
    """把一条点名指令投出去。返回 {ok, ack, queue_ids, group_id, chat_id, error}。

    认不出目标就**不发**（`resolve_say_target` 只认完全相等，认重了也拒）——
    发错人一句提示都没有，比不发坏得多。
    """
    chat, why = ports.resolve_say_target(order.target)
    if not chat:
        log.info("direct_send_target_rejected", target=order.target, reason=str(why or ""))
        return {"ok": False, "error": str(why or "target_not_found"),
                "ack": "", "queue_ids": [], "group_id": "", "chat_id": ""}
    try:
        res = await ports.say_many(ports.split_bubbles(order.body), dry_run=dry_run,
                                   chat_id=chat, platform=platform, turn_id=turn_id)
    except Exception as exc:
        warn("direct_send_deliver_failed", target=order.target, error=repr(exc)[:160])
        return {"ok": False, "error": repr(exc)[:120],
                "ack": "", "queue_ids": [], "group_id": "", "chat_id": chat}
    data = dict(getattr(res, "data", None) or {})
    return {"ok": bool(getattr(res, "ok", False)),
            "ack": str(data.get("ack") or ""),
            "queue_ids": [int(x) for x in (data.get("queue_ids") or [])],
            "group_id": str(data.get("group_id") or ""),
            "chat_id": chat,
            "error": str(getattr(res, "error", "") or "")}


async def run(rows, *, chat_id: str, platform: str = "wechat", turn_id: str = "",
              mode_: str = "", owners: set[str] | None = None, db: str = "") -> dict:
    """一拍里跑一次：认出就发（shadow 只记）。返回 {mode, orders, sent}。

    `db` 给了就按 msg_id 回查真实 sender_id（见 `sender_ids()` 里那串踩坑记录）。
    """
    current = mode_ or mode()
    if current == MODE_OFF:
        return {"mode": MODE_OFF, "orders": 0, "sent": 0}
    state = load_state()
    items = state.setdefault("items", {})
    _who = sender_ids(db, rows) if db else {}
    orders = find_orders(rows, chat_id=chat_id, owners=owners, seen=items.keys(),
                         sender_ids_map=_who)
    if not orders:
        return {"mode": current, "orders": 0, "sent": 0}
    sent = 0
    for order in orders:
        if current == MODE_SHADOW:
            log.info("direct_send_shadow", chat_id=chat_id, target=order.target,
                     chars=len(order.body), msg_id=order.msg_id)
            items[order.msg_id] = {"at": order.at, "target": order.target,
                                   "state": "shadow", "queue_ids": [], "group_id": ""}
            continue
        res = await deliver(order, platform=platform, turn_id=turn_id)
        ok = bool(res.get("ok"))
        items[order.msg_id] = {"at": order.at, "target": order.target,
                               "state": "queued" if ok else "failed",
                               "queue_ids": res.get("queue_ids") or [],
                               "group_id": res.get("group_id") or "",
                               "ack": res.get("ack") or "",
                               "chat_id": res.get("chat_id") or ""}
        if ok:
            sent += 1
            log.info("direct_send_queued", target=order.target, chat_id=res.get("chat_id"),
                     queue_ids=res.get("queue_ids"), ack=res.get("ack"),
                     chars=len(order.body))
        else:
            log.info("direct_send_failed", target=order.target, error=res.get("error"))
    _trim(items)
    save_state(state)
    return {"mode": current, "orders": len(orders), "sent": sent}


def _field(row, name: str) -> Any:
    try:
        return row[name]
    except Exception:
        return getattr(row, name, None)


def _query_queue(ids: list[int], conn: Any = None) -> list[dict]:
    """按队列行号读 outbound_queue 的状态。查不到就回空（不许崩，也不许静默）。"""
    if not ids:
        return []
    own = conn is None
    if own:
        try:
            from db import world_db
        except Exception as exc:
            warn("direct_send.queue_unavailable", error=repr(exc)[:120])
            return []
        try:
            conn = world_db.conn()
        except Exception as exc:
            warn("direct_send.queue_connect_failed", error=repr(exc)[:120])
            return []
    marks = ",".join("?" * len(ids))
    try:
        rows = conn.execute(
            "select * from outbound_queue where id in (%s)" % marks,
            list(ids)).fetchall()
    except Exception as exc:
        warn("direct_send.queue_query_failed", error=repr(exc)[:160])
        return []
    finally:
        if own:
            try:
                conn.close()
            except Exception as exc:
                warn("direct_send_queue_close_failed", error=repr(exc)[:120])
    out = []
    for r in rows or ():
        out.append({"id": int(_field(r, "id") or 0),
                    "state": str(_field(r, "state") or ""),
                    "ack": str(_field(r, "ack") or ""),
                    "why": str(_field(r, "why") or ""),
                    "chat_id": str(_field(r, "chat_id") or ""), "payload": _field(r, "payload"), "platform": str(_field(r, "platform") or ""), "account_id": str(_field(r, "account_id") or "")})
    return out

def recently_sent(chat_id: str, text: str, *, window_s: int = 120, conn=None) -> bool:
    """同一句话最近是不是已经发出去了（或正在发）。

    2026-11-04 真机：同一条「下午三点倒垃圾」被 direct_send 排了 261、又被模型这一拍的
    say 排了 262，群里真的出现两条一模一样的消息。判据只认 outbound_queue 里的事实，
    不猜意图：同会话 + 同正文 + 还没死（pending/sending/sent）+ 最近 window_s 秒内动过。
    """
    cid = str(chat_id or "").strip()
    body = str(text or "").strip()
    if not cid or not body:
        return False
    try:
        if conn is None:
            from db import world_db as _w  # 懒导入：这个模块导入期不许开库
            conn = _w.conn()
        cur = conn.execute(
            "select count(*) from outbound_queue where chat_id = ? "
            "and state in ('pending', 'sending', 'sent') "
            "and coalesce(json_extract(payload, '$.text'), '') = ? "
            "and coalesce(updated_ts, created_ts, 0) >= cast(strftime('%s', 'now') as integer) - ?",
            (cid, body, int(window_s)),
        )
        row = cur.fetchone()
        return int((row or [0])[0] or 0) > 0
    except Exception as exc:  # noqa: BLE001 - 查不出来就当没发过，绝不许拖垮发送
        warn("direct_send.recently_sent_failed", err=repr(exc)[:80])
        return False


def _verified(item: dict, marks: dict) -> tuple[bool, list[str]]:
    """这几条队列行，在微信账库里真有对应的消息吗（2026-11-04 §24 地基②）。

    `state = 'sent'` 只代表「发送工按下了回车」，不是「微信里真有这条」：
    真库 67 条 sent 里 23 条查无此消息（凭据率 65.7%）。所以「送到了」必须多问一层，
    判据在 `loop/receipts.py`（队列 sent → 有 msg_id → 账库真有这个 id）。
    """
    from loop import receipts as _rc

    whys: list[str] = []
    for q in (item.get("queue_ids") or []):
        try:
            row = marks.get(int(q))
        except Exception:  # noqa: BLE001 - 非数字 id 直接算没凭据
            row = None
        if not row:
            whys.append("队列行 %s 读不到" % q)
            continue
        rec = _rc.verify_row(row)
        if not rec.verified:
            whys.append(rec.why)
    return (not whys), whys


def check_deliveries(*, state: dict | None = None, conn: Any = None,
                     limit: int = 20) -> dict:
    """只读核验 queued/unverified 订单；本地观察不等于平台送达。

    local_observed 是现行结果名；delivered 返回键仅保留旧调用兼容。
    """
    own = state is None
    st = load_state() if own else state
    items = st.setdefault("items", {})
    todo = [(mid, it) for mid, it in items.items()
            if str(it.get("state") or "") in ("queued", "unverified") and it.get("queue_ids")]
    if not todo:
        return {"checked": 0, "local_observed": [], "delivered": [], "skipped": [], "failed": [], "pending": [], "unverified": []}
    todo = todo[: max(1, int(limit))]
    ids = [int(q) for _, it in todo for q in (it.get("queue_ids") or [])]
    marks = {r["id"]: r for r in _query_queue(ids, conn=conn)}
    delivered: list[str] = []
    failed: list[str] = []
    pending: list[str] = []
    unverified: list[str] = []
    skipped: list[str] = []
    dirty = False
    for mid, it in todo:
        previous_state = str(it.get("state") or "")
        states = [marks[int(q)]["state"] for q in (it.get("queue_ids") or []) if int(q) in marks]
        if not states:
            pending.append(mid)
            continue
        if len(states) == len(it.get("queue_ids") or []) and all(s == "skipped" for s in states) and all(marks[int(q)].get("ack") == "dry_run" for q in it["queue_ids"]):
            dirty = True
            it["state"] = "skipped"
            it["reason"] = "dry_run"
            skipped.append(mid)
        elif any(s in DEAD_STATES for s in states):
            dirty = True
            it["state"] = "failed"
            failed.append(mid)
            log.info("direct_order_failed", msg_id=mid, target=it.get("target"),
                     states=states)
        elif all(s in DONE_STATES for s in states) or any(s == "unverified" for s in states):
            _ok_all, _whys = _verified(it, marks)
            if not _ok_all:
                # 未核实保持可复查，不能把缺少观察解释成未发送。
                reason = "; ".join(_whys)[:200]
                changed = previous_state != "unverified" or it.get("unverified_why") != reason
                it["state"] = "unverified"
                it["unverified_why"] = reason
                dirty = dirty or changed
                unverified.append(mid)
                if changed:
                    log.warning("direct_order_unverified", msg_id=mid, target=it.get("target"), why=reason)
                # 2026-11-04 ④ 主人抄送：「我替你说了一句，但查不到那条」必须让主人知道，
                # 否则他以为话已经到了（E-59 的另一半）。
                try:
                    from loop import owner_copy as _oc3
                    if previous_state != "unverified":
                        _oc3.note("order_unverified",
                              target=str(it.get("chat") or it.get("target") or ""))
                except Exception as _oexc3:  # noqa: BLE001
                    log.warning("owner_copy.order_unverified_failed", err=repr(_oexc3)[:120])
            else:
                dirty = True
                it["state"] = "local_observed"
                it["evidence_kind"] = "local_outgoing_record"
                it.pop("unverified_why", None)
                delivered.append(mid)
                log.info("direct_order_local_observed", msg_id=mid, target=it.get("target"),
                         queue_ids=it.get("queue_ids"))
        else:
            pending.append(mid)
    if own and dirty:
        save_state(st)
    return {"checked": len(todo), "local_observed": delivered, "delivered": delivered,
            "failed": failed, "pending": pending, "unverified": unverified, "skipped": skipped}


__all__ = ["MODE_OFF", "MODE_SHADOW", "MODE_ON", "Order", "mode", "owner_ids", "recently_sent", "sender_ids",
           "is_group", "find_orders", "state_path", "load_state", "save_state",
           "deliver", "run", "check_deliveries"]
