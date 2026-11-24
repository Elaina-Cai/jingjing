#!/usr/bin/env python
"""微信读侧：按**真库形状**（微信 4.x）验一遍。

真库实测形状（``data/decrypted/wechat/<wxid>/merge_msg.db``）：
- 每个会话一张表 ``Msg_<md5(会话 id)>``，**没有** ``str_talker`` 列；
- 正文列 ``message_content``，可能是 str，也可能是 zstd 压过的 bytes；
- ``real_sender_id`` 指向 ``Name2Id.rowid``；等于自己那一行 = 我发的；
- 群消息正文常以 ``wxid_xxx: `` / ``xxxxx@chatroom: `` 开头；
- 卡片正文是 XML（``<msg>`` / ``<appmsg>`` / ``<sysmsg>``），不许念进 prompt；
- 昵称/备注在兄弟文件 ``contact.db`` 里。
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
# 临时目录的唯一入口（见 tests/_tempdirs.py）：不给 dir= 时系统 TEMP 不可写会静默回落到仓库根。
from _tempdirs import temp_base  # noqa: E402

from core.execution.gui import wechat as w  # noqa: E402

CHAT = "12345678901@chatroom"
SELF = "wxid_me0000000000"
FRIEND = "wxid_friend000001"


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _zstd(raw: str) -> bytes:
    import zstandard

    return zstandard.ZstdCompressor().compress(raw.encode("utf-8"))


def _make_v4(root: Path) -> Path:
    db = root / "merge_msg.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE Name2Id (user_name TEXT PRIMARY KEY)")
    for name in (SELF, FRIEND, CHAT):
        conn.execute("INSERT INTO Name2Id (user_name) VALUES (?)", (name,))
    table = "Msg_" + _md5(CHAT)
    conn.execute(
        f"CREATE TABLE {table} (local_id INTEGER PRIMARY KEY, server_id INTEGER, "
        "local_type INTEGER, sort_seq INTEGER, real_sender_id INTEGER, create_time INTEGER, "
        "status INTEGER, upload_status INTEGER, download_status INTEGER, server_seq INTEGER, "
        "origin_source INTEGER, source TEXT, message_content BLOB, compress_content BLOB, "
        "packed_info_data BLOB)"
    )
    rows = [
        (1, 1, 1000, f"{FRIEND}: 早上好"),
        (2, 1, 1001, "我说一句"),
        (3, 49, 1002, "<?xml version=\"1.0\"?><msg><appmsg><title>卡片</title></appmsg></msg>"),
        (4, 10000, 1003, "<sysmsg type=\"sysmsgtemplate\">系统</sysmsg>"),
        (5, 1, 1004, _zstd("压过的正文")),
        (6, 47, 1005, "表情包"),
    ]
    for local_id, local_type, ts, content in rows:
        sender = 1 if local_id == 2 else 2  # 1 = 自己，2 = 好友
        conn.execute(
            f"INSERT INTO {table} (local_id, server_id, local_type, sort_seq, real_sender_id, "
            "create_time, message_content) VALUES (?,?,?,?,?,?,?)",
            (local_id, local_id, local_type, local_id, sender, ts, content),
        )
    conn.commit()
    conn.close()
    contact = root / "contact.db"
    c = sqlite3.connect(contact)
    c.execute("CREATE TABLE contact (id INTEGER PRIMARY KEY, username TEXT, remark TEXT, nick_name TEXT)")
    c.execute("INSERT INTO contact (username, remark, nick_name) VALUES (?,?,?)", (FRIEND, "小明", "小明昵称"))
    c.execute("INSERT INTO contact (username, remark, nick_name) VALUES (?,?,?)", (CHAT, "测试群", "测试群昵称"))
    c.commit()
    c.close()
    return db


def _make_legacy(root: Path) -> Path:
    db = root / "legacy.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE Msg (id INTEGER PRIMARY KEY, str_talker TEXT, str_content TEXT, "
        "is_sender INTEGER, create_time INTEGER)"
    )
    conn.execute(
        "INSERT INTO Msg (str_talker, str_content, is_sender, create_time) VALUES (?,?,?,?)",
        ("someone@chatroom", "老形状的一行", 0, 500),
    )
    conn.commit()
    conn.close()
    return db


def main() -> int:
    failed = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal failed
        if cond:
            print(f"PASS {name}")
        else:
            failed += 1
            print(f"FAIL {name} {detail}")

    with tempfile.TemporaryDirectory(dir=str(temp_base())) as tmp:
        root = Path(tmp)
        db = _make_v4(root)

        # 1) 表名 = md5(会话 id)
        conn = w._open_ro(db)
        check("4.x：Msg_<md5> 能解析", w._chat_table(conn, CHAT) == "Msg_" + _md5(CHAT))
        check("4.x：不认识的会话返回空", w._chat_table(conn, "nobody") == "")

        # 2) 自己那一行
        self_rowid = w._self_rowid(conn, SELF)
        check("自己 rowid = 1", self_rowid == 1, str(self_rowid))
        check("未知账号 rowid = 0", w._self_rowid(conn, "") == 0)
        conn.close()

        rows = w.read_recent(db, CHAT, limit=10, account_id=SELF)
        texts = [r["text"] for r in rows]
        check("读到 6 条", len(rows) == 6, str(len(rows)))
        check("按时间升序", [r["time"] for r in rows] == sorted(r["time"] for r in rows))
        check("我发的能认出来", rows[1]["mine"] is True and rows[0]["mine"] is False)
        check("前缀剥掉", texts[0] == "早上好", texts[0])
        check("昵称从 contact.db 来", rows[0]["who"] == "小明", rows[0]["who"])
        check("自己那行 who = 我", rows[1]["who"] == "我", rows[1]["who"])
        check("卡片念占位不念 XML", texts[2] == "[链接/文件]", texts[2])
        check("系统消息念占位", texts[3] == "[系统]", texts[3])
        check("zstd 能解开", texts[4] == "压过的正文", texts[4])
        check("表情念占位", texts[5] == "[表情]", texts[5])
        check("没有 XML 泄漏", not any("<msg" in t or "<sysmsg" in t or "<?xml" in t for t in texts))

        # 3) limit 与「所有会话」
        check("limit 生效", len(w.read_recent(db, CHAT, limit=2, account_id=SELF)) == 2)
        merged = w.read_recent(db, "", limit=4, account_id=SELF)
        check("无 chat 时跨表合并", len(merged) == 4, str(len(merged)))
        check("合并也按时间升序", [r["time"] for r in merged] == sorted(r["time"] for r in merged))

        # 4) 老形状兜底
        legacy = _make_legacy(root)
        old = w.read_recent(legacy, "someone@chatroom", limit=5)
        check("老形状（str_talker）还能读", len(old) == 1 and old[0]["text"] == "老形状的一行", str(old))

        # 5) 库不在要说清楚
        try:
            w.read_recent(root / "nope.db", CHAT)
            check("库不在应抛 GuiError", False)
        except w.GuiError as exc:
            check("库不在抛 not_connected", exc.code == "not_connected", exc.code)

        # 6) 开关关着不许读
        origin = w._mode
        w._mode = lambda: "off"  # type: ignore[assignment]
        try:
            w.snapshot(CHAT, db_path=db)
            check("off 档应 blocked", False)
        except w.GuiError as exc:
            check("off 档 blocked", exc.code == "blocked", exc.code)
        finally:
            w._mode = origin  # type: ignore[assignment]

        # 7) 快照标题用昵称（显式 on：这条不许依赖机器上有没有设 env）
        w._mode = lambda: "on"  # type: ignore[assignment]
        snap = w.snapshot(CHAT, db_path=db)
        check("标题含群名", "测试群" in snap.title, snap.title)
        check("快照有正文", bool(snap.text.strip()))
        check("digest 稳定", snap.digest() == w.snapshot(CHAT, db_path=db).digest())

        # 8) 纯函数：render_lines 用 who
        line = w.render_lines([{"text": "嗨", "mine": False, "time": 0, "who": "小明"}])
        check("render_lines 用 who", "小明: 嗨" in line, line)
        w._mode = origin  # type: ignore[assignment]

    print(f"RESULT {failed} failed")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
