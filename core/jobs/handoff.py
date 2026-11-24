"""办不下去时交给人什么（U-28）。

一件事走不下去，出口不该只说「抱歉我没办成」。人接手需要三样东西：

1. **做到哪了**——具体到第几步、那一步的结果，不是「进度 60%」；
2. **还差什么**——剩下步骤的原文（他看得懂的动作串，不是内部名词）；
3. **他自己要动哪里**——链接、已经填好的字段、证据截图。

这不是失败报告，是交接单：写的人不写「我们失败了」，而写「球在你那边，这是我手里的牌」。
所以三条措辞纪律：

- **不假设人会接手**：不写「我等你确认」这种要人回话的句子；
- **不假装办成**：`done` 不在这里（那是 `done` 停因），措辞里不出现「已办 / 已完成」；
- **一次一件**：同一件事只交一次（`handed_off` 事件幂等，重复调用返回 ``False``）。

三档（`features.HANDOFF`）：``off`` 一行都不算（默认）/ ``shadow`` 只算文本写日志
（不落事件、不进 ``extra``）/ ``on`` 才落 ``handed_off`` 事件并把文本交给下游。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Sequence

from core.contracts.plan import Handoff, Job

log = logging.getLogger(__name__)

HANDOFF_STOPS: tuple[str, ...] = ("budget", "turns", "failed", "no_executor", "blocked")
"""哪些停因值得交接。

