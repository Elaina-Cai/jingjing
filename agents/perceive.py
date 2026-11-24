"""会话内感知（Step 2b）：把判断从"另起一次 chat"改成"同一段 transcript 的下一轮"。

主人 2026-10-23 拍板的字段只有四个：route / addressee / why / need。
它和 Step 2a 的感知卡是**同一套词**——2a 是把已有判断搬进来，2b 是让判断本身
发生在会话里，所以模型看到的词汇不许变。

边界（必须写清，否则下一步会做错）：
- 这个工具**不负责"要不要花钱"**。判 silent/defer 的回合在建会话之前就被代码
  挡掉了；`skip/defer` 在这里表示"会话已经起了，但我不接"。
- 解析失败 / 模型没调 / provider 不支持强制 tool_choice → 调用方 fail-open
  回今天的行为（继续正常生成），绝不把"没判出来"当成"judge 说 skip"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from safelog import warn

PERCEIVE_TOOL_NAME = "perceive"
VALID_ROUTES = ("speak", "defer", "skip")
VALID_ADDRESSEES = ("her", "other", "room", "unknown")
WHY_MAX = 400
NEED_MAX = 200
# Step 2c-2（2026-10-23）：群聊里判 defer 时要能说"多久之后再看这条"。
# 上限 600 秒是防呆：写错一个 99999 就等于这条消息永不回看，而日志里只有一行忽略。
DEFER_MAX_SECONDS = 30 * 24 * 3600.0   # 2026-10-31 取消上限：30 天（原来的 600 秒也是防呆）
DEFER_DEFAULT_SECONDS = 0.0
CARD_HEADER = "[我刚怎么判断的·可以推翻]"
GOAL_HEADER = "[这一轮想做什么]"
GOAL_ABOUT_MAX = 1000000
GOAL_CRITERIA_MAX = 1000000
# Step 2d（2026-10-23）：N1 退役后便签的**唯一**生产者变成 perceive 本身。
# 上限 2 条是防呆：一次判完顺手写五条，等于让候选区替她把话记满。
NOTES_MAX = 1000000
NOTE_CONTENT_MAX = 1000000
NOTE_QUOTE_MAX = 1000000

PERCEIVE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": PERCEIVE_TOOL_NAME,
        "description": (
            "判这一轮：该不该接、对谁说、凭什么、还缺什么。每回合必须调用一次。"
            "只写你看到的事实，不写推测。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "route": {
                    "type": "string",
                    "enum": list(VALID_ROUTES),
                    "description": "speak=该接；defer=先等等（别人还在说/留宽限）；skip=不该接",
                },
                "addressee": {
                    "type": "string",
                    "enum": list(VALID_ADDRESSEES),
                    "description": "her=冲她说的；other=冲别人；room=对全群；unknown=判不出（不许猜）",
                },
                "why": {
                    "type": "string",
                    "description": f"依据，≤{WHY_MAX} 字，写看到的事实",
                },
                "need": {
                    "type": "string",
                    "description": f"还缺什么才能答好，≤{NEED_MAX} 字；没有就空串",
                },
                "defer_seconds": {
                    "type": "number",
                    "description": (
                        "route=defer 时：隔几秒再回来看这条（0=按默认宽限窗口，最多 600）"
                    ),
                },
                "notes": {
                    "type": "array",
                    "maxItems": NOTES_MAX,
                    "description": (
                        "这一轮有没有值得记下的事（0~2 条）；没有就不填。"
                        "quote 必须照抄窗口里出现过的一句原话，抄不出就不写。"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "slot": {
                                "type": "string",
                                "description": "open_loop=还没了结的事 / habit=习惯与偏好",
                            },
                            "content": {
                                "type": "string",
                                "description": f"一句话写清是什么，≤{NOTE_CONTENT_MAX} 字",
                            },
                            "quote": {
                                "type": "string",
                                "description": f"支持这条的那句原话（照抄，≥4 字），≤{NOTE_QUOTE_MAX} 字",
                            },
                        },
                        "required": ["slot", "content", "quote"],
                    },
                },
                "goal": {
                    "type": "object",
                    "description": (
                        "这一轮想达成什么（答完就不管 / 要动手办 / 边说边办）。"
                        "纯接话可以不填。"
                    ),
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["answer", "do", "both", "none"],
                            "description": "answer=只是答；do=要办成事；both=边说边办；none=没有目标",
                        },
                        "about": {
                            "type": "string",
                            "description": f"一句话说清想达成什么，≤{GOAL_ABOUT_MAX} 字",
                        },
                        "to_whom": {
                            "type": "string",
                            "description": "她想给谁看（人 id 或 room / 空）",
                        },
                        "criteria": {
                            "type": "string",
                            "description": f"怎么算做到了（答到哪一点 / 什么算办成），≤{GOAL_CRITERIA_MAX} 字",
                        },
                    },
                    "required": ["kind", "about"],
                },
            },
            "required": ["route", "addressee", "why"],
        },
    },
}


@dataclass(frozen=True)
class PerceptionResult:
    route: str
    addressee: str
    why: str
    need: str = ""
    goal: Any = None  # agents.turn_session.TurnGoal | None
    defer_seconds: float = DEFER_DEFAULT_SECONDS
    notes: tuple[dict[str, str], ...] = ()


def _defer_seconds(value: Any) -> float:
    """defer 秒数：认不出/负数 → 0（= 按默认窗口），超上限夹到 600。

    **不抛**：秒数只是窗口参数，写坏了按默认窗口走，比让整轮 fail-open 好。
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        warn("agents/perceive.py:154 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return DEFER_DEFAULT_SECONDS
    if seconds != seconds:  # NaN
        return DEFER_DEFAULT_SECONDS
    return max(0.0, min(DEFER_MAX_SECONDS, seconds))


def _notes(value: Any) -> tuple[dict[str, str], ...]:
    """便签：只收 ``{slot, content, quote}`` 三键完整的条目，**从不抛**。

    缺 quote 的条目直接丢——候选区的规矩是「证据必须落在这回合窗口的原话上」，
    在这里放宽等于让一条无根据的便签等人去批。
    """
    if not isinstance(value, (list, tuple)):
        return ()
    out: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        slot = " ".join(str(item.get("slot") or "").split())[:40]
        content = " ".join(str(item.get("content") or "").split())[:NOTE_CONTENT_MAX]
        quote = " ".join(str(item.get("quote") or "").split())[:NOTE_QUOTE_MAX]
        if not slot or not content or len(quote) < 4:
            continue
        out.append({"slot": slot, "content": content, "quote": quote})
        if len(out) >= NOTES_MAX:
            break
    return tuple(out)


def parse_perceive_args(args: Any) -> PerceptionResult:
    """把工具参数归一化成 PerceptionResult（**非法就抛**，由调用方 fail-open）。

    纪律：`addressee` 认不出时回退 `unknown`——**绝不把"判不出"写成 other**；
    `route` 不在词表里则抛（route 是行为开关，猜错会让她在不该说的时候说）。
    """
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"perceive 参数不是 JSON: {exc}") from exc
    if not isinstance(args, dict):
        raise ValueError("perceive 参数不是对象")
    route = str(args.get("route") or "").strip().lower()
    if route not in VALID_ROUTES:
        raise ValueError(f"perceive.route 非法: {route!r}")
    addressee = str(args.get("addressee") or "").strip().lower()
    if addressee not in VALID_ADDRESSEES:
        addressee = "unknown"
    why = " ".join(str(args.get("why") or "").split())[:WHY_MAX]
    need = " ".join(str(args.get("need") or "").split())[:NEED_MAX]
    # 目标坏形状**不抛**：route 是行为开关，猜错会开口；goal 只是记账，
    # 缺了就当"这轮没有目标"，不许因为它让整轮 fail-open 变成"没说 skip"。
    goal = None
    try:
        from agents.turn_session import TurnGoal

        goal = TurnGoal.from_payload(args.get("goal"))
    except Exception:  # noqa: BLE001 - 目标解析失败不影响判断本身
        warn("agents/perceive.py:212 Exception", err=repr(__import__("sys").exc_info()[1]))
        goal = None
    return PerceptionResult(
        route=route,
        addressee=addressee,
        why=why,
        need=need,
        goal=goal,
        defer_seconds=_defer_seconds(args.get("defer_seconds")),
        notes=_notes(args.get("notes")),
    )


