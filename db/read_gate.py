"""账号库的只读入口（唯一契约）。

为什么要有：面板 / 引擎 / 脚本各自 sqlite3.connect 过一次，写法各不一样，
其中一处还踩过「mode=ro 读不到 WAL」的坑（日志房间那次，数据看着是空的）。
这里把「怎么只读、读不到怎么办、名字从哪来」收成一处，别处不再直接 connect。
"""
from __future__ import annotations

import os
import sqlite3

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA = os.path.join(_ROOT, "data")


def _ro_uri(path: str) -> str:
    return "file:%s?mode=ro" % str(path).replace("\\", "/")


def open_ro(path: str) -> sqlite3.Connection:
    """只读打开。先试 URI 只读；WAL 库在只读模式下可能打不开（缺 -shm），
    退回普通连接 + PRAGMA query_only=ON —— 同样不会改一个字节。"""
    try:
        c = sqlite3.connect(_ro_uri(path), uri=True, timeout=2.0)
        c.row_factory = sqlite3.Row
        c.execute("select 1 from sqlite_master limit 1").fetchall()
        return c
    except Exception:
        c = sqlite3.connect(path, timeout=3.0)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA query_only=ON")
        except Exception:
            pass
        return c


def rows(path: str, sql: str, params=()) -> list:
    con = open_ro(path)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def candidates() -> list[str]:
    """data/platforms/<平台>/<账号>/account.db，存在才算。"""
    out = []
    base = os.path.join(_DATA, "platforms")
    for plat in ("wechat", "qq"):
        p = os.path.join(base, plat)
        if not os.path.isdir(p):
            continue
        for acc in os.listdir(p):
            db = os.path.join(p, acc, "account.db")
            if os.path.exists(db):
                out.append(db)
    return out


def newest_stamp(path: str) -> str:
    try:
        for r in rows(path, "select max(created_at) as t from messages"):
            return str(r["t"] or "")
    except Exception:
        pass
    return ""


def live_account() -> str:
    """消息最新的那个账号库（测试账号常年在最前面）。"""
    best, best_at = "", ""
    for p in candidates():
        at = newest_stamp(p)
        if at > best_at:
            best, best_at = p, at
    return best


def labels() -> dict:
    """wxid / 群 id → 人话名字（remark > 群昵称 > nick_name > alias）。"""
    out: dict = {}
    for acc in candidates():
        try:
            for r in rows(acc, "select wxid, coalesce(remark,'') as remark,"
                               " coalesce(nickname,'') as nickname from contacts"):
                u = str(r["wxid"] or "")
                if not u or u in out:
                    continue
                out[u] = str(r["remark"] or r["nickname"] or u)
        except Exception:
            continue
    for p in (os.path.join(_DATA, "decrypted", "contact.db"),
              os.path.join(_DATA, "decrypted", "wechat", "contact.db")):
        if not os.path.exists(p):
            continue
        try:
            for r in rows(p, "select username, remark, nick_name, alias from contact"):
                u = str(r["username"] or "")
                if u and u not in out:
                    out[u] = str(r["remark"] or r["nick_name"] or r["alias"] or u)
        except Exception:
            continue
    return out


def recent(chat_id: str, limit: int = 50, *, db_path: str = "") -> list:
    """某个会话最近的几行（读口标准用法示例）。"""
    path = db_path or live_account()
    if not path:
        return []
    return rows(path, "select * from messages where chat_id=? order by created_at desc limit ?",
                (chat_id, int(limit)))
