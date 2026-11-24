"""发完回读核对（2026-11-04「全 C」地基②：说发出去了，得有凭据）。

**为什么需要这个模块。** 2026-11-04 的真教训（§18/§19 的 E-50、§22 的 E-59）是同一件事的两种形态：

- 账本里有 4 条 `say` 回执写着「报到安排…」，`messages` 里一条都没有；`ack` 是**空串**。
- `jobs` 有 13 件 `done`，而 `outbound_queue` 里 `turn_id like 'job%'` 的行是 0 —— 发送步骤从没接过队列。

两次都是「内部记了一笔完成」，而对外的凭据不存在。所以这里把「发出去」拆成三层，缺一层都不算数：

1. 队列行真的到 `state = 'sent'`；
2. 这一行有真实的 `msg_id`（`why` 形如 `msg 2534904576254688467`，由发送工回读微信写入）；
3. 那个 id 在微信账库 `messages` 里真的存在（能查到才算落地）。

`ack` 单独出现**永远不算证据**：它可以是 `pending`、可以是空串（真库 257/258 就是这样），也可以被写错。
"""

from __future__ import annotations

import json  # noqa: F401  (backfill 里读 payload 用)
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from .safelog import warn

#: 队列行 `why` 里 msg_id 的前缀（由 `loop/desk.py` 回读后写入，别改这两个字，历史行全靠它）
MSG_PREFIX = "msg "

#: 回读找工作的时间窗（秒）：队列行更新时间与微信消息时间的差不能超过它
BACKFILL_WINDOW_S = 600.0


@dataclass(frozen=True)
class Receipt:
    """一条队列行的核对结论。`verified` 为真才允许说「已经发出去了」。"""

    queue_id: int = 0
    chat_id: str = ""
    state: str = ""
    ack: str = ""
    msg_id: str = ""
    verified: bool = False
    why: str = ""
    verification_error: str = ""


def parse_msg_id(why: Any) -> str:
    """从 `why` 里取出 msg_id；不是 `msg <id>` 的形状一律返回空串。"""
    s = str(why or "").strip()
    if not s.startswith(MSG_PREFIX):
        return ""
    return s[len(MSG_PREFIX):].strip()


def judge(*, state: Any, ack: Any, why: Any, msg_exists: bool | None) -> tuple[bool, str]:
    """三层判据的纯函数版本。返回 `(verified, 人话原因)`。

    `msg_exists` 的三种取值都要能分辨：`True` 查到了、`False` 查不到、`None` 账库读不了。
    账库读不了时说「没核对」，不说「已发出」——宁可低估，不许虚报。
    """
    st = str(state or "").strip()
    ak = str(ack or "").strip()
    mid = parse_msg_id(why)
    if st == "unverified":
        return False, "执行结果未知，是否发出尚未核对"
    if st != "sent":
        return False, "还没发出去（state=%s）" % (st or "空")
    if not mid:
        return False, "队列行到 sent 了，但没回读到 msg_id（ack=%s）" % (ak or "空")
    if msg_exists is None:
        return False, "回读到 msg %s，但微信账库读不到，先当没核对" % mid
    if not msg_exists:
        return False, "回读的 msg %s 在微信账库里不存在" % mid
    return True, "msg %s" % mid


def _world_db() -> Any:
    from db import world_db as _w

    return _w


def _account_db() -> Any:
    from . import ports as _p

    return _p._account_db()


def _ro(path: Any) -> Any:
    return sqlite3.connect("file:%s?mode=ro" % str(path).replace("\\", "/"), uri=True)


def _connect_world(conn: Any = None) -> tuple[Any, bool]:
    if conn is not None:
        return conn, False
    return _world_db().conn(), True


