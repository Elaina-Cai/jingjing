"""参数校验器：只判「值能不能落地」，不问「齐不齐」。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.tools import validate as v

FAILS: list[str] = []
COUNT = 0


def check(name: str, cond: bool, detail: object = "") -> None:
    global COUNT
    COUNT += 1
    if cond:
        print(f"  ok   {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name} {detail}")


def t_text() -> None:
    check("text：空的算缺", v.check("text", "   ")[0] is False)
    check("text：正常放行", v.check("text", "明天带伞")[:2] == (True, "明天带伞"))
    check("text：超长挡下", v.check("text", "啊" * (v.MAX_TEXT_CHARS + 1))[2] == "too_long")


def t_chat() -> None:
    check("chat：wxid 放行", v.check("chat", "wxid_seed01")[0] is True)
    check("chat：群号放行", v.check("chat", "10000000004@chatroom")[0] is True)
    check("chat：整句话挡下", v.check("chat", "提醒一下群里的人明天带伞带伞带伞  啊啊")[0] is False)
    check("chat：空的算缺", v.check("chat", "")[2] == "empty")


def t_time() -> None:
    check("time：绝对时刻放行", v.check("time", "2026-10-22 09:00")[0] is True)
    check("time：相对说法挡下", v.check("time", "明天")[2] == "not_absolute")


def t_url_amount() -> None:
    check("url：http 放行", v.check("url", "https://example.com")[0] is True)
    check("url：非 http 挡下", v.check("url", "example.com")[2] == "not_http")
    check("amount：正常放行", v.check("amount", "20")[1] == 20.0)
    check("amount：非数挡下", v.check("amount", "两百")[2] == "not_number")
    check("amount：超上限挡下", v.check("amount", "5000")[2] == "over_limit")


def t_unknown_is_not_blocked() -> None:
    ok, out, reason = v.check("tone", "温和")
    check("不认得的类型放行（不拦只记）", ok is True and out == "温和" and reason == "")


def t_validate_params() -> None:
    got = v.validate_params({"chat": "", "text": "明天带伞", "tone": "温和"})
    check("批量：整体不合格", got["ok"] is False)
    check("批量：缺的是 chat", [b["name"] for b in got["bad"]] == ["chat"])
    check("批量：不认得的不进 bad", got["unknown"] == ["tone"])
    check("批量：合格值原样保留", got["resolved"]["text"] == "明天带伞")
    check("一句话人话", "chat（empty）" in v.describe_bad(got["bad"]))
    good = v.validate_params({"chat": "wxid_seed01", "text": "在"})
    check("批量：全合格", good["ok"] is True)


def t_no_required_table() -> None:
    check("没有 requires 表（只有校验器表）", not hasattr(v, "REQUIRES"))
    check("校验器表覆盖八类", set(v.VALIDATORS) == {"chat", "text", "query", "time", "url", "amount", "element", "path"})


def main() -> int:
    for fn in (t_text, t_chat, t_time, t_url_amount, t_unknown_is_not_blocked, t_validate_params, t_no_required_table):
        fn()
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
