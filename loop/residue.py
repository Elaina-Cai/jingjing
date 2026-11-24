"""输入框残留的计数器（纯状态机，不碰 UI、不读环境变量）。

为什么单独一个模块
------------------
2026-11-04 主人在旁边看到「她在 Elaina 私聊 / 作业群 / 银月的测试群 之间来回切，
40 秒才发出去」。查日志：desk 是单线程串行，一条消息发失败就取下一行，而三行
分别属于三个会话 —— 于是每失败一次就换一个窗口激活。真正卡住的是**输入框里
有上一轮没发出去的残句**，而「清残留」这个动作自己也在失败（`desk.log` 里
`desk_residue_cleared ok=false` 连了三次），所以每条都在同一步上打转，直到
某一次侥幸清干净。

这里只放两件事：数到第几次该停下、停下之后别再把这条排回退避。
判断为什么在纯函数里：它不需要窗口、不需要键鼠，能单测；而 `desk.deliver`
要驱动真实微信，没法在测试里跑。
"""
from __future__ import annotations

__all__ = [
    "RESIDUE_MARKER",
    "RESIDUE_STRIKE_LIMIT",
    "note_strike",
    "reset",
    "should_terminate",
    "take_terminal",
]

#: 发送层报「框里已经有字」时用的那句话（认这条即算一次残留失败）。
RESIDUE_MARKER = "输入框里已经有字"

#: 同一条消息连续几次残留失败就判死。三次：一次是自己贴残的、一次是自愈没成，
#: 第三次还没好就说明不是偶发，再重试只是让窗口继续来回跳。
RESIDUE_STRIKE_LIMIT = 3

_STRIKES: dict = {}
_TERMINAL: set = set()


def note_strike(item_id, why: str = "") -> tuple:
    """记一次残留失败。返回 ``(why, 次数, 是否判死)``。

    判死时把次数清掉（这条不会再重试了，留着计数没意义），并在 ``why`` 后面
    补一句给人和给自己看的原因 —— 主人从面板上看到的就是这句话。
    """
    sid = int(item_id or 0)
    n = _STRIKES.get(sid, 0) + 1
    if n >= RESIDUE_STRIKE_LIMIT:
        _STRIKES.pop(sid, None)
        _TERMINAL.add(sid)
        note = "%s（连续 %d 次清不掉输入框，先停下等人看一眼）" % (str(why or ""), n)
        return note[:200], n, True
    _STRIKES[sid] = n
    return str(why or ""), n, False


def reset(item_id) -> None:
    """这条已经发出去了：计数清零（下次偶发失败还从 1 开始数）。"""
    _STRIKES.pop(int(item_id or 0), None)


def should_terminate(item_id) -> bool:
    """只问不取：这条是不是已经被判死了。"""
    return int(item_id or 0) in _TERMINAL


def take_terminal(item_id) -> bool:
    """问并取走判死标记（settle 时用一次）。"""
    sid = int(item_id or 0)
    if sid in _TERMINAL:
        _TERMINAL.discard(sid)
        return True
    return False