def msg_in_db(msg_id: Any, chat_id: str = "", *, db: Any = None) -> bool | None:
    """这个 msg_id 在微信账库里存在吗？`None` = 读不了（调用方必须当「没核对」）。"""
    mid = str(msg_id or "").strip()
    if not mid:
        return False
    path = db or _account_db()
    if not path or not os.path.exists(str(path)):
        return None
    try:
        c = _ro(path)
        try:
            if chat_id:
                hit = c.execute(
                    "select 1 from messages where cast(id as text) = ? and chat_id = ? limit 1",
                    (mid, str(chat_id))).fetchone()
            else:
                hit = c.execute(
                    "select 1 from messages where cast(id as text) = ? limit 1", (mid,)).fetchone()
            return bool(hit)
        finally:
            c.close()
    except Exception as exc:  # noqa: BLE001 - 账库读不了要如实说「没核对」，不许猜
        warn("receipts.msg_in_db_failed", msg_id=mid, err=repr(exc)[:120])
        return None


def read_queue_row(queue_id: Any, *, conn: Any = None) -> dict:
    """读一条队列行（只读需要的列）；读不到返回空字典。"""
    try:
        qid = int(queue_id or 0)
    except Exception:  # noqa: BLE001 - 非数字 id 就是没有这条
        return {}
    if qid <= 0:
        return {}
    c, own = _connect_world(conn)
    try:
        cur = c.execute(
            "select * from outbound_queue where id = ?",
            (qid,))
        row = cur.fetchone()
        if not row:
            return {}
        cols = [d[0] for d in (cur.description or [])]
        return dict(zip(cols, row))
    except Exception as exc:  # noqa: BLE001 - 队列读不到时由调用方按「没核对」处理
        warn("receipts.read_queue_row_failed", queue_id=qid, err=repr(exc)[:120])
        return {}
    finally:
        if own:
            try:
                c.close()
            except Exception as exc:  # noqa: BLE001 - 关连接失败不影响判据，但要留痕
                warn("receipts.close_failed", where="read_queue_row", err=repr(exc)[:80])


def _message_matches(msg_id, chat_id, text, *, db=None, platform="", account_id=""):
    from core.execution.local_text_receipt import account_path, matches, SenderEvidenceUnavailable
    from db import world_db
    path = account_path(world_db.ROOT, platform, account_id)
    if db is not None:
        from pathlib import Path
        if path is None or Path(db).resolve() != Path(path).resolve():
            return None
    if not path or not os.path.isfile(str(path)):
        return None
    try:
        return matches(path, msg_id, chat_id, text)
    except SenderEvidenceUnavailable:
        raise
    except Exception as exc:
        warn("receipts.msg_in_db_failed", msg_id=msg_id, err=repr(exc)[:120])
        return None


def verify_row(row: dict, *, db: Any = None) -> Receipt:
    """把一行队列行判成 `Receipt`。"""
    row = dict(row or {})
    if not row:
        return Receipt(verified=False, why="队列里没有这一行")
    mid = parse_msg_id(row.get("why"))
    exists: bool | None = None
    error = ""
    if mid and str(row.get("state") or "") == "sent":
        text = _payload_text(row.get("payload"))
        if not text or not row.get("chat_id"):
            error = "observation_inputs_missing"
        elif not row.get("platform") or not row.get("account_id"):
            error = "observation_source_missing"
        else:
            from core.execution.local_text_receipt import SenderEvidenceUnavailable
            try:
                exists = _message_matches(mid, str(row["chat_id"]), text, db=db, platform=str(row.get("platform") or ""), account_id=str(row.get("account_id") or ""))
            except SenderEvidenceUnavailable:
                error = "sender_evidence_unavailable"
            if exists is None and not error:
                error = "local_store_unavailable"
    ok, why = judge(state=row.get("state"), ack=row.get("ack"), why=row.get("why"), msg_exists=exists)
    if error == "sender_evidence_unavailable":
        why = "本地消息缺少明确发送者证据，尚未核对"
    return Receipt(queue_id=int(row.get("id") or 0) or 0, chat_id=str(row.get("chat_id") or ""),
                   state=str(row.get("state") or ""), ack=str(row.get("ack") or ""),
                   msg_id=mid, verified=ok, why=why,
                   verification_error=error)


