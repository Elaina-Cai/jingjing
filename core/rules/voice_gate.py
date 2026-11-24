# -*- coding: utf-8 -*-
"""语音能不能发：触发的前置条件（BR-062，2026-10-29）。

队列原文（`docs/该抄的清单.md:62`）：

    BR-062 — 触发条件可计算：主人最近 10 分钟开过口且之后没发过语音，才允许再发。

同一份清单里还有一条语气（`PIT-094` 一带）：**模型只能请求，不能决定**。
所以「能不能发语音」必须是算出来的，不是她觉得自己可以。

这里只回答一件事：**这个会话、这一刻，允许发语音吗。** 它不回答
「这一句要不要变语音」（那是 `loop/voice_out.decide()`：模式、长度、可念性），
也不回答「发得出去吗」（那是 `core/speech/voice_send.py` 的平台能力表）。

判定只用调用方给的事实，本模块**不查库**：

    chat_kind        private / group（账库 messages.chat_type 的原词）
    who              这个会话里最近开口的人是不是主人
    owner_spoke_at   主人最近一次开口的时刻（epoch 秒）
    owner_key        chat_id + '|' + msg_id —— 主人那条消息的账目键
    used             已经换过语音的键集合

为什么单独一层（踩坑档案 #14 的教训）：

* 阈值只许一份 —— `WINDOW_S = 600` 之外，全库不许再出现「10 分钟」的字面量；
* 判据只许一个出口 —— 别处不许再判「主人开没开口」；
* 账目只许一个写口 —— `mark_used()`，其余函数只读。

三个反例（写在这里，免得以后被「简化」掉）：

1. 不要用「群里最近一条消息是不是主人发的」代替 `owner_spoke_at` ——
   主人两小时前说过话，也会被当成刚开口。
2. 不要用「已经发过语音」代替「这条主人消息已经用掉了」 ——
   群里主人连发三句，只该换一段声音。
3. 算不出主人是谁就当没开口（fail closed）—— 宁可少发一段声音，
   也不要在群里对着陌生人开口。

路径：`loop/ports.py:_maybe_voice()` 收事实 → `loop/voice_out.decide()` 调
`allow()` → 放行才合成与发送 → 真发出去了才 `mark_used()`。
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from core.infra import tempo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MARKER = os.path.join(ROOT, 'data', '.voice_trigger.json')

WINDOW_S = tempo.s("voice.window")  # 主人「最近开过口」的唯一口径；数值在 core/infra/tempo.py

PRIVATE_KINDS = frozenset({'private', 'friend', 'direct', 'dm', 'one_to_one', 'person'})
GROUP_KINDS = frozenset({'group', 'chatroom', 'chat_room', 'groupchat', 'group_chat', 'room'})

_MEM = None             # 进程内镜像：省掉每句话读盘，也让测试能塞自己的账


def key(chat_id, msg_id) -> str:
    """主人那条消息的账目键。群里一条消息最多换一段声音，靠它去重。"""
    return '%s|%s' % (str(chat_id or ''), str(msg_id or ''))


def spoke_at_from_text(value) -> float | None:
    """账库 created_at（形如 '2026-06-11 12:49:40.000000'）→ epoch 秒。

    换算只在这一处做；换不出来给 None，调用方按「主人没开过口」处理。
    """
    raw = str(value or '').strip()
    if not raw:
        return None
    if raw.lstrip('-').isdigit():
        num = float(raw)
        return num / 1000.0 if len(raw) > 12 else num
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S',
                '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(raw, fmt).timestamp()
        except ValueError:
            continue
    return None


def allow(*, chat_kind: str, who: str, owner_spoke_at: float | None = None,
          now: float | None = None, owner_key: str = '', used=frozenset(),
          allow_anyone: bool = False, window_s: float = WINDOW_S) -> tuple[bool, str]:
    """这个会话这一刻允不允许发语音。返回 (要不要, 原因)。"""
    kind = str(chat_kind or '').strip().lower()
    person = str(who or '').strip().lower()
    if allow_anyone:
        return True, 'anyone_allowed'
    if kind in PRIVATE_KINDS:
        if person == 'owner':
            return True, 'private_owner'
        return False, 'not_owner_private'
    if kind in GROUP_KINDS:
        if owner_spoke_at is None:
            return False, 'owner_silent'
        try:
            spoke = float(owner_spoke_at)
        except (TypeError, ValueError):
            return False, 'owner_silent'
        if spoke <= 0:
            return False, 'owner_silent'
        clock = float(time.time() if now is None else now)
        gap = clock - spoke
        if gap > float(window_s) or gap < -float(window_s):
            # 未来时间也不开门：时钟/时区对不上时，安静比抢话好
            return False, 'owner_stale'
        anchor = str(owner_key or '').strip()
        if not anchor:
            return False, 'no_anchor_msg'
        if anchor in (used or ()):
            return False, 'already_replied_to_this_msg'
        return True, 'group_owner_recent'
    return False, 'unknown_chat_kind'


def _today(now=None) -> str:
    return time.strftime('%Y-%m-%d', time.localtime(float(time.time() if now is None else now)))


def _load(now=None) -> dict:
    """账目（进程内镜像 + 落盘）。

    坏文件不当空：账本坏了意味着「不知道这条消息换过声音没有」，当空就等于允许再发一次
    （CA-057 说的把坏数据当放宽）。这里只把状态记下来，要不要收紧由 _gate 决定——
    allow() 保持纯函数，不在里面摸全局状态。
    """
    global _MEM
    day = _today(now)
    if _MEM is None:
        data = {}
        try:
            from core.infra.jsonio import read_json_checked
            got, status = read_json_checked(MARKER, default={})
            globals()['_BOOK_STATUS'] = status
            data = got if isinstance(got, dict) else {}
        except Exception:  # noqa: BLE001
            data = {}
        keys = data.get('keys') if data.get('day') == day else []
        _MEM = {'day': day, 'keys': [str(k) for k in (keys or [])]}
    if _MEM.get('day') != day:
        _MEM = {'day': day, 'keys': []}         # 跨天清零：旧键没有复用意义，文件也不膨胀
    return _MEM


def ledger_corrupt() -> bool:

    """账本读坏了吗。只在那次读取失败后为真（_load 里记的）。"""

    return globals().get('_BOOK_STATUS') == 'corrupt'


def used_keys(*, now=None) -> frozenset:
    return frozenset(_load(now).get('keys') or ())


def mark_used(chat_id, msg_id, *, now=None) -> bool:
    """唯一写口：记下「这条主人消息已经换过声音」。真发出去之后才调。"""
    if not str(msg_id or '').strip():
        return False
    anchor = key(chat_id, msg_id)
    book = _load(now)
    if anchor in book['keys']:
        return False
    book['keys'].append(anchor)
    try:
        from core.infra.jsonio import atomic_write_json
        atomic_write_json(MARKER, {'day': book['day'], 'keys': list(book['keys'])})
    except Exception:  # noqa: BLE001
        pass    # 落盘失败不回滚内存：本进程内照样不会重复发
    return True


def reset() -> None:
    """测试用：清掉进程内镜像（不动磁盘）。"""
    global _MEM
    _MEM = None
    globals()['_BOOK_STATUS'] = 'ok'
