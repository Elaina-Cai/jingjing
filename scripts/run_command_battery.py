"""离线对账 20 条命令：把「她每一步怎么判」逐条打出来（不发、不写库）。

用法：``python -X utf8 -m scripts.run_command_battery``

四段依次执行，任何一段说不通就短路：

1. **动词表**（`planner.looks_like_task`）——命中了哪个词；
2. **语义兜底**（`planner.classify_task`，真模型，只回 yes/no）——动词表没命中时才跑；
3. **能力闸**（`capability_gate.gate`）——我们家到底有没有这个口；
4. **槽**——缺关键参数就问一句（问句原文来自 `capability_gate.slot_question`）。

发送与回读必须真机验（微信 UI 那段跑不了），本脚本不假装。
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.jobs import capability_gate as G  # noqa: E402
from core.jobs import slots as S  # noqa: E402
from core.jobs import planner as P  # noqa: E402

CASES: list[tuple[str, str]] = [
    ("往「鲸鲸的测试群」说一句：明天带伞，别忘", "do"),
    ("往「Elaina」发一句：我在", "do"),
    ("记一下周五交报告", "do"),
    ("今晚九点提醒我吃药", "do"),
    ("帮我搜一下「微信 4.x 撤回时限」", "do"),
    ("提醒一下群里的人明天带伞", "ask"),
    ("到点提醒我", "ask"),
    ("帮她发一句我晚点到", "ask"),
    ("帮我给他转 200 块", "refuse"),
    ("帮我订明早去北京的高铁", "refuse"),
    ("给我妈打个电话", "refuse"),
    ("把这封邮件发出去", "refuse"),
    ("上次说的那个资料发我一下", "ask"),
    ("别在群里说这件事", "refuse"),
    ("今天好累", "say"),
    ("你刚才发的那句撤回", "do"),
    ("现在几点", "say"),
    ("杭州今天天气怎么样", "say"),
    ("在鲸鲸的测试群里说一句：今天下雨记得带伞", "do"),
    ("我们明天几点集合", "say"),
]

TARGET_RE = re.compile(r"「([^」]{1,30})」|(?:往|给|在)([^，。；\s]{1,20}?)(?:里|那边)?(?:说|发)")


def _verbs(text: str) -> list[str]:
    return [w for w in getattr(P, "ACTION_VERBS", ()) if w and w in text]


def _target(text: str) -> str:
    m = TARGET_RE.search(text)
    if not m:
        return ""
    return (m.group(1) or m.group(2) or "").strip()


def _missing_slot(text: str) -> str:
    """只判两件最容易缺的：发给谁 / 什么时候（其余交给真链路的校验器）。"""
    if any(w in text for w in ("发", "说一句", "告诉", "转告", "提醒一下")):
        if not _target(text) and not any(w in text for w in ("我", "我这边")):
            return "chat"
    if "提醒" in text and not re.search(r"\d|今晚|明早|明天|周[一二三四五六日天]|点", text):
        return "time"
    return ""


async def main() -> int:
    send = P._default_send()
    miss = 0
    over = 0
    for index, (text, expect) in enumerate(CASES, start=1):
        hit_words = _verbs(text)
        if hit_words:
            stage1 = f"命中「{hit_words[0]}」"
            task = True
        else:
            try:
                task = bool(await P.classify_task(user_text=text, send=send))
                stage1 = "语义兜底=" + ("要办" if task else "聊天")
            except Exception as exc:  # noqa: BLE001
                task = False
                stage1 = f"语义兜底失败({type(exc).__name__})"
        verdict = G.gate(text)
        if not task:
            final = "当聊天回"
        elif not verdict["ok"]:
            final = "拒：" + G.refuse_line(verdict["reason"])
        else:
            _miss = S.missing_slots(text)
            final = "问：" + S.ask_for(_miss) if _miss else "办（开一件事，不可逆则等人批准）"
        ok = (
            (expect == "do" and final.startswith("办"))
            or (expect == "ask" and final.startswith("问"))
            or (expect == "refuse" and final.startswith("拒"))
            or (expect == "say" and final.startswith("当聊天"))
        )
        if not ok:
            if expect in ("do", "ask") and final.startswith("当聊天"):
                miss += 1
            elif final.startswith(("办", "问")):
                over += 1
        print(f"[{index:02d}] 输入：{text}")
        print(f"     ① 动词表：{stage1}")
        print(f"     ② 能力闸：{'放行' if verdict['ok'] else '没有这个口（' + verdict['kind'] + '）'}")
        print(f"     ③ 最终：{final}")
        print(f"     期望：{expect} → {'✅' if ok else '❌'}")
    print()
    print(f"RESULT 漏 {miss} / 滥 {over} / 共 {len(CASES)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