def verify(queue_ids: Any, *, conn: Any = None, db: Any = None) -> dict:
    """核对一组队列 id。返回 `{"verified": [...], "unverified": [...], "pending": [...]}`。"""
    verified: list[int] = []
    unverified: list[dict] = []
    pending: list[int] = []
    for q in (queue_ids or []):
        try:
            qid = int(q)
        except Exception:  # noqa: BLE001 - 非数字 id 直接计入 unverified
            unverified.append({"queue_id": q, "why": "不是队列 id"})
            continue
        row = read_queue_row(qid, conn=conn)
        if not row:
            pending.append(qid)
            continue
        rec = verify_row(row, db=db)
        if rec.verified:
            verified.append(qid)
        elif rec.state in ("pending", "sending"):
            pending.append(qid)
        else:
            unverified.append({"queue_id": qid, "why": rec.why, "state": rec.state, "ack": rec.ack})
    return {"verified": verified, "unverified": unverified, "pending": pending}


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split()).rstrip("。.…、，,~～!！?？ ")


def _payload_text(payload: Any) -> str:
    try:
        obj = json.loads(payload) if isinstance(payload, (str, bytes)) else (payload or {})
    except Exception:  # noqa: BLE001 - payload 坏掉就当没有正文
        return ""
    if isinstance(obj, dict):
        return str(obj.get("text") or "")
    return ""


def _find_message(text: str, chat_id: str, ts: float, *, db: Any, window_s: float) -> str:
    """在微信账库里按「同会话 + 同正文 + 时间相近」找她自己发的那条，返回 msg_id 或空串。"""
    body = _norm(text)
    if not body or not chat_id:
        return ""
    path = db or _account_db()
    if not path or not os.path.exists(str(path)):
        return ""
    try:
        c = _ro(path)
        try:
            rows = c.execute(
                "select id from messages where is_self = 1 and chat_id = ? "
                "and replace(replace(ifnull(content, ''), ' ', ''), char(10), '') = ? "
                "and abs(cast(strftime('%s', created_at) as integer) - ?) <= ? "
                "order by id desc limit 5",
                (str(chat_id), body.replace(" ", ""), int(ts), int(window_s))).fetchall()
        finally:
            c.close()
    except Exception as exc:  # noqa: BLE001 - 找不着就当没找到，注意这里是读不到而不是没有
        warn("receipts.find_message_failed", chat_id=chat_id, err=repr(exc)[:120])
        return ""
    for row in rows or []:
        mid = str(row[0] or "").strip()
        if mid:
            return mid
    return ""


def _reconcile_observation(row, connection, *, root=None, dry_run=False):
    from db import world_db as world
    from core.execution.local_text_receipt import account_path, observe, ObservationSourceChanged
    path = account_path(root or world.ROOT, row["platform"], row["account_id"])
    try:
        mid = observe(path, row["chat_id"], row["text"], row["baseline"], source_token=row.get("source_token", ""))
    except ObservationSourceChanged as exc:
        warn("receipts_observation_source_unavailable", queue_id=row["queue_id"], attempt_no=row["attempt_no"], reason=str(exc))
        return ""
    if mid and not dry_run:
        world.confirm_outbound(row["chat_id"], row["text"], msg_id=mid, receipt_id=row["receipt_id"],
            platform=row["platform"], account_id=row["account_id"], queue_id=row["queue_id"],
            attempt_no=row["attempt_no"], connection=connection)
    return mid


