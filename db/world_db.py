"""世界账本：收到的消息、发出的话，各一张表。

2026-10-28 建立。目的：让"她看到的"和"真实世界"共用一本账，
不再靠 ±90 秒时间窗去猜"这句是不是我发的""到底到了没有"。

约定：
- 时间一律存 UTC ISO（`YYYY-MM-DDTHH:MM:SS`），展示时再转本地。
- inbound 的唯一键是 (platform, account_id, msg_id)；msg_id 用源库自增 id 拼。
- outbound 的唯一键是 (turn_id, seq)，text_hash 用来回查"这条是不是我发的"。
"""
from __future__ import annotations

import hashlib
import contextlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from safelog import warn
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "data", "world.db")
_LOCK = threading.RLock()
_MEM = {"fresh": 0.0, "at": 0.0}

_SCHEMA = [
    """create table if not exists outbound_observation_source(
        queue_id integer not null, attempt_no integer not null, source_token text not null,
        primary key(queue_id,attempt_no))""",

    """create table if not exists outbound_observation(
        queue_id integer not null, attempt_no integer not null,
        platform text not null, account_id text not null, chat_id text not null,
        text_hash text not null, baseline integer not null,
        receipt_id integer not null default 0,
        primary key(queue_id,attempt_no))""",

    """create table if not exists inbound_event(
        id integer primary key autoincrement,
        platform text not null, account_id text not null, chat_id text not null,
        msg_id text not null, ts_utc text not null,
        who text default '', who_id text default '',
        is_self integer not null default 0, media_path text default '',
        quote_user text default '', quote_id text default '',
        platform_msg_id text default '',
        kind text default 'text', text text default '',
        media_md5 text default '', src text default '',
        unique(platform, account_id, msg_id))""",
    """create table if not exists outbound_receipt(
        id integer primary key autoincrement,
        turn_id text default '', seq integer default 0,
        platform text not null, account_id text default '', chat_id text not null,
        text text default '', text_hash text not null,
        sent_at_utc text not null, method text default '', ack text default '',
        msg_id text default '', confirmed_at text default '',
        unique(turn_id, seq))""",
    "create index if not exists ix_in_ts on inbound_event(ts_utc)",
    "create index if not exists ix_in_chat on inbound_event(chat_id, ts_utc)",
    "create index if not exists ix_in_acct_chat_ts on inbound_event(account_id, chat_id, ts_utc)",
    "create index if not exists ix_out_hash on outbound_receipt(chat_id, text_hash)",
    "create table if not exists meta(k text primary key, v text)",
    # ---- 2026-10-28 投递队列：一张表只回答"要不要送、送到没" ----
    # 设计要点：
    #  · 幂等靠 idem_key（group_id|seq），不靠正文 hash —— 改一个字不该变成新动作
    #  · 时间一律 epoch REAL：只做算术，不存本地时间，杜绝时区/跨零点歧义
    #  · 六态：pending → sending → sent|failed|expired；组内连带 skipped
    #  · owner + heartbeat_at 判"进程还活着"，比 started_at 可靠
    #  · payload 存 JSON（text/image/file 字段不同），另存 preview/text_hash 供面板与对账
    """create table if not exists outbound_queue(
        id integer primary key autoincrement,
        idem_key text not null unique,
        group_id text not null default '',
        seq integer not null default 0,
        turn_id text not null default '',
        platform text not null default 'wechat',
        account_id text not null default '',
        chat_id text not null,
        kind text not null default 'text',
        payload text not null default '{}',
        preview text not null default '',
        text_hash text not null default '',
        state text not null default 'pending',
        attempts integer not null default 0,
        max_attempts integer not null default 50,
        priority integer not null default 0,
        next_at real not null default 0,
        not_after real not null default 0,
        owner text not null default '',
        claimed_at real not null default 0,
        heartbeat_at real not null default 0,
        ack text not null default '',
        why text not null default '',
        created_ts real not null default 0,
        updated_ts real not null default 0,
        settled_ts real not null default 0)""",
    # 每次尝试一行，append-only：今天的教训是"宽表会把线索盖掉"
    """create table if not exists outbound_attempt(
        id integer primary key autoincrement,
        queue_id integer not null,
        attempt_no integer not null default 1,
        owner text not null default '',
        started_ts real not null default 0,
        ended_ts real not null default 0,
        result text not null default '',
        ack text not null default '',
        why text not null default '',
        probe text not null default '',
        unique(queue_id, attempt_no))""",
    "create index if not exists ix_q_state on outbound_queue(state, next_at)",
    "create index if not exists ix_q_group on outbound_queue(group_id, seq)",
    "create index if not exists ix_q_chat on outbound_queue(chat_id, state)",
    "create index if not exists ix_a_qid on outbound_attempt(queue_id)",
]

# `create table if not exists` 不会给已有库补列；这里做可重复的增量迁移。
_INBOUND_ADDITIONS = (
    ("who_id", "text default ''"),
    ("is_self", "integer not null default 0"),
    ("media_path", "text default ''"),
    ("quote_user", "text default ''"),
    ("quote_id", "text default ''"),
    ("platform_msg_id", "text default ''"),
)


def _ensure_inbound_columns(c: sqlite3.Connection) -> None:
    cols = {str(r[1]) for r in c.execute("pragma table_info(inbound_event)")}
    for name, decl in _INBOUND_ADDITIONS:
        if name not in cols:
            c.execute("alter table inbound_event add column %s %s" % (name, decl))