def render_receipt(result: PerceptionResult) -> str:
    """工具回执：把**四个字段全部**带回会话（"叠加前面的思考"要能落地）。

    之前只回 route/addressee，why 与 need 只进了台账 —— 等于模型判完就把依据
    和缺口丢了，后面的生成轮看不到"我为什么这么判、还缺什么"。这不是审计问题，
    是**语义丢失**：判了却不让主体看见，等于没判。

    仍然不带任何评语（不评价自己的判断好不好），只陈述四个字段；need 非空时
    给一句中性指引（先补齐再答），与既有工具指引同一口径。
    """
    lines = [
        f"已记下：route={result.route}；addressee={result.addressee}",
    ]
    if result.why:
        lines.append(f"why: {result.why}")
    if result.need:
        lines.append(f"need: {result.need}")
        lines.append("（need 非空：先把它补齐——问一句、或先调对应工具，再答。）")
    if result.route == "defer" and getattr(result, "defer_seconds", 0.0) > 0:
        lines.append(
            f"defer_seconds: {int(result.defer_seconds)}（{int(result.defer_seconds)} 秒后再看这条）"
        )
    goal = getattr(result, "goal", None)
    if goal is not None and getattr(goal, "is_real", False):
        lines.append(goal.render_line())
        lines.append("（目标记下了：这一轮按它收口；做不到就说做不到，别装作办成了。）")
    notes = tuple(getattr(result, "notes", ()) or ())
    if notes:
        lines.append(f"notes: {len(notes)} 条（已进候选区等人批；批了才算记住。）")
    return "\n".join(lines)


def render_card(result: PerceptionResult) -> str:
    """落进 session.notes 的卡片——与 Step 2a 的 user 侧卡片同形同词。"""
    lines = [CARD_HEADER, f"route: {result.route}", f"addressee: {result.addressee}"]
    if result.why:
        lines.append(f"why: {result.why}")
    if result.need:
        lines.append(f"need: {result.need}")
    goal = getattr(result, "goal", None)
    if goal is not None and getattr(goal, "is_real", False):
        lines.append(goal.render_line())
    return "\n".join(lines)

TRACE_PATH_ENV = "YINYUE_PERCEIVE_TRACE_PATH"


def _default_trace_path() -> Path:
    return Path(__file__).resolve().parent.parent / "logs" / "perceive_trace.jsonl"


def trace_path() -> Path:
    """落盘位置（env 可覆盖，报表脚本按同一个默认值读）。"""
    import os

    raw = str(os.environ.get(TRACE_PATH_ENV) or "").strip()
    return Path(raw) if raw else _default_trace_path()


def append_trace(record: dict[str, Any], *, path: Path | None = None) -> bool:
    """追加一行对账记录（**永不抛**：报表不许拖垮回合）。"""
    try:
        item = dict(record or {})
        item.setdefault("at", datetime.now().isoformat(timespec="seconds"))
        target = path or trace_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        return True
    except Exception:  # noqa: BLE001 - 审计不许影响业务
        warn("agents/perceive.py:294 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False