def backfill(*, conn: Any = None, db: Any = None, limit: int = 50,
             window_s: float = BACKFILL_WINDOW_S, dry_run: bool = True,
             now: float | None = None, root: Any = None) -> dict:
    """仅以已绑定的发送前快照补充本地观察；旧时间窗同文不是凭据。"""
    from db import world_db as world
    from core.execution.local_text_receipt import account_path, observe
    c, own = _connect_world(conn)
    out = {"seen": 0, "fixed": [], "unmatched": [], "dry_run": dry_run}
    try:
        ids = [int(r[0]) for r in c.execute("select id from outbound_queue where state='sent' and ack<>'confirmed' order by id desc limit ?", (max(1, int(limit)),)).fetchall()]
        out["seen"] = len(ids)
        try:
            snapshots = {int(r["queue_id"]): r for r in world.pending_outbound_observations(connection=c)}
        except sqlite3.OperationalError as exc:
            out.update(unmatched=ids, error="operation_binding_unavailable: " + str(exc))
            return out
        for qid in ids:
            row = snapshots.get(qid)
            if row is None:
                out["unmatched"].append(qid)
                continue
            mid = _reconcile_observation(row, c, root=root, dry_run=dry_run)
            if not mid:
                out["unmatched"].append(qid)
                continue
            out["fixed"].append({"queue_id": qid, "msg_id": mid})
    except Exception as exc:
        c.rollback()
        warn("receipts.backfill_failed", err=repr(exc)[:160])
        out["error"] = repr(exc)[:160]
    finally:
        if own:
            c.close()
    return out


UNVERIFIED: str = "unverified"


def mark_unverified(*, conn: Any = None, db: Any = None, dry_run: bool = True,
                    limit: int = 500, note: str = "") -> dict:
    """把「state=sent 但证不出来」的历史行落成 unverified（2026-11-04 主人拍板：回写）。

    只动 `state`，原 `why` 保留、后面追加一句为什么被降级；判据与 `verify_row()` 完全一致
    （三层：sent + msg_id + 该 id 真在 messages 里）。`state='unverified'` 不会被
    `db/world_db.py::claim_next()` 取走（它只认 `state='pending'`），所以这个状态是**终局**。
    """
    stamp = str(note or time.strftime("%Y-%m-%d"))
    out: dict = {"seen": 0, "marked": [], "kept": [], "unresolved": [], "changed_during_check": [], "dry_run": bool(dry_run)}
    c, own = _connect_world(conn)
    try:
        cur = c.execute(
            "select * from outbound_queue "
            "where state = 'sent' order by id limit ?", (max(1, int(limit)),))
        rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
        out["seen"] = len(rows)
        for row in rows:
            qid, ack, why = row["id"], row["ack"], row["why"]
            got = verify_row(row, db=db)
            if got.verification_error:
                out["unresolved"].append({"queue_id": int(qid), "why": got.verification_error})
                continue
            if got.verified:
                out["kept"].append(int(qid))
                continue
            if dry_run:
                out["marked"].append(int(qid))
                continue
            add = "回读不到凭据（%s 回写：%s）" % (stamp, got.why)
            why2 = ("%s｜%s" % (str(why or ""), add)) if str(why or "").strip() else add
            changed = c.execute("update outbound_queue set state=?,why=?,updated_ts=? where id=? and state='sent' and coalesce(ack,'')=? and coalesce(why,'')=?",
                      (UNVERIFIED, why2[:400], float(time.time()), int(qid), str(ack or ""), str(why or "")))
            if changed.rowcount:
                out["marked"].append(int(qid))
            else:
                out["changed_during_check"].append(int(qid))
        if out["marked"] and not dry_run:
            c.commit()
    except Exception as exc:  # noqa: BLE001 - 回写失败要留痕，不许假装改过了
        warn("receipts.mark_unverified_failed", err=repr(exc)[:160])
        out["error"] = repr(exc)[:160]
        return out
    finally:
        if own:
            try:
                c.close()
            except Exception as exc:  # noqa: BLE001 - 关连接失败不影响结果，但要留痕
                warn("receipts.close_failed", where="mark_unverified", err=repr(exc)[:80])
    return out


