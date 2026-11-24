# -*- coding: utf-8 -*-
"""B2 落地回归：``reminders`` 18 列 → WorkItem 字段映射 + ``repeat_rule`` 换算。

对应 docs/任务模型.md「B2 逐字段对照表」与 ``core/tasks/mapping.py``：

① 18 列逐项核对去向（含「无对应物」列进 ``spec_json.legacy``）；
② ``repeat_rule`` 逐值换算（daily/每天/每日、weekly/每周/每星期、monthly/每月/每个月、
   ``<N>h``/``<N>H``/``<N>d``/``<N>D``/``<N>天``（可带空格）、空）；
③ **未识别 repeat_rule** → 一次性 + 原文保留到 ``spec_json.legacy_repeat_rule`` + 告警：
   只认「纯整数 + 单位」，``2.5h`` / ``1h1d`` / ``-2h`` / ``12小时`` / ``30分钟`` /
   ``每周一`` / 纯数字一律不猜（旧实现的数字拼接会读成 25h / 11d / 2h）；
④ mention 回退（JSON 数组优先 / ``[mention_name]`` / 分组与私聊）；
⑤ purpose / status 未知值 → 保守映射 + 告警，不丢弃；
⑥ ``due_at`` 是 **naive 本地墙钟**（带偏移的输入先转本地再去掉 tzinfo）；
⑦ 名单解析**只有一个 owner**（``split_mention_names``）：迁移侧与投递侧
   （``agents.reminder_trigger.parse_mention_names``）对同一输入给出同一答案。

Run directly:
    python tests/test_reminder_mapping.py
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
# 隔离前导（照 tests/test_event_chain_links.py）：本文件只用纯函数，但保持同一纪律，
# 万一以后加了走 bootstrap 的断言也不会碰到生产库。
_TEST_LOG_DIR = str(Path("data") / "_test_logs_reminder_mapping")
Path(_TEST_LOG_DIR).mkdir(parents=True, exist_ok=True)
os.environ.setdefault("LOG_DIR", _TEST_LOG_DIR)
_ISO_ROOT = (ROOT / "data" / "_reminder_mapping_test" / "_iso").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("META_DB_PATH", str(_ISO_ROOT / "meta.db"))
os.environ.setdefault("PLATFORMS_DIR", str(_ISO_ROOT / "platforms"))
os.environ.setdefault("DATA_DIR", str(_ISO_ROOT / "data"))

from core.tasks.mapping import (  # noqa: E402
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_PRIORITY,
    LEGACY_ANCHORS_MISSING,
    REMINDER_COLUMNS,
    REMINDER_COLUMN_TARGETS,
    convert_repeat_rule,
    map_reminder_row,
    mention_names_from_fields,
    parse_repeat_rule,
)


def _full_row(**overrides) -> dict:
    """一条「18 列全填」的群聊提醒行（列名照 db/models.py Reminder）。"""
    row = {
        "id": 7,
        "persona_id": 1,
        "content": "该开会了",
        "remind_at": datetime(2026, 10, 1, 8, 0, 0),
        "status": "pending",
        "repeat_rule": "每周",
        "source_platform": "wechat",
        "source_account_id": "bot_a",
        "source_chat_id": "room@chatroom",
        "source_chat_type": "group",
        "requester_id": "p_owner",
        "requester_name": "老板",
        "requester_is_owner": 1,
        "mention_name": "小三",
        "mention_names": '["小三", "小四"]',
        "purpose": "remind",
        "attempts": 2,
        "created_at": datetime(2026, 9, 30, 9, 0, 0),
    }
    row.update(overrides)
    return row


def test_reminder_columns_are_18_and_all_mapped() -> None:
    """表结构 18 列，逐列都有去向（不许有「没人管」的列）。"""
    assert len(REMINDER_COLUMNS) == 18
    assert set(REMINDER_COLUMN_TARGETS) == set(REMINDER_COLUMNS)
    assert all(text.strip() for text in REMINDER_COLUMN_TARGETS.values())


def test_full_row_mapping_covers_every_column() -> None:
    """18 列逐项核对：目标列、来源列、无对应物列。"""
    row = _full_row()
    assert set(row) == set(REMINDER_COLUMNS)

    mapped = map_reminder_row(row)
    fields = mapped.as_work_item_fields()
    spec = json.loads(fields["spec_json"])

    # content → goal + spec_json.content
    assert mapped.goal == fields["goal"] == "该开会了" == spec["content"]
    # remind_at → due_at（naive 本地墙钟）
    assert fields["due_at"] == datetime(2026, 10, 1, 8, 0, 0)
    assert fields["due_at"].tzinfo is None
    # status：pending → open
    assert fields["status"] == "open"
    # repeat_rule → repeat_spec_json + spec_json.legacy_repeat_rule
    assert json.loads(fields["repeat_spec_json"]) == {
        "every": 1,
        "unit": "week",
        "anchor": "due",
    }
    assert spec["legacy_repeat_rule"] == "每周"
    # source_* → target_* 与 origin_* 同值复制
    assert fields["target_platform"] == fields["origin_platform"] == "wechat"
    assert fields["target_bot_account_id"] == fields["origin_bot_account_id"] == "bot_a"
    assert fields["target_chat_id"] == fields["origin_chat_id"] == "room@chatroom"
    assert fields["target_chat_type"] == fields["origin_chat_type"] == "group"
    # requester_id → requester_person_id；requester_is_owner → role + 布尔
    assert fields["requester_person_id"] == "p_owner"
    assert fields["requester_role"] == "master"
    assert fields["requester_is_owner"] == 1
    # mention_names → mention_names_json（JSON 数组优先）
    assert json.loads(fields["mention_names_json"]) == ["小三", "小四"]
    assert mapped.mention_names == ("小三", "小四")
    # purpose → kind；trigger / priority / attempts / max_attempts / created_at
    assert fields["kind"] == "notify"
    assert fields["trigger"] == "at"
    assert fields["priority"] == DEFAULT_PRIORITY
    assert fields["attempts"] == 2
    assert fields["max_attempts"] == DEFAULT_MAX_ATTEMPTS == 3
    assert fields["created_at"] == datetime(2026, 9, 30, 9, 0, 0)
    # 无对应物的列 → spec_json.legacy（并标锚点缺失）
    assert spec["legacy"] == {
        "reminder_id": 7,
        "persona_id": 1,
        "requester_name": "老板",
        "anchors_missing": LEGACY_ANCHORS_MISSING is True,
    }
    assert spec["schema_version"] == 1
    assert mapped.warning_codes == ()


def test_convert_repeat_rule_values() -> None:
    """repeat_rule 逐值换算（只认别名与「纯整数 + 单位」的规范形式）。"""
    cases = {
        "daily": (1, "day"),
        "每天": (1, "day"),
        "每日": (1, "day"),
        "weekly": (1, "week"),
        "每周": (1, "week"),
        "每星期": (1, "week"),
        "monthly": (1, "month"),
        "每月": (1, "month"),
        "每个月": (1, "month"),
        "8h": (8, "hour"),
        "2H": (2, "hour"),
        "2d": (2, "day"),
        "3D": (3, "day"),
        "3天": (3, "day"),
        "3 d": (3, "day"),
        " Daily ": (1, "day"),
    }
    for raw, (every, unit) in cases.items():
        spec = convert_repeat_rule(raw)
        assert spec == {"every": every, "unit": unit, "anchor": "due"}, raw
    for empty in ("", None, "   "):
        parsed = parse_repeat_rule(empty)
        assert parsed.spec is None and parsed.recognized is True


def test_unrecognized_repeat_rule_keeps_raw_and_warns() -> None:
    """未识别：一次性 + 原文保留 + 告警，**不跳过不丢、不猜**。"""
    row = _full_row(repeat_rule="每月最后一天")
    mapped = map_reminder_row(row)
    assert mapped.repeat_spec is None
    assert "repeat_rule_unrecognized" in mapped.warning_codes
    assert mapped.spec_json["legacy_repeat_rule"] == "每月最后一天"
    # 一次性：repeat_spec_json 为空串（不是 None / 不是丢字段）
    assert mapped.as_work_item_fields()["repeat_spec_json"] == ""
    # 畸形写法一律未识别：旧实现挑数字拼接，会读出 2.5h→25h、1h1d→11d、-2h→2h。
    malformed = (
        "0h", "abc", "2.5h", "1h1d", "-2h", "2", "12小时", "30分钟", "每周一",
    )
    for raw in malformed:
        parsed = parse_repeat_rule(raw)
        assert parsed.spec is None and parsed.recognized is False, raw
        again = map_reminder_row(dict(row, repeat_rule=raw))
        assert again.spec_json["legacy_repeat_rule"] == raw
        assert again.repeat_spec is None, raw
        assert "repeat_rule_unrecognized" in again.warning_codes
    # 数字拼接的具体产物一个都不许出现：25h / 11d / 2h 都是猜出来的周期。
    assert convert_repeat_rule("2.5h") is None
    assert convert_repeat_rule("1h1d") is None
    assert convert_repeat_rule("-2h") is None


def test_purpose_and_status_mapping() -> None:
    """unknown purpose/status 保守映射 + 告警；已知值一一对应。"""
    assert map_reminder_row(_full_row(purpose="remind")).kind == "notify"
    assert map_reminder_row(_full_row(purpose="act")).kind == "act"
    unknown = map_reminder_row(_full_row(purpose="call", status="running"))
    assert unknown.kind == "notify"
    assert unknown.status == "open"
    assert "purpose_unknown" in unknown.warning_codes
    assert "status_unknown" in unknown.warning_codes

    for raw, expected in (("pending", "open"), ("done", "done"), ("failed", "failed")):
        assert map_reminder_row(_full_row(status=raw)).status == expected


def test_mention_names_fallbacks() -> None:
    """JSON 数组优先；空则单值回退；非 JSON 按分隔符切；私聊恒为空。"""
    row = _full_row()
    assert map_reminder_row(dict(row, mention_names="")).mention_names == ("小三",)
    assert map_reminder_row(
        dict(row, mention_names="", mention_name="")
    ).mention_names == ()
    assert map_reminder_row(
        dict(row, mention_names="张三, 李四、王五")
    ).mention_names == ("张三", "李四", "王五")
    assert map_reminder_row(
        dict(row, mention_names='["独苗"]')
    ).mention_names == ("独苗",)
    private = map_reminder_row(
        dict(row, source_chat_type="private", source_chat_id="wxid_friend")
    )
    assert private.mention_names == ()
    assert json.loads(private.as_work_item_fields()["mention_names_json"]) == []
    assert private.requester_role == "master"
    ordinary = map_reminder_row(
        dict(row, requester_is_owner=0, source_chat_type="private")
    )
    assert ordinary.requester_role == "ordinary"
    assert ordinary.requester_is_owner is False


def test_mention_names_owner_is_shared_with_delivery_path() -> None:
    """⑦名单解析只有一个 owner：五种输入下迁移侧与投递侧结果一致。

    事故面（两份实现给出不同名单）：``"张三,李四"`` 在投递侧（scheduler →
    ``parse_mention_names``）被**整串当成一个人**（到点 @ 不到人），迁移侧
    （``_split_names``）切成两个；``'["张三","李四"]'`` 两侧都认，所以缺陷只在
    非 JSON 行上显形——正是 44 版遗留行的形态。
    """
    from agents.reminder_trigger import parse_mention_names

    from core.tasks.mapping import split_mention_names

    cases = {
        "张三,李四": ("张三", "李四"),
        '["张三","李四"]': ("张三", "李四"),
        "": (),
        None: (),
        "独苗": ("独苗",),
    }
    row = _full_row()
    for raw, expected in cases.items():
        owner = split_mention_names(raw)
        assert owner == expected, (raw, owner)
        # 投递侧（scheduler 调用的公开入口）必须是同一答案。
        assert tuple(parse_mention_names(raw)) == owner, raw
        # 迁移侧（map_reminder_row）也必须是同一答案。
        mapped = map_reminder_row(dict(row, mention_names=raw, mention_name=""))
        assert mapped.mention_names == expected, (raw, mapped.mention_names)

    # 旧行为的两处具体差异：整串当一名 / JSON 字符串残留引号。
    assert parse_mention_names("张三,李四") == ["张三", "李四"]
    assert split_mention_names('"张三"') == ("张三",)
    # 44 版单值列的兜底仍只认一个名字源（空则回退）。
    assert mention_names_from_fields("", "老王") == ("老王",)
    assert mention_names_from_fields('["小三"]', "老王") == ("小三",)
    assert mention_names_from_fields(None, None) == ()


def test_due_at_is_naive_local_wall_clock() -> None:
    """naive 输入原样；带偏移的输入转本地再去掉 tzinfo（不存 UTC）。"""
    naive = map_reminder_row(_full_row(remind_at=datetime(2026, 10, 1, 8, 0)))
    assert naive.due_at == datetime(2026, 10, 1, 8, 0)
    assert naive.due_at.tzinfo is None

    aware_raw = "2026-10-01T08:00:00+00:00"
    # 独立口径：naive UTC 数字 + 本地偏移（不借实现里的那个表达式，避免同义反复）。
    utc_naive = datetime.fromisoformat(aware_raw).replace(tzinfo=None)
    offset = datetime.fromisoformat(aware_raw).astimezone().utcoffset() or timedelta(0)
    expected = utc_naive + offset
    aware = map_reminder_row(_full_row(remind_at=aware_raw))
    assert aware.due_at == expected
    assert aware.due_at.tzinfo is None

    assert map_reminder_row(_full_row(remind_at=None)).due_at is None
    assert "due_at_missing" in map_reminder_row(
        _full_row(remind_at=None)
    ).warning_codes


def test_accepts_row_mapping_and_orm_object() -> None:
    """三种来源都能喂进来：dict / SQLAlchemy Row（无 .get）/ ORM 实例。"""
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite://")
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("select '该开会了' as content, 'pending' as status")
            ).first()
    finally:
        engine.dispose()
    assert not hasattr(row, "get")  # Row 只有 _mapping
    mapped = map_reminder_row(row)
    assert mapped.goal == "该开会了" and mapped.status == "open"

    class _FakeReminder:
        def __init__(self) -> None:
            self.content = "写周报"
            self.status = "done"

    assert map_reminder_row(_FakeReminder()).goal == "写周报"
    assert map_reminder_row(_FakeReminder()).status == "done"
    try:
        map_reminder_row(object())
        raise AssertionError("非 Mapping 必须报 TypeError")
    except TypeError:
        pass


def main() -> bool:
    tests = [fn for name, fn in globals().items() if name.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
