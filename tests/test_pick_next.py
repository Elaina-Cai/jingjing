"""pick_next / salience：按显著度挑会话（2026-10-31 取代“最久没人理、取前 N 个”）。

为什么要有这一条：旧实现按 last_at 升序取前 3 个，结果“刚说完话的人”被排到最后截断
（2026-10-30 实测：主人的私聊被挤出名单，她没回）。新判据把所有活着的窗口算成一个
可解释的分，一次挑最高的那个；分项留在返回值里，便于在 trace 里回答“为什么先看它”。
"""

import sys
import pathlib
from datetime import datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loop import run as R  # noqa: E402

_NOW = datetime(2026, 10, 31, 12, 0, 0)
_FAILED = 0
_TOTAL = 0


def check(name: str, cond: bool, detail="") -> None:
    global _FAILED, _TOTAL
    _TOTAL += 1
    if not cond:
        _FAILED += 1
        print("FAIL " + name + (" :: " + repr(detail) if detail != "" else ""))


def _cell(chat: str, *, tier: str = "", after: bool = False, mins: float = 1) -> dict:
    at = (_NOW - timedelta(minutes=mins)).strftime("%Y-%m-%d %H:%M:%S")
    return {"chat_id": chat, "label": chat, "tier": tier,
            "after_mine": ["x"] if after else [], "last_at": at}


def main() -> bool:
    R._OWNER_CACHE.clear()
    R._OWNER_CACHE["wxid_owner"] = True
    for other in ("g1@chatroom", "g2@chatroom", "g3@chatroom", "g4@chatroom"):
        R._OWNER_CACHE[other] = False

    owner_ask = _cell("wxid_owner", tier="ask", mins=1)
    group_ask = _cell("g1@chatroom", tier="ask", mins=30)
    fresh_talk = _cell("g2@chatroom", after=True, mins=1)
    stale_talk = _cell("g4@chatroom", after=True, mins=40)
    idle = {"chat_id": "g3@chatroom", "label": "g3", "tier": "", "after_mine": [], "last_at": ""}

    s_ask, _ = R.salience(owner_ask, now=_NOW)
    s_talk, _ = R.salience(fresh_talk, now=_NOW)
    check("问我的分高于只是有人在说", s_ask > s_talk, (s_ask, s_talk))

    s_stale, p_stale = R.salience(stale_talk, now=_NOW)
    check("新鲜度起作用（新 > 旧）", s_talk > s_stale, (s_talk, s_stale))
    check("等太久会升级（wait > 0）", p_stale["wait"] > 0, p_stale)

    plain = _cell("g1@chatroom", tier="ask", mins=30)
    check("主人私聊加权", R.salience(owner_ask, now=_NOW)[0] > R.salience(plain, now=_NOW)[0])
    check("没动静的会话不进候选", R.salience(idle, now=_NOW)[0] == 0.0)

    snap = {"cells": [group_ask, fresh_talk, owner_ask]}
    key, detail = R.pick_next(snap, now=_NOW)
    check("选中最高分", key == "wxid_owner", (key, detail))
    check("明细带分项与年龄", set(detail.get("parts", {})) == {"need", "fresh", "wait", "owner", "quiet"}
          and "age_min" in detail, detail)
    key2, _ = R.pick_next(snap, taken=(key,), now=_NOW)
    check("跳过已选的会话", key2 == "g1@chatroom", key2)
    check("空快照返回空", R.pick_next({"cells": []}, now=_NOW) == ("", {}))

    print("RESULT %d failed" % _FAILED)
    return _FAILED == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
