# -*- coding: utf-8 -*-
r"""三个「新写的块」必须登记进 ``assembler._BLOCK_POLICY``（漏登记 ≠ 少条注释）。

漏登记的后果是三条，全都静默：
1. ``_classify`` 把它们标成 ``undeclared`` + 每条回合一条 warning；
2. **整块按 PERSONA 永不裁**——预算表对它们完全失效；
3. ``context_trace`` 里查不到 owner，出问题时不知道是谁塞进来的。

跑法：``.venv_it\Scripts\python.exe -X utf8 -u tests/test_prompt_block_registration.py``
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context import assembler  # noqa: E402
from core.context.job_readout import JOB_READOUT_HEADER  # noqa: E402
from core.tools.prompt import TOOL_BLOCK_HEADER  # noqa: E402

PASSED = 0
FAILED = 0

#: 三个块的块头必须与真正渲染出来的第一行逐字一致
EXPECTED = {
    JOB_READOUT_HEADER: ("context.job_readout", 10),   # RELATION
    # 2026-10-21：这两块从 AUX 提到 RELATION。AUX 的定义是"丢了不影响正确性"，
    # 但真机证据相反——工具清单被整块丢掉时她答"我这边没有往群里发消息的工具，
    # 只有查东西、记笔记这些"，于是能力再强也等于没有。
    "[现在这一屏]": ("context.senses", 10),             # RELATION
    TOOL_BLOCK_HEADER: ("tool.prompt", 10),            # RELATION
}


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"PASS {name}")
    else:
        FAILED += 1
        print(f"FAIL {name}" + (f"  [{detail}]" if detail else ""))


def _policy() -> dict[str, tuple[str, int]]:
    return {row[0]: (row[2], row[3]) for row in assembler._BLOCK_POLICY}  # noqa: SLF001


def test_registered() -> None:
    policy = _policy()
    for header, (source, priority) in EXPECTED.items():
        check(f"{header} 登记在册", header in policy, str(sorted(policy)[:6]))
        if header in policy:
            check(f"{header} 的 owner 是 {source}", policy[header][0] == source, str(policy[header]))
            check(f"{header} 的优先级是 {priority}", policy[header][1] == priority, str(policy[header]))


def test_no_undeclared() -> None:
    """真渲染文本分类：三块都不许落进 undeclared。"""
    texts = [
        JOB_READOUT_HEADER + "\n第 2 步 · 打开会话（等你点头）",
        "[现在这一屏]\n前台窗口：Edge",
        TOOL_BLOCK_HEADER + "\nweb_search(query) 查网页",
    ]
    blocks = assembler._classify(texts)  # noqa: SLF001
    for block in blocks:
        check(f"{block.name} 不是 undeclared", block.source != "undeclared", str(block))


def test_source_headers_match_code() -> None:
    """块头字符串只有一个真源：漏改一处就会出现「登记了但永不命中」。"""
    sources = {
        "core/context/job_readout.py": JOB_READOUT_HEADER,
        "core/tools/prompt.py": TOOL_BLOCK_HEADER,
    }
    for path, header in sources.items():
        text = io.open(Path(path), encoding="utf-8-sig").read()
        check(f"{path} 里块头字面量一致", f'"{header}"' in text, header)


TESTS = (test_registered, test_no_undeclared, test_source_headers_match_code)


def main() -> int:
    for fn in TESTS:
        print(f"[{fn.__name__}]")
        fn()
    print()
    print(f"RESULT {FAILED} failed / {PASSED + FAILED} checked")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
