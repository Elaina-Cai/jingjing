#!/usr/bin/env python
"""「现在开着哪个会话」：按可信度排队，认不准就不猜。

四档证据（从准到不准）：explicit 1.0 / anchor 0.9 / ocr 0.7 / recent_db 0.3；
重名 → ambiguous 0.5 且 chat_id 为空（错会话比没会话更坏）。
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

CHAT_A = "10000000007@chatroom"
CHAT_B = "10000000008@chatroom"
NAME_A = "甲群"
NAME_B = "乙群"


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _make_db(root: Path, chats: dict[str, tuple[str, int]]) -> Path:
    db = root / "merge_msg.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE Name2Id (user_name TEXT PRIMARY KEY)")
    for chat in chats:
        conn.execute("INSERT INTO Name2Id (user_name) VALUES (?)", (chat,))
    for chat, (_name, ts) in chats.items():
        table = "Msg_" + _md5(chat)
        conn.execute(
            f"CREATE TABLE {table} (local_id INTEGER PRIMARY KEY, local_type INTEGER, "
            "real_sender_id INTEGER, create_time INTEGER, message_content TEXT)"
        )
        conn.execute(
            f"INSERT INTO {table} (local_id, local_type, real_sender_id, create_time, message_content) "
            "VALUES (1, 1, 0, ?, ?)",
            (ts, f"{chat} 的一条"),
        )
    conn.commit()
    conn.close()
    contact = root / "contact.db"
    c = sqlite3.connect(contact)
    c.execute("CREATE TABLE contact (id INTEGER PRIMARY KEY, username TEXT, remark TEXT, nick_name TEXT)")
    for chat, (name, _ts) in chats.items():
        c.execute("INSERT INTO contact (username, remark, nick_name) VALUES (?,?,?)", (chat, name, name))
    c.commit()
    c.close()
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

    # 0) 档位本身：越靠屏幕越准
    conf = w.SOURCE_CONFIDENCE
    check(
        "档位单调：explicit > anchor > ocr > recent_db",
        conf[w.SOURCE_EXPLICIT] > conf[w.SOURCE_ANCHOR] > conf[w.SOURCE_OCR] > conf[w.SOURCE_RECENT_DB],
        str(conf),
    )
    check("recent_db 只有 0.3（它不知道屏幕）", conf[w.SOURCE_RECENT_DB] == 0.3)

    with tempfile.TemporaryDirectory(dir=str(temp_base())) as tmp:
        root = Path(tmp)
        db = _make_db(root, {CHAT_A: (NAME_A, 1000), CHAT_B: (NAME_B, 2000)})

        # 1) 调用方直接给：最准，其它证据一律不看
        got = w.resolve_current_chat(
            explicit=CHAT_A, anchor=lambda: CHAT_B, ocr=lambda: NAME_B, db_path=db
        )
        check("explicit 胜出", got.chat_id == CHAT_A and got.source == w.SOURCE_EXPLICIT)
        check("explicit 置信度 1.0", got.confidence == 1.0)

        # 2) 锚点：我们开的那个
        got = w.resolve_current_chat(anchor=lambda: CHAT_B, ocr=lambda: NAME_A, db_path=db)
        check("anchor 胜过 ocr", got.chat_id == CHAT_B and got.source == w.SOURCE_ANCHOR, got.as_dict())
        check("anchor 置信度 0.9", got.confidence == 0.9)

        # 3) 锚点探针炸了 → 退到下一档，不许把回合打挂
        def boom() -> str:
            raise RuntimeError("probe down")

        got = w.resolve_current_chat(anchor=boom, ocr=lambda: NAME_A, db_path=db)
        check("锚点炸了不影响解析", got.chat_id == CHAT_A and got.source == w.SOURCE_OCR, got.as_dict())

        # 4) OCR：屏幕上读到的名字，按 contact.db 反查
        got = w.resolve_current_chat(ocr=lambda: NAME_B, db_path=db)
        check("ocr 反查到会话", got.chat_id == CHAT_B and got.source == w.SOURCE_OCR, got.as_dict())
        check("ocr 记下读到的名字", got.display == NAME_B)

        # 5) OCR 读到不认识的名字 → 弃权，不猜
        got = w.resolve_current_chat(ocr=lambda: "没这个群", db_path=db)
        check("认不出就弃权", got.chat_id == "" and got.source == w.SOURCE_AMBIGUOUS)
        check("弃权置信度 0.5", got.confidence == 0.5)
        check("弃权不算认识", got.known is False)

        # 6) 重名：两个会话同名 → 也弃权
        dup = Path(tmp) / "dup"
        dup.mkdir()
        db2 = _make_db(dup, {CHAT_A: ("同名群", 1000), CHAT_B: ("同名群", 2000)})
        got = w.resolve_current_chat(ocr=lambda: "同名群", db_path=db2)
        check("重名弃权", got.chat_id == "" and got.source == w.SOURCE_AMBIGUOUS, got.as_dict())

        # 7) 什么证据都没有 → 数据库最近活跃（0.3），并说清楚
        got = w.resolve_current_chat(db_path=db)
        check("退到数据库最近活跃", got.chat_id == CHAT_B and got.source == w.SOURCE_RECENT_DB, got.as_dict())
        check("最近活跃置信度 0.3", got.confidence == 0.3)

        # 8) 库都不在 → none
        got = w.resolve_current_chat(db_path=root / "nope.db")
        check("没有库就是 none", got.source == w.SOURCE_NONE and got.confidence == 0.0)

        # 9) 标题必须写出证据档（数据库那档不许冒充屏幕）
        check(
            "最近活跃会在标题里声明",
            "不是屏幕上的会话" in w._title_with_evidence(NAME_B, 3, w.SOURCE_RECENT_DB),
        )
        check("锚点也会声明", "锚点" in w._title_with_evidence(NAME_A, 3, w.SOURCE_ANCHOR))
        check("explicit 不加多余话", w._title_with_evidence(NAME_A, 3, w.SOURCE_EXPLICIT) == f"微信 · {NAME_A} · 3 条")

        # 10) snapshot(chat="") 走解析，并在标题里声明
        origin = w._anchor_chat
        w._anchor_chat = lambda: ""  # type: ignore[assignment]
        try:
            w._mode = lambda: "on"  # type: ignore[assignment]
            snap = w.snapshot(db_path=db)
            check("快照标题声明最近活跃", "不是屏幕上的会话" in snap.title, snap.title)
            check("快照正文非空", bool(snap.text.strip()))
            # 11) 认不准时抛 no_target，而不是编一个会话
            origin_resolver = w.resolve_current_chat
            w.resolve_current_chat = lambda **_kw: w.CurrentChat(  # type: ignore[assignment]
                source=w.SOURCE_AMBIGUOUS, confidence=0.5
            )
            try:
                w.snapshot(db_path=db)
                check("重名时快照应报 no_target", False)
            except w.GuiError as exc:
                check("重名时快照报 no_target", exc.code == "no_target", exc.code)
            finally:
                w.resolve_current_chat = origin_resolver  # type: ignore[assignment]
        finally:
            w._anchor_chat = origin  # type: ignore[assignment]

        # 12) 锚点必须过期作废
        import core.execution.wx_chat as wx

        saved = dict(wx._ACTIVE_CHAT)
        try:
            wx._ACTIVE_CHAT.clear()
            wx._ACTIVE_CHAT.update(
                {"at": 1.0, "raw_who": CHAT_A, "display": NAME_A, "identity": CHAT_A}
            )
            check("过期锚点返回空", wx.active_chat_id() == "", wx.active_chat_id())
            wx._ACTIVE_CHAT.update({"at": __import__("time").time()})
            check("新鲜锚点返回会话", wx.active_chat_id() == CHAT_A, wx.active_chat_id())
        finally:
            wx._ACTIVE_CHAT.clear()
            wx._ACTIVE_CHAT.update(saved)

    print(f"RESULT {failed} failed")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
