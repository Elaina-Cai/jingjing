# -*- coding: utf-8 -*-
"""BR-062 要的那几个事实，只读一次账库（2026-10-29）。

`loop/voice_out.decide()` 要判「主人最近 10 分钟开过口吗」，但它不该知道 SQL
长什么样；这里把账库里的那几行读成一份字典交给它：

    {'chat_kind': 'group', 'who': 'owner' 或 'other',
     'owner_spoke_at': float 或 None, 'owner_key': 'chat_id|msg_id',
     'owner_msg_id': str}

两条原则：

* **读不出来就给一份「什么都没」的事实**（`chat_kind` 为空），让
  `core.rules.voice_gate` 里的判据 fail closed —— 拿不准时绝不开口；
  把「不许」写在上一层，而不是在这里悄悄放过。
* 身份走 `core.rules.engine.is_owner()`（确定性身份服务的唯一转发点，解析
  不出来就是 False），不在这里猜主人号 —— 踩坑档案 #13 的口径：写死的主人号
  换号之后会变成「对陌生人开口」。
"""
from __future__ import annotations

import os
import sqlite3

SCAN = 50       # 只回看最近 50 条：够覆盖「10 分钟内开口」，又不至于把整个群读一遍

BLANK = {'chat_kind': '', 'who': 'other', 'owner_spoke_at': None,
         'owner_key': '', 'owner_msg_id': ''}


def facts(db_path, *, chat_id, platform: str = 'wechat') -> dict:
    """这个会话的两三件事：是不是群、主人最近一次开口的时刻与那条消息号。"""
    if not db_path or not str(chat_id or '').strip() or not os.path.exists(str(db_path)):
        return dict(BLANK)
    con = None
    rows = []
    try:
        con = sqlite3.connect('file:%s?mode=ro' % str(db_path).replace('\\', '/'), uri=True)
        rows = con.execute(
            'select msg_id, sender_id, sender_name, chat_type, created_at'
            ' from messages where chat_id = ? order by created_at desc limit ?',
            (str(chat_id), SCAN)).fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass
    if not rows:
        return dict(BLANK)
    from core.rules.engine import is_owner
    from core.rules.voice_gate import key, spoke_at_from_text

    kind = str(rows[0][3] or '').strip().lower()
    if not kind:
        kind = 'group' if looks_group(chat_id) else 'private'
    for msg_id, sender_id, sender_name, _kind, created_at in rows:
        try:
            mine = bool(is_owner(platform=platform, sender_id=str(sender_id or ''),
                                 sender_name=str(sender_name or '')))
        except Exception:  # noqa: BLE001
            mine = False
        if not mine:
            continue
        return {'chat_kind': kind, 'who': 'owner',
                'owner_spoke_at': spoke_at_from_text(created_at),
                'owner_key': key(chat_id, msg_id), 'owner_msg_id': str(msg_id or '')}
    return {'chat_kind': kind, 'who': 'other', 'owner_spoke_at': None,
            'owner_key': '', 'owner_msg_id': ''}


def looks_group(chat_id) -> bool:
    """`chat_type` 缺列时的兜底：微信群 xxx@chatroom、QQ 群 group_xxx。"""
    cid = str(chat_id or '')
    return '@chatroom' in cid or cid.startswith('group_') or '_group' in cid
