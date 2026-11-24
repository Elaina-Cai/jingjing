"""群成员旁路表（CB-139 甲）：平台侧的权威成员关系，落在**她看不见**的地方。

为什么不能写进 ``group_memberships`` / ``person_notes``：这两处都喂提示词，塞进去等于
替她凭空「认识」一群人，而且可以 @ 出去（真发到群里）。理由见 ``docs/踩坑档案.md`` #58。

所以落一张独立库里的旁路表，只回答一个三态问题：**这个平台身份此刻在不在这个群里**：

- ``True``  —— 在（最近一次同步里有）
- ``False`` —— 不在（同步过这个群，但名单里没这个人）
- ``None``  —— **说不准**（没同步过这个群，或副本已过期）

``None`` 不是 ``False``：判据要区分「确认不在」和「不知道」。快照里消失的人只把
``still_listed`` 置 0，**永不删行**——判据问的是此刻，历史要留。

平台相关的读取（解密副本、三张表）在适配层 ``core/execution/wx_roster.py``；
本模块只处理通用表结构与三态语义。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
import time
import unicodedata
from pathlib import Path
from typing import Iterable, Mapping

ENV_PATH = "YINYUE_ROSTER_DB"
DEFAULT_REL = "data/roster_shadow.db"

SCHEMA = """
create table if not exists room_member (
    chat_id     text not null,
    wxid        text not null,
    nick        text not null default '',
    is_owner    integer not null default 0,
    still_listed integer not null default 1,
    synced_at   text not null,
    primary key (chat_id, wxid)
);
create table if not exists room_sync (
    chat_id   text primary key,
    synced_at text not null,
    members   integer not null default 0
);
create table if not exists roster_out (
    chat_id text not null,
    person  text not null,
    why     text not null default '',
    at      text not null
);
"""


def db_path() -> Path:
    raw = os.environ.get(ENV_PATH, "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[2] / DEFAULT_REL


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    target = Path(path) if path is not None else db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(target))
    con.executescript(SCHEMA)
    return con


def write_rows(
    rows: Mapping[str, Mapping[str, str]],
    *,
    synced_at: str = "",
    owners: Mapping[str, str] | None = None,
    con: sqlite3.Connection | None = None,
) -> dict[str, int]:
    """把一次快照写进旁路表：按 ``(chat_id, wxid)`` 幂等 upsert，**不删**任何行。

    没出现在本次快照里的人只把 ``still_listed`` 置 0（退群/离开的线索），
    历史行连同 ``synced_at`` 一起保留 —— 判据只问「此刻在不在」，
    不代表「以前不在过」。
    """
    stamp = synced_at or time.strftime("%Y-%m-%d %H:%M:%S")
    boss = dict(owners or {})
    own = con is None
    con = con or connect()
    try:
        rooms = members_in = 0
        for chat_id, people in rows.items():
            rooms += 1
            members_in += len(people)
            for wxid, nick in people.items():
                con.execute(
                    "insert into room_member (chat_id, wxid, nick, is_owner, still_listed, synced_at)"
                    " values (?,?,?,?,1,?)"
                    " on conflict(chat_id, wxid) do update set nick=excluded.nick,"
                    " still_listed=1, synced_at=excluded.synced_at",
                    (chat_id, wxid, nick, 1 if boss.get(chat_id) == wxid else 0, stamp),
                )
            con.execute(
                "update room_member set still_listed=0 where chat_id=? and wxid not in (%s)"
                % ",".join("?" * len(people)) if people else
                "update room_member set still_listed=0 where chat_id=?",
                (chat_id, *people.keys()),
            )
            con.execute(
                "insert into room_sync (chat_id, synced_at, members) values (?,?,?)"
                " on conflict(chat_id) do update set synced_at=excluded.synced_at,"
                " members=excluded.members",
                (chat_id, stamp, len(people)),
            )
        con.commit()
        return {"rooms": rooms, "members": members_in}
    finally:
        if own:
            con.close()


def members(chat_id: str, *, listed_only: bool = True, con: sqlite3.Connection | None = None) -> set[str]:
    own = con is None
    con = con or connect()
    try:
        sql = "select wxid from room_member where chat_id=?"
        if listed_only:
            sql += " and still_listed=1"
        return {str(r[0]) for r in con.execute(sql, (chat_id,))}
    finally:
        if own:
            con.close()


def contains(chat_id: str, wxid: str, *, con: sqlite3.Connection | None = None) -> bool | None:
    """在 → True，不在 → False，**这张表里没有这个群** → None（不知道，不是「不在」）。"""
    own = con is None
    con = con or connect()
    try:
        if con.execute("select 1 from room_sync where chat_id=?", (chat_id,)).fetchone() is None:
            return None
        row = con.execute(
            "select still_listed from room_member where chat_id=? and wxid=?", (chat_id, wxid)
        ).fetchone()
        return bool(row and row[0])
    finally:
        if own:
            con.close()


def synced_at(chat_id: str, *, con: sqlite3.Connection | None = None) -> str:
    own = con is None
    con = con or connect()
    try:
        row = con.execute("select synced_at from room_sync where chat_id=?", (chat_id,)).fetchone()
        return str(row[0]) if row else ""
    finally:
        if own:
            con.close()



# ---------------------------------------------------------------- 判据（BR-069 乙）

MAX_AGE_MS = 6 * 60 * 60 * 1000
"""名单多旧就不算数：六小时。比「这个人上次说话」长，比「一天没同步」短。"""

VERDICT_IN = "在册"
VERDICT_OUT = "不在册"
VERDICT_UNSURE = "说不准"


def match_key(text: str) -> str:
    """把一个人名压成「只剩字母与数字」的键（NFKC → 丢非字母数字 → casefold）。

    为什么不用一张「要去掉的装饰符」清单：真实群名片里有人用泰文/藏文的组合符、
    波浪线、括号注释、全角空格、emoji 拼名（本库 308 人里 27 个有这类装饰）。
    按 Unicode 类别只留字母与数字，比逐个枚举装饰符稳，也不会因为出现新装饰而漏。

    代价：``A-B`` 与 ``AB`` 会归一成同一个键 —— 所以调用方命中多个候选时必须
    回「说不准」，不许自己挑一个。
    """
    s = unicodedata.normalize("NFKC", str(text or ""))
    return "".join(ch for ch in s if unicodedata.category(ch)[0] in ("L", "N")).casefold()


def resolve(
    chat_id: str,
    person: str,
    *,
    max_age_ms: int = MAX_AGE_MS,
    con: sqlite3.Connection | None = None,
) -> tuple[str, str]:
    """把「她要叫的那个人」对到旁路表上，回 ``(三态, 依据)``。纯查表，不写库。

    三态是刻意的：``说不准`` 不等于 ``不在册`` —— 没同步过的房间、过期的快照、
    重名的人，都只能说不准。调用方要自己决定「说不准」时放不放行，
    别在这里替它猜。
    """
    cid = str(chat_id or "").strip()
    key = str(person or "").strip()
    if not cid or not key:
        return VERDICT_UNSURE, "没给房间或没给称呼"
    own = con is None
    conn = con or connect()
    try:
        row = conn.execute(
            "select count(*) from room_sync where chat_id = ?", (cid,)
        ).fetchone()
        if not row or not row[0]:
            return VERDICT_UNSURE, "这张表里没有这个房间（从没同步过）"
        if not fresh(cid, max_age_ms=max_age_ms, con=conn):
            return VERDICT_UNSURE, "快照比 %d 小时旧" % (max_age_ms // 3600000)
        rows = conn.execute(
            "select wxid, nick from room_member where chat_id = ? and still_listed = 1",
            (cid,),
        ).fetchall()
    finally:
        if own:
            conn.close()
    want = key.casefold()
    for wxid, _nick in rows:
        if str(wxid or "").casefold() == want:
            return VERDICT_IN, "wxid 对上"
    hits = [w for w, n in rows if str(n or "").strip().casefold() == want]
    if len(hits) == 1:
        return VERDICT_IN, "昵称对上"
    if len(hits) > 1:
        return VERDICT_UNSURE, "名单里有 %d 个人叫这个名字" % len(hits)
    want_key = match_key(key)
    if not want_key:
        return VERDICT_UNSURE, "这个称呼里没有一个字母或数字，没法比"
    near = [w for w, n in rows if match_key(n) == want_key]
    if len(near) == 1:
        return VERDICT_IN, "写法归一后对上"
    if len(near) > 1:
        return VERDICT_UNSURE, "写法归一后有 %d 个候选" % len(near)
    if len(want_key) >= 2:
        prefix = [w for w, n in rows if match_key(n).startswith(want_key)]
        if prefix:
            return VERDICT_UNSURE, "名单里有 %d 个名字以这个开头（写法可能不同）" % len(prefix)
    return VERDICT_OUT, "这份名单（%d 人）里没有这个人" % len(rows)

def fresh(chat_id: str, *, max_age_ms: int, con: sqlite3.Connection | None = None) -> bool:
    """副本还新不新。时间戳解析失败 = 不新（fail closed）。"""
    own = con is None
    con = con or connect()
    try:
        stamp = synced_at(chat_id, con=con)
    finally:
        if own:
            con.close()
    if not stamp:
        return False
    try:
        at = time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return False
    return (time.time() - at) * 1000.0 <= float(max_age_ms)


def note_out(chat_id: str, person: str, why: str = "",
             *, con: sqlite3.Connection | None = None) -> None:
    """记一笔「判成不在册」。这是台账，不是判定 —— 只写，不改名单，也不改人。"""
    own = con is None
    conn = con or connect()
    try:
        conn.execute(
            "insert into roster_out (chat_id, person, why, at) values (?, ?, ?, ?)",
            (str(chat_id or "")[:64], str(person or "")[:64], str(why or "")[:200],
             time.strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
    finally:
        if own:
            conn.close()


def _cutoff(hours: int) -> str:
    return (datetime.now() - timedelta(hours=max(0, int(hours)))).strftime("%Y-%m-%d %H:%M:%S")


def recent_outs(*, hours: int = 24, limit: int = 5,
                con: sqlite3.Connection | None = None) -> list[tuple[str, str, str, str]]:
    """最近判成「不在册」的几笔：``[(chat_id, person, why, at), …]``，新的在前。"""
    own = con is None
    conn = con or connect()
    try:
        rows = conn.execute(
            "select chat_id, person, why, at from roster_out where at >= ?"
            " order by at desc, rowid desc limit ?",
            (_cutoff(hours), max(1, int(limit))),
        ).fetchall()
        return [(str(a), str(b), str(c), str(d)) for a, b, c, d in rows]
    finally:
        if own:
            conn.close()


def count_outs(*, hours: int = 24, con: sqlite3.Connection | None = None) -> int:
    """时段内判成「不在册」的笔数（给巡检表看，不读日志文本）。"""
    own = con is None
    conn = con or connect()
    try:
        row = conn.execute("select count(*) from roster_out where at >= ?", (_cutoff(hours),)).fetchone()
        return int(row[0]) if row else 0
    finally:
        if own:
            conn.close()
