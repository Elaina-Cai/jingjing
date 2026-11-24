"""看「她这一拍到底看到了什么」。

用法：
    python scripts/prompt_show.py latest
    python scripts/prompt_show.py <turn_id>
    python scripts/prompt_show.py <turn_id> --raw
    python scripts/prompt_show.py --diff <turn_a> <turn_b>

数据来自 `data/prompt/<turn_id>/request.json`（由 `core/infra/promptlog.py` 在
llm 客户端发请求前写入，见 `docs/提示词形状改造施工.md` Step 2）。
只读；不改任何状态。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.infra import promptlog  # noqa: E402 - 必须在 sys.path 补好之后


def _brief(text: str, n: int = 40) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= n else one[:n] + "…"


def _roles(messages: list) -> list[str]:
    return [str(m.get("role") or "?") for m in messages if isinstance(m, dict)]


def show(turn_id: str, *, raw: bool = False) -> int:
    data = promptlog.load(turn_id)
    if not data:
        print(f"没有这一拍的记录：{promptlog.path(turn_id)}")
        return 2
    if raw:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return 0
    rounds = data.get("rounds") or []
    print(f"turn {turn_id}  {len(rounds)} 轮  {promptlog.path(turn_id)}")
    prev = None
    for rd in rounds:
        msgs = rd.get("messages") or []
        tools = rd.get("tools") or []
        head = (f"[轮 {rd.get('i')}] {rd.get('provider') or '-'} / {rd.get('model') or '-'}"
                f"  messages {len(msgs)} 条  tools {len(tools)} 个")
        if rd.get("note"):
            head += f"  note={rd['note']}"
        print(head)
        if rd.get("params"):
            print(f"    params {json.dumps(rd['params'], ensure_ascii=False)}")
        if not tools:
            print("    tools 为空（这一轮没有告诉她能做什么）")
        else:
            names = [t.get("function", {}).get("name") or t.get("name") or "?" for t in tools]
            print("    tools: " + ", ".join(map(str, names)))
        start = 0
        if prev is not None and len(msgs) >= len(prev):
            start = len(prev)
            print(f"    （比上一轮 +{len(msgs) - len(prev)} 条）")
        for i in range(start, len(msgs)):
            m = msgs[i] if isinstance(msgs[i], dict) else {}
            body = m.get("content")
            if isinstance(body, list):
                body = json.dumps(body, ensure_ascii=False)
            n_calls = len(m.get("tool_calls") or [])
            tail = f"  tool_calls {n_calls}" if n_calls else ""
            print(f"    [{i}] {m.get('role') or '?':<9} {len(str(body or '')):>5} 字  {_brief(body)}{tail}")
        prev = msgs
    return 0


def diff(a: str, b: str) -> int:
    da, db = promptlog.load(a), promptlog.load(b)
    if not da or not db:
        print("两次都要有记录才能比")
        return 2
    ra, rb = da.get("rounds") or [], db.get("rounds") or []
    print(f"{a}  {len(ra)} 轮 / {b}  {len(rb)} 轮")
    for k in range(max(len(ra), len(rb))):
        x = ra[k] if k < len(ra) else {}
        y = rb[k] if k < len(rb) else {}
        xm, ym = x.get("messages") or [], y.get("messages") or []
        print(f"[轮 {k + 1}] messages {len(xm)} -> {len(ym)}   tools {len(x.get('tools') or [])} -> {len(y.get('tools') or [])}")
        for j in range(min(len(xm), len(ym))):
            xa = str((xm[j] or {}).get("content") or "")
            ya = str((ym[j] or {}).get("content") or "")
            if xa != ya:
                mark = "同" if len(xa) == len(ya) else f"{len(xa)} -> {len(ya)} 字"
                print(f"    [{j}] {mark}  改：{_brief(xa, 24)} → {_brief(ya, 24)}")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "--diff":
        if len(argv) < 3:
            print("用法：--diff <turn_a> <turn_b>")
            return 2
        return diff(argv[1], argv[2])
    turn_id = argv[0]
    if turn_id == "latest":
        got = promptlog.turns()
        if not got:
            print(f"还没有任何记录：{promptlog.root()}")
            return 2
        turn_id = got[0]
    return show(turn_id, raw="--raw" in argv[1:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
