"""说话前先问一句：这个人/这个群，我到底认不认识（2026-11-04「全 C」地基③）。

「全 C」＝外发不再停下来等点头，于是"认得名字"第一次变成真风险：认错一个人，话就发到
别人手机上了。这个模块只做两件事，都只认事实：

1. ``allowed(chat_id)`` —— **白名单**：这个会话在微信账库里**有过真实消息**吗？
   一条都没有 = 她从没跟这个人说过话，那"主动去找他"就不该发生。发到一个凭空造出来的
   chat_id，等于往空气里说话，还会让台账以为办成了（E-59 那种假账）。
2. ``whois(name)`` —— 名字 → chat_id，只认**完全相等**（全半角/空白归一后），认重了就
   列候选拒绝，绝不按字典序猜第一个（2026-10-24 差点发错人那次）。

不判断"该不该说"，也不做内容审查 —— 那是别的层的事。
"""
from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass, field

from .safelog import warn

__all__ = ["Target", "allowed", "whois", "senders", "history_depth", "MAX_SENDER_PAIRS", "MIN_HISTORY"]

#: 至少一条真实消息才算"认识"。0 会让谁都能发，1 是"真的说过话"。
MIN_HISTORY: int = 1


def _db(db=None) -> str:
    """账库路径。默认用 `loop/ports.py` 里那个"消息最多的库"（就是本号）。"""
    if db:
        return str(db)
    from . import ports as _p

    return str(_p._account_db())


def _connect(path: str):
    return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=5)


def _norm(name: str) -> str:
    """全半角归一 + 抹掉空白。只用来判"是不是同一个名字"，不用于显示。"""
    s = unicodedata.normalize("NFKC", str(name or "")).strip()
    return "".join(s.split())


@dataclass(frozen=True)
class Target:
    """一次寻址的结果。``chat_id`` 为空就表示拒绝，``why`` 说明为什么。"""

    chat_id: str = ""
    name: str = ""
    why: str = ""
    candidates: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return bool(self.chat_id) and not self.why


def history_depth(chat_id: str, *, db=None) -> int:
    """这个会话在账库里有多少条真实消息（自己发的也算 —— 那也是"真的说过话"）。"""
    cid = str(chat_id or "").strip()
    if not cid:
        return 0
    try:
        con = _connect(_db(db))
        try:
            row = con.execute("select count(*) from messages where chat_id=?", (cid,)).fetchone()
        finally:
            con.close()
        return int((row or [0])[0] or 0)
    except Exception as exc:  # noqa: BLE001
        warn("targets.history_unreadable", chat_id=cid, err=repr(exc)[:120])
        return 0


def allowed(chat_id: str, *, db=None, min_history: int = MIN_HISTORY) -> bool:
    """白名单：微信里真的跟这个会话有过往来吗。

    读不到库时**不放行**（返回 False）：全 C 之下，"没查清就发"比"没发"坏得多。
    """
    return history_depth(chat_id, db=db) >= int(min_history)




#: 反查名字时最多看多少条 (sender_name, sender_id) 组合（真库 123 条，留足余量）。
MAX_SENDER_PAIRS: int = 5000


def senders(name: str, *, db=None) -> tuple[str, ...]:
    """群里叫这个名字的人，可能是哪个 wxid（从 `messages.sender_name` 反查）。

    2026-11-04 补：通讯录只有 302 行、备注 2 条，群里大家用的是显示名
    （「P5」「P6」…），`whois` 从前只查 contacts，于是这些名字一个都认不出。
    这里只负责**认人**；能不能发仍要过 `allowed()` —— 认得出不等于有来往。
    """
    want = _norm(name)
    if not want:
        return ()
    try:
        con = _connect(_db(db))
        try:
            rows = con.execute(
                "select distinct sender_name, sender_id from messages "
                "where is_self=0 and coalesce(sender_id,'')<>'' limit ?",
                (int(MAX_SENDER_PAIRS),)).fetchall()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        warn("targets.senders_unreadable", err=repr(exc)[:120])
        return ()
    out: list[str] = []
    for sender_name, sender_id in rows:
        sid = str(sender_id or "").strip()
        if sid and _norm(sender_name) == want and sid not in out:
            out.append(sid)
    return tuple(sorted(out))
def whois(name: str, *, db=None) -> Target:
    """名字 → chat_id。只认完全相等；认重了列候选、绝不猜。"""
    raw = str(name or "").strip()
    if not raw:
        return Target(name=raw, why="名字是空的")
    n = _norm(raw)
    try:
        con = _connect(_db(db))
        try:
            rows = con.execute(
                "select wxid, remark, nickname from contacts "
                "where wxid is not null and wxid<>''").fetchall()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        warn("targets.contacts_unreadable", err=repr(exc)[:120])
        return Target(name=raw, why="通讯录读不到，不敢猜")

    hits: list[str] = []
    for wxid, remark, nick in rows:
        cid = str(wxid or "").strip()
        if not cid:
            continue
        if cid == raw or _norm(remark) == n or _norm(nick) == n:
            if cid not in hits:
                hits.append(cid)
    if len(hits) == 1:
        cid = hits[0]
        if not allowed(cid, db=db):
            return Target(name=raw, why="认得出「%s」，但微信里从没跟这个会话说过话" % raw[:24])
        return Target(chat_id=cid, name=raw)
    if not hits:
        sids = senders(raw, db=db)
        if not sids:
            return Target(name=raw,
                          why="认不出「%s」：通讯录和聊天记录里都没有这个名字" % raw[:24])
        if len(sids) > 1:
            return Target(name=raw,
                          why="「%s」在群里对得上 %d 个人，不敢猜" % (raw[:24], len(sids)),
                          candidates=tuple(sids[:5]))
        sid = sids[0]
        if not allowed(sid, db=db):
            return Target(name=raw,
                          why="认得出「%s」是群里的名字，但没跟这个人单独说过话" % raw[:24])
        return Target(chat_id=sid, name=raw)
    return Target(name=raw,
                  why="「%s」对得上 %d 个会话，不敢猜" % (raw[:24], len(hits)),
                  candidates=tuple(hits[:5]))
