"""一轮对话的装载容器（2026-10-23 Step 1）。

目的不是"再包一层"，而是把「这一轮模型看到的全部」收敛成一个对象：
system（整轮稳定）+ 历史轮次 + 当前这段 + 本轮累积的结论。

为什么要有它：现在三段调用（判断 / 生成 / 自审）各自在自己的函数里拼
`messages`，谁也看不见谁想过什么。把这个列表交给一个会话对象之后，下一步
（Step 2）就能把判断与自审改成"同一段 transcript 的下一轮"，而不是另起
一份 prompt。

Step 1 只做等价抽取：消息形状、追加顺序、裁剪行为都与抽出来之前逐字一致，
任何调用点都不改语义。

2026-10-23 Step 2c：这里多了 ``TurnGoal``——「这一轮想达成什么」从一句
自然语言变成可读的结构，感知层填它、生成层改它、回合末压成一行笔记。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 想做的那件事的种类。``none`` 是"这一轮没有目标"（纯接话），不是错误。
GOAL_KINDS: tuple[str, ...] = ("answer", "do", "both", "none")

GOAL_ABOUT_MAX = 200
GOAL_CRITERIA_MAX = 200


def _clip(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


@dataclass
class TurnGoal:
    """这一轮想达成什么（主人的三问之一：目标是什么）。

    - ``kind``：``answer`` 只是答；``do`` 只是办；``both`` 边说边办；
      ``none`` 没有目标（纯社交接话）。
    - ``about``：一句话说清想达成什么。
    - ``to_whom``：承接感知层的 ``addressee``（她想给谁看）。
    - ``criteria``：怎么算做到了——答到哪一点 / 什么算办成。

    纪律：``kind`` 认不出时折成 ``none``（猜错种类会让"该办的事"被当成
    闲聊答过去）；``about`` 为空也折成 ``none``（没有内容的目标等于没有）。
    """

    kind: str = "none"
    about: str = ""
    to_whom: str = ""
    criteria: str = ""

    def __post_init__(self) -> None:
        kind = str(self.kind or "").strip().lower()
        self.kind = kind if kind in GOAL_KINDS else "none"
        self.about = _clip(self.about, GOAL_ABOUT_MAX)
        self.to_whom = _clip(self.to_whom, 40)
        self.criteria = _clip(self.criteria, GOAL_CRITERIA_MAX)
        if not self.about:
            self.kind = "none"

    @property
    def is_real(self) -> bool:
        """真的有目标（``none`` 与空目标都不算）。"""
        return self.kind != "none" and bool(self.about)

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "about": self.about,
            "to_whom": self.to_whom,
            "criteria": self.criteria,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "TurnGoal | None":
        """从工具参数里取 ``goal``。取不到返回 None（不是"空目标"）。"""
        if not isinstance(payload, dict):
            return None
        goal = cls(
            kind=payload.get("kind") or "",
            about=payload.get("about") or "",
            to_whom=payload.get("to_whom") or payload.get("to") or "",
            criteria=payload.get("criteria") or "",
        )
        if not goal.about:
            return None
        return goal

    def render_line(self) -> str:
        """一行给人/给模型看的说法（卡与回执共用同一句）。"""
        kind_label = {
            "answer": "答",
            "do": "办",
            "both": "边说边办",
            "none": "没有目标",
        }.get(self.kind, self.kind)
        line = f"goal: {kind_label} · {self.about}"
        if self.criteria:
            line += f"（算成：{self.criteria}）"
        return line


def render_turn_note(
    goal: Any = None,
    *,
    route: str = "",
    addressee: str = "",
    outcome: str = "",
    why: str = "",
) -> str:
    """回合末那一行笔记：想做什么 -> 判成什么 -> 结果如何。

    只写结构、不写原文：下一回合她看到的应该是"我上次打算干这个、办成了/没办成"，
    而不是把这一轮的整段思考复制进 prompt。
    """
    parts: list[str] = []
    if goal is not None and getattr(goal, "is_real", False):
        parts.append(f"想：{goal.about}")
        if getattr(goal, "criteria", ""):
            parts.append(f"算成：{goal.criteria}")
    if route:
        parts.append(f"判：{route}")
    if addressee:
        parts.append(f"对：{addressee}")
    if why:
        parts.append(f"因为：{_clip(why, 80)}")
    if outcome:
        parts.append(f"结果：{outcome}")
    if not parts:
        return ""
    return "〔这轮〕" + "｜".join(parts)


@dataclass
class TurnSession:
    """一个回合的 transcript（同一会话内只追加、不重写）。

    - ``system``：整轮不变的稳定前缀（人设 + 常驻块）。缓存命中靠它逐字不变。
    - ``turns``：历史轮次（``window_turns``），role 只有 user/assistant。
    - ``current``：当前这一段 user 文本。
    - ``notes``：本轮的结构化结论（Step 2 起由感知/决断写入）。
    - ``goal``：这一轮想达成什么（Step 2c 起由感知层填）。
    """

    system: str
    turns: list[dict] = field(default_factory=list)
    current: str = ""
    notes: list[str] = field(default_factory=list)
    goal: "TurnGoal | None" = None

    def __post_init__(self) -> None:
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system},
            *list(self.turns or []),
            {"role": "user", "content": self.current},
        ]

    # ---- 追加（只往后长，永不改前面的内容）----
    def append_assistant(self, content: str, **extra: Any) -> dict[str, Any]:
        item = {"role": "assistant", "content": content}
        item.update(extra)
        self.messages.append(item)
        return item

    def append_tool(self, content: str, *, tool_call_id: str = "") -> dict[str, Any]:
        item = {"role": "tool", "content": content}
        if tool_call_id:
            item["tool_call_id"] = tool_call_id
        self.messages.append(item)
        return item

    def note(self, text: str) -> None:
        """记一条本轮结论（短句）。"""
        text = str(text or "").strip()
        if text:
            self.notes.append(text)

    def set_goal(self, goal: Any) -> None:
        """写入/覆盖本回合的目标。传 None 表示"这轮没有目标"。"""
        self.goal = goal if isinstance(goal, TurnGoal) else None

    # ---- 预算 ----
    def trim(self, *, max_chars: int = 24000, keep_groups: int = 2) -> int:
        """按预算裁掉最早的 tool 轮次组，保持 tool_call_id 配对。

        2026-10-24：旧大脑的 ``_trim_tool_messages`` 随 ``agents.conversational``
        一起退役，这里保留等价实现（整组删，不拆散 assistant(tool_calls)+tool）。
        """
        messages = self.messages

        def _size() -> int:
            return sum(len(str(m.get("content") or "")) for m in messages)

        dropped = 0
        while _size() > max_chars:
            idx = next((i for i, m in enumerate(messages) if m.get("tool_calls")), None)
            if idx is None:
                break
            end = idx + 1
            while end < len(messages) and messages[end].get("role") == "tool":
                end += 1
            groups_left = sum(1 for m in messages[idx:] if m.get("tool_calls"))
            if groups_left <= keep_groups:
                break
            del messages[idx:end]
            dropped += end - idx
        return dropped

    # ---- 审计 ----
    def snapshot(self) -> dict[str, Any]:
        """落 trace 用的结构快照：只记形状与计数，不搬原文。"""
        goal = self.goal.as_dict() if isinstance(self.goal, TurnGoal) else {}
        return {
            "system_chars": len(self.system or ""),
            "turns": len(self.turns or []),
            "current_chars": len(self.current or ""),
            "messages": len(self.messages),
            "notes": len(self.notes),
            "goal": goal,
            "roles": [str(m.get("role") or "") for m in self.messages],
        }