def resend(qid: Any, *, conn: Any = None, now: Any = None,
           enqueue: Any = None) -> dict:
    """兼容旧追认调用；缺少送达凭据时明确拒绝自动重发。"""
    from db import world_db as _w

    now = float(now or time.time())
    row = read_queue_row(qid, conn=conn)
    if not row:
        return {"ok": False, "why": "not_found", "old": int(qid or 0)}
    chat = str(row.get("chat_id") or "")
    state = str(row.get("state") or "")
    if state not in ("sent", UNVERIFIED):
        return {"ok": False, "why": "not_settled", "old": int(qid), "state": state}
    raw_payload = row.get("payload")
    marks = raw_payload if raw_payload is not None else {}
    if isinstance(marks, (str, bytes)):
        try:
            marks = json.loads(marks or "{}")
        except Exception as exc:  # noqa: BLE001 - 读不懂就不敢重发（可能已经重发过）
            warn("receipts.resend_payload_unreadable", qid=int(qid), err=repr(exc)[:80])
            return {"ok": False, "why": "payload_unreadable", "old": int(qid)}
    if not isinstance(marks, dict):
        marks = {}
    text = str(marks.get("text") or "")
    if not text.strip():
        return {"ok": False, "why": "no_text", "old": int(qid)}
    if marks.get("retry_of"):
        return {"ok": False, "why": "already_retried", "old": int(qid),
                "retry_of": int(marks.get("retry_of") or 0)}

    # 缺失本地凭据不是未发送证明；此入口不再具备创建新外发任务的能力。
    return {"ok": False, "why": "delivery_uncertain", "old": int(qid)}


def reverse_check(*, conn: Any = None, db: Any = None, limit: int = 500) -> dict:
    """反向核对：账上说「有凭据」的那 46 条，微信里那条消息真的就是这句话吗。

    2026-11-05（§29.5 第 1 条）：`verify_row` 只验三层（state / why 里的 msg_id / 这条 id 在不在），
    **不验内容**。E-47 那种「两条队列行认领同一个 msg_id」当时就是靠人眼发现的；现在补一层：
    把 `messages` 里那条的正文和会话，与队列行的 `payload.text` / `chat_id` 对上。

    只读，不改任何行。返回 `{seen, checked, ok, no_msg_id, problems:[{queue_id, chat_id, msg_id, kind, payload_head, message_head}]}`，
    `kind` ∈ `text_mismatch` / `chat_mismatch` / `msg_missing`。
    """
    rows: list = []
    try:
        c, _own = _connect_world(conn)
        rows = list(c.execute(
            "select id, chat_id, state, ack, why, payload from outbound_queue "
            "where state = 'sent' order by id limit ?", (int(limit),)))
    except Exception as exc:  # noqa: BLE001 - 读不到库要说出来，不许当成「全对」
        warn("receipts.reverse_check_failed", err=repr(exc)[:160])
        return {"seen": 0, "checked": 0, "ok": 0, "no_msg_id": 0,
                "problems": [], "error": repr(exc)[:160]}
    out = {"seen": len(rows), "checked": 0, "ok": 0, "no_msg_id": 0, "problems": []}
    for r in rows:
        why = str(_cell(r, "why") or "")
        mid = parse_msg_id(why)
        if not mid:
            out["no_msg_id"] += 1
            continue
        out["checked"] += 1
        payload = _cell(r, "payload")
        chat = str(_cell(r, "chat_id") or "")
        want = _norm(_payload_text(payload))
        got, err = _fetch_message(mid, db=db)
        if err:
            out["problems"].append({"queue_id": _qid(r), "chat_id": chat, "msg_id": mid,
                                    "kind": "msg_missing", "payload_head": want[:40],
                                    "message_head": "<读不到：%s>" % err[:60]})
            continue
        if got is None:
            out["problems"].append({"queue_id": _qid(r), "chat_id": chat, "msg_id": mid,
                                    "kind": "msg_missing", "payload_head": want[:40],
                                    "message_head": ""})
            continue
        real_chat = str(got.get("chat_id") or "")
        real_text = _norm(got.get("content"))
        if real_chat and chat and real_chat != chat:
            out["problems"].append({"queue_id": _qid(r), "chat_id": chat, "msg_id": mid,
                                    "kind": "chat_mismatch", "payload_head": want[:40],
                                    "message_head": real_text[:40]})
            continue
        if want and real_text != want:
            out["problems"].append({"queue_id": _qid(r), "chat_id": chat, "msg_id": mid,
                                    "kind": "text_mismatch", "payload_head": want[:40],
                                    "message_head": real_text[:40]})
            continue
        out["ok"] += 1
    if out["problems"]:
        warn("receipts.reverse_check_problems", n=len(out["problems"]),
             kinds=sorted({p["kind"] for p in out["problems"]}))
    return out


