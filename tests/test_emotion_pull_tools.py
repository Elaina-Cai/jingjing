# -*- coding: utf-8 -*-
"""情绪层 / 态度层从「注入」改成「工具拉取」（2026-10-23 主人拍板）。

口径
====
- 人设层仍始终全量注入（不在本文件范围）；
- 情绪层与态度层**不进 prompt**，模型自己填参数来取；
- 情绪读数**跨会话合并**——同一个人在不同群里的读数要合成一条，心境更是不该按窗口切开。

本文件钉四件事
==============
① 折叠函数提出来之后，单份模型的口径**逐字不变**（`_fold_emotion_events` == `_fold_events`）；
② 跨会话合并：两份模型的读数合起来；按人过滤只数那个人的事件；
③ 衰减照旧（半衰期没被合并改掉）；
④ 两件工具真的注册、真的能读（心境 / 对人的态度 / 立场卡分人）。

Run directly:
    python tests/test_emotion_pull_tools.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.context import emotion                                   # noqa: E402
from core.context.situation import WorldModel, note_emotion        # noqa: E402

NOW = datetime(2026, 10, 23, 12, 0)
FAILS: list[str] = []
COUNT = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if cond:
        print(f"  ok   {name}")
    else:
        FAILS.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


def _model(*pairs: tuple[str, float], at: str) -> WorldModel:
    """一份世界模型，里面几条 emotion_observed（who_id, level）。"""
    model = WorldModel()
    for who_id, level in pairs:
        note_emotion(model, level=level, who_id=who_id, who=who_id.upper(), at=at)
    return model


# ---------------------------------------------------------------- ① 口径不变
def test_fold_events_matches_single_model() -> None:
    model = _model(("w1", 0.4), ("w2", -0.2), at="2026-10-23T10:00")
    old = emotion._fold_emotion_events(model, now=NOW, half_life_hours=48.0)
    new = emotion._fold_events(model.events, now=NOW, half_life_hours=48.0)
    check("单份模型：新旧折叠结果逐字相同", old == new, f"{old} vs {new}")
    check("单份模型：条数=事件数", old[1] == 2, str(old))
    # 老公开 API 仍可用且走同一条路
    mood = emotion.current_mood(model, now=NOW, half_life_hours=48.0)
    check("current_mood 与合并读数（单模型）一致", mood.samples == 2, str(mood))


# ---------------------------------------------------------------- ② 跨会话合并
def test_mood_merges_across_chats() -> None:
    a = _model(("w1", 0.4), at="2026-10-23T12:00")   # 与 NOW 同一刻 => 权重恰好 1
    b = _model(("w2", 0.4), at="2026-10-23T12:00")
    one = emotion.mood_across_chats(models=[a], now=NOW, half_life_hours=48.0)
    both = emotion.mood_across_chats(models=[a, b], now=NOW, half_life_hours=48.0)
    check("单独一份的读数", abs(one.level - 0.4) < 1e-6 and one.samples == 1, str(one))
    check("两份合并：条数相加", both.samples == 2, str(both))
    check("两份合并：水平相加（clamp 内）", abs(both.level - 0.8) < 1e-6, str(both))
    check("两份合并：被 clamp 到 ±1", both.level <= 1.0, str(both))


def test_attitude_filters_person_across_chats() -> None:
    a = _model(("w1", 0.5), ("w2", 0.9), at="2026-10-23T11:30")
    b = _model(("w1", 0.5), at="2026-10-23T11:30")
    w1 = emotion.attitude_across_chats("w1", models=[a, b], now=NOW, half_life_hours=48.0)
    w2 = emotion.attitude_across_chats("w2", models=[a, b], now=NOW, half_life_hours=48.0)
    check("跨会话按人过滤：w1 两条", w1.samples == 2, str(w1))
    check("跨会话按人过滤：w2 一条", w2.samples == 1, str(w2))
    check("不认识的 id 返回空读数", emotion.attitude_across_chats(
        "nobody", models=[a, b], now=NOW).samples == 0)
    mood = emotion.mood_across_chats(models=[a, b], now=NOW, half_life_hours=48.0)
    check("心境不过滤人（三条全算）", mood.samples == 3, str(mood))
    check("无读数时标签是稳态", emotion.MoodState().label == "平")


# ---------------------------------------------------------------- ③ 衰减照旧
def test_decay_survives_merging() -> None:
    fresh = _model(("w1", 1.0), at="2026-10-23T11:00")
    stale = _model(("w1", 1.0), at="2026-10-19T12:00")      # 96h，恰两个半衰期
    f = emotion.attitude_across_chats("w1", models=[fresh], now=NOW, half_life_hours=48.0)
    s = emotion.attitude_across_chats("w1", models=[stale], now=NOW, half_life_hours=48.0)
    check("新鲜的权重≈1", f.level > 0.9, str(f))
    check("两个半衰期前≈0.25", abs(s.level - 0.25) < 0.05, str(s))
    both = emotion.attitude_across_chats("w1", models=[fresh, stale], now=NOW, half_life_hours=48.0)
    check("合并后是各自衰减之和（超出 ±1 就 clamp）",
          abs(both.level - min(1.0, f.level + s.level)) < 1e-6, str(both))
    check("坏时间戳的事件不计入", emotion._fold_events(
        [SimpleNamespace(kind="emotion_observed", at="坏", payload={"level": 0.9})],
        now=NOW)[1] == 0)


# ---------------------------------------------------------------- ④ 两件工具
def _run(coro):
    return asyncio.run(coro)


def test_read_emotion_tool() -> None:
    from agents.tools.emotion import ReadEmotionTool

    a = _model(("w1", 0.6), at="2026-10-23T11:30")
    b = _model(("w1", 0.3), at="2026-10-23T11:30")
    original = emotion.models_for_reading
    emotion.models_for_reading = lambda chat_id="": [a, b]
    try:
        text = _run(ReadEmotionTool().execute(person_id="w1", person_name="小A")).output
    finally:
        emotion.models_for_reading = original
    check("心境一行", "心境：" in text, text)
    check("对其人的态度一行", "对小A：" in text, text)
    check("跨会话合并了那个人的两条读数", "2 条读数" in text, text)
    check("给了读数就有语气建议", "照常即可" in text or "放松" in text or "保持" in text, text)
    check("注明了跨会话口径", "跨会话合并" in text, text)


def test_read_emotion_tool_without_readings() -> None:
    from agents.tools.emotion import ReadEmotionTool

    original = emotion.models_for_reading
    emotion.models_for_reading = lambda chat_id="": []
    try:
        text = _run(ReadEmotionTool().execute()).output
    finally:
        emotion.models_for_reading = original
    check("没有读数时仍是稳态标签", "心境：平" in text, text)
    check("没有读数时说明原因", "没有情绪读数" in text, text)
    check("没给 person_id 就不出现对人的行", "对" not in text.split("心境：")[1][:2], text)


_DDL = """
CREATE TABLE stance_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL DEFAULT 'group',
    platform TEXT NOT NULL DEFAULT '',
    account_id TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL DEFAULT '',
    person_key TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'owner',
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
)
"""


def _repo():
    from sqlalchemy import create_engine, text as _sql
    from sqlalchemy.pool import StaticPool

    engine = create_engine("sqlite://", poolclass=StaticPool, future=True)
    with engine.begin() as conn:
        conn.execute(_sql(_DDL))
    return SimpleNamespace(engine=engine)


def _seed(repo, *, chat_id: str, person_key: str, content: str) -> None:
    from sqlalchemy import text as _sql

    with repo.engine.begin() as conn:
        conn.execute(
            _sql("insert into stance_cards (chat_id, person_key, content) values (:c, :p, :x)"),
            {"c": chat_id, "p": person_key, "x": content},
        )


def test_stance_tool_person_filter() -> None:
    from agents.tools.emotion import GetStanceCardsTool

    repo = _repo()
    _seed(repo, chat_id="g1", person_key="", content="不替人催债")
    _seed(repo, chat_id="g1", person_key="wxid_a", content="对小A说话软一点")
    _seed(repo, chat_id="g1", person_key="wxid_b", content="对小B只谈正事")
    tool = GetStanceCardsTool(meta_repo=repo)
    only_a = _run(tool.execute(person_id="wxid_a", chat_id="g1")).output
    check("群卡总在", "不替人催债" in only_a, only_a)
    check("命中这个人的卡", "对小A说话软一点" in only_a, only_a)
    check("不出现别人的卡", "对小B只谈正事" not in only_a, only_a)
    everything = _run(tool.execute(chat_id="g1")).output
    check("不给 person_id 时两张分人卡都在", "对小A" in everything and "对小B" in everything,
          everything)


def test_stance_tool_empty() -> None:
    from agents.tools.emotion import GetStanceCardsTool

    text = _run(GetStanceCardsTool(meta_repo=_repo()).execute(person_id="wxid_x")).output
    check("没有卡时说清楚", "没有生效的立场卡" in text, text)


def test_tools_registered_and_declared() -> None:
    from agents.tools.base import ToolRegistry
    from core.tools import declared as D

    tools = ToolRegistry.create_default().all_tools()
    check("read_emotion 已注册", "read_emotion" in tools)
    check("get_stance_cards 已注册", "get_stance_cards" in tools)
    check("read_emotion 的 schema 带 person_id",
          "person_id" in tools["read_emotion"].schema()["function"]["parameters"]["properties"])
    check("get_stance_cards 的 schema 带 person_id",
          "person_id" in tools["get_stance_cards"].schema()["function"]["parameters"]["properties"])
    names = D.declared_names()
    check("read_emotion 已声明", "read_emotion" in names, str(sorted(names)))
    check("get_stance_cards 已声明", "get_stance_cards" in names)
    effects = {str(getattr(e, "value", e)) for e in D.declared_specs()[0].effects}
    check("声明面的 effect 拼写没跑偏", effects in ({"read"}, {"write_local"}, {"external_send"}),
          str(effects))


def main() -> int:
    for fn in (
        test_fold_events_matches_single_model,
        test_mood_merges_across_chats,
        test_attitude_filters_person_across_chats,
        test_decay_survives_merging,
        test_read_emotion_tool,
        test_read_emotion_tool_without_readings,
        test_stance_tool_person_filter,
        test_stance_tool_empty,
        test_tools_registered_and_declared,
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