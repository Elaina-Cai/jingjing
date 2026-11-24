"""Step 3 退役守卫：不再做「无上下文重写」。

原来主循环在拿到草稿后，会再调一次 ``llm.chat``（只看「草稿 + 口吻卡」，
看不到 system / 窗口 / 工具结果）把它改成"更口语"的版本。问题不在质量，在**主体**：
说话的那个模型永远不知道自己实际说了什么，而这与"一个主体、判断与说话共享同一段
上下文"正好相反。所以整段退役，口吻约束交给人设层，出站只留确定性机械过滤。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agents.conversational as c  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("ok " if ok else "FAIL ") + name + ("" if ok else f" {detail}"))
    if not ok:
        FAILS.append(name)


def t_rewrite_is_gone() -> None:
    check("_render_spoken_reply 已删", not hasattr(c.ConversationalAgent, "_render_spoken_reply"))
    check("_speak_render_system 已删", not hasattr(c.ConversationalAgent, "_speak_render_system"))
    src = inspect.getsource(c.ConversationalAgent._run_persona_brain_turn_stream)
    check("回合里不再调它", "_render_spoken_reply" not in src)
    check("草稿原样成为回复", "reply = content" in src)


def t_no_extra_llm_call() -> None:
    src = inspect.getsource(c.ConversationalAgent._run_persona_brain_turn_stream)
    check("回合里只有工具循环的 chat_tools（没有第二处 chat）", "self.llm.chat(" not in src)


def t_flag_retired() -> None:
    from core import features

    check("SPEAK_CONTRACT 常量已退役", not hasattr(features, "SPEAK_CONTRACT"))
    sibling = (ROOT / "tests" / "test_env_flag_isolation.py").read_text(encoding="utf-8")
    check("兄弟表不再登记它", "YINYUE_FEATURE_SPEAK_CONTRACT" not in sibling)


def t_fast_path_also_clean() -> None:
    src = inspect.getsource(c.ConversationalAgent._process_message)
    check("快路径也不再重写", "_render_spoken_reply" not in src)


def main() -> int:
    t_rewrite_is_gone()
    t_no_extra_llm_call()
    t_flag_retired()
    t_fast_path_also_clean()
    print(f"\nRESULT {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())