#: `outbound_queue` 里 reverse_check 取的列顺序（tuple 行要靠它按下标取）。
_RC_COLS = ("id", "chat_id", "state", "ack", "why", "payload")


def _cell(row: Any, key: str) -> Any:
    """从一行里取一列：dict / sqlite3.Row 按名字，裸 tuple 按位置。

    2026-11-05：第一版只处理了 dict，测试里那个连接没设 row_factory，直接
    `TypeError: tuple indices must be integers` —— 三种行形态都得能读。
    这里不用 try/except 兜：先看形态再取值，取不到就是 None（不静默吞异常）。
    """
    if hasattr(row, "keys"):  # dict 或 sqlite3.Row
        return row[key] if key in row.keys() else None
    if isinstance(row, (tuple, list)) and key in _RC_COLS:
        n = _RC_COLS.index(key)
        if 0 <= n < len(row):
            return row[n]
    return None



def _qid(row: Any) -> int:
    try:
        return int(_cell(row, "id") or 0)
    except Exception:  # noqa: BLE001
        return 0


def _fetch_message(msg_id: str, *, db: Any = None) -> tuple:
    """按 msg_id 取 `(dict | None, 错误串)`。读不到要能区分「没有这条」和「库打不开」。"""
    path = db if db is not None else _account_db()
    if not path or not os.path.exists(str(path)):
        return None, "account.db 不在"
    try:
        c = _ro(str(path))
    except Exception as exc:  # noqa: BLE001
        return None, repr(exc)[:80]
    try:
        row = c.execute("select id, chat_id, content from messages where cast(id as text) = ?",
                        (str(msg_id),)).fetchone()
    except Exception as exc:  # noqa: BLE001
        return None, repr(exc)[:80]
    finally:
        try:
            c.close()
        except Exception as exc:  # noqa: BLE001
            warn("receipts.close_failed", where="_fetch_message", err=repr(exc)[:120])
    if row is None:
        return None, ""
    if isinstance(row, dict) or hasattr(row, "keys"):
        got = {"id": str(row["id"]), "chat_id": str(row["chat_id"] or ""),
               "content": str(row["content"] or "")}
    else:  # 裸 tuple：列顺序就是上面 select 里写的 id, chat_id, content
        got = {"id": str(row[0]), "chat_id": str(row[1] or ""),
               "content": str(row[2] or "")}
    return got, ""


#: 「卡住」的那些行，按正文回查时要往回看多久（秒）。追认线程 `_CONFIRM_DELAYS` 合计
#: 465 秒，卡住的门槛是 600 秒，所以往回看一小时足够覆盖「其实早就到了，只是没读到 id」。
STUCK_LOOKBACK_S = 3600.0


