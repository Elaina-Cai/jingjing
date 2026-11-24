# -*- coding: utf-8 -*-
"""system prompt 出站白名单（2026-10-23 主人点名 m07351）。

主人点名保留的只有：
- 人设：**只留身份首行**（`你是鲸鲸。`），`[人格]` 与全部规则块都不再出现；
- 世界窗口：`[世界窗口]`、`[现在这一屏]`；
- 记忆：六块全留；在手的事：四块全留。

本文件钉三件事：**该留的真留**、**该丢的真丢**、**丢掉的时候有账**。
另有两条防倒退：白名单里的名字必须都是登记过的块头（防打错字静默失效）；
``YINYUE_PROMPT_SCOPE=all`` 必须逐字节还原（回滚面是真的）。

Run directly:
    python tests/test_prompt_scope.py
"""
from __future__ import annotations

import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.context import prompt_layers as pl                      # noqa: E402
from core.context import prompt_scope as sc                       # noqa: E402

COUNT = 0
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    print(f"[{'ok' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if (detail and not ok) else ""))
    if not ok:
        FAILS.append(f"{name} :: {detail}")


class _Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, dict]] = []

    def info(self, event: str, **kw) -> None:
        self.lines.append((event, kw))


def _prompt() -> str:
    blocks = [
        ("你好。", ""),
        ("[人格]", "- 身份映射若干行\n- 性格倾向（由强到弱）：…"),
        ("[基本表达]", "说人话"),
        ("[不确定与沉默]", "拿不准就别装懂"),
        ("[沉默协议]", "可以不回"),
        ("[拿不准时]", "问一句"),
        ("[安全与边界]", "不越界"),
        ("[语气风格]", "别客套"),
        ("[遇事怎么反应]", "先看再说"),
        ("[强制规则]", "没看图不许描述"),
        ("[主动触达规则]", "主动搭话"),
        ("[纠错确认]", "改口"),
        ("[你说过·作数]", "- 昨晚说早点睡"),
        ("[世界窗口]", "12:01 她：在吗"),
        ("[现在这一屏]", "主人私聊：在吗"),
        ("[局况]", "群里三个人"),
        ("[当前局面]", "她刚回复过"),
        ("[时间感知]", "今天是周五"),
        ("[指代事实]", "他=张三"),
        ("[消息归属]", "上一条是谁说的"),
        ("[位置]", "在家"),
        ("[群聊最近消息·仅作上下文]", "- 1 2 3"),
        ("[最近对话]", "- 4 5 6"),
        ("[读场]", "- P5.location = 到了"),
        ("[本轮理解]", "她在问我"),
        ("[话题簇与锚点]", "话题一"),
        ("[事件记忆·索引]", "1. key=abc 摘要"),
        ("[按需记忆]", "- 他怕冷"),
        ("[场外补充]", "- 群里最近流行这个梗"),
        ("[群成员名单·权威]", "- 张三(wxid_a)"),
        ("[关系事实]", "- 主人：很熟"),
        ("[在手的事]", "- 提醒:喝水"),
        ("[本轮步骤]", "1. 查一下"),
        ("要真办就附一段计划", "<plan>{}</plan>"),
        ("[本轮可用的工具]", "- send_message"),
    ]
    out = []
    for head, body in blocks:
        out.append(head)
        if body:
            out.append(body)
        out.append("")
    return "\n".join(out).strip()


def t_identity_line_survives() -> None:
    text, _ = sc.apply_scope(_prompt())
    check("身份首行「你是鲸鲸。」保留", text.lstrip().startswith("你好。"), text[:40])


def t_persona_blocks_all_dropped() -> None:
    text, dropped = sc.apply_scope(_prompt())
    persona = [
        "[人格]", "[基本表达]", "[不确定与沉默]", "[沉默协议]", "[拿不准时]", "[安全与边界]",
        "[语气风格]", "[遇事怎么反应]", "[强制规则]", "[主动触达规则]",
        "[纠错确认]", "[你说过·作数]",
    ]
    left = [b for b in persona if b in text]
    check("人设层 12 块全部不再出现", not left, "还在: " + ", ".join(left))
    missing = [b for b in persona if b not in dropped]
    check("人设层 12 块全部进了丢弃账", not missing, "没记账: " + ", ".join(missing))


