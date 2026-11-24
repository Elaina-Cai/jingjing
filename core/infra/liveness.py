"""心跳的唯一契约，和唯一的判死公式。

为什么要有这个文件
------------------
2026-10-28 之前：引擎把心跳写进 `logs/loop_engine.json`，监管器却去读
`logs/loop_heartbeat.json`（那是 scheduler 的心跳）——**没人看引擎的心跳**。
引擎卡死时，监管器一切正常，只有人肉等「端口不开」才发现。

同一天还发现另一个形状：判死靠写死的秒数（`max_age_s = 120.0`），
于是节拍一改（拍子 90 秒超时 + 4 秒间隔），判死的阈值就和现实脱钩。
两个都是「量词散落在各处」。

本文件的规矩
------------
1. 生产者（引擎 / scheduler）自己声明 `beat_ms`：**下一次写心跳最多多久之后**。
2. 消费者（监管器 / 面板）只问 `alive(name)`，不自己算秒数。
3. 判死只有一条公式：`now_ms - at_ms > beat_ms * GRACE`（实现只在
   `core/infra/duration.py` 的 `deadline_ms()`，GRACE 也在那里定义）。
4. 心跳文件名只在这里出现（`scripts/check.py` 的 `hbpath` 规则会盯着）。
"""

from __future__ import annotations

import ctypes
import json
import os

from core.infra import duration
from core.infra.duration import now_ms, human  # noqa: F401

LOGS_DIR = os.path.join(os.getcwd(), "logs")

# 文件名 → 生产者。除契约字段（pid / at_ms / beat_ms）外都是自由字段。

def heartbeat_names() -> dict:
    """心跳文件名唯一出处（别处要读这些文件就取这里，别再抄字符串）。"""
    return dict(_HEARTBEATS)

_HEARTBEATS = {
    "engine": "loop_engine.json",
    "scheduler": "loop_heartbeat.json",
    # QQ 的「我还在听」：OneBot 正向 WS 连着的时候由 provider 自己写。
    "qq": "qq_link.json",
}


def path(name: str) -> str:
    """心跳文件的绝对路径。名字不认识就抛错——静默返回空路径会让调用方以为文件不存在。"""
    leaf = _HEARTBEATS.get(name)
    if not leaf:
        raise KeyError("未知的心跳名：%r（已登记：%s）" % (name, ", ".join(sorted(_HEARTBEATS))))
    return os.path.join(LOGS_DIR, leaf)


def read(name: str) -> dict:
    """读一份心跳；文件不在或读坏都返回空 dict（心跳坏了不等于进程死了）。"""
    try:
        with open(path(name), encoding="utf-8") as fh:
            got = json.load(fh)
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def pid_alive(pid) -> bool:
    """这个 pid 还在吗（心跳文件会残留，不能只看文件新不新）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        # 句柄能拿到 ≠ 进程还在（句柄还没回收时也会成功），再看一眼退出码
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CloseHandle(h)
        return (code.value == 259) if ok else True  # 259 = STILL_ACTIVE
    except Exception:  # noqa: BLE001 - 查不到就当没了
        return False


def _ms(got: dict, key: str) -> int:
    """读一个毫秒字段；读不出来算 0（= 契约没被满足，由 state() 判 unknown）。"""
    try:
        return int(got.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def age_ms(name: str) -> int | None:
    """心跳多久没更新了（毫秒）。读不出来返回 None（面板要显示“不知道”，不要显示 0）。"""
    at = _ms(read(name), "at_ms")
    if at <= 0:
        return None
    return max(0, duration.now_ms() - at)


def deadline_ms(name: str) -> int | None:
    """这个心跳多久不更新就算死（毫秒）。生产者没声明 beat_ms 就返回 None（= 不可判）。"""
    beat = _ms(read(name), "beat_ms")
    if beat <= 0:
        return None
    return duration.deadline_ms(0, beat)


def state(name: str) -> str:
    """这个心跳现在是什么状态。**「判死」只有这一处**，别处不许再算一遍。

    四态而不是布尔，因为「不符合契约」和「死了」是两件事：
      alive   —— 心跳够新、pid 还在
      stale   —— pid 在，但心跳超过 beat_s × GRACE 没更新（卡死）
      gone    —— 没有心跳文件，或者 pid 已经不在了
      unknown —— 心跳缺 at / 缺 beat_s，契约没被满足，**不可判**
    unknown 不许当成 dead：旧代码写的旧格式心跳、字段名写错、生产者忘了声明
    beat_s，都会落到这里。把它当 dead 就会误杀正在跑的进程（升级瞬间尤其危险），
    把它当 alive 又会漏掉真的卡死——所以它单独算一态，由消费方各自决定怎么办。
    消费方的规矩：不许 kill、不许对外宣称"世界停了"，只许记一条响的日志。
    """
    got = read(name)
    if not got:
        return "gone"
    if not pid_alive(got.get("pid")):
        return "gone"
    at = _ms(got, "at_ms")
    beat = _ms(got, "beat_ms")
    if at <= 0 or beat <= 0:
        return "unknown"
    if duration.now_ms() > duration.deadline_ms(at, beat):
        return "stale"
    return "alive"


def contract(name: str) -> list[str]:
    """这个心跳违反了契约哪几条（空列表 = 合规）。给人看，也方便测试。"""
    got = read(name)
    if not got:
        return ["没有心跳文件"]
    bad = []
    if not got.get("pid"):
        bad.append("缺 pid")
    if _ms(got, "at_ms") <= 0:
        bad.append("缺 at_ms（epoch 毫秒）")
    if _ms(got, "beat_ms") <= 0:
        bad.append("缺 beat_ms（生产者没声明节拍）")
    return bad


def alive(name: str) -> bool:
    """活着 = state() 是 alive。保留这个布尔入口给只想问“活没活”的调用点。"""
    return state(name) == "alive"


def why(name: str) -> str:
    """判死的理由（给人看，不参与判断）。"""
    got = read(name)
    if not got:
        return "心跳文件不存在或读不出来"
    probs = contract(name)
    if probs:
        return "心跳不符合契约：" + "、".join(probs)
    st = state(name)
    at = _ms(got, "at_ms")
    if st == "gone":
        return "心跳新，但 pid=%r 不在了" % (got.get("pid"),)
    if st == "stale":
        beat = _ms(got, "beat_ms")
        return "心跳 %s 没更新，超过上限 %s（beat_ms=%s × GRACE=%s）" % (
            duration.human(duration.now_ms() - at),
            duration.human(duration.deadline_ms(0, beat)),
            duration.human(beat), duration.GRACE)
    return "活着"
