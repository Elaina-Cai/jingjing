"""她「能看见什么、能动手到哪」的唯一读法（只读，不写任何东西）。

它回答的问题
------------
平台登记表（`core/execution/platform_registry.py` 的 `PlatformDef.senses`）说的是
「这个平台**应该**有什么能力」；这里说的是「**现在**真的有没有」。

两条轴
------
- `perceive` 看得见：收消息、看图、看动态。它坏了是**静默**的 —— 和「没人说话」
  长得一模一样，所以只能由「这条通道自己的心跳」来证明（`Sense.proof="beat"`）。
- `act` 动得了手：打字、发图、撤回。它坏了是**响亮**的 —— 抛错、被拒、没回执，
  看回执就行，所以不设时效。

三条纪律
--------
1. 读不出来就是 `unknown`，绝不写成 `ok`（踩坑档案 #27：拿不到结论不能写成零）。
2. 判据按 `proof` 走，不许写 `if platform == "wechat"` 这种分支 —— 否则加平台就得
   改判据，和 BR-009 要修的病一样。
3. 这是观测，不是告警：这里不重启、不重连、不写一条状态。
"""

from __future__ import annotations

import os
import time

from core.execution.platform_registry import PlatformDef, all_platforms
from core.infra import liveness, skips

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DECRYPTED = os.path.join(ROOT, "data", "decrypted")

SHAPE_SAYS = {
    "both": "看得见，也动得了手",
    "see_only": "看得见，但动不了手",
    "act_only": "手能动，但看不见（会对着过时的世界动作）",
    "none": "还没接",
}
EVIDENCE_SAYS = {"ok": "刚看过", "stale": "有点旧", "unknown": "查不到证据"}


def _newest_mtime(root: str) -> float | None:
    best = None
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in ("__pycache__", ".git")]
        for fn in fns:
            try:
                m = os.path.getmtime(os.path.join(dp, fn))
            except OSError as exc:  # 遍历途中文件被删/占用：这一次没有它，不是「没有证据」
                skips.skip("senses._newest_mtime", "absent", repr(exc)[:120])
                continue
            if best is None or m > best:
                best = m
    return best


# 走心跳的探针：source → liveness 里登记的心跳名。
# 判死只有一处（core/infra/liveness.py::state），这里只做映射，不自己算秒数。
LIVENESS_BEATS = {"peer_push": "qq"}


def age_ms(source: str, *, now: float | None = None) -> float | None:
    """这条证据多久没更新了（毫秒）。读不出来返回 None —— 不是 0，也不是「很新」。"""
    now = time.time() if now is None else now
    if source == "decrypted":
        newest = _newest_mtime(DECRYPTED)
    elif source in LIVENESS_BEATS:
        return _liveness_age_ms(LIVENESS_BEATS[source])
    else:
        newest = None  # 还没接探针：不许假装新鲜
    if newest is None:
        return None
    return max(0.0, (now - newest) * 1000.0)


def _liveness_age_ms(name: str) -> float | None:
    """把心跳状态翻成「多久没更新」。读不出来仍是 None —— 不冒充新鲜。"""
    try:
        state = liveness.state(name)
    except Exception:  # noqa: BLE001 - 名字没登记/文件坏了：不知道，不是「活着」
        return None
    if state in ("alive", "stale"):
        age = liveness.age_ms(name)
        return None if age is None else float(age)
    if state == "gone":
        return 1e12  # 文件/进程没了：极旧（不是「查不到」，是「没在听」）
    return None


def evidence(sense, *, now: float | None = None) -> str:
    """一条能力的证据状态：ok / stale / unknown（操作类看回执，这里只回 declared）。"""
    if sense.proof == "ack":
        return "declared"
    age = age_ms(sense.source, now=now)
    if age is None:
        return "unknown"
    return "ok" if age <= sense.beat_ms else "stale"


def row(platform: PlatformDef, *, now: float | None = None) -> dict:
    per = tuple(s for s in platform.senses if s.axis == "perceive")
    act = tuple(s for s in platform.senses if s.axis == "act")
    if not platform.senses:
        shape = "none"
    elif per and act:
        shape = "both"
    elif per:
        shape = "see_only"
    else:
        shape = "act_only"

    marks = [evidence(s, now=now) for s in per]
    worst = "unknown" if "unknown" in marks else ("stale" if "stale" in marks else "ok")

    if shape == "none":
        line = "%s：还没接" % platform.display
    elif shape == "act_only":
        line = "%s：手能动（%d 项），但看不见 —— 会对着过时的世界动作" % (platform.display, len(act))
    elif shape == "see_only":
        line = "%s：看得见（%s），但动不了手" % (platform.display, EVIDENCE_SAYS[worst])
    else:
        line = "%s：看得见（%s），手能动（%d 项）" % (platform.display, EVIDENCE_SAYS[worst], len(act))

    return {
        "key": platform.key,
        "display": platform.display,
        "shape": shape,
        "evidence": worst,
        "perceive": [s.says for s in per],
        "act": [s.says for s in act],
        "line": line,
    }


def table(*, now: float | None = None) -> list[dict]:
    return [row(p, now=now) for p in all_platforms().values()]


def summary(*, now: float | None = None) -> str:
    return "\n".join(r["line"] for r in table(now=now))
