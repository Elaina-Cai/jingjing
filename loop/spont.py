"""自发开口的闸门（2026-11-04）。

2026-11-04 18:46，她在「示例作业群」自问自答了三遍自己编的报到安排：那一拍是自发醒，
群里 57 分钟没人说话，`spont_streak` 已经 12 连。她后来自己在账本里写「再说一句就只是
刷存在感」——这条判断当时没有代码替她守住，现在补上。

两条闸门：

1. **连发上限**：连续 `SPONT_STREAK_MAX` 次自发开口都没人接，就停；有人说话
   （`selfstate.note_tick(heard=True)`）会把 streak 归零，那时自动恢复。
2. **没人说话就不主动开口**：最近 `GROUP_QUIET_S` 秒里没有任何人来过消息（`hot_chats` 为空），
   自言自语除了制造噪声没有别的作用。
"""

from core.infra.envflags import text as _text

SPONT_STREAK_MAX = int(_text("YINYUE_SPONT_STREAK_MAX", "3") or "3")
GROUP_QUIET_S = float(_text("YINYUE_SPONT_QUIET_S", "1800") or "1800")
__all__ = ["SPONT_STREAK_MAX", "GROUP_QUIET_S", "gate"]


def addressed_allows(addressed, *, is_group: bool) -> bool:
    """群里没有指向她的话，就不许开新话题（用户 2026-11-04 选的「松档」B）。

    私聊不受这条约束 —— 私聊里对方开口本来就是要回的。
    ``addressed`` 是 ``loop/addressing.py`` 的 ``Verdict.kind``：``named`` / ``called`` /
    ``following`` / ``questioned`` 都算"有人在跟我说话"，空串算"群里的闲聊"。
    """
    if not is_group:
        return True
    return bool(str(addressed or "").strip())


def gate(*, streak: int, last_heard: float, now: float, hot_chats=None):
    """这一拍能不能自发开口。返回 `(allowed, reason)`；`reason` 只在拦下时有值。

    - `streak` 是「连续自发开口、没人接」的次数（`selfstate` 的 `pulse.spont_streak`）
    - `hot_chats` 是最近 `GROUP_QUIET_S` 秒里真有人说过话的会话集合
    """
    n = max(0, int(streak or 0))
    if n >= SPONT_STREAK_MAX:
        return False, "streak=%d" % n
    hot = {str(c) for c in (hot_chats or ()) if str(c or "")}
    if not hot:
        # 一个人都没说话：这一拍的自发开口必然是自问自答。
        return False, "quiet>%ds" % int(GROUP_QUIET_S)
    if float(last_heard or 0.0) > float(now or 0.0):
        # 时钟回拨 / 未来的 last_heard：当「安静」处理，别拿它当活跃证据。
        return False, "clock"
    return True, ""
