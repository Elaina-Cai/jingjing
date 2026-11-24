"""工具闭环的会话作用域（``YINYUE_TOOL_SCOPE_CHATS``）。纯脚本，无 pytest。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.tools import turnloop as T

FAILS: list[str] = []
COUNT = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if ok:
        print("  ok", name)
    else:
        FAILS.append(f"{name} {detail}")
        print("  FAIL", name, detail)


def _src(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def t_empty_means_all() -> None:
    print("[t_empty_means_all]")
    os.environ.pop(T.SCOPE_ENV, None)
    check("没设 env = 全部允许", T.in_tool_scope("wxid_any") is True)
    check("tool_scope 空元组", T.tool_scope() == ())


def t_whitelist() -> None:
    print("[t_whitelist]")
    os.environ[T.SCOPE_ENV] = " 10000000004@chatroom , wxid_seed01 "
    check("名单内", T.in_tool_scope("10000000004@chatroom") is True)
    check("名单内（私聊）", T.in_tool_scope("wxid_seed01") is True)
    check("名单外", T.in_tool_scope("10000000001@chatroom") is False)
    check("空 chat_id 不在名单内", T.in_tool_scope("") is False)
    check("去空白去空项", T.tool_scope() == ("10000000004@chatroom", "wxid_seed01"))
    os.environ.pop(T.SCOPE_ENV, None)


def t_handle_verdict_gate() -> None:
    print("[t_handle_verdict_gate]")
    os.environ[T.SCOPE_ENV] = "10000000004@chatroom"
    out = T.handle_verdict({"tools": [{"tool": "web_search", "purpose": "查天气"}]}, turn_id="t1", chat_id="10000000001@chatroom")
    check("名单外 = 一行不做", out is None)
    check("名单外不留结果块", T.consume_block("10000000001@chatroom") == "")
    os.environ.pop(T.SCOPE_ENV, None)


def t_wiring() -> None:
    print("[t_wiring]")
    conv = _src("agents/conversational.py")
    block = conv[conv.index("def _tool_specs_block("):]
    block = block[: block.index("\ndef ", 10)]
    check("清单块读作用域", "_turnloop.in_tool_scope(chat_id)" in block)
    check("清单块拿 extra 里的 chat_id", 'extra.get("chat_id")' in block)
    check("调用点传 extra", "prompt, _tool_specs_block(extra)" in conv)
    loop = _src("core/tools/turnloop.py")
    hv = loop[loop.index("def handle_verdict("):]
    hv = hv[: hv.index("\ndef ", 10)]
    check("闭环入口先判作用域", "if not in_tool_scope(chat_id):" in hv)


def main() -> int:
    t_empty_means_all()
    t_whitelist()
    t_handle_verdict_gate()
    t_wiring()
    print()
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    for f in FAILS:
        print("  -", f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
