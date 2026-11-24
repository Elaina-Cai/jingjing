"""TurnSession（Step 1 等价抽取）契约。

锁四件事：消息形状与抽出来之前逐字一致；只往后追加、不重写；裁剪行为仍走
既有实现（预算与 tool_call_id 配对不能被这次抽取改掉）；结构快照只记形状不
搬原文。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.conversational import _messages_with_turns  # noqa: E402
from agents.turn_session import TurnSession  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"ok {name}")
    else:
        FAILS.append(name)
        print(f"FAIL {name} {detail}".rstrip())


def t_shape_matches_legacy() -> None:
    turns = [
        {"role": "user", "content": "她先说"},
        {"role": "assistant", "content": "我回一句"},
    ]
    s = TurnSession(system="SYS", turns=turns, current="现在")
    check("形状与旧实现逐字一致", s.messages == _messages_with_turns("SYS", turns, "现在"))
    check("system 是第一条", s.messages[0]["role"] == "system")
    check("当前段是最后一条 user", s.messages[-1] == {"role": "user", "content": "现在"})


def t_empty_turns_is_legacy_pair() -> None:
    s = TurnSession(system="S", turns=[], current="C")
    check("无轮次 = 两条消息", [m["role"] for m in s.messages] == ["system", "user"])


def t_append_only_forward() -> None:
    s = TurnSession(system="S", turns=[], current="C")
    before = list(s.messages)
    ident = id(s.messages)
    s.append_assistant("", tool_calls=[{"id": "1"}])
    s.append_tool("结果", tool_call_id="1")
    check("追加不改前面的消息", s.messages[: len(before)] == before)
    check("list 身份不变（调用方持有的是同一个列表）", id(s.messages) == ident)
    check("tool 项带 call id", s.messages[-1].get("tool_call_id") == "1")
    bare = s.append_tool("无 id")
    check("无 call id 时不写该键", "tool_call_id" not in bare)


def t_note_ignores_blank() -> None:
    s = TurnSession(system="S")
    s.note("   ")
    s.note("判断：她在问我")
    check("空结论不入账", s.notes == ["判断：她在问我"])


def t_trim_still_uses_legacy_budget() -> None:
    s = TurnSession(system="S", turns=[], current="C")
    for i in range(6):
        s.append_assistant("", tool_calls=[{"id": str(i)}])
        s.append_tool("x" * 4000, tool_call_id=str(i))
    dropped = s.trim()
    check("裁剪真的丢了轮次组", dropped > 0, f"dropped={dropped}")
    check(
        "system 与当前段仍在（当前段是第 2 条，轮次为空）",
        s.messages[0]["role"] == "system" and s.messages[1] == {"role": "user", "content": "C"},
        f"head={s.messages[:2]}",
    )
    tail_tools = [m for m in s.messages if m.get("role") == "tool"]
    ids = {m.get("tool_call_id") for m in tail_tools}
    call_ids = {c["id"] for m in s.messages if m.get("tool_calls") for c in m["tool_calls"]}
    check("保留下来的 tool 与调用仍配对", ids <= call_ids, f"orphan={ids - call_ids}")


def t_snapshot_reports_shape_only() -> None:
    s = TurnSession(system="SYS", turns=[{"role": "user", "content": "a"}], current="b")
    snap = s.snapshot()
    check(
        "快照键齐",
        set(snap) == {
            "system_chars", "turns", "current_chars", "messages", "notes", "goal", "roles",
        },
        str(set(snap)),
    )
    check("没目标时 goal 是空 dict", snap.get("goal") == {}, str(snap.get("goal")))
    check("roles 与消息数一致", len(snap["roles"]) == snap["messages"] == len(s.messages))
    check("快照不含原文", "SYS" not in str(snap) and '"a"' not in str(snap))


def t_module_is_pure() -> None:
    src = inspect.getsource(TurnSession)
    check("容器本身不调模型/不落库", not any(w in src for w in ("llm.chat", "sqlite3", "structlog")))
    mod = (ROOT / "agents" / "turn_session.py").read_text(encoding="utf-8")
    head = mod.split('"""', 2)[-1]
    check("顶层不 import 重模块", "import agents.conversational" not in head)


def main() -> int:
    t_shape_matches_legacy()
    t_empty_turns_is_legacy_pair()
    t_append_only_forward()
    t_note_ignores_blank()
    t_trim_still_uses_legacy_budget()
    t_snapshot_reports_shape_only()
    t_module_is_pure()
    print(f"\nRESULT {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())