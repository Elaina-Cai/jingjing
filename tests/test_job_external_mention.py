"""对外发送里的 @：``send:群名|@小奇 在吗`` 要把 @ 交给真的 chip，正文里不许再留一份。

跑法：``python -X utf8 -u tests/test_job_external_mention.py``
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.jobs import executor  # noqa: E402

_FAILED: list[str] = []
_TOTAL = 0


def check(name: str, cond: object, detail: str = "") -> None:
    global _TOTAL
    _TOTAL += 1
    if cond:
        print(f"  ok   {name}")
        return
    _FAILED.append(name)
    print(f"  FAIL {name}  {detail}")


def step(action: str) -> SimpleNamespace:
    return SimpleNamespace(n=1, action=action, effects=executor.EXTERNAL_EFFECT)


def run(action: str, *, target: str = "鲸鲸的测试群") -> tuple[dict, list[dict]]:
    seen: list[dict] = []

    def external(to: str, text: str, *, turn_id: str = "", mention: str = "") -> dict:
        seen.append({"to": to, "text": text, "turn_id": turn_id, "mention": mention})
        return {"status": "sent", "detail": "ok", "local_id": 90, "server_id": 1230}

    out = executor._run_external(
        step(action),
        allow_external=True,
        external=external,
        target=target,
        turn_id="t-1",
    )
    return out, seen


def main() -> int:
    print("[1] split_mention 纯函数")
    check("前导 @ 剥出来", executor.split_mention("@小奇 在吗") == ("小奇", "在吗"), repr(executor.split_mention("@小奇 在吗")))
    check("多个空格也认", executor.split_mention("@小奇    在吗") == ("小奇", "在吗"), repr(executor.split_mention("@小奇    在吗")))
    # 2026-10-21：没有分隔符就不是「@谁 + 正文」——真机 `@Elaina，命令已执行`
    # 曾被整串当成名字、正文变空（吞正文比多留一个 @ 严重得多）。
    check("只有 @名字（无分隔符不拆）", executor.split_mention("@小奇") == ("", "@小奇"), repr(executor.split_mention("@小奇")))
    check("有空格才拆", executor.split_mention("@小奇 命令已执行") == ("小奇", "命令已执行"), repr(executor.split_mention("@小奇 命令已执行")))
    check("没有 @ 原样返回", executor.split_mention("在吗") == ("", "在吗"), repr(executor.split_mention("在吗")))
    check("正文中间 @ 不认", executor.split_mention("在吗 @小奇") == ("", "在吗 @小奇"), repr(executor.split_mention("在吗 @小奇")))
    check("空白 @ 不认", executor.split_mention("@ 在吗") == ("", "@ 在吗"), repr(executor.split_mention("@ 在吗")))
    check("空串安全", executor.split_mention("") == ("", ""), repr(executor.split_mention("")))

    print("[2] 动作串里带 @")
    out, seen = run("send:鲸鲸的测试群|@小奇 在吗")
    check("发出去了", out["ok"] is True, repr(out))
    check("只调一次", len(seen) == 1, repr(seen))
    check("mention 传对", seen and seen[0]["mention"] == "小奇", repr(seen))
    check("正文里没有 @小奇", seen and seen[0]["text"] == "在吗", repr(seen))
    check("收件人还是群", seen and seen[0]["to"] == "鲸鲸的测试群", repr(seen))
    check("changed 里带 local_id", out.get("changed", {}).get("local_id") == 90, repr(out))

    print("[3] 不带 @ 的老口径逐字不变")
    out, seen = run("send:张三|明天见")
    check("mention 是空串", seen and seen[0]["mention"] == "", repr(seen))
    check("正文原样", seen and seen[0]["text"] == "明天见", repr(seen))
    check("收件人张三", seen and seen[0]["to"] == "张三", repr(seen))

    print("[4] 只有 @ 没有正文：仍然发得出去（就是拍一下）")
    out, seen = run("send:鲸鲸的测试群|@小奇")
    check("ok", out["ok"] is True, repr(out))
    # 2026-10-21 之后：没有分隔符就不拆，`@小奇` 整串是正文（宁可多留一个 @，
    # 也不要把正文吞掉）。
    check("没有分隔符就当正文原样", seen and seen[0]["mention"] == "" and seen[0]["text"] == "@小奇", repr(seen))

    print("[5] 全空就不发")
    out, seen = run("send:鲸鲸的测试群|")
    check("unsupported", "unsupported" in out["detail"], repr(out))
    check("一次都没调", seen == [], repr(seen))

    print("[6] 老发送器不认 mention 时不许炸")
    def legacy(to: str, text: str, *, turn_id: str = "") -> dict:
        return {"status": "sent", "detail": "ok", "local_id": 91}

    out = executor._run_external(
        step("send:鲸鲸的测试群|@小奇 在吗"),
        allow_external=True,
        external=legacy,
        target="鲸鲸的测试群",
        turn_id="t-2",
    )
    check("失败但被兜住（不冒泡）", out["ok"] is False, repr(out))
    check("原因是 TypeError", "TypeError" in out["detail"], repr(out))

    print("[7] 静态纪律")
    src = (ROOT / "core/jobs/executor.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    check("split_mention 在 executor 里", "split_mention" in names, sorted(names))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    check(
        "executor 不 import speech",
        not any(m.startswith("core.speech") for m in imported),
        sorted(imported),
    )
    check("MENTION_PREFIX 只在这里定义一次", src.count("MENTION_PREFIX: str") == 1, str(src.count("MENTION_PREFIX: str")))

    print(f"RESULT {len(_FAILED)} failed")
    for name in _FAILED:
        print(f"  - {name}")
    return len(_FAILED)


if __name__ == "__main__":
    raise SystemExit(main())
