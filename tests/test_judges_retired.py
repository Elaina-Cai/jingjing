# -*- coding: utf-8 -*-
"""退役守卫：准入 judge 与 N1 都不再调模型（2026-10-23 主人拍板）。

「要不要说 / 对谁说 / 他要什么 / 怎么答」这些语义判断全部交给会话里的模型
（perceive + 生成回合）；代码只留机械事实。本文件钉住四件事：

1. 两个 judge 入口（``admission._judge`` / ``intent._call_llm``）不许长回来；
2. 全仓不许再出现 ``agent_name="admission_judge"`` 这类判定调用；
3. N1 宿主（``core/context/intent.py``）与其陪伴模块整文件已删，全仓无人 import 它；
4. ``admission.evaluate`` 即使拿到一个会炸的 llm 也不调用它——它只走规则窗口。

Run directly: python tests/test_judges_retired.py
"""
from __future__ import annotations

import asyncio
import inspect
import io
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 兄弟组（同一张 flag 表里的读开关）要一起钉住：生产 .env 或同进程别的测试
# 改过它们，就会让这里的断言变成"看运气"（tests/test_env_flag_isolation.py 守着）。
os.environ["YINYUE_ADMISSION_MODE"] = "on"
os.environ["YINYUE_FEATURE_CHAT_WINDOW"] = "1"
os.environ["YINYUE_FEATURE_REPLY_INTENT"] = "off"
os.environ.pop("YINYUE_ADMISSION_CHATS", None)
os.environ.pop("YINYUE_CHAT_WINDOW_CHATS", None)

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))


#: 只认**真的 import**（注释/文档里提到旧模块名不算）。
_N1_IMPORT_RE = re.compile(
    r"(?:^|\n)\s*(?:from\s+core\.context\.intent\s+import|from\s+core\.context\s+import\s+intent\b"
    r"|from\s+core\.contracts\.reply_intent\s+import|import\s+core\.context\.intent\b"
    r"|import\s+core\.contracts\.reply_intent\b)"
)


def _src(rel):
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_judge_entries_are_gone():
    admission = _src("core/context/admission.py")
    check("准入: _judge 已删", "async def _judge(" not in admission)
    check("准入: 不再调 llm.chat", "llm.chat(" not in admission)


def test_n1_host_is_gone():
    for rel in ("core/context/intent.py", "core/context/intent_trace.py",
                "core/contracts/reply_intent.py"):
        check(f"{rel} 已删", not os.path.exists(os.path.join(ROOT, rel)))
    hits = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in {
            ".git", ".venv", ".venv_it", ".test_tmp", "__pycache__", "backups", "logs",
            "output", "data", "tests", "docs", "scripts",
        }]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            try:
                text = io.open(path, encoding="utf-8").read()
            except Exception:
                continue
            if _N1_IMPORT_RE.search(text):
                hits.append(os.path.relpath(path, ROOT))
    check("全仓无人 import N1 宿主", not hits, repr(hits))


def test_no_judge_agent_names_anywhere():
    hits = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in {
            ".git", ".venv", ".venv_it", ".test_tmp", "__pycache__", "backups", "logs",
            "output", "data", "tests",
        }]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            try:
                text = io.open(path, encoding="utf-8").read()
            except Exception:
                continue
            for needle in ('agent_name="admission_judge"', 'agent_name="intent_judge"'):
                if needle in text:
                    hits.append(f"{os.path.relpath(path, ROOT)}: {needle}")
    check("全仓没有 judge 判定调用", not hits, repr(hits))


def test_run_intent_is_gone():
    try:
        import core.context.intent  # noqa: F401
    except ModuleNotFoundError:
        check("import core.context.intent 已不可能", True)
    else:
        check("import core.context.intent 已不可能", False, "模块又回来了")


def test_admission_never_calls_llm():
    from core.context import admission

    class Boom:
        async def chat(self, *a, **kw):
            raise AssertionError("准入不许再调模型")

    rows = [
        {"event_id": "e1", "sender_name": "主人", "content": "在吗", "is_self": False,
         "created_at": "2026-10-23 22:00:00", "sender_id": "wxid_a", "chat_id": "c@chatroom"},
        {"event_id": "e2", "sender_name": "鲸鲸", "content": "在", "is_self": True,
         "created_at": "2026-10-23 22:01:00", "sender_id": "self", "chat_id": "c@chatroom"},
    ]
    result = asyncio.run(admission.evaluate(
        chat_id="c@chatroom", reason="mentioned_in_group", current="在吗",
        sender_name="主人", rows=rows, llm=Boom(), is_group=True,
    ))
    check("evaluate 拿到 llm 也不调用", result is not None, repr(result))
    check("evaluate 只出规则窗口", getattr(result, "fallback", "") in {"t2", "full"},
          repr(getattr(result, "fallback", "")))


def main() -> bool:
    test_judge_entries_are_gone()
    test_n1_host_is_gone()
    test_no_judge_agent_names_anywhere()
    test_run_intent_is_gone()
    test_admission_never_calls_llm()
    failed = 0
    for name, ok, detail in CHECKS:
        if ok:
            print(f"  ok   {name}")
        else:
            failed += 1
            print(f"  FAIL {name} {detail}")
    print(f"RESULT {failed} failed / {len(CHECKS)} checks")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)