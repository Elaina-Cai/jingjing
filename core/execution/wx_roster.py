"""群成员关系的**适配层**读取口（CB-139 甲的微信侧）。

只做一件事：把本机解密副本（``data/decrypted/wechat/<wxid>/contact.db``）里的
``chat_room`` / ``chatroom_member`` / ``contact`` 三张表读成 {chat_id: {wxid: 昵称}}。
表结构与边界（没同步过、副本过期）由 ``core/context/room_members.py`` 那边的通用表负责，
这里不碰"该不该让她知道"——那是判定层的事。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

def read_decrypted(contact_db: Path | str) -> dict[str, dict[str, str]]:
    """读解密副本 → ``{chat_id: {wxid: nick}}``（纯读，打不开就抛，不吞）。"""
    con = sqlite3.connect("file:%s?mode=ro" % Path(contact_db).as_posix(), uri=True)
    try:
        nick = {
            int(r[0]): (str(r[1]), str(r[2] or "") or str(r[1]))
            for r in con.execute("select id, username, nick_name from contact")
        }
        rooms = {int(r[0]): str(r[1]) for r in con.execute(
            "select id, username from chat_room"
        )}
        out: dict[str, dict[str, str]] = {u: {} for u in rooms.values()}
        for room_id, member_id in con.execute("select room_id, member_id from chatroom_member"):
            room = rooms.get(int(room_id))
            got = nick.get(int(member_id))
            if not room or not got:
                continue
            out[room][got[0]] = got[1]
        return out
    finally:
        con.close()


def read_owners(contact_db: Path | str) -> dict[str, str]:
    """``{chat_id: owner_wxid}``（群主）。拿不到就是空字典，不猜。"""
    con = sqlite3.connect("file:%s?mode=ro" % Path(contact_db).as_posix(), uri=True)
    try:
        return {str(u): str(o or "") for u, o in con.execute("select username, owner from chat_room")}
    finally:
        con.close()