_READY = [False]


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def to_utc(text: str) -> str:
    """把本地时间字符串（`2026-10-28 11:22:33` / `10-28 11:22`）转成 UTC ISO。失败返回空串。"""
    s = str(text or "").strip()
    if not s:
        return ""
    s = s.replace("Z", "").replace("T", " ")
    if len(s) > 19 and s[10] == " ":  # 2026-10-28 05:27:18.123456 → 截到秒（微信 created_at 带微秒）
        s = s[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
        except Exception:
            continue
        if fmt == "%m-%d %H:%M":
            continue
        return (dt - timedelta(hours=_tz_off())).strftime("%Y-%m-%dT%H:%M:%S")
    try:  # 10-28 11:22（缺年份）
        dt = datetime.strptime("%d-%s" % (datetime.now().year, s), "%Y-%m-%d %H:%M")
        return (dt - timedelta(hours=_tz_off())).strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return ""


def _tz_off() -> int:
    """本地时区偏移（小时，东八区返回 +8）。

    2026-11-02：这里原来带一个负号，于是 `to_utc` 变成「加 8 小时」、`to_local` 变成「减 8 小时」，
    两个方向同时反。后果是 `ts_utc` 全落在未来 → `freshness()` 把"世界有多旧"永远算成 0 →
    引擎以为世界新鲜、不去补同步；玩家看到的红点（世界落后 88 分钟）才是真相。
    """
    return int((datetime.now().astimezone().utcoffset() or timedelta()).total_seconds() // 3600)


def to_local(ts_utc: str, fmt: str = "%m-%d %H:%M") -> str:
    s = str(ts_utc or "").strip()
    if not s:
        return ""
    try:
        dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S") + timedelta(hours=_tz_off())
    except Exception:
        return s
    return dt.strftime(fmt)


def text_hash(text: str) -> str:
    return hashlib.md5(str(text or "").strip().encode("utf-8")).hexdigest()[:16]


def conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    c = sqlite3.connect(PATH, timeout=6.0)
    c.row_factory = sqlite3.Row
    c.execute("pragma journal_mode=wal")
    c.execute("pragma synchronous=normal")
    if not _READY[0]:
        try:
            with _LOCK:
                if not _READY[0]:
                    with c:
                        c.execute("begin immediate")
                        for stmt in _SCHEMA:
                            c.execute(stmt)
                        _ensure_inbound_columns(c)
                        c.execute("insert or ignore into meta(k, v) values('installed_at', ?)", (utcnow(),))
                    _READY[0] = True
        except sqlite3.Error:
            c.close()
            raise
    return c


def installed_at() -> str:
    try:
        with _LOCK, conn() as c:
            row = c.execute("select v from meta where k='installed_at'").fetchone()
        return str(row["v"] if row else "")
    except Exception:
        return ""


# ---------------- 写入 ----------------

def record_inbound(platform: str, account_id: str, chat_id: str, msg_id: str, ts_local: str,
                   who: str = "", who_id: str = "", kind: str = "text", text: str = "",
                   media_md5: str = "", src: str = "", is_self: int = 0,
                   media_path: str = "", platform_msg_id: str = "",
                   quote_user: str = "", quote_id: str = "") -> bool:
    ts = to_utc(ts_local) or utcnow()
    try:
        with _LOCK, conn() as c:
            cur = c.execute(
                _INSERT_INBOUND,
                (platform, account_id, chat_id, msg_id, ts, who, who_id, kind,
                 str(text or "")[:4000], media_md5, src, int(is_self or 0),
                 str(media_path or "")[:400], str(platform_msg_id or ""),
                 str(quote_user or ""), str(quote_id or "")))
            c.commit()
            return bool(cur.rowcount)
    except Exception:
        return False


def record_outbound(chat_id: str, text: str, *, platform: str = "wechat", account_id: str = "",
                    turn_id: str = "", method: str = "", ack: str = "", seq: int | None = None) -> int:
    """她真的按下回车了 → 记一行。返回行 id（失败 0）。"""
    # 2026-10-28：chat_id 参数可能传的是显示名（「文件传输助手」「示例作业群」），
    # 而 messages/回读用的是真 chat_id。这里统一解析成 chat_id，解析不到就原样存。
    try:
        if chat_id and ("@" not in str(chat_id)) and (not str(chat_id).startswith(("wxid_", "filehelper"))):
            from core.execution import chat_alias as _ca
            chat_id = str(_ca.key_of(str(chat_id)) or chat_id)
    except Exception:
        pass
    try:
        with _LOCK, conn() as c:
            c.execute("begin immediate")
            if seq is None:
                row = c.execute("select coalesce(max(seq), -1) as m from outbound_receipt where turn_id=?",
                                (turn_id,)).fetchone()
                seq = int(row["m"]) + 1
            previous = c.execute("select * from outbound_receipt where turn_id=? and seq=?", (turn_id, int(seq))).fetchone()
            if previous is not None:
                if (previous["platform"], previous["account_id"], previous["chat_id"], previous["text"]) != (platform, account_id, chat_id, str(text or "")):
                    warn("outbound_receipt_operation_conflict", turn_id=turn_id, seq=seq, receipt_id=previous["id"])
                    return 0
                return int(previous["id"])
            cur = c.execute(
                "insert into outbound_receipt(turn_id, seq, platform, account_id, chat_id,"
                " text, text_hash, sent_at_utc, method, ack) values(?,?,?,?,?,?,?,?,?,?)",
                (turn_id, int(seq), platform, account_id, chat_id, str(text or ""),
                 text_hash(text), utcnow(), method, ack))
            c.commit()
            return int(cur.lastrowid or 0)
    except Exception as exc:
        warn("outbound_receipt_record_failed", turn_id=turn_id, seq=seq, error=repr(exc)[:200])
        return 0


def _require_observation_claim(c, row, owner, attempt_no):
    if row is None or row["state"] != "sending" or not owner or int(attempt_no) <= 0 or row["owner"] != owner or int(row["attempts"]) != int(attempt_no):
        raise ValueError("observation_claim_mismatch")
    if c.execute("select 1 from outbound_attempt where queue_id=? and attempt_no=? and owner=? and result='running'",
                 (int(row["id"]), int(attempt_no), owner)).fetchone() is None:
        raise ValueError("observation_attempt_missing")


def save_outbound_observation(qid: int, baseline: int, *, owner: str, attempt_no: int, source_token: str = "") -> None:
    """发送前保存当前认领尝试的不可变游标；不猜账号或旧尝试。"""
    if baseline is None or int(baseline) < 0:
        raise ValueError("observation_baseline_missing")
    with _LOCK, _open() as c:
        c.execute("begin immediate")
        row = c.execute("select * from outbound_queue where id=?", (int(qid),)).fetchone()
        if row is None or row["state"] != "sending" or not all((row["platform"], row["account_id"], row["chat_id"])):
            raise ValueError("observation_source_missing")
        _require_observation_claim(c, row, owner, attempt_no)
        values = (int(qid), int(row["attempts"]), row["platform"], row["account_id"], row["chat_id"], row["text_hash"], int(baseline))
        previous = c.execute("select queue_id,attempt_no,platform,account_id,chat_id,text_hash,baseline from outbound_observation where queue_id=? and attempt_no=?", values[:2]).fetchone()
        if previous is not None and tuple(previous) != values:
            raise ValueError("observation_snapshot_conflict")
        c.execute("insert or ignore into outbound_observation(queue_id,attempt_no,platform,account_id,chat_id,text_hash,baseline) values(?,?,?,?,?,?,?)", values)
        previous_source = c.execute("select source_token from outbound_observation_source where queue_id=? and attempt_no=?", values[:2]).fetchone()
        if previous is not None and previous_source is None and source_token:
            raise ValueError("observation_legacy_source_missing")
        if previous_source is not None and previous_source[0] != source_token:
            raise ValueError("observation_source_conflict")
        if source_token:
            decoded = json.loads(source_token)
            if not isinstance(decoded, list) or len(decoded) not in (3, 4) or decoded[1] != int(baseline):
                raise ValueError("observation_source_invalid")
            c.execute("insert or ignore into outbound_observation_source(queue_id,attempt_no,source_token) values(?,?,?)", (int(qid), int(attempt_no), source_token))
        c.commit()


def bind_observation_receipt(qid: int, receipt_id: int, *, owner: str, attempt_no: int) -> None:
    """把本次发送账目关联到保存的来源；不能改绑另一操作。"""
    with _LOCK, _open() as c:
        c.execute("begin immediate")
        claimed = c.execute("select * from outbound_queue where id=?", (int(qid),)).fetchone()
        _require_observation_claim(c, claimed, owner, attempt_no)
        row = c.execute("select o.* from outbound_observation o join outbound_queue q on q.id=o.queue_id and q.attempts=o.attempt_no where q.id=?", (int(qid),)).fetchone()
        receipt = c.execute("select * from outbound_receipt where id=?", (int(receipt_id),)).fetchone()
        if row is None or receipt is None or int(receipt["seq"]) != int(attempt_no) or receipt["turn_id"] != "queue:%d" % int(qid) or any(row[k] != receipt[k] for k in ("platform", "account_id", "chat_id", "text_hash")):
            raise ValueError("observation_receipt_mismatch")
        if row["receipt_id"] not in (0, int(receipt_id)):
            raise ValueError("observation_receipt_conflict")
        c.execute("update outbound_observation set receipt_id=? where queue_id=? and attempt_no=?", (int(receipt_id), int(qid), row["attempt_no"]))
        c.commit()


def pending_outbound_observations(*, connection=None) -> list[dict]:
    """恢复只读追认的来源快照；不完整关联不推测补齐。"""
    with _LOCK, (contextlib.nullcontext(connection) if connection is not None else _read_conn()) as c:
        rows = c.execute("select o.*,r.text,q.created_ts as queue_created_ts,q.ack as queue_ack,coalesce(s.source_token,'') as source_token from outbound_observation o "
            "join outbound_queue q on q.id=o.queue_id and q.attempts=o.attempt_no "
            "join outbound_receipt r on r.id=o.receipt_id left join outbound_observation_source s on s.queue_id=o.queue_id and s.attempt_no=o.attempt_no "
            "where q.state in ('sent','unverified') and q.ack<>'confirmed' "
            "and r.turn_id='queue:' || q.id and r.seq=o.attempt_no "
            "and q.platform=o.platform and q.account_id=o.account_id and q.chat_id=o.chat_id and q.text_hash=o.text_hash "
            "and r.platform=o.platform and r.account_id=o.account_id and r.chat_id=o.chat_id and r.text_hash=o.text_hash "
            "order by o.queue_id").fetchall()
    return [dict(row) for row in rows]


def confirm_outbound(chat_id: str, text: str, *, msg_id: str = "", ack: str = "confirmed",
                     confirmed_at: str = "", receipt_id: int = 0,
                     platform: str = "", account_id: str = "", queue_id: int = 0, attempt_no: int = 0, connection=None) -> int:
    """本地回读凭据精确绑定既有发送回执；不按同文最新一条猜归属。"""
    if not all((receipt_id, platform, account_id, chat_id, msg_id)) or ack != "confirmed":
        raise ValueError("outbound_confirmation_source_missing")
    if queue_id and int(attempt_no) <= 0:
        raise ValueError("outbound_confirmation_attempt_missing")
    with _LOCK, (contextlib.nullcontext(connection) if connection is not None else _open()) as c:
        c.execute("begin immediate")
        row = c.execute("select * from outbound_receipt where id=?", (int(receipt_id),)).fetchone()
        if row is None:
            raise ValueError("outbound_confirmation_receipt_missing")
        if (row["platform"], row["account_id"], row["chat_id"], row["text_hash"]) != (platform, account_id, chat_id, text_hash(text)):
            raise ValueError("outbound_confirmation_source_mismatch")
        if queue_id:
            queued = c.execute("select * from outbound_queue where id=?", (int(queue_id),)).fetchone()
            if attempt_no and queued is not None and int(queued["attempts"]) != int(attempt_no):
                raise ValueError("outbound_confirmation_attempt_changed")
            if queued is None or row["turn_id"] != "queue:%d" % int(queue_id) or (queued["platform"], queued["account_id"], queued["chat_id"], queued["text_hash"]) != (platform, account_id, chat_id, text_hash(text)):
                raise ValueError("outbound_confirmation_queue_mismatch")
        if queue_id and int(row["seq"]) != int(attempt_no):
            raise ValueError("outbound_confirmation_receipt_attempt_mismatch")
        already_confirmed = row["ack"] == "confirmed"
        if already_confirmed and str(row["msg_id"] or "") != str(msg_id):
            raise ValueError("outbound_confirmation_evidence_conflict")
        if queue_id and queued["ack"] == "confirmed" and queued["why"] != "msg " + str(msg_id):
            raise ValueError("outbound_confirmation_queue_conflict")
        used = c.execute("select id from outbound_receipt where platform=? and account_id=? and msg_id=? and id<>? limit 1",
                         (platform, account_id, str(msg_id), int(receipt_id))).fetchone()
        if used is not None:
            raise ValueError("outbound_confirmation_evidence_claimed")
        changed = 0
        if not already_confirmed:
            cur = c.execute("update outbound_receipt set ack=?,msg_id=?,confirmed_at=? where id=?",
                            (ack, str(msg_id), confirmed_at or utcnow(), int(receipt_id)))
            changed += int(cur.rowcount)
        if queue_id:
            cur = c.execute("update outbound_queue set ack='confirmed',why=? where id=? and (ack<>'confirmed' or why<>?)",
                            ("msg " + str(msg_id), int(queue_id), "msg " + str(msg_id)))
            changed += int(cur.rowcount)
        c.commit()
        return int(changed > 0)


# ---------------- 读取 ----------------

def by_text(chat_id: str, text: str, *, window_s: int = 180) -> dict | None:
    """这句是不是她发的？按 text_hash 命中最近 window_s 内的发出记录。"""
    # 2026-10-28：文件消息在微信库里 content 只有文件名，而账本里记的是「文件：xxx」，
    # 所以这里同时试原文与「文件：」前缀两种 hash，文件也能被认成「我发的」。
    hs = []
    for _t in (text, "文件：" + str(text or "")):
        _h = text_hash(_t)
        if _h and _h not in hs:
            hs.append(_h)
    if not hs:
        return None
    since = (datetime.now(timezone.utc) - timedelta(seconds=int(window_s))).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        _marks = ",".join("?" * len(hs))
        with _LOCK, conn() as c:
            row = c.execute(
                "select * from outbound_receipt where chat_id=? and text_hash in (" + _marks + ") and sent_at_utc >= ?"
                " order by id desc limit 1", tuple([chat_id] + hs + [since])).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def confirmed_self_sent(*, platform: str, account_id: str, chat_id: str, external_msg_id: str, text: str) -> bool:
    """Match the platform message to its exact locally confirmed outbound receipt."""
    if not all((platform, account_id, chat_id, external_msg_id, text)):
        return False
    uri = Path(PATH).resolve().as_uri() + "?mode=ro"
    with _LOCK, contextlib.closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as c:
        c.execute("begin")
        inbound = c.execute(
            "select msg_id,who_id,is_self from inbound_event where platform=? and account_id=?"
            " and chat_id=? and platform_msg_id=? limit 2",
            (platform, account_id, chat_id, external_msg_id),
        ).fetchall()
        if len(inbound) != 1 or inbound[0][1:] != ("self", 1):
            return False
        # The mirror prefixes the source db row ID. The receipt stores that row ID directly.
        prefix = f"{platform}:{account_id}:"
        local_key = str(inbound[0][0])
        local_id = local_key.removeprefix(prefix)
        if local_id == local_key or not local_id.isdecimal():
            return False
        receipts = c.execute(
            "select id from outbound_receipt where platform=? and account_id=? and chat_id=?"
            " and msg_id=? and ack='confirmed' and text_hash=? and text=? limit 2",
            (platform, account_id, chat_id, local_id, text_hash(text), text),
        ).fetchall()
        return len(receipts) == 1


def recent_outbound(chat_id: str = "", *, limit: int = 5) -> list:
    try:
        with _LOCK, conn() as c:
            if chat_id:
                rows = c.execute("select * from outbound_receipt where chat_id=? order by id desc limit ?",
                                 (chat_id, int(limit))).fetchall()
            else:
                rows = c.execute("select * from outbound_receipt order by id desc limit ?",
                                 (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def freshness() -> dict:
    """她手上最新一条消息有多旧（秒）+ 最近一次确认/待确认条数。"""
    try:
        with _LOCK, conn() as c:
            row = c.execute("select max(ts_utc) as m, count(*) as n from inbound_event").fetchone()
            pend = c.execute("select count(*) as n from outbound_receipt where ack <> 'confirmed'").fetchone()
        newest = str(row["m"] or "")
        age = -1.0
        if newest:
            try:
                dt = datetime.strptime(newest[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                age = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
            except Exception:
                age = -1.0
        return {"newest_utc": newest, "age_s": round(age, 1), "n_in": int(row["n"] or 0),
                "n_pending": int(pend["n"] or 0)}
    except Exception:
        return {"newest_utc": "", "age_s": -1.0, "n_in": 0, "n_pending": 0}


# ---------------- 回填：把各账号库里已有的消息抄进来 ----------------

def _accounts() -> list:
    out = []
    base = os.path.join(ROOT, "data", "platforms")
    if not os.path.isdir(base):
        return out
    for plat in sorted(os.listdir(base)):
        pd = os.path.join(base, plat)
        if not os.path.isdir(pd):
            continue
        for acc in sorted(os.listdir(pd)):
            db = os.path.join(pd, acc, "account.db")
            if os.path.isfile(db) and db not in ("", None):
                out.append((plat, acc, db))
    return out


from core.infra import skips  # noqa: E402  (静默跳过要留痕：skips.KINDS)


_INSERT_INBOUND = (
    "insert or ignore into inbound_event(platform, account_id, chat_id, msg_id, ts_utc,"
    " who, who_id, kind, text, media_md5, src, is_self, media_path,"
    " platform_msg_id, quote_user, quote_id)"
    " values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)

# 老行可能早于这些列，或早于同步开始读它们：只补空值，绝不覆盖已有值。
_FILL_INBOUND_METADATA = (
    "update inbound_event set"
    " who_id = coalesce(nullif(who_id,''), :who_id),"
    " platform_msg_id = coalesce(nullif(platform_msg_id,''), :platform_msg_id),"
    " quote_user = coalesce(nullif(quote_user,''), :quote_user),"
    " quote_id = coalesce(nullif(quote_id,''), :quote_id)"
    " where platform=:platform and account_id=:account_id and chat_id=:chat_id and msg_id=:msg_id"
    " and (ifnull(platform_msg_id,'')='' or platform_msg_id=:platform_msg_id)"
    " and (ifnull(quote_id,'')='' or quote_id=:quote_id)"
    " and ((ifnull(who_id,'')='' and :who_id!='')"
    " or (ifnull(platform_msg_id,'')='' and :platform_msg_id!='')"
    " or (ifnull(quote_user,'')='' and :quote_user!='')"
    " or (ifnull(quote_id,'')='' and :quote_id!=''))"
)


def sync_accounts(limit_per_account: int = 4000, throttle_s: float = 2.0) -> dict:
    """增量回填（按 msg_id 去重），节流调用。返回 {added, scanned}。

    2026-10-31 性能修复：以前是「每一行都调一次 record_inbound()」，而 record_inbound
    每次都 `_LOCK + conn()`，conn() 里还有 os.makedirs + 三条 pragma。实测在一次
    build_snapshot 里这个函数扫 4397 行 = 4399 次连接 = **8.1 秒**（占建快照的 96%），
    而她一拍要建两次快照 —— 这就是"回复为什么慢"的主因。
    现在：一条连接、executemany 分块，去重仍交给 unique(platform, account_id, msg_id)。
    """
    now = time.time()
    if now - float(_MEM.get("at") or 0) < float(throttle_s):
        return {"added": 0, "scanned": 0, "skipped": True}
    _MEM["at"] = now
    added = scanned = 0
    pending: list = []
    meta_pending: list = []
    try:
        con = conn()
    except Exception:
        return {"added": 0, "scanned": 0, "skipped": False}
    try:
        for plat, acc, db in _accounts():
            try:
                # Path.as_uri 会转义 #、空格等合法路径字符；手拼 file: 会截断。
                source_uri = Path(db).resolve().as_uri() + "?mode=ro"
                with sqlite3.connect(source_uri, uri=True, timeout=4.0) as src:
                    src.row_factory = sqlite3.Row
                    cols = {r["name"] for r in src.execute("pragma table_info(messages)")}
                    who_col = "sender_name" if "sender_name" in cols else ("sender" if "sender" in cols else "''")
                    kind_col = "msg_type_name" if "msg_type_name" in cols else "'text'"
                    path_col = "ifnull(media_path,'')" if "media_path" in cols else "''"
                    # 2026-11-04：真实发送者 id（wxid）。没有它，「谁在说话」只能靠显示名，
                    # 而显示名改一次就认不出人（E-42/E-44）。老库没这列时退回空串。
                    sid_col = "ifnull(sender_id,'')" if "sender_id" in cols else "''"
                    self_col = "ifnull(is_self,0)" if "is_self" in cols else "0"
                    # 平台自己给的消息 id 与引用元数据：原样读取，缺失时退回空串，不拿本地 id 猜。
                    pmsg_col = "ifnull(msg_id,'')" if "msg_id" in cols else "''"
                    qid_col = "ifnull(quote_reply_id,'')" if "quote_reply_id" in cols else "''"
                    quser_col = "ifnull(quote_reply_user,'')" if "quote_reply_user" in cols else "''"
                    rows = src.execute(
                        "select id, chat_id, content, created_at, %s as is_self, %s as who, %s as kind,"
                        " %s as media_path, %s as sid, %s as platform_msg_id,"
                        " %s as quote_id, %s as quote_user"
                        " from messages order by created_at desc, id desc limit ?"
                        % (self_col, who_col, kind_col, path_col, sid_col, pmsg_col, qid_col, quser_col),
                        (int(limit_per_account),)).fetchall()
                batch = []
                meta_batch = []
                for r in rows:
                    scanned += 1
                    local_mid = "%s:%s:%s" % (plat, acc, r["id"])
                    who_id = str(r["sid"] or "")
                    platform_msg_id = str(r["platform_msg_id"] or "")
                    quote_id = str(r["quote_id"] or "")
                    quote_user = str(r["quote_user"] or "")
                    # 2026-10-28：is_self=1（这个号发出的）也入库，带标记，读的时候显式 where is_self=0
                    batch.append((
                        plat, acc, str(r["chat_id"]), local_mid,
                        to_utc(str(r["created_at"] or "")) or utcnow(),
                        str(r["who"] or ""), who_id, str(r["kind"] or "text"),
                        str(r["content"] or "")[:4000], "", "account.db",
                        int(r["is_self"] or 0), str(r["media_path"] or "")[:400],
                        platform_msg_id, quote_user, quote_id,
                    ))
                    if who_id or platform_msg_id or quote_user or quote_id:
                        meta_batch.append({
                            "who_id": who_id, "platform_msg_id": platform_msg_id,
                            "quote_user": quote_user, "quote_id": quote_id,
                            "platform": plat, "account_id": acc, "chat_id": str(r["chat_id"]),
                            "msg_id": local_mid,
                        })
                pending.extend(batch)
                meta_pending.extend(meta_batch)
            except Exception as exc:  # noqa: BLE001 - 一个账号的库坏了不拖累其余账号
                skips.skip("db/world_db.py:sync_accounts:account", "malformed", repr(exc)[:120])
                continue
        if pending:
            before = con.total_changes
            with _LOCK:
                for _i in range(0, len(pending), 500):
                    con.executemany(_INSERT_INBOUND, pending[_i:_i + 500])
                # 只算真正插入的行；下面补元数据的 UPDATE 不能算成新消息。
                inserted = int(con.total_changes - before)
                for _i in range(0, len(meta_pending), 500):
                    con.executemany(_FILL_INBOUND_METADATA, meta_pending[_i:_i + 500])
                # 2026-10-31：提交只做一次，放在所有账户都扫完之后。
                # 仓库规矩（tests/pytest_style/test_batch_commit_discipline.py）：循环里的 commit
                # 会把"部分成功"变成正常状态；这里靠 unique 幂等，重扫一遍也不会重复插。
                con.commit()
            added += inserted
    finally:
        try:
            con.close()
        except Exception as exc:  # noqa: BLE001 - 关不上也只是句柄晚点回收
            skips.skip("db/world_db.py:sync_accounts:close", "flutter", repr(exc)[:120])
    return {"added": added, "scanned": scanned, "skipped": False}


def backfill_missing_message_metadata(limit_per_account: int = 5000) -> dict:
    """只补 inbound_event 里缺失的平台元数据，幂等；不会在启动时自动调用。

    与 sync_accounts 的差别：sync 只读每个账号库最近 limit_per_account 行，更早的
    历史永远轮不到。这里从 world 库的缺口出发，按源 id 精确回查，只补
    platform_msg_id 为空的行，已有值一律不覆盖。
    """
    scanned = accounts = 0
    try:
        con = conn()
    except Exception:
        return {"scanned": 0, "updated": 0, "accounts": 0}
    before = con.total_changes
    try:
        for plat, acc, db in _accounts():
            try:
                # 只取真正的缺口；已填过平台号的行不重复回查。
                gaps = con.execute(
                    "select platform, account_id, chat_id, msg_id from inbound_event"
                    " where platform=? and account_id=? and ifnull(platform_msg_id,'')=''"
                    " order by id asc limit ?",
                    (plat, acc, int(limit_per_account))).fetchall()
                if not gaps:
                    continue
                accounts += 1
                # world msg_id 形如 platform:account:<source messages.id>。
                wanted = {}
                for g in gaps:
                    scanned += 1
                    try:
                        src_id = int(str(g["msg_id"]).rsplit(":", 1)[1])
                    except Exception as exc:
                        warn("backfill_src_id_unparsed", msg_id=str(g["msg_id"])[:60], err=repr(exc)[:80])
                        continue
                    wanted[src_id] = g
                if not wanted:
                    continue
                # Path.as_uri 会转义 #、空格等合法路径字符；手拼 file: 会截断。
                source_uri = Path(db).resolve().as_uri() + "?mode=ro"
                with contextlib.closing(sqlite3.connect(
                        source_uri, uri=True, timeout=4.0)) as src:
                    src.row_factory = sqlite3.Row
                    cols = {r["name"] for r in src.execute("pragma table_info(messages)")}
                    if not cols or "id" not in cols or "msg_id" not in cols:
                        continue
                    sid_col = "ifnull(sender_id,'')" if "sender_id" in cols else "''"
                    sname_col = "ifnull(sender_name,'')" if "sender_name" in cols else "''"
                    qid_col = "ifnull(quote_reply_id,'')" if "quote_reply_id" in cols else "''"
                    quser_col = "ifnull(quote_reply_user,'')" if "quote_reply_user" in cols else "''"
                    meta = []
                    ids = list(wanted)
                    for i in range(0, len(ids), 500):
                        chunk = ids[i:i + 500]
                        rows = src.execute(
                            "select id, ifnull(msg_id,'') as platform_msg_id, %s as sid,"
                            " %s as sender_name, %s as quote_id, %s as quote_user"
                            " from messages where id in (%s)"
                            % (sid_col, sname_col, qid_col, quser_col,
                               ",".join("?" * len(chunk))),
                            tuple(chunk)).fetchall()
                        for r in rows:
                            g = wanted.get(int(r["id"]))
                            if g is None:
                                continue
                            # 与可读来源一致：真实发送者 id 优先，缺失才退回显示名。
                            who_id = str(r["sid"] or "") or str(r["sender_name"] or "")
                            platform_msg_id = str(r["platform_msg_id"] or "")
                            quote_id = str(r["quote_id"] or "")
                            quote_user = str(r["quote_user"] or "")
                            if not (who_id or platform_msg_id or quote_user or quote_id):
                                continue
                            meta.append({
                                "platform": g["platform"], "account_id": g["account_id"],
                                "chat_id": g["chat_id"], "msg_id": g["msg_id"],
                                "who_id": who_id, "platform_msg_id": platform_msg_id,
                                "quote_user": quote_user, "quote_id": quote_id,
                            })
                if meta:
                    with _LOCK:
                        for i in range(0, len(meta), 500):
                            con.executemany(_FILL_INBOUND_METADATA, meta[i:i + 500])
            except Exception as exc:  # noqa: BLE001 - 一个账号的库坏了不拖累其余账号
                skips.skip("db/world_db.py:backfill_missing_message_metadata:account",
                           "malformed", repr(exc)[:120])
                continue
        # 所有账号处理完后只提交一次，避免"部分成功"变成正常状态。
        with _LOCK:
            con.commit()
        updated = int(con.total_changes - before)
    finally:
        try:
            con.close()
        except Exception as exc:  # noqa: BLE001 - 关不上也只是句柄晚点回收
            skips.skip("db/world_db.py:backfill_missing_message_metadata:close",
                       "flutter", repr(exc)[:120])
    return {"scanned": scanned, "updated": updated, "accounts": accounts}


def backfill_outbound(days: int = 3) -> int:
    """把账本里「她发出去」的历史抄过来（只补一次，方便归属判定）。"""
    added = 0
    try:
        from loop import ledger as _lg  # noqa: F401  (保持导入路径一致)
    except Exception:
        pass
    from core.infra import actions as _actions  # 流水文件名只在一处（PIT-111）

    for i in range(int(days)):
        day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        path = _actions.path(day)
        if not os.path.isfile(path):
            continue
        for line in open(path, encoding="utf-8", errors="replace"):
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("act") != "say":
                continue
            if str(row.get("result") or "") not in ("sent", "ok"):
                continue
            text = str(row.get("text") or "")
            if not text:
                continue
            if record_outbound(str(row.get("target") or ""), text, platform="wechat",
                               turn_id=str(row.get("turn_id") or ""), method="ledger",
                               ack=str(row.get("ack") or "unconfirmed")):
                added += 1
    return added


def _at_local(ts_utc: str) -> str:
    """UTC ISO → 本地 `%Y-%m-%d %H:%M:%S`（和 account.db 的 created_at 逐字对齐）。"""
    t = str(ts_utc or "").strip()
    if not t:
        return ""
    try:
        dt = datetime.strptime(t[:19], "%Y-%m-%dT%H:%M:%S") + timedelta(hours=_tz_off())
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return to_local(t)


def sense_rows(db_path: str, *, since_hours: int = 168, limit: int = 400, chat_id: str = "") -> list:
    """给 loop.ports.sense_sync 的兼容视图：字段名与 account.db 的 messages 一致。

    2026-10-28 加：读口切换用。msg_id 形如 wechat:<acct>:<id>，这里还原成整数 id。
    """
    acct = os.path.basename(os.path.dirname(str(db_path or "")))
    platform = os.path.basename(os.path.dirname(os.path.dirname(os.path.normpath(str(db_path or "")))))
    if acct in ("", "data", "platforms", "decrypted"):
        # 静默返回 0 行是最难查的一种错：路径不对时账号就是目录名
        warn("sense_rows_db_path_looks_wrong", acct=acct,
             db_path=str(db_path or "")[:80])
    if not acct:
        return []
    lo = "-%d hours" % int(since_hours)  # 交给 SQLite 现算，避免本模块没 import datetime
    out = []
    # 2026-10-31：主人要求"回到一开始"。meta.history_since 是一道绝对地板：
    # 这一秒之前的世界史一律不给她看（行还在库里，只是读不到——删了会被
    # sync_accounts 从 account.db 增量灌回来，删是白删）。
    _floor = ""
    try:
        with _read_conn() as _fc:
            _fr = _fc.execute("select v from meta where k='history_since'").fetchone()
            _floor = str((_fr[0] if _fr else "") or "")
    except Exception:  # noqa: BLE001
        _floor = ""
    try:
        with conn() as c:
            rows = c.execute(
                "select msg_id, chat_id, ifnull(who,''), ifnull(text,''), ts_utc, ifnull(is_self,0),"
                " ifnull(quote_user,''), ifnull(media_path,''), ifnull(kind,'text'), ifnull(quote_id,''),"
                 " ifnull(who_id,''), ifnull(platform_msg_id,'') from inbound_event"
                " where platform=? and account_id=? and ts_utc>=strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)"
                " and (? = '' or ts_utc >= ?)"
                " and (? = '' or chat_id = ?) order by ts_utc desc limit ?",
                (platform, acct, lo, _floor, _floor, str(chat_id or ""), str(chat_id or ""), int(limit))).fetchall()
    except Exception as exc:  # noqa: BLE001
        # 读不出来和「没有新消息」在调用方看是同一件事；至少留一条带平台和账号的告警。
        warn("sense_rows_failed", platform=platform, acct=acct, err=repr(exc)[:120])
        return []
    for r in rows:
        mid = str(r[0] or "")
        try:
            _id = int(mid.rsplit(":", 1)[-1])
        except Exception:
            _id = 0
        _cid = r[1] or ""
        out.append({
            "id": _id,
            "chat_id": _cid,
            "chat_type": "group" if str(_cid).endswith("@chatroom") else "private",
            "speaker": r[2],
            "created_at": _at_local(r[4]),
            "content": r[3],
            "is_self": int(r[5] or 0),
            "kind": r[8],
            "quote_user": r[6],
            "media_path": r[7],
            "quote_id": r[9],
            # 2026-11-04：真实发送者 id（wxid）。空串＝采集层还没落 who_id，
            # 消费方必须自己容错（loop/direct_send.py 会退回按正文本地回查）。
            "sender_id": str(r[10] or ""),
            # 平台自己的消息 id（messages.msg_id 原文），与本地拼接 id 是两回事。
            "platform_msg_id": str(r[11] or ""),
        })
    out.reverse()
    return out


# ======================== 投递队列（2026-10-28） ========================
@contextlib.contextmanager
def _read_conn():
    """**只读、短命**的连接，用完就关。

    为什么不能用 conn()：它是进程级复用的长连接，SELECT 之后隐式事务一直不结束，
    于是面板读到的永远是第一次查询那一刻的快照 —— 今天踩到的是"删掉的行还在面板上"。
    只读统计一律走这里。
    """
    c = sqlite3.connect(str(PATH), timeout=6.0, isolation_level=None)
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        try:
            c.close()
        except Exception:
            pass


@contextlib.contextmanager
def _open():
    """取一条连接，用完**一定关**。

    conn() 只做 commit 不 close（历史写法），长跑进程里每次调用都会漏一条连接，
    WAL 下这些悬空连接会占住读锁，最终表现成 `database is locked`。
    """
    c = conn()
    try:
        yield c
    finally:
        try:
            c.close()
        except Exception:
            pass



# 对外 7 个函数：enqueue / claim_next / heartbeat / settle / reap_stale / expire / queue_stats。
# 语义写在各自 docstring 里；改之前先读 docstring，再读 docs/动作队列-设计.md。

_QUEUE_STATES = ("pending", "sending", "sent", "failed", "expired", "skipped", "unverified")


def _qrow(r) -> dict:
    """sqlite3.Row → dict，并补一列本地可读时间（epoch 仍保留，方便算术）。"""
    if r is None:
        return {}
    d = {k: r[k] for k in r.keys()}
    for k in ("next_at", "not_after", "claimed_at", "heartbeat_at",
              "created_ts", "updated_ts", "settled_ts"):
        v = float(d.get(k) or 0)
        d[k + "_txt"] = (time.strftime("%m-%d %H:%M:%S", time.localtime(v)) if v > 0 else "")
    return d


def enqueue(group_id: str, chat_id: str, items: list, *, platform: str = "wechat",
            account_id: str = "", turn_id: str = "", ttl_s: float | None = None,
            max_attempts: int = 50, priority: int = 0, stagger_s: float = 0.0,
            expires_at: float | None = None) -> list:
    """一组投递意图落盘（单事务，全成全败）。返回各条 id（幂等的重复入队返回原 id）。

    items 每项形如：
      {"kind": "text",  "text": "..."}
      {"kind": "image", "path": "..."}
      {"kind": "file",  "path": "...", "name": "..."}

    `stagger_s`：同一组内第 i 条的 `next_at` 往后推 i × stagger_s 秒。
    一拍发多个气泡时要留打字节奏 —— 顺序由 claim_next 的 seq 保证，
    但节奏得靠 next_at 错开，否则 desk 会在一轮循环里连着把几条全推出去。
    """
    now = time.time()
    ids = []
    with _LOCK, _open() as c:
        c.execute("begin immediate")
        if group_id.startswith("owner-trial:"):
            occupied = c.execute(
                "select 1 from outbound_queue where platform=? and account_id=? and chat_id=? "
                "and not_after=? and group_id like 'owner-trial:%' and group_id<>? limit 1",
                (platform, account_id, chat_id, expires_at, group_id),
            ).fetchone()
            if occupied:
                return []
        for i, raw in enumerate(items):
            key = "%s|%d" % (group_id, i)
            hit = c.execute("select id from outbound_queue where idem_key=?", (key,)).fetchone()
            if hit:
                ids.append(int(hit["id"]))
                continue
            it = dict(raw or {})
            k = str(it.get("kind") or "text")
            txt = str(it.get("text") or "")
            preview = str(txt or it.get("name") or it.get("path") or "")[:80]
            cur = c.execute(
                "insert into outbound_queue(idem_key, group_id, seq, turn_id, platform,"
                " account_id, chat_id, kind, payload, preview, text_hash, state,"
                " next_at, not_after, priority, max_attempts, created_ts, updated_ts)"
                " values(?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?,?)",
                (key, group_id, i, turn_id, platform, account_id, chat_id, k,
                 json.dumps(it, ensure_ascii=False), preview,
                 text_hash(txt) if k == "text" else "",
                 now + (i * float(stagger_s or 0.0)),
                 expires_at if expires_at is not None else now + (float(ttl_s) if ttl_s is not None else _ttl_default()),
                 int(priority), int(max_attempts), now, now))
            ids.append(int(cur.lastrowid))
        c.commit()
    return ids


def claim_next(*, platform: str = "wechat", owner: str = "", now=None,
               group_id: str = "") -> dict | None:
    """原子认领一条并开一行 attempt；没有可发的返回 None。

    组顺序：同 group 里 seq 更小的还没走完时，不允许认领后面的 —— 保证"一拍多气泡"
    按顺序送达；CAS（where state='pending'）保证并发只有一个赢家。
    """
    now = float(now or time.time())
    with _LOCK, _open() as c:
        c.execute("begin immediate")
        try:
            sql = ("select * from outbound_queue q where q.state='pending'"
                   " and q.next_at <= ? and q.platform = ? and q.not_after > ?"
                   " and not exists (select 1 from outbound_queue p"
                   "   where p.group_id = q.group_id and p.seq < q.seq"
                   "     and p.state in ('pending','sending','unverified'))")
            args = [now, str(platform), now]
            if group_id:
                sql += " and q.group_id = ?"
                args.append(str(group_id))
            sql += " order by q.priority desc, q.next_at asc, q.id asc limit 1"
            row = c.execute(sql, args).fetchone()
            if not row:
                c.commit()
                return None
            qid = int(row["id"])
            n = int(row["attempts"] or 0) + 1
            got = c.execute(
                "update outbound_queue set state='sending', owner=?, attempts=?,"
                " claimed_at=?, heartbeat_at=?, updated_ts=?"
                " where id=? and state='pending'", (str(owner), n, now, now, now, qid))
            if int(got.rowcount or 0) != 1:
                c.commit()          # 被别人抢走
                return None
            c.execute(
                "insert or replace into outbound_attempt(queue_id, attempt_no, owner,"
                " started_ts, result) values(?,?,?,?,'running')",
                (qid, n, str(owner), now))
            c.commit()
            fresh = c.execute("select * from outbound_queue where id=?", (qid,)).fetchone()
        except Exception:
            c.rollback()
            raise
    d = _qrow(fresh)
    try:
        d["payload"] = json.loads(d.get("payload") or "{}")
    except Exception:
        d["payload"] = {}
    return d


def heartbeat(qid: int, owner: str, now=None, *, attempt_no: int = 0) -> bool:
    """只有当前 sending 尝试可续期；旧心跳不能延缓中断回收。"""
    if not owner or int(attempt_no) <= 0:
        raise ValueError("outbound_heartbeat_claim_missing")
    now = float(now or time.time())
    with _LOCK, _open() as c:
        changed = c.execute("update outbound_queue set heartbeat_at=? where id=? and owner=? and attempts=? and state='sending' "
                            "and exists(select 1 from outbound_attempt where queue_id=? and attempt_no=? and owner=? and result='running')",
                            (now, int(qid), str(owner), int(attempt_no), int(qid), int(attempt_no), str(owner)))
        c.commit()
        return changed.rowcount == 1


def _has_bound_queue_confirmation(c, row) -> bool:
    """本地确认必须仍能回指同操作、来源、会话和正文的回执。"""
    if row["ack"] != "confirmed":
        return False
    return c.execute("select id from outbound_receipt where turn_id=? and platform=? and account_id=? and chat_id=? and text_hash=? and seq=? and ack='confirmed' and msg_id<>'' limit 1",
        ("queue:%d" % int(row["id"]), row["platform"], row["account_id"], row["chat_id"], row["text_hash"], int(row["attempts"]))).fetchone() is not None


def confirmed_queue_settled_ts(
    queue_ids: list[int], *, platform: str, account_id: str, chat_id: str, turn_id: str,
) -> float | None:
    """核实同会话同回合的每条发送，返回最后一次执行落定的时间。"""
    if not queue_ids or not all((platform, account_id, chat_id, turn_id)):
        return None
    if any(type(qid) is not int or qid <= 0 for qid in queue_ids) or len(set(queue_ids)) != len(queue_ids):
        return None
    try:
        uri = Path(PATH).resolve().as_uri() + "?mode=ro"
        with _LOCK, contextlib.closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as c:
            c.row_factory = sqlite3.Row
            c.execute("begin")
            placeholders = ",".join("?" for _ in queue_ids)
            rows = c.execute(f"SELECT * FROM outbound_queue WHERE id IN ({placeholders})", queue_ids).fetchall()
            if len(rows) != len(queue_ids):
                return None
            latest = 0.0
            for row in rows:
                if row["state"] not in ("sent", "unverified"):
                    return None
                if (row["platform"], row["account_id"], row["chat_id"], row["turn_id"]) != (platform, account_id, chat_id, turn_id):
                    return None
                settled = float(row["settled_ts"] or 0)
                if settled <= 0 or int(row["attempts"] or 0) <= 0 or not _has_bound_queue_confirmation(c, row):
                    return None
                latest = max(latest, settled)
    except Exception:
        return None
    return latest


#: 「确定没打字」这类失败的原因片段。2026-11-22：文本发送按回车前中止时没有回执证据，
#: 旧代码把它记成 unverified（可能已发出），于是消息永久丢掉。这些片段能证明输入框没被改动。
_NOT_EXECUTED_MARKS = (
    "没打字",
    "没有动它",
    "无法激活微信窗口",
    "微信未连接",
    "已中止发送",
)


def requeue_not_executed(*, since_ts: float, limit: int = 10, now: float | None = None) -> list[int]:
    """把最近 since_ts 之后、证据表明「一个字都没打」的 unverified 行重排。

    只在重启时调用。返回被重排的 id 列表。设置 state=pending、next_at=now；
    attempts 不重置（保留历史），max_attempts 仍生效。
    """
    marks = tuple("%" + m + "%" for m in _NOT_EXECUTED_MARKS)
    clauses = " OR ".join("ifnull(why,'') like ?" for _ in marks)
    out: list[int] = []
    stamp = float(now if now is not None else time.time())
    with _LOCK, _open() as c:
        c.execute("begin immediate")
        rows = c.execute(
            "select id from outbound_queue where state='unverified'"
            " and ifnull(updated_ts,0) >= ? and (" + clauses + ")"
            " order by id desc limit ?",
            (float(since_ts), *marks, int(limit)),
        ).fetchall()
        ids = [int(r[0]) for r in rows]
        for qid in ids:
            c.execute(
                "update outbound_queue set state='pending', next_at=?, updated_ts=?"
                " where id=? and state='unverified'",
                (stamp, stamp, qid),
            )
        c.commit()
        out = ids
    return out


def settle(qid: int, ok: bool, *, ack: str = "", why: str = "",
           backoff_s: float = 0.0, terminal: bool = False, not_executed: bool = False, owner: str = "", attempt_no: int = 0, now=None) -> str:
    """收尾一次尝试，返回新状态。

    ok=True → sent；失败且有明确 not_executed 证据才允许 pending；
    缺失执行证据 → unverified；确定未执行且次数用完 → failed。

    ``terminal=True``：这次失败就是终局，不看还剩几次（2026-11-04 起给发送工用：
    输入框连续清不掉时别再排退避，否则同一条会在几个会话之间反复激活窗口，
    看起来像"她在来回纠结"，其实是队列在重试）。
    """
    if not owner or int(attempt_no) <= 0:
        raise ValueError("outbound_settle_claim_missing")
    now = float(now or time.time())
    with _LOCK, _open() as c:
        c.execute("begin immediate")
        row = c.execute("select * from outbound_queue where id=?", (int(qid),)).fetchone()
        if not row:
            return ""
        if row["state"] != "sending" or str(row["owner"] or "") != str(owner) or int(row["attempts"] or 0) != int(attempt_no):
            raise ValueError("outbound_settle_claim_mismatch")
        claim = c.execute("select 1 from outbound_attempt where queue_id=? and attempt_no=? and owner=? and result='running'",
                          (int(qid), int(attempt_no), str(owner))).fetchone()
        if claim is None:
            raise ValueError("outbound_settle_attempt_missing")
        att = int(row["attempts"] or 0)
        if ok:
            state = "skipped" if ack == "dry_run" else "sent"
        elif not not_executed:
            state = "unverified"
            ack = "unconfirmed"
        elif terminal or att >= int(row["max_attempts"] or 50):
            state = "failed"
        else:
            state = "pending"
        if _has_bound_queue_confirmation(c, row):
            ack, why = "confirmed", str(row["why"] or "")
        nxt = now if state != "pending" else now + float(backoff_s)
        c.execute(
            "update outbound_queue set state=?, ack=?, why=?, next_at=?, updated_ts=?,"
            " owner='', settled_ts=? where id=?",
            (state, str(ack or ""), str(why or "")[:400], nxt, now,
             now if state in ("sent", "failed", "unverified", "skipped") else 0.0, int(qid)))
        c.execute(
            "update outbound_attempt set ended_ts=?, result=?, ack=?, why=?"
            " where queue_id=? and attempt_no=?",
            (now, ("dry_run" if ack == "dry_run" else "sent") if ok else ("uncertain" if state == "unverified" else ("not_executed" if not_executed else "failed")),
             str(ack or ""), str(why or "")[:400], int(qid), att))
        c.commit()
    return state


def mark_confirmation_unknown(qid: int, why: str, *, attempt_no: int, connection=None) -> int:
    """缺少观察只能标不确定，不能覆盖已提交的本地凭据。"""
    with _LOCK, (contextlib.nullcontext(connection) if connection is not None else _open()) as c:
        if not c.in_transaction:
            c.execute("begin immediate")
        changed = c.execute("update outbound_queue set ack='unconfirmed',why=? where id=? and attempts=? and ack<>'confirmed'",
                            (str(why)[:400], int(qid), int(attempt_no)))
        if changed.rowcount:
            c.execute("update outbound_attempt set ack='unconfirmed',why=? where queue_id=? and attempt_no=? and ack<>'confirmed'",
                      (str(why)[:400], int(qid), int(attempt_no)))
        c.commit()
        return int(changed.rowcount)


def reap_stale(stale_s: float = 120.0, now=None) -> int:
    """回收"进程死在发送中"：心跳失效 → unverified；可能已发送，不能自动重放。"""
    now = float(now or time.time())
    n = 0
    with _LOCK, _open() as c:
        c.execute("begin immediate")
        rows = c.execute(
            "select * from outbound_queue"
            " where state='sending' and heartbeat_at < ?", (now - float(stale_s),)).fetchall()
        for r in rows:
            qid, att, mx = int(r["id"]), int(r["attempts"] or 0), int(r["max_attempts"] or 3)
            state = "unverified"
            ack, why = ("confirmed", str(r["why"] or "")) if _has_bound_queue_confirmation(c, r) else ("unconfirmed", "发送中断，执行结果不确定；等待核对")
            c.execute("update outbound_queue set state=?, ack=?, owner='', why=?, updated_ts=?"
                      " where id=?", (state, ack, why, now, qid))
            c.execute("update outbound_attempt set ended_ts=?, result='aborted'"
                      " where queue_id=? and attempt_no=?", (now, qid, att))
            n += 1
        c.commit()
    return n


def _ttl_default() -> float:
    """排队多久算作废（秒）：默认 600，`YINYUE_QUEUE_TTL_S` 可改，夹在 30..604800。

    2026-10-30：一句写好的话 10 分钟后静默作废（发送工没在跑）。无声是第二层问题。
    """
    from core.infra import envflags

    raw = (envflags.text("YINYUE_QUEUE_TTL_S", "") or "").strip()
    try:
        got = float(raw) if raw else 600.0
    except ValueError:
        got = 600.0
    return max(30.0, min(7 * 86400.0, got))


def expire(now=None, *, on_expired=None) -> int:
    """TTL 只作废未认领的 pending；已开始发送的任务交给收尾或中断回收。

    `on_expired(qid, chat_id, preview, waited_s)` 每真作废一条调一次——由调用方记日志/落账。
    DB 层本来不碰日志，所以用回调，而不是直接 warn。
    """
    now = float(now or time.time())
    n = 0
    notifications = []
    with _LOCK, _open() as c:
        c.execute("begin immediate")
        rows = c.execute(
            "select id, group_id, created_ts, chat_id, preview from outbound_queue"
            " where state='pending' and not_after > 0 and not_after <= ?",
            (now,)).fetchall()
        for r in rows:
            qid, gid, born = int(r["id"]), str(r["group_id"] or ""), float(r["created_ts"] or 0)
            _chat, _prev = str(r["chat_id"] or ""), str(r["preview"] or "")
            # 只处理"此刻仍然是 pending"的行：rows 是循环开始时取的快照，
            # 同组的后一条可能已经被上面那句改成 skipped，再改一次就会重复计数
            # （2026-10-28 实测：n=3 而 expired 只有 2、skipped 被覆盖成 0）。
            got0 = c.execute(
                "update outbound_queue set state='expired', why=?, updated_ts=?"
                " where id=? and state='pending'",
                ("超过 %.0f 秒没发出去" % max(0.0, now - born), now, qid))
            if int(got0.rowcount or 0) != 1:
                continue
            n += 1
            notifications.append((qid, _chat, _prev, max(0.0, now - born)))
            if gid:
                got = c.execute(
                    "update outbound_queue set state='skipped', why=?, updated_ts=?"
                    " where group_id=? and state='pending' and id<>?",
                    ("同组前一条已作废", now, gid, qid))
                n += int(got.rowcount or 0)
        c.commit()
    if on_expired is not None:
        for notice in notifications:
            try:
                on_expired(*notice)
            except Exception as exc:
                warn("queue_expiry_notification_failed", queue_id=notice[0], error=repr(exc)[:200])
    return n


def queue_stats(limit: int = 10) -> dict:
    """面板用：各状态计数 + 最近未完成的 N 条。只读，不抛异常。"""
    out = {"n": 0, "pending": 0, "sending": 0, "sent": 0, "failed": 0,
           "expired": 0, "skipped": 0, "unverified": 0, "rows": []}
    try:
        with _read_conn() as c:
            for st in _QUEUE_STATES:
                r = c.execute("select count(*) from outbound_queue where state=?",
                              (st,)).fetchone()
                out[st] = int(r[0]) if r else 0
                out["n"] += out[st]
            rows = c.execute(
                "select * from outbound_queue where state in"
                " ('pending','sending','failed','expired','unverified') order by id desc limit ?",
                (int(limit),)).fetchall()
            out["rows"] = [_qrow(r) for r in rows]
    except Exception as _exc:          # 面板要能看见"为什么会是 0"，不许静默
        out["err"] = repr(_exc)[:200]
    return out

def last_outbound(chat_id: str = "", *, limit: int = 3, platform: str = "", account_id: str = "") -> list:
    """最近几条出站（队列侧，带 state/ack/why）。只读，不抛异常。

    2026-11-01：她要能回答"我上一条到底到没到"——队列是唯一记着 fate 的地方。
    """
    out = []
    try:
        with _LOCK, conn() as c:
            filters, params = [], []
            if platform or account_id:
                if not all((platform, account_id, chat_id)):
                    return []
                filters.extend(["platform=?", "account_id=?"])
                params.extend([platform, account_id])
            if chat_id:
                filters.append("chat_id=?")
                params.append(str(chat_id))
            where = (" where " + " and ".join(filters)) if filters else ""
            rows = c.execute("select * from outbound_queue" + where + " order by id desc limit ?",
                             (*params, int(limit))).fetchall()
        out = [_qrow(r) for r in rows]
    except Exception as exc:  # noqa: BLE001 - 只读辅助，坏了就当查不到，但要留痕
        warn("world_db.last_outbound_failed", chat_id=chat_id, err=repr(exc)[:120])
        out = []
    return out
