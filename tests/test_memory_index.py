# -*- coding: utf-8 -*-
"""记忆「push 留索引」这条链：索引块只给 key，正文按 key 取（2026-10-23 拍板）。

守的是什么
==========
1. 提示词里**不能**再出现整段正文（那正是要省掉的东西），只能出现 key + 时间 + 开头一句；
2. 同一轮里索引与正文必须是**同一份**数据（绑定身份的索引快照），
   否则「m1 说是加班，取回来却是别的事」这种错位无法排查；
3. 取全文这件事必须真的走得通（工具能按 key 命中、错 key 会说清有哪些）；
4. 关掉开关要能回到旧行为（整段正文注入），别把退路焊死。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.tools.memory_index import ReadMemoryTool  # noqa: E402
from core.terminal import memory_gateway as mg  # noqa: E402
from core.terminal.memory_gateway import MemoryGateway, MemoryHit  # noqa: E402

FAILS: list[str] = []
COUNT = 0
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def check(name: str, cond: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if cond:
        print(f"  ok   {name}")
    else:
        FAILS.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


LONG = (
    "主人说他这周每天加班到十一点，回家只想躺着，不想说话，也别催他早睡。"
    "这条她记了两天，周五还得问一句周报到底交了没有（收尾这段是唯一的，用来验证"
    "索引块里没有正文）。"
)


def _hits() -> list[MemoryHit]:
    return [
        MemoryHit(
            memory_id="e1",
            content=LONG,
            occurred_at=NOW - timedelta(days=2),
            event_type="message",
            speaker="主人",
            source_event_ids=("ev-1", "ev-2"),
            score=0.9,
        ),
        MemoryHit(
            memory_id="e2",
            content="鲸鲸答应周五提醒主人交周报",
            occurred_at=NOW - timedelta(hours=3),
            event_type="message",
            speaker="鲸鲸",
            source_event_ids=("ev-3",),
            score=0.7,
        ),
    ]


class _StubGateway(MemoryGateway):
    """只替换「从哪拿 hits」与「principal 是谁」，其余（render/attach）走真实现。"""

    def __init__(self, hits: list[MemoryHit]) -> None:
        self._hits = hits
        self.calls: list[str] = []

    def principal_from_context(self, context, *, refresh: bool = False):  # noqa: D102
        from core.terminal.principal import PrincipalContext, ChatType, RelationLevel
        if getattr(context,'principal',None) is None:
            data=context.extra
            context.principal=PrincipalContext(data['platform'],data['account_id'],data['chat_id'],
                ChatType.GROUP if data.get('is_group') else ChatType.PRIVATE,'person','P',RelationLevel.ORDINARY,
                group_id=data['chat_id'] if data.get('is_group') else '')
        return context.principal

    def recall(self, principal, **kwargs):  # noqa: D102
        self.calls.append(str(kwargs.get("query") or ""))
        return list(self._hits)


def _context(extra: dict | None = None) -> SimpleNamespace:
    values = {"platform": "wechat", "account_id": "bot", "chat_id": "c1", "is_group": True}
    values.update(extra or {})
    ctx = SimpleNamespace(extra=values)
    ctx.chat_id = "c1"
    ctx.is_group = True
    return ctx


def test_preview_is_short_and_clean() -> None:
    line = mg._preview("@鲸鲸 " + "很长的内容" * 40)
    check("预览不含 @ 提及", "@" not in line)
    check("预览会被截断", line.endswith("…") and len(line) <= mg.INDEX_PREVIEW_CHARS + 1, str(len(line)))
    check("短文本不走截断", mg._preview("加班") == "加班")


def test_entries_keep_full_text_and_source_events() -> None:
    entries = mg.index_entries(_hits())
    check("key 从 m1 起顺序编号", [e["key"] for e in entries] == ["m1", "m2"], str([e["key"] for e in entries]))
    check("正文完整留在 entries 里", entries[0]["content"] == LONG)
    check("索引行只有开头一句", entries[0]["preview"] != LONG and LONG not in entries[0]["preview"])
    check("来源事件 id 一起留着", entries[0]["source_event_ids"] == ["ev-1", "ev-2"])
    check("条数上限生效", len(mg.index_entries(_hits(), max_items=1)) == 1)
    check("空正文的条目被丢掉", mg.index_entries([MemoryHit("x", "   ", NOW, "message")]) == [])


def test_entries_and_read_memory_keep_structured_claim_metadata() -> None:
    hit = MemoryHit(
        memory_id="memory-address",
        content="address = 深圳",
        occurred_at=NOW,
        event_type="belief",
        source_event_ids=("evt-source",),
        metadata={
            "subject_key": "person-a",
            "predicate": "lives_in",
            "conflict": True,
            "evidence_state": "unknown",
        },
    )
    entries = mg.index_entries([hit])
    check(
        "索引保留事实键",
        entries[0]["metadata"]["subject_key"] == "person-a"
        and entries[0]["metadata"]["predicate"] == "lives_in",
        str(entries),
    )
    ctx = _context()
    _StubGateway([hit]).attach_to_context(ctx)
    result = __import__("asyncio").run(_run_tool(ctx, keys="m1"))
    claims = result.data.get("memory_claims") or []
    check(
        "read_memory 保留冲突和证据状态",
        claims
        and claims[0]["metadata"]["conflict"] is True
        and claims[0]["metadata"]["evidence_state"] == "unknown",
        str(result.data),
    )


def test_render_index_never_carries_the_body() -> None:
    entries = mg.index_entries(_hits())
    block = mg.render_index(entries)
    check("有表头", block.startswith(mg.INDEX_HEADER))
    check("有「怎么取全文」的提示", "read_memory" in block)
    check("每行带 key", "- m1 " in block and "- m2 " in block)
    m1_line = next((line for line in block.splitlines() if "- m1 " in line), "")
    check("带来源、发言人与时间", "[10-21 12:00] [某人说过] 主人:" in m1_line, m1_line)
    check("正文尾部不在块里", LONG[-30:] not in block)
    check("空 entries 不渲染", mg.render_index([]) == "")


def test_attach_to_context_pushes_the_index_only() -> None:
    old = os.environ.pop(mg.MEMORY_INDEX_ENV, None)
    try:
        ctx = _context()
        gateway = _StubGateway(_hits())
        block = gateway.attach_to_context(ctx, query="加班", limit=6)
        check("推给提示词的是索引", block.startswith(mg.INDEX_HEADER) and "- m1 " in block)
        check("正文没被推进提示词", LONG not in block)
        from core.memory.index_scope import bound_index
        snapshot = bound_index(ctx)
        check("正文只进绑定身份的索引快照",
              snapshot is not None and snapshot.entries_for(ctx)[0]["content"] == LONG)
        check("extra 里不再留未绑定身份的正文副本", "memory_index" not in ctx.extra)
        check("旧口径的键还在（下游不用改）",
              ctx.extra.get("terminal_memory_attached") is True
              and ctx.extra.get("terminal_memory_count") == 2
              and ctx.extra.get("terminal_memory_sources") == ["ev-1", "ev-2", "ev-3"])
        check("recall 用的是这一轮的问题", gateway.calls == ["加班"], str(gateway.calls))
    finally:
        if old is not None:
            os.environ[mg.MEMORY_INDEX_ENV] = old


def test_switch_off_restores_the_old_block() -> None:
    old = os.environ.get(mg.MEMORY_INDEX_ENV)
    try:
        os.environ[mg.MEMORY_INDEX_ENV] = "off"
        ctx = _context()
        block = _StubGateway(_hits()).attach_to_context(ctx, query="加班")
        check("关掉后回到整段正文", block.startswith("[事件记忆") and "加班到十一点" in block)
        from core.memory.index_scope import bound_index
        snapshot = bound_index(ctx)
        check("关掉后不写索引表", snapshot is not None and snapshot.entries_for(ctx) == [])
        check("开关读法：off/0/关 都算关",
              not mg.memory_index_enabled())
        for value in ("", "on", "1", "yes"):
            os.environ[mg.MEMORY_INDEX_ENV] = value
            check(f"开关读法：{value or '空'} 算开", mg.memory_index_enabled())
    finally:
        os.environ.pop(mg.MEMORY_INDEX_ENV, None)
        if old is not None:
            os.environ[mg.MEMORY_INDEX_ENV] = old


async def _run_tool(ctx, **kwargs):
    tool = ReadMemoryTool()
    tool.set_context(ctx)
    return await tool.execute(**kwargs)


def test_tool_reads_the_pushed_body() -> None:
    entries = mg.index_entries(_hits())
    ctx = _context()
    _StubGateway(_hits()).attach_to_context(ctx)

    one = __import__("asyncio").run(_run_tool(ctx, keys="m1"))
    check("按 key 拿到全文", LONG in one.output and one.is_error is False)
    check("带发言人", "主人" in one.output)
    check("data 里回报来源事件", one.data.get("source_event_ids") == ["ev-1", "ev-2"], str(one.data))

    many = __import__("asyncio").run(_run_tool(ctx, keys="m1, m2"))
    check("一次取多条", LONG in many.output and "周报" in many.output)

    by_id = __import__("asyncio").run(_run_tool(ctx, keys="e2"))
    check("也接受 memory_id", "周报" in by_id.output and by_id.is_error is False)


def test_tool_says_what_is_available() -> None:
    entries = mg.index_entries(_hits())
    ctx = _context()
    _StubGateway(_hits()).attach_to_context(ctx)

    bad = __import__("asyncio").run(_run_tool(ctx, keys="m9"))
    check("错 key 报 unknown_memory_key", bad.error == "unknown_memory_key", bad.error)
    check("错 key 时列出可用的", "m1" in bad.output and "m2" in bad.output)

    empty = __import__("asyncio").run(_run_tool(ctx, keys=""))
    check("没给 key 时要求给", empty.error == "memory_key_required", empty.error)

    no_index = __import__("asyncio").run(_run_tool(_context(), keys="m1"))
    check("没有索引时说清为什么", no_index.error == "no_memory_index", no_index.error)


def test_wiring_is_declared_and_registered() -> None:
    from agents.tools.base import ToolRegistry
    from core.routing.extra_contract import EXTRA_CONTRACT
    from core.tools.declared import DECLARED

    names = set(ToolRegistry.create_default().all_tools())
    check("工具已注册", {"read_memory", "read_emotion", "get_stance_cards"} <= names)
    check("工具已声明", "read_memory" in {row[0] for row in DECLARED})
    check("正文不再经 extra 传递（I7）", "memory_index" not in EXTRA_CONTRACT)
    base_src = (ROOT / "agents/tools/base.py").read_text(encoding="utf-8")
    check("注册点在 base.create_default 里", "registry.register(ReadMemoryTool())" in base_src)


def main() -> int:
    import asyncio

    for fn in (
        test_preview_is_short_and_clean,
        test_entries_keep_full_text_and_source_events,
        test_render_index_never_carries_the_body,
        test_attach_to_context_pushes_the_index_only,
        test_switch_off_restores_the_old_block,
        test_tool_reads_the_pushed_body,
        test_tool_says_what_is_available,
        test_wiring_is_declared_and_registered,
    ):
        print(fn.__name__)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            FAILS.append(f"{fn.__name__} raised {exc!r}")
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    for item in FAILS:
        print(f"  - {item}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
