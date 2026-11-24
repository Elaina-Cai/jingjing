"""B2：``reminders`` → ``work_items`` 逐字段映射（纯函数，2026-09-30）。

``docs/任务模型.md`` §11.1 的 B2 缺口是「迁移前先把 18 列旧表映射成什么定死」。
本模块只做纯计算：不碰数据库、不生成 id、不写日志（告警以 ``warnings`` 返回，
由调用方 :func:`log_mapping_warnings` 落日志），因此可以直接单测。

主人拍板（2026-09-30）：

1. ``due_at`` 存 **naive 本地墙钟**（与现状 ``reminders.remind_at``、与事件流一致），
   时区来源记在 ``user_tz``；不做 UTC 换算（带偏移的输入先转本地再去掉 tzinfo）；
2. **未识别的 ``repeat_rule``** → 迁成一次性 + 原文保留到
   ``spec_json.legacy_repeat_rule`` + 告警，**不跳过不丢**；识别口径只有
   「别名」与「纯整数 + 单位」（:data:`_REPEAT_QUANTITY_RE`），不做数字拼接。
3. **@ 名单只有一份解析**：``mention_names`` / 44 版单值 ``mention_name`` 的读法
   由 :func:`split_mention_names` 定死（JSON 数组优先 → 否则按分隔符切 → 空则空），
   迁移读取方与投递读取方（``agents.reminder_trigger.parse_mention_names``）
   共用这一个 owner——两份实现曾给出不同名单。

时区口径不在本模块：``due_at`` / ``created_at`` 统一走
:func:`core.temporal.parse_naive_local`（naive 本地墙钟的唯一换算入口）。

P1 建表后由 ``scripts/migrate_reminders_to_work_items.py``（**尚未写**，见
``docs/任务模型.md`` §13）调用 :func:`map_reminder_row` 与
:meth:`MappedReminder.as_work_item_fields`。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog

from core import temporal
from core.tasks.spec_schema import (
    REPEAT_ANCHOR_DUE,
    REPEAT_DAY,
    REPEAT_HOUR,
    REPEAT_MONTH,
    REPEAT_WEEK,
    SPEC_SCHEMA_VERSION,
)
from safelog import warn

log = structlog.get_logger()

#: ``spec_json.schema_version`` 的值（B5：字面量的**唯一声明处**在
#: :mod:`core.tasks.spec_schema`；本模块只再导出，``store`` 等既有读点不受影响）。

#: ``reminders`` 没有的台账头锚点（external_msg_id / session_id / command_id …）：
#: 迁移时没有来源，如实标记，别让下游以为「有锚点只是没填」。
LEGACY_ANCHORS_MISSING = True
#: §4.3：notify / act 失败重试 ≤3 次止损（与 ``reminders`` 投递端一致）。
DEFAULT_MAX_ATTEMPTS = 3
#: §4.1 仲裁优先级：交互 0 > 到点 1 > 主动/后台 2；提醒全是「到点」。
DEFAULT_PRIORITY = 1
#: reminders 都是定时触发。
DEFAULT_TRIGGER = "at"

#: 18 列（``db/models.py`` Reminder 与 ``db/meta_models.py`` Reminder 同列）。
REMINDER_COLUMNS: tuple[str, ...] = (
    "id",
    "persona_id",
    "content",
    "remind_at",
    "status",
    "repeat_rule",
    "source_platform",
    "source_account_id",
    "source_chat_id",
    "source_chat_type",
    "requester_id",
    "requester_name",
    "requester_is_owner",
    "mention_name",
    "mention_names",
    "purpose",
    "attempts",
    "created_at",
)

#: 每一列的去向（与 ``docs/任务模型.md``「B2 逐字段对照表」同一份口径）。
REMINDER_COLUMN_TARGETS: dict[str, str] = {
    "id": "spec_json.legacy.reminder_id（单库自增，不能当 work_item_id）",
    "persona_id": "spec_json.legacy.persona_id（WorkItem 无 persona 维度）",
    "content": "goal + spec_json.content",
    "remind_at": "due_at（naive 本地墙钟，来源时区记 user_tz）",
    "status": "status（pending→open / done→done / failed→failed；其它→open+告警）",
    "repeat_rule": "repeat_spec_json + spec_json.legacy_repeat_rule（原文永不丢）",
    "source_platform": "target_platform + origin_platform",
    "source_account_id": "target_bot_account_id + origin_bot_account_id",
    "source_chat_id": "target_chat_id + origin_chat_id",
    "source_chat_type": "target_chat_type + origin_chat_type（并决定 @ 与 role）",
    "requester_id": "requester_person_id",
    "requester_name": "spec_json.legacy.requester_name（对齐 command_records 的是 id/role）",
    "requester_is_owner": "requester_is_owner + requester_role",
    "mention_name": "mention_names_json（mention_names 为空时的单值回退）",
    "mention_names": "mention_names_json（JSON 数组优先）",
    "purpose": "kind（remind→notify / act→act；其它→notify+告警）",
    "attempts": "attempts",
    "created_at": "created_at",
}

PURPOSE_KIND: dict[str, str] = {"remind": "notify", "act": "act"}
STATUS_MAP: dict[str, str] = {"pending": "open", "done": "done", "failed": "failed"}

#: repeat_rule 取值来源：``agents/tools/reminder.py`` 的提示词（daily/每天、weekly/每周、
#: monthly/每月、``<N>h``、``<N>d``）。**刻意不照抄** ``db/repo_reminder.py`` 的宽松解析
#: （它把数字一个个挑出来拼：``2.5h``→25h、``1h1d``→11d、``-2h``→2h），迁移只认
#: 「纯整数 + 单位」的规范形式，认不出来就交回一次性 + 原文 + 告警（不猜）。
_REPEAT_ALIASES: dict[str, tuple[int, str]] = {
    "daily": (1, REPEAT_DAY),
    "每天": (1, REPEAT_DAY),
    "每日": (1, REPEAT_DAY),
    "weekly": (1, REPEAT_WEEK),
    "每周": (1, REPEAT_WEEK),
    "每星期": (1, REPEAT_WEEK),
    "monthly": (1, REPEAT_MONTH),
    "每月": (1, REPEAT_MONTH),
    "每个月": (1, REPEAT_MONTH),
}
#: 规范数量形式：纯整数（无小数/正负号/前导零）+ 单位（``h``/``d``/``天``），
#: 允许中间空格与大小写（``2h`` / ``2H`` / ``3天`` / ``3D`` / ``3 d``）。``小时`` /
#: ``分钟`` / ``每周一`` / ``2.5h`` / ``1h1d`` / ``-2h`` / 纯数字一律落到未识别路径。
_REPEAT_QUANTITY_RE = re.compile(r"^([1-9][0-9]*)\s*(h|d|天)$", re.IGNORECASE)
#: 顺延基准：``due`` = 按上一次的到点时间顺延（等价旧 ``_calc_next_repeat``）。
#: 字面量的**唯一声明处**在 :mod:`core.tasks.spec_schema`（B5 的 schema 按它校验
#: ``repeat_spec.anchor``），这里只再导出。「完成后再算下一次」（``anchor=completed``）
#: 曾在 2026-09-30 预留成一个常量 ``REPEAT_ANCHOR_COMPLETED``，但全仓无读取方、也没有
#: 任何路径会产出它——speculative generality，按「无触发条件即删除」处理（要用时再加，
#: 加的时候连同产出方一起加）。


@dataclass(frozen=True)
class RepeatParse:
    """``repeat_rule`` 的解析结果（原文一并带回，便于保留与告警）。"""

    spec: dict | None
    recognized: bool
    raw: str


@dataclass(frozen=True)
class MappingWarning:
    """映射告警（不落日志，由调用方决定怎么写）：``code`` 稳定可断言。"""

    code: str
    detail: str = ""


@dataclass(frozen=True)
class MappedReminder:
    """一条 ``reminders`` 行映射出来的 WorkItem 字段。"""

    goal: str
    kind: str
    trigger: str
    status: str
    due_at: datetime | None
    priority: int
    target_platform: str
    target_bot_account_id: str
    target_chat_id: str
    target_chat_type: str
    origin_platform: str
    origin_bot_account_id: str
    origin_chat_id: str
    origin_chat_type: str
    requester_person_id: str
    requester_role: str
    requester_is_owner: bool
    mention_names: tuple[str, ...]
    attempts: int
    max_attempts: int
    created_at: datetime | None
    repeat_spec: dict | None
    spec_json: dict
    warnings: tuple[MappingWarning, ...] = ()

    @property
    def warning_codes(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.warnings)

    def as_work_item_fields(self) -> dict[str, Any]:
        """摊平成 ``work_items`` 行字段（JSON 列按项目习惯存 Text）。

        不返回 ``work_item_id`` / ``idempotency_key``：工作项 id 由写入方生成
        （UUID），幂等键默认取它的值（§4.1）。
        """
        return {
            "kind": self.kind,
            "trigger": self.trigger,
            "status": self.status,
            "goal": self.goal,
            "due_at": self.due_at,
            "priority": self.priority,
            "repeat_spec_json": (
                _dump(self.repeat_spec) if self.repeat_spec else ""
            ),
            "target_platform": self.target_platform,
            "target_bot_account_id": self.target_bot_account_id,
            "target_chat_id": self.target_chat_id,
            "target_chat_type": self.target_chat_type,
            "origin_platform": self.origin_platform,
            "origin_bot_account_id": self.origin_bot_account_id,
            "origin_chat_id": self.origin_chat_id,
            "origin_chat_type": self.origin_chat_type,
            "requester_person_id": self.requester_person_id,
            "requester_role": self.requester_role,
            "requester_is_owner": 1 if self.requester_is_owner else 0,
            "mention_names_json": _dump(list(self.mention_names)),
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "created_at": self.created_at,
            "spec_json": _dump(self.spec_json),
        }


def parse_repeat_rule(raw: Any) -> RepeatParse:
    """解析 ``repeat_rule``：认出来给 spec，认不出来给 ``recognized=False``。

    空 / None 是合法的「一次性」（``recognized=True``、``spec=None``）；
    非空但认不出来只有 ``recognized=False`` 能区分，调用方据此**保留原文 + 告警**。

    只认两类：别名（daily/每周/每月…，整串精确匹配，大小写不敏感）与
    :data:`_REPEAT_QUANTITY_RE` 的「纯整数 + 单位」。**不做数字拼接**：
    ``2.5h`` / ``1h1d`` / ``-2h`` / ``12小时`` / ``30分钟`` / ``2`` 全部未识别——
    宁可退回一次性并留下原文，也不猜出一个错误的重复周期。
    """
    text = str(raw or "").strip()
    if not text:
        return RepeatParse(spec=None, recognized=True, raw="")
    alias = _REPEAT_ALIASES.get(text.lower())
    if alias is not None:
        return RepeatParse(
            spec=_repeat_spec(alias[0], alias[1]), recognized=True, raw=text
        )
    match = _REPEAT_QUANTITY_RE.match(text)
    if match is None:
        return RepeatParse(spec=None, recognized=False, raw=text)
    unit = REPEAT_HOUR if match.group(2).lower() == "h" else REPEAT_DAY
    return RepeatParse(
        spec=_repeat_spec(int(match.group(1)), unit), recognized=True, raw=text
    )


def convert_repeat_rule(raw: str | None) -> dict | None:
    """``repeat_rule`` 文本 → ``repeat_spec``（纯函数）。

    空 → None（一次性）；未识别 → None（**原文由** :func:`map_reminder_row`
    保留到 ``spec_json.legacy_repeat_rule`` 并告警**，不静默丢弃**）。
    """
    return parse_repeat_rule(raw).spec


def map_reminder_row(row: Mapping) -> MappedReminder:
    """``reminders`` 行 → WorkItem 字段（见 ``REMINDER_COLUMN_TARGETS``）。

    ``row`` 可以是 dict、SQLAlchemy ``Row``（走 ``_mapping``）或 ORM 实例（走属性）；
    映射只读不改，无 IO。
    """
    row = _as_mapping(row)
    warnings: list[MappingWarning] = []

    content = str(row.get("content") or "")
    purpose = str(row.get("purpose") or "").strip().lower() or "remind"
    kind = PURPOSE_KIND.get(purpose)
    if kind is None:
        warnings.append(MappingWarning("purpose_unknown", purpose))
        kind = "notify"

    raw_status = str(row.get("status") or "").strip().lower()
    status = STATUS_MAP.get(raw_status)
    if status is None:
        warnings.append(MappingWarning("status_unknown", raw_status))
        status = "open"

    due_at = temporal.parse_naive_local(row.get("remind_at"))
    if due_at is None:
        warnings.append(MappingWarning("due_at_missing", ""))

    platform = str(row.get("source_platform") or "")
    account_id = str(row.get("source_account_id") or "")
    chat_id = str(row.get("source_chat_id") or "")
    chat_type = str(row.get("source_chat_type") or "").strip().lower()

    requester_person_id = str(row.get("requester_id") or "").strip()
    is_owner = _truthy(row.get("requester_is_owner"))
    requester_role = (
        "master" if is_owner else ("group_member" if chat_type == "group" else "ordinary")
    )

    parsed = parse_repeat_rule(row.get("repeat_rule"))
    if not parsed.recognized:
        warnings.append(MappingWarning("repeat_rule_unrecognized", parsed.raw))

    legacy = {
        "reminder_id": _int_or_none(row.get("id")),
        "persona_id": _int_or_none(row.get("persona_id")),
        "requester_name": str(row.get("requester_name") or ""),
        "anchors_missing": LEGACY_ANCHORS_MISSING,
    }
    spec_json = {
        "schema_version": SPEC_SCHEMA_VERSION,
        "content": content,
        "repeat_spec": parsed.spec,
        # 原文**永远**留一份：未识别时它是唯一线索，识别时它是回放依据。
        "legacy_repeat_rule": parsed.raw,
        "legacy": legacy,
    }

    return MappedReminder(
        goal=content.strip(),
        kind=kind,
        trigger=DEFAULT_TRIGGER,
        status=status,
        due_at=due_at,
        priority=DEFAULT_PRIORITY,
        target_platform=platform,
        target_bot_account_id=account_id,
        target_chat_id=chat_id,
        target_chat_type=chat_type,
        origin_platform=platform,
        origin_bot_account_id=account_id,
        origin_chat_id=chat_id,
        origin_chat_type=chat_type,
        requester_person_id=requester_person_id,
        requester_role=requester_role,
        requester_is_owner=is_owner,
        mention_names=_mention_names(row, chat_type),
        attempts=_int_or_none(row.get("attempts")) or 0,
        max_attempts=DEFAULT_MAX_ATTEMPTS,
        created_at=temporal.parse_naive_local(row.get("created_at")),
        repeat_spec=parsed.spec,
        spec_json=spec_json,
        warnings=tuple(warnings),
    )


def split_mention_names(raw: Any) -> tuple[str, ...]:
    """解析「@ 名单」字段 → 名字元组（**全仓唯一 owner**）。

    上游产出方 ``agents/tools/reminder.py::_normalize_mention``（写
    ``reminders.mention_names``）、迁移读取方 :func:`map_reminder_row`、投递读取方
    ``agents.reminder_trigger.parse_mention_names`` 必须给出**同一个答案**；以前
    「非 JSON 就整串当一名」与「按分隔符切」两份实现并存，同一行数据在迁移与投递
    两条路上会得到不同名单（评测里已经能看出差异），所以在这里定死语义：

    1. ``None`` / ``""`` / 纯空白 → ``()``（**空列表**，不是 ``[""]``）；
    2. ``list`` / ``tuple`` → 逐项 ``str().strip()``，丢空项（项内**不再切分**：
       列表里的一项就是一个名字）；
    3. ``str`` → 先 ``json.loads``：
       * JSON 数组 → 按 2 处理（``'["张三","李四"]'`` → 两名）；
       * JSON 字符串 → 一名（``'"张三"'`` → ``("张三",)``，不残留引号）；
       * 其它 JSON 标量（数字/布尔/对象）→ 落到 4；
    4. 其余字符串 → 按 ``,，、;；`` 与空白切分（`"张三,李四"` → 两名；这是 44 版
       单值列/模型手写输入的常见形态，与产出方 `_normalize_mention` 同一套分隔符）。

    刻意**不去重**：重复是上游写入的问题，这里静默去重只会把上游 bug 藏起来
    （消费方要「只 @ 一次」就自己 ``dict.fromkeys``）。
    """
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple)):
        return tuple(str(item).strip() for item in raw if str(item).strip())
    text = str(raw).strip()
    if not text:
        return ()
    try:
        parsed = json.loads(text)
    except ValueError:
        warn("core/tasks/mapping.py:374 ValueError", err=repr(__import__("sys").exc_info()[1]))
        parsed = None
    if isinstance(parsed, list):
        return tuple(str(item).strip() for item in parsed if str(item).strip())
    if isinstance(parsed, str):
        return (parsed.strip(),) if parsed.strip() else ()
    return tuple(part for part in re.split(r"[,，、;；\s]+", text) if part)


def mention_names_from_fields(primary: Any, single: Any = None) -> tuple[str, ...]:
    """``mention_names`` 优先；空则回退 44 版的单值 ``mention_name``。

    两列只是「同一份名单的新旧表示」，回退逻辑也只有这一处（scheduler 的
    ``parse_mention_names(a) or parse_mention_names(b)`` 是同一口径的另一种写法）。
    """
    names = split_mention_names(primary)
    if names:
        return names
    return split_mention_names(single)


def log_mapping_warnings(mapped: MappedReminder) -> None:
    """把映射告警落日志（本模块唯一的 IO 出口，与纯映射分开）。"""
    for item in mapped.warnings:
        log.warning("reminder_mapping_warning", code=item.code, detail=item.detail)


def _as_mapping(row: Any) -> Mapping:
    """把 dict / SQLAlchemy ``Row`` / ORM 实例统一成只读 Mapping。

    ``Row`` 没有 ``.get()``（只有 ``_mapping``），ORM 实例连 ``keys()`` 都没有，
    所以在这里归一，调用方（P1 迁移脚本）不用每个来源写一份适配。
    """
    if isinstance(row, Mapping):
        return row
    mapping = getattr(row, "_mapping", None)
    if isinstance(mapping, Mapping):
        return mapping
    data = getattr(row, "__dict__", None)
    if isinstance(data, dict):
        return {key: value for key, value in data.items() if not key.startswith("_")}
    raise TypeError(f"map_reminder_row 需要 Mapping，收到 {type(row).__name__}")


def _repeat_spec(every: int, unit: str) -> dict:
    return {"every": int(every), "unit": unit, "anchor": REPEAT_ANCHOR_DUE}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        warn("core/tasks/mapping.py:431 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "y", "是")


def _mention_names(row: Mapping, chat_type: str) -> tuple[str, ...]:
    """群聊 @ 名单；私聊恒为 ``()``（对齐 ``CreateReminderTool``：私聊忽略 mention）。

    解析与回退都在 owner :func:`mention_names_from_fields` 里。
    """
    if chat_type != "group":
        return ()
    return mention_names_from_fields(row.get("mention_names"), row.get("mention_name"))