`done` 是办成了；`need_approval` / `awaiting` 是**正在**等人（那有第六态回执管）；
`shadow` 是开关档位不是事实；`state` 是终态。都不在这里。
"""

HANDOFF_WHY: dict[str, str] = {
    "budget": "这一步的工具预算用完了",
    "turns": "推进的回合数用完了",
    "failed": "有一步执行失败了",
    "no_executor": "我没有能把这一步做完的执行器",
    "blocked": "这一步被闸挡住了",
}

MAX_HANDOFF_CHARS = 400
MAX_ITEMS = 3
MAX_STEP_CHARS = 60
MAX_GOAL_CHARS = 60
HANDOFF_DIR = Path("data") / "handoff"


def _mode() -> str:
    from core import features

    return features.mode(features.HANDOFF, features.MODE_OFF)


def _clip(text: Any, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def should_handoff(stop: str, job: Job | None = None) -> bool:
    """这个停因要不要交给人。纯函数，不读开关、不碰台账。"""
    if stop not in HANDOFF_STOPS:
        return False
    # `job` 只用来排掉终态；拿不准（缺字段/是别的对象）就当「不知道」，不因此不交。
    state = str(getattr(job, "state", "") or "")
    if state in ("done", "cancelled", "expired"):
        return False
    return True


def remaining_steps(job: Job, limit: int = MAX_ITEMS) -> list[str]:
    """还差的步骤原文（按序号取，最多 ``limit`` 条）。"""
    left = [s for s in job.steps if s.n > job.step_n]
    return [_clip(s.action, MAX_STEP_CHARS) for s in left[:limit]]


def prefilled_fields(job: Job, limit: int = 6) -> tuple[str, ...]:
    """能从动作串里直接抄给人家的字段（``工具:参数`` 的参数那一段）。

    只抄**剩余步骤**的参数：做完的那些参数已经是既成事实，写进交接单只会让人以为还要再填。
    """
    out: list[str] = []
    for step in job.steps:
        if step.n <= job.step_n:
            continue
        _tool, sep, arg = step.action.partition(":")
        if sep and arg.strip():
            out.append(_clip(arg, MAX_STEP_CHARS))
        if len(out) >= limit:
            break
    return tuple(out)


def build_handoff(
    job: Job,
    *,
    reason: str,
    url: str = "",
    screenshots: Sequence[str] = (),
    note: str = "",
) -> Handoff:
    """把「球在你那边」这件事写成四槽里的槽 ④。纯函数。"""
    return Handoff(
        url=url,
        prefilled=prefilled_fields(job),
        screenshots=tuple(screenshots),
        note=_clip(note or HANDOFF_WHY.get(reason, "这一步我办不下去了"), 120),
    )


def _last_done(job: Job) -> str:
    done = [s for s in job.steps if s.n <= job.step_n]
    if not done:
        return "还没开始动手"
    last = done[-1]
    return f"第 {last.n} 步「{_clip(last.action, MAX_STEP_CHARS)}」"


def handoff_text(job: Job, handoff: Handoff, *, reason: str) -> str:
    """给人看的那一段（≤ ``MAX_HANDOFF_CHARS`` 字）。

    三句结构固定：**没办完的原因 → 做到哪了 → 还差什么 / 你要动哪里**。
    """
    why = HANDOFF_WHY.get(reason, "这一步我办不下去了")
    lines = [f"没办完（{why}）：{_clip(job.goal, MAX_GOAL_CHARS)}"]
    lines.append(f"已经走到：{_last_done(job)}")
    left = remaining_steps(job)
    if left:
        lines.append("还差：" + "；".join(f"{i}) {s}" for i, s in enumerate(left, 1)))
    else:
        lines.append("没有下一步了，剩下的是你那边的事")
    if handoff.note:
        lines.append(f"你要动的：{handoff.note}")
    if handoff.url:
        lines.append(handoff.url)
    if handoff.prefilled:
        lines.append("我先填好：" + " / ".join(handoff.prefilled[:4]))
    text = "\n".join(lines)
    return _clip(text, MAX_HANDOFF_CHARS) if len(text) > MAX_HANDOFF_CHARS else text


def handoff_markdown(job: Job, handoff: Handoff, *, reason: str, detail: str = "") -> str:
    """可交付的完整交接单（走出口时当 ``kind=file`` 发出去）。"""
    why = HANDOFF_WHY.get(reason, "这一步我办不下去了")
    out = [f"# 交接单 · {_clip(job.goal, MAX_GOAL_CHARS)}", ""]
    out.append(f"- 为什么停下：{why}（`{reason}`）")
    out.append(f"- 走到哪了：{_last_done(job)}，共 {len(job.steps)} 步")
    out.append(f"- 用了多少：工具 {job.tool_calls_total} 次 / 回合 {job.turns_total} 次")
    if detail:
        out += ["", "## 最后一步的原始结果", "", "```", detail.strip()[:1200], "```"]
    out += ["", "## 还差什么", ""]
    if job.steps:
        for step in job.steps:
            mark = "x" if step.n <= job.step_n else " "
            eff = f" [{step.effects}]" if step.effects != "read" else ""
            out.append(f"- [{mark}] {step.n}. {step.action}{eff}")
    else:
        out.append("- （没有登记步骤）")
    if handoff.prefilled:
        out += ["", "## 已经填好的字段", ""]
        out += [f"- {item}" for item in handoff.prefilled]
    if handoff.url:
        out += ["", "## 链接", "", handoff.url]
    if handoff.screenshots:
        out += ["", "## 证据", ""]
        out += [f"- {p}" for p in handoff.screenshots]
    if handoff.note:
        out += ["", "## 你要动的", "", handoff.note]
    out += ["", "---", "", "（这条是交接单，不是失败报告：前面做完的都在，剩下的是你那边的事。）", ""]
    return "\n".join(out)


def write_handoff(job: Job, markdown: str, *, out_dir: Path | None = None) -> str:
    """把交接单落到 ``data/handoff/<job_id>.md``，返回路径（失败返回空串）。"""
    try:
        base = Path(out_dir) if out_dir is not None else HANDOFF_DIR
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{job.job_id or 'job'}.md"
        path.write_text(markdown, encoding="utf-8")
        return str(path)
    except Exception as exc:  # noqa: BLE001 - 交接单落不下来不许拖垮回合
        log.warning("handoff_write_failed job_id=%s error=%s", job.job_id, exc)
        return ""


def record_handoff(
    job: Job,
    handoff: Handoff,
    *,
    store: Any = None,
    reason: str = "",
    detail: str = "",
) -> bool:
    """落一行 ``handed_off``（同一件事只交一次）。三档语义在这里。"""
    mode = _mode()
    if mode == "off":
        return False
    text = handoff_text(job, handoff, reason=reason)
    if mode != "on":
        log.info(
            "handoff_shadow job_id=%s reason=%s chars=%d", job.job_id, reason, len(text)
        )
        return False
    if store is None:
        from core.jobs.store import default_store

        store = default_store()
    try:
        if "handed_off" in store.event_kinds(job.job_id):
            return False
        seq = store.append(
            job.job_id,
            "handed_off",
            payload={
                "reason": reason,
                "handoff": {
                    "url": handoff.url,
                    "prefilled": list(handoff.prefilled),
                    "screenshots": list(handoff.screenshots),
                    "note": handoff.note,
                },
                "steps_left": remaining_steps(job, limit=MAX_ITEMS),
                "text": text,
                "detail": detail[:600],
            },
            actor="judge",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("handoff_record_failed job_id=%s error=%s", job.job_id, exc)
        return False
    log.info("handoff_recorded job_id=%s reason=%s seq=%s", job.job_id, reason, seq)
    return bool(seq)


def apply_handoff(
    stop: str,
    job: Job,
    *,
    store: Any = None,
    detail: str = "",
    note: str = "",
    url: str = "",
    screenshots: Sequence[str] = (),
) -> str:
    """停因 → 交接单文本（落库 + 落盘）。返回给人看的那段；不该交接返回空串。

    调用点只需要这一句：停下来的那件事，够不够格交给人，交给谁看、写什么，全在这里。
    """
    if not should_handoff(stop, job):
        return ""
    package = build_handoff(job, reason=stop, url=url, screenshots=screenshots, note=note)
    if not record_handoff(job, package, store=store, reason=stop, detail=detail):
        return ""
    write_handoff(job, handoff_markdown(job, package, reason=stop, detail=detail))
    return handoff_text(job, package, reason=stop)