def find_by_text(chat_id: str, text: str, *, since_s: float = STUCK_LOOKBACK_S,
                 db: Any = None) -> tuple:
    """按正文回查「这条到底发出去没有」→ `(dict | None, 错误串)`。

    2026-11-05（§29.5 第 2 条尾巴）：卡住的行没有 msg_id，三层判据帮不上忙；唯一的办法是
    拿原话去 `messages` 里找同会话、近一小时内、自己发的、正文相同的那条。
    找到 = 它其实到了（追认只是没读到 id）；找不到 = 才轮到重发。

    只读；`db` 可注入（测试给临时 account.db）。
    """
    want = _norm(text)
    if not want:
        return None, "no_text"
    path = db if db is not None else _account_db()
    if not path or not os.path.exists(str(path)):
        return None, "account.db 不在"
    try:
        c = _ro(str(path))
    except Exception as exc:  # noqa: BLE001 - 打不开要说出来，不许当成「没发」
        return None, repr(exc)[:80]
    try:
        back = "-%d seconds" % int(max(60.0, float(since_s)))
        rows = c.execute(
            "select id, chat_id, content from messages "
            "where chat_id = ? and ifnull(is_self, 0) = 1 "
            "and created_at >= datetime('now', 'localtime', ?) "
            "order by id desc limit 20", (str(chat_id), back)).fetchall()
    except Exception as exc:  # noqa: BLE001 - 查询失败＝没读到，不是「没有」
        return None, repr(exc)[:80]
    finally:
        try:
            c.close()
        except Exception as exc:  # noqa: BLE001 - 关连接失败不影响判据，但要留痕
            warn("receipts.close_failed", where="find_by_text", err=repr(exc)[:80])
    for row in rows:
        if isinstance(row, dict) or hasattr(row, "keys"):
            mid = str(row["id"])
            ch = str(row["chat_id"] or "")
            content = str(row["content"] or "")
        else:  # 裸 tuple：列顺序就是 select 里的 id, chat_id, content
            mid, ch, content = str(row[0]), str(row[1] or ""), str(row[2] or "")
        if _norm(content) == want:
            return {"id": mid, "chat_id": ch, "content": content}, ""
    return None, ""


def settle_stuck(qid: Any, *, conn: Any = None, db: Any = None, now: Any = None,
                 confirm: Any = None, enqueue: Any = None, root: Any = None) -> dict:
    """卡住巡检只核对已绑定的本地观察，不重发、不按时间窗补造确认。"""
    from db import world_db as world
    row = read_queue_row(qid, conn=conn)
    if not row:
        return {"ok": False, "action": "", "why": "not_found", "old": int(qid or 0)}
    qid = int(row.get("id") or qid)
    if row.get("state") not in ("sent", UNVERIFIED):
        return {"ok": False, "action": "", "why": "not_settled", "old": qid, "state": row.get("state")}
    c, own = _connect_world(conn)
    try:
        try:
            snapshot = next((r for r in world.pending_outbound_observations(connection=c) if int(r["queue_id"]) == qid), None)
        except sqlite3.OperationalError:
            snapshot = None
        if snapshot is None:
            return {"ok": False, "action": "", "why": "operation_binding_missing", "old": qid}
        mid = _reconcile_observation(snapshot, c, root=root)
        if not mid:
            return {"ok": False, "action": "", "why": "delivery_uncertain", "old": qid}
        return {"ok": True, "action": "already_there", "why": "", "old": qid, "msg_id": mid}
    except Exception as exc:
        c.rollback()
        warn("receipts.settle_lookup_failed", qid=qid, err=repr(exc)[:120])
        return {"ok": False, "action": "", "why": "lookup_failed", "old": qid, "error": repr(exc)[:120]}
    finally:
        if own:
            c.close()