def t_world_keeps_two() -> None:
    text, dropped = sc.apply_scope(_prompt())
    check("[世界窗口] 保留", "[世界窗口]" in text)
    check("[现在这一屏] 保留", "[现在这一屏]" in text)
    gone = ["[局况]", "[当前局面]", "[时间感知]", "[指代事实]", "[消息归属]", "[位置]",
            "[群聊最近消息", "[最近对话", "[读场]", "[本轮理解]", "[话题簇与锚点]"]
    left = [b for b in gone if b in text]
    check("世界窗口其余 11 块全丢", not left, "还在: " + ", ".join(left))


def t_memory_and_job_kept() -> None:
    text, _ = sc.apply_scope(_prompt())
    kept = ["[事件记忆·索引]", "[按需记忆]", "[场外补充]", "[群成员名单·权威]",
            "[关系事实]", "[在手的事]", "[本轮步骤]", "要真办就附一段计划", "[本轮可用的工具]"]
    missing = [b for b in kept if b not in text]
    check("记忆六块 + 在手的事四块保留", not missing, "丢了: " + ", ".join(missing))


def t_rollback_switch_is_byte_exact() -> None:
    raw = _prompt()
    old = os.environ.get(sc.SCOPE_ENV)
    os.environ[sc.SCOPE_ENV] = "all"
    try:
        text, dropped = sc.apply_scope(raw)
    finally:
        if old is None:
            os.environ.pop(sc.SCOPE_ENV, None)
        else:
            os.environ[sc.SCOPE_ENV] = old
    check("YINYUE_PROMPT_SCOPE=all 逐字节还原", text == raw and not dropped)


def t_drops_are_logged() -> None:
    log = _Log()
    _, dropped = sc.apply_scope(_prompt(), log=log)
    events = [event for event, _ in log.lines]
    check("丢弃时写 prompt_block_dropped", events == ["prompt_block_dropped"], str(events))
    payload = log.lines[0][1] if log.lines else {}
    check("账里有块名与条数", payload.get("count") == len(dropped) and len(payload.get("blocks") or []) == len(dropped))


def t_unknown_headers_are_not_eaten() -> None:
    text, dropped = sc.apply_scope("你好。\n\n[随口一句]\n不要被吃掉\n\n[局况]\n细节")
    check("未登记的块头不被静默删除", "[随口一句]" in text and "不要被吃掉" in text)
    check("同一段里的登记块仍然按名单判", "[局况]" in dropped and "[局况]" not in text)


def t_no_triple_blank_lines() -> None:
    text, _ = sc.apply_scope(_prompt())
    check("丢段后不留连续空行", "\n\n\n" not in text)


def t_idempotent() -> None:
    once, _ = sc.apply_scope(_prompt())
    twice, dropped2 = sc.apply_scope(once)
    check("再过滤一次内容不变、也不该再有丢弃", once == twice and not dropped2, str(dropped2))


def t_whitelist_names_are_registered() -> None:
    unknown = [b for b in sc.KEEP_BLOCKS if pl.layer_of(b) == ""]
    check("白名单里的名字都是登记过的块头（防打错字）", not unknown, "打错的: " + ", ".join(unknown))


def t_wiring_is_in_the_reply_path() -> None:
    import agents.conversational as conv

    shell = inspect.getsource(conv.ConversationalAgent._build_system_prompt)
    check("唯一出口（薄壳）真的调了 apply_scope", "prompt_scope.apply_scope(" in shell)
    check("薄壳作用在 raw 拼装结果上", "_build_system_prompt_raw(" in shell)
    # 有人绕过薄壳直接调 raw 就等于白名单失效——全仓只许薄壳调它。
    module_src = inspect.getsource(conv)
    callers = module_src.count("self._build_system_prompt_raw(")
    check("仓库里只有薄壳一处调用 raw", callers == 1, f"实际 {callers} 处")


def main() -> int:
    for fn in (
        t_identity_line_survives,
        t_persona_blocks_all_dropped,
        t_world_keeps_two,
        t_memory_and_job_kept,
        t_rollback_switch_is_byte_exact,
        t_drops_are_logged,
        t_unknown_headers_are_not_eaten,
        t_no_triple_blank_lines,
        t_idempotent,
        t_whitelist_names_are_registered,
        t_wiring_is_in_the_reply_path,
    ):
        print(fn.__name__)
        try:
            fn()
        except Exception as exc:                       # noqa: BLE001
            import traceback

            traceback.print_exc()
            FAILS.append(f"{fn.__name__} raised {exc!r}")
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    for item in FAILS:
        print(f"  - {item}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())