def summary(*, days: float = 7.0, conn: Any = None, db: Any = None, now: Any = None) -> dict:
    """一段时间里「说发出去」的行到底有多少有凭据（§28；周报与审计共用这一处口径）。

    分桶：`verified`（三层都过）/ `unverified`（还写着 sent 却证不出来）/ `rewritten`
    （已回写 unverified）/ `pending`（还在路上）/ `not_sent`（failed 等）。
    `rate` 的分母是 `verified + unverified + rewritten` —— 回写之后不能拿 `sent` 当分母，
    否则会报 100%。
    """
    now = time.time() if now is None else float(now)
    since = now - max(1.0, float(days or 1.0)) * 86400.0
    out = {"days": float(days or 0.0), "total": 0, "claimed": 0, "verified": 0,
           "unverified": 0, "rewritten": 0, "pending": 0, "not_sent": 0, "rate": 0.0}
    c, own = _connect_world(conn)
    try:
        cur = c.execute(
            "select * from outbound_queue "
            "where coalesce(updated_ts, created_ts, 0) >= ? order by id", (since,))
        rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
        out["total"] = len(rows)
        for row in rows:
            rec = verify_row(row, db=db)
            st = str(row["state"] or "")
            if rec.verified:
                out["verified"] += 1
            elif st in ("pending", "sending"):
                out["pending"] += 1
            elif st == "sent":
                out["unverified"] += 1
            elif st == UNVERIFIED:
                out["rewritten"] += 1
            else:
                out["not_sent"] += 1
        out["claimed"] = out["verified"] + out["unverified"] + out["rewritten"]
        out["rate"] = (float(out["verified"]) / out["claimed"]) if out["claimed"] else 0.0
    except Exception as exc:  # noqa: BLE001 - 汇总失败要留痕，不许假装"这周 0 条"
        warn("receipts.summary_failed", err=repr(exc)[:160])
        out["error"] = repr(exc)[:160]
        return out
    finally:
        if own:
            try:
                c.close()
            except Exception as exc:  # noqa: BLE001 - 关连接失败不影响结果，但要留痕
                warn("receipts.close_failed", where="summary", err=repr(exc)[:80])
    return out


def requeue_failed(qid: Any, *, conn: Any = None, now: Any = None) -> dict:
    """兼容旧调用；终局失败不能绕过原队列证据与次数策略创建新任务。"""
    from db import world_db as _w

    now = float(now or time.time())
    row = read_queue_row(qid, conn=conn)
    if not row:
        return {"ok": False, "why": "not_found", "old": int(qid or 0)}
    chat = str(row.get("chat_id") or "")
    state = str(row.get("state") or "")
    if state != "failed":
        return {"ok": False, "why": "not_failed", "old": int(qid), "state": state}
    raw_payload = row.get("payload")
    marks = raw_payload if raw_payload is not None else {}
    if isinstance(marks, (str, bytes)):
        try:
            marks = json.loads(marks or "{}")
        except Exception as exc:  # noqa: BLE001 - 读不懂就不敢补发（可能已经补过）
            warn("receipts.requeue_payload_unreadable", qid=int(qid), err=repr(exc)[:80])
            return {"ok": False, "why": "payload_unreadable", "old": int(qid)}
    if not isinstance(marks, dict):
        marks = {}
    text = str(marks.get("text") or "")
    if not text.strip():
        return {"ok": False, "why": "no_text", "old": int(qid)}
    if marks.get("retry_of"):
        return {"ok": False, "why": "already_retried", "old": int(qid),
                "retry_of": int(marks.get("retry_of") or 0)}

    # failed 不是历史任务未发送的证明；也不能新建任务绕过原次数上限。
    return {"ok": False, "why": "retry_policy_exhausted", "old": int(qid)}


__all__ = ["BACKFILL_WINDOW_S", "MSG_PREFIX", "Receipt", "STUCK_LOOKBACK_S", "UNVERIFIED", "backfill",
           "find_by_text", "judge", "mark_unverified", "msg_in_db", "parse_msg_id", "read_queue_row",
           "requeue_failed", "resend", "reverse_check", "settle_stuck", "summary", "verify", "verify_row"]
