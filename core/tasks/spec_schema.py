"""B5：``work_items.spec_json`` 的 kind 级 schema（``docs/archive/任务清单_2026-10-06.md:50``）。

要解决的问题
------------
``spec_json`` 是每条 WorkItem 的结构化参数（``docs/任务模型.md`` §4.1 / §5.2）。P1 只
做到「带版本」（``spec_schema_version`` 列），**没有任何 kind 级校验**：任何写入点都能
往里塞任意键，读侧只能 ``.get()`` 猜。本模块是那份 schema 的**唯一入口**：
:func:`validate_spec` 按 ``kind`` 返回错误清单（空元组 = 合规）。

三条纪律
--------
1. **规则从既有写入点与文档反推，不发明**：允许键 / 必填键的出处逐条写在
   :data:`ALLOWED_FIELDS` / :data:`REQUIRED_FIELDS` 上方；枚举**不在这里复制一份**，
   延迟导入读 owner（``slot_fill.STATES`` / ``slot_fill.MAX_ASK_BATCH`` /
   ``store.WORK_ITEM_STATUSES`` / ``store.RUN_OUTCOMES``）；
2. **未知字段 default-deny**：不在允许集里的键一律报错——嵌套的
   ``slot_fill`` / ``slots[]`` / ``transitions[]`` / ``repeat_spec`` / ``legacy`` 一起算。
   自由留痕字典（``notes`` / ``archive`` / ``receipt`` / ``steps``）只做结构校验，
   因为它们的形状本来就是开放的；
3. **错误信息带 kind / 字段 / 期望 / 实得**：格式 ``<kind>.<字段路径>: 期望 X，实得 Y``
   （中文化口径照 ``core/tasks/slot_fill.py::verify_chain``），测试可以整串断言。

消费点（判据必须有人消费）
--------------------------
``core/tasks/store.py`` 的三个写入点各调一次，不合规**不写**：

* :func:`~core.tasks.store.record_created`（notify：提醒工具双写）与
  :func:`~core.tasks.store.record_slot_fill`（act：N5 参数收集）——
  记 ``work_item_spec_invalid`` 告警后返回 ``None``，与它们「台账写不进去不打断调用方」
  的既有契约一致；
* :func:`~core.tasks.store.record_migrated`（存量迁移）——**抛** :class:`SpecSchemaError`：
  迁移脚本只把**异常**记成 ``failed`` 并给非零退出码
  （``scripts/migrate_reminders_to_work_items.py:215-220``），返回 ``None`` 既不插也不
  失败——那等于把 B2 的「不跳过不丢」反过来做。

三个 kind 的实测形状
--------------------
* ``notify``：``core/tasks/mapping.py:295-302`` 的 5 键（``schema_version`` / ``content`` /
  ``repeat_spec`` / ``legacy_repeat_rule`` / ``legacy``），迁移再加
  ``legacy.source_db``（:func:`~core.tasks.store.record_migrated`）；``legacy`` 四个子键
  见 ``docs/任务模型.md`` §11.4 对照表（``:605-632``）；
* ``act``：``core/tasks/store.py:_slot_fill_fields`` 的 ``spec_json.slot_fill`` +
  ``goal``（``SlotFill.to_dict()`` 的键集就是 ``slot_fill`` 的允许集）；
* ``answer``：**全仓没有写入点**（``core/tasks/store.py:23`` 自述「``answer`` 入表不
  在这里」）——规则按 §4.3（一次 agent turn，done = 回复已送达）与 §5.2（结构化参数
  ``content``）定死，等生产者出现时直接生效，本期不动任何生产路径。

本期不做
--------
* ``spec_json.steps[]`` 的逐字段 schema：``docs/任务模型.md:176`` 定了它的**位置**，
  但全仓还没有产出方 ⇒ 只校验结构（``list[Mapping]``），不发明步骤字段；
* 存量 ``spec_json`` 的回填 / 修正：不合规的行**只报不改**（迁移与写入点自己会拦住
  新的不合规行）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: kind 封闭集合（``docs/任务模型.md:106`` / §4.3；DB 默认 ``'notify'``）。
KINDS: tuple[str, ...] = ("answer", "notify", "act")

#: ``spec_json.schema_version`` 的当前值（版本常量的**唯一声明处**；
#: ``core/tasks/mapping.py`` 与 ``core/tasks/store.py`` 都从这里取）。
SPEC_SCHEMA_VERSION = 1

#: ``repeat_spec.unit`` 的封闭集合：``mapping._REPEAT_ALIASES`` 与
#: ``_REPEAT_QUANTITY_RE`` 实际产出的单位就是这四个（daily/每周/每月/``<N>h``/``<N>d``）。
REPEAT_HOUR = "hour"
REPEAT_DAY = "day"
REPEAT_WEEK = "week"
REPEAT_MONTH = "month"
REPEAT_UNITS: tuple[str, ...] = (REPEAT_HOUR, REPEAT_DAY, REPEAT_WEEK, REPEAT_MONTH)
#: ``repeat_spec.anchor``：顺延基准只有「按上一次到点」（``mapping.REPEAT_ANCHOR_DUE``
#: 的那个字面量；``anchor=completed`` 全仓无产出方，已按 speculative generality 删掉）。
REPEAT_ANCHOR_DUE = "due"

#: 每个 kind 的**允许键**（default-deny：不在里面的键一律报错）。
#:
#: * ``answer`` / ``notify`` 的键来自 ``mapping.map_reminder_row`` 实际写入；
#: * ``act`` 的 ``slot_fill`` / ``goal`` 来自 ``store._slot_fill_fields``，
#:   ``steps`` 来自 ``docs/任务模型.md:176``（计划步骤的位置，暂无产出方）；
#: * ``schema_version`` 三个 kind 都允许（§5.2：每个 JSON 带 schema_version）。
ALLOWED_FIELDS: dict[str, tuple[str, ...]] = {
    "answer": ("schema_version", "content"),
    "notify": (
        "schema_version",
        "content",
        "repeat_spec",
        "legacy_repeat_rule",
        "legacy",
    ),
    "act": ("schema_version", "goal", "slot_fill", "steps"),
}

#: 每个 kind 的**必填键**（缺一个就是不合规：该行没有「要干什么」）。
#:
#: * ``notify``：``content``（§4.1「``spec_json.content`` 给机器读」）+ ``schema_version``
#:   + ``legacy_repeat_rule``（``mapping.py:299`` 的硬不变式「原文**永远**留一份」）；
#: * ``act``：``goal`` + ``slot_fill``（一行 act 没有槽位表就不知道要问什么/干什么）；
#: * ``answer``：``content``（§5.2 结构化参数）。
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "answer": ("schema_version", "content"),
    "notify": ("schema_version", "content", "legacy_repeat_rule"),
    "act": ("goal", "slot_fill"),
}

#: ``repeat_spec`` 的允许键（``mapping._repeat_spec`` 只写这三个）。
_REPEAT_SPEC_FIELDS: tuple[str, ...] = ("every", "unit", "anchor")
#: ``legacy`` 的允许键：四个「无对应物」列（§11.4 对照表）+ 迁移补的来源库标签。
_LEGACY_FIELDS: tuple[str, ...] = (
    "reminder_id",
    "persona_id",
    "requester_name",
    "anchors_missing",
    "source_db",
)
_LEGACY_REQUIRED: tuple[str, ...] = (
    "reminder_id",
    "persona_id",
    "requester_name",
    "anchors_missing",
)
#: ``slot_fill`` 的允许键 = ``slot_fill.SlotFill.to_dict()`` 的键集（状态机是 owner；
#: 它加字段而这里没跟上是**故意**会炸的：加字段必须连 schema 一起改）。
_SLOT_FILL_FIELDS: tuple[str, ...] = (
    "schema_version",
    "fill_id",
    "chat_id",
    "requester_person_id",
    "action_id",
    "spec_kind",
    "state",
    "slots",
    "asked_batch",
    "confirm_message_ref",
    "confirm_token",
    "confirm_ref",
    "confirm_quoted",
    "confirm_word",
    "nudges",
    "last_nudge_at",
    "created_at",
    "expires_at",
    "goal",
    "thread_id",
    "ask_text",
    "pending_slots",
    "transitions",
    "notes",
    "executed",
    "executed_at",
    "execution_ref",
    "work_item_id",
    "archive",
    "receipt",
    "work_item_status",
)
_SLOT_FILL_REQUIRED: tuple[str, ...] = ("fill_id", "state", "slots", "work_item_status")
#: ``slots[]`` 的允许键 = ``slot_fill.Slot.to_dict()``。
_SLOT_FIELDS: tuple[str, ...] = (
    "name",
    "expected_type",
    "expected_example",
    "required",
    "value",
    "valid",
    "invalid_reason",
    "ttl_seconds",
    "closed",
    "aliases",
    "source",
    "evidence",
)
_SLOT_REQUIRED: tuple[str, ...] = ("name", "expected_type")
#: ``transitions[]`` 的允许键 = ``slot_fill.Transition.to_dict()``。
_TRANSITION_FIELDS: tuple[str, ...] = ("from", "to", "reason", "at", "evidence")
_TRANSITION_REQUIRED: tuple[str, ...] = ("from", "to", "reason", "at")

#: 报错前缀：日志里按它捞（``store`` 的 ``work_item_spec_invalid`` 告警）。
SPEC_ERROR_PREFIX = "spec_json"


class SpecSchemaError(ValueError):
    """``spec_json`` 不合规（迁移路径**大声**失败用；见模块 docstring 的消费点）。"""

    def __init__(self, kind: str, errors: Sequence[str]) -> None:
        self.kind = str(kind)
        self.errors: tuple[str, ...] = tuple(errors)
        super().__init__(
            f"spec_json 不合规（kind={self.kind}，{len(self.errors)} 条）："
            + "；".join(self.errors)
        )


# --------------------------------------------------------------------- 唯一入口
def validate_spec(kind: str, spec: Any) -> tuple[str, ...]:
    """``(kind, spec_json)`` → 错误清单（**空元组 = 合规**；本模块唯一入口）。

    ``spec`` 是 ``work_items.spec_json`` 的**解析结果**（dict），不是它的字符串。
    未知 ``kind``、``spec`` 不是 Mapping 都只回一条错误，不做二次推断。
    """
    raw_kind = str(kind or "").strip().lower()
    if raw_kind not in KINDS:
        return (
            f"kind: 期望 {'/'.join(KINDS)} 之一，实得 {_show(kind)}",
        )
    if not isinstance(spec, Mapping):
        return (
            f"{raw_kind}.{SPEC_ERROR_PREFIX}: 期望 Mapping(dict)，实得 {_show(spec)}",
        )
    errors: list[str] = []
    _check_keys(raw_kind, spec, ALLOWED_FIELDS[raw_kind], errors)
    for key in REQUIRED_FIELDS[raw_kind]:
        if key not in spec:
            errors.append(_err(raw_kind, key, "必填", "缺失"))
    if raw_kind == "notify":
        _check_notify(spec, errors)
    elif raw_kind == "act":
        _check_act(spec, errors)
    else:
        _check_answer(spec, errors)
    return tuple(errors)


# ------------------------------------------------------------------- 逐 kind 规则
def _check_answer(spec: Mapping, errors: list[str]) -> None:
    """``answer``：一次 agent turn 要说的内容（§4.3）。"""
    _check_version("answer", spec, errors)
    _check_str("answer", spec, "content", errors, non_empty=True)


def _check_notify(spec: Mapping, errors: list[str]) -> None:
    """``notify``：到点提醒（内容 + 重复 + 迁移原文/遗留列）。"""
    _check_version("notify", spec, errors)
    _check_str("notify", spec, "content", errors, non_empty=True)
    # 允许空串：一次性提醒的原文就是空（``mapping.parse_repeat_rule`` 的空输入）。
    _check_str("notify", spec, "legacy_repeat_rule", errors)
    if "repeat_spec" in spec:
        _check_repeat_spec(spec.get("repeat_spec"), errors)
    if "legacy" in spec:
        _check_legacy(spec.get("legacy"), errors)


def _check_repeat_spec(raw: Any, errors: list[str]) -> None:
    """``repeat_spec``：``None`` = 一次性（合法）；dict 必须正好是 every/unit/anchor。"""
    if raw is None:
        return
    prefix = "notify.repeat_spec"
    if not isinstance(raw, Mapping):
        errors.append(_err("notify", "repeat_spec", "None 或 Mapping(every/unit/anchor)",
                           _show(raw)))
        return
    _check_keys(prefix, raw, _REPEAT_SPEC_FIELDS, errors)
    for key in _REPEAT_SPEC_FIELDS:
        if key not in raw:
            errors.append(_err(prefix, key, "必填", "缺失"))
    if "every" in raw:
        _check_int(prefix, raw, "every", errors, minimum=1)
    if "unit" in raw:
        _check_enum(prefix, raw, "unit", errors, REPEAT_UNITS)
    if "anchor" in raw:
        _check_enum(prefix, raw, "anchor", errors, (REPEAT_ANCHOR_DUE,))


def _check_legacy(raw: Any, errors: list[str]) -> None:
    """``legacy``：``reminders`` 里「无对应物」的列（§11.4 对照表 ``:605-632``）。"""
    prefix = "notify.legacy"
    if not isinstance(raw, Mapping):
        errors.append(_err("notify", "legacy", "Mapping", _show(raw)))
        return
    _check_keys(prefix, raw, _LEGACY_FIELDS, errors)
    for key in _LEGACY_REQUIRED:
        if key not in raw:
            errors.append(_err(prefix, key, "必填", "缺失"))
    for key in ("reminder_id", "persona_id"):
        if key in raw:
            _check_int(prefix, raw, key, errors, minimum=1, nullable=True)
    _check_str(prefix, raw, "requester_name", errors)
    _check_bool(prefix, raw, "anchors_missing", errors)
    if "source_db" in raw:
        _check_str(prefix, raw, "source_db", errors, non_empty=True)


def _check_act(spec: Mapping, errors: list[str]) -> None:
    """``act``：槽位收集（``slot_fill``）+ 目标 + 计划步骤的位置。"""
    _check_version("act", spec, errors)
    # 允许空串：``store._slot_fill_fields`` 的 goal 列会退回 action_id，值本身是 str。
    _check_str("act", spec, "goal", errors)
    if "steps" in spec:
        _check_mapping_list("act.steps", spec.get("steps"), errors)
    if "slot_fill" not in spec:
        return
    raw = spec.get("slot_fill")
    if not isinstance(raw, Mapping):
        errors.append(_err("act", "slot_fill", "Mapping", _show(raw)))
        return
    _check_slot_fill(raw, errors)


def _check_slot_fill(raw: Mapping, errors: list[str]) -> None:
    prefix = "act.slot_fill"
    _check_keys(prefix, raw, _SLOT_FILL_FIELDS, errors)
    for key in _SLOT_FILL_REQUIRED:
        if key not in raw:
            errors.append(_err(prefix, key, "必填", "缺失"))
    if "fill_id" in raw:
        _check_str(prefix, raw, "fill_id", errors, non_empty=True)
    if "schema_version" in raw:
        _check_int(prefix, raw, "schema_version", errors, minimum=1)
    if "spec_kind" in raw:
        _check_enum(prefix, raw, "spec_kind", errors, KINDS)
        if raw.get("spec_kind") != "act":
            errors.append(_err(prefix, "spec_kind", "与行 kind 一致（act）",
                               _show(raw.get("spec_kind"))))
    if "state" in raw:
        _check_enum(prefix, raw, "state", errors, _owner_states())
    if "work_item_status" in raw:
        _check_enum(prefix, raw, "work_item_status", errors, _owner_statuses())
        _check_done_state(raw, errors)
    if "asked_batch" in raw:
        names = _check_str_list(prefix, raw, "asked_batch", errors)
        if names is not None and len(names) > _owner_max_ask_batch():
            errors.append(_err(
                prefix, "asked_batch",
                f"≤{_owner_max_ask_batch()} 项（R-P1 一次最多问几项是硬值）",
                f"{len(names)} 项 {_short(names)}",
            ))
    if "nudges" in raw:
        _check_int(prefix, raw, "nudges", errors, minimum=0)
    for key in ("confirm_quoted", "confirm_word", "executed"):
        _check_bool(prefix, raw, key, errors)
    for key in (
        "chat_id", "requester_person_id", "action_id", "confirm_message_ref",
        "confirm_token", "confirm_ref", "last_nudge_at", "created_at", "expires_at",
        "goal", "thread_id", "ask_text", "executed_at", "execution_ref", "work_item_id",
    ):
        _check_str(prefix, raw, key, errors)
    if "pending_slots" in raw:
        _check_str_list(prefix, raw, "pending_slots", errors)
    if "slots" in raw:
        _check_slots(raw.get("slots"), errors)
    if "transitions" in raw:
        _check_transitions(raw.get("transitions"), errors)
    if "receipt" in raw:
        _check_receipt(raw.get("receipt"), errors)
    # 自由留痕：只结构（``notes`` 是 list，``archive`` / ``receipt`` 是 dict——
    # 形状由 ``slot_fill.SlotFill`` 的字段类型定，本模块不另发明）。
    if "archive" in raw:
        _check_mapping(f"{prefix}.archive", raw.get("archive"), errors)
    if "notes" in raw:
        _check_mapping_list(f"{prefix}.notes", raw.get("notes"), errors)


def _check_done_state(raw: Mapping, errors: list[str]) -> None:
    """**不许硬写完成**：``done`` 只能配 ``DONE``（§4.3 / R-C5「不许假承诺」）。

    ``SlotFill.work_item_status()`` 是这条翻译的 owner；这里只钉住它的值域关系：
    ``state=DONE`` ⟺ ``work_item_status='done'``，``state=ABORTED`` → 只能是
    ``cancelled`` / ``failed``（失败回执与「搁太久了」必须分得开）。
    """
    status = raw.get("work_item_status")
    state = raw.get("state")
    if state is None:
        return
    if status == "done" and state != "DONE":
        errors.append(_err("act.slot_fill", "work_item_status",
                           f"state=DONE 才允许 done（当前 state={state}）", _show(status)))
    if state == "DONE" and status != "done":
        errors.append(_err("act.slot_fill", "work_item_status",
                           "state=DONE → done", _show(status)))
    if state == "ABORTED" and status not in ("cancelled", "failed"):
        errors.append(_err("act.slot_fill", "work_item_status",
                           "state=ABORTED → cancelled/failed", _show(status)))


def _check_slots(raw: Any, errors: list[str]) -> None:
    if not isinstance(raw, (list, tuple)):
        errors.append(_err("act.slot_fill", "slots", "list[Mapping]", _show(raw)))
        return
    names: list[str] = []
    for index, item in enumerate(raw):
        prefix = f"act.slot_fill.slots[{index}]"
        if not isinstance(item, Mapping):
            errors.append(_err(prefix, "", "Mapping", _show(item)))
            continue
        _check_keys(prefix, item, _SLOT_FIELDS, errors)
        for key in _SLOT_REQUIRED:
            if key not in item:
                errors.append(_err(prefix, key, "必填", "缺失"))
        if "name" in item:
            _check_str(prefix, item, "name", errors, non_empty=True)
            if isinstance(item.get("name"), str) and item.get("name"):
                names.append(str(item["name"]))
        _check_str(prefix, item, "expected_type", errors)
        for key in ("expected_example", "invalid_reason", "source", "evidence"):
            _check_str(prefix, item, key, errors)
        for key in ("required", "valid", "closed"):
            _check_bool(prefix, item, key, errors)
        if "ttl_seconds" in item:
            _check_int(prefix, item, "ttl_seconds", errors, minimum=1)
        if "aliases" in item:
            _check_str_list(prefix, item, "aliases", errors)
    duplicated = sorted({name for name in names if names.count(name) > 1})
    if duplicated:
        errors.append(_err(
            "act.slot_fill", "slots",
            "槽位名不重复（slots_for_tools 按名去重）",
            f"重复 {', '.join(duplicated)}",
        ))


def _check_transitions(raw: Any, errors: list[str]) -> None:
    if not isinstance(raw, (list, tuple)):
        errors.append(_err("act.slot_fill", "transitions", "list[Mapping]", _show(raw)))
        return
    states = _owner_states()
    for index, item in enumerate(raw):
        prefix = f"act.slot_fill.transitions[{index}]"
        if not isinstance(item, Mapping):
            errors.append(_err(prefix, "", "Mapping", _show(item)))
            continue
        _check_keys(prefix, item, _TRANSITION_FIELDS, errors)
        for key in _TRANSITION_REQUIRED:
            if key not in item:
                errors.append(_err(prefix, key, "必填", "缺失"))
        for key in ("from", "to"):
            if key in item:
                _check_enum(prefix, item, key, errors, states)
        _check_str(prefix, item, "reason", errors)
        _check_str(prefix, item, "at", errors)
        _check_str(prefix, item, "evidence", errors)


def _check_receipt(raw: Any, errors: list[str]) -> None:
    """回执行（A1-d）：非空时 ``outcome`` 必须落在 ``store.RUN_OUTCOMES`` 里。"""
    if not isinstance(raw, Mapping):
        errors.append(_err("act.slot_fill", "receipt", "Mapping", _show(raw)))
        return
    if not raw:
        return
    if "outcome" in raw:
        _check_enum("act.slot_fill.receipt", raw, "outcome", errors, _owner_outcomes())
    _check_bool("act.slot_fill.receipt", raw, "ok", errors)


# ------------------------------------------------------------------- 小工具
def _check_version(kind: str, spec: Mapping, errors: list[str]) -> None:
    if "schema_version" not in spec:
        return
    value = spec.get("schema_version")
    _check_int(kind, spec, "schema_version", errors, minimum=1)
    if _is_int(value) and int(value) != SPEC_SCHEMA_VERSION:
        errors.append(_err(kind, "schema_version", f"{SPEC_SCHEMA_VERSION}(int)",
                           _show(value)))


def _check_keys(prefix: str, data: Mapping, allowed: Sequence[str],
                errors: list[str]) -> None:
    """未知字段 default-deny（按 key 排序，错误清单可重复断言）。"""
    for key in sorted(set(data) - set(allowed), key=str):
        errors.append(_err(
            prefix, str(key),
            f"允许集 {_allowed_text(allowed)} 之内",
            f"未知字段，值 {_show(data[key])}",
        ))


def _check_str(prefix: str, data: Mapping, key: str, errors: list[str],
               *, non_empty: bool = False) -> None:
    if key not in data:
        return
    expected = "str(非空)" if non_empty else "str"
    value = data.get(key)
    if not isinstance(value, str):
        errors.append(_err(prefix, key, expected, _show(value)))
    elif non_empty and not value.strip():
        errors.append(_err(prefix, key, expected, _show(value)))


def _check_bool(prefix: str, data: Mapping, key: str, errors: list[str]) -> None:
    if key not in data:
        return
    value = data.get(key)
    if not isinstance(value, bool):
        errors.append(_err(prefix, key, "bool", _show(value)))


def _check_int(prefix: str, data: Mapping, key: str, errors: list[str],
               *, minimum: int | None = None, nullable: bool = False) -> None:
    if key not in data:
        return
    value = data.get(key)
    if nullable and value is None:
        return
    if not _is_int(value):
        errors.append(_err(prefix, key, _int_expect(minimum, nullable), _show(value)))
        return
    if minimum is not None and int(value) < minimum:
        errors.append(_err(prefix, key, _int_expect(minimum, nullable), _show(value)))


def _check_enum(prefix: str, data: Mapping, key: str, errors: list[str],
                allowed: Sequence[str]) -> None:
    if key not in data:
        return
    value = data.get(key)
    if not isinstance(value, str) or value not in allowed:
        errors.append(_err(prefix, key, "∈ " + "/".join(allowed), _show(value)))


def _check_str_list(prefix: str, data: Mapping, key: str,
                    errors: list[str]) -> list[str] | None:
    if key not in data:
        return None
    value = data.get(key)
    if not isinstance(value, (list, tuple)):
        errors.append(_err(prefix, key, "list[str]", _show(value)))
        return None
    bad = [item for item in value if not isinstance(item, str)]
    if bad:
        errors.append(_err(prefix, key, "list[str]", _show(bad[0])))
        return None
    return [str(item) for item in value]


def _check_mapping_list(prefix: str, value: Any, errors: list[str]) -> None:
    """``list[Mapping]`` 结构校验（``notes`` / ``steps`` 这类自由留痕）。"""
    if not isinstance(value, (list, tuple)):
        _check_mapping(prefix, value, errors, expect="list[Mapping]")
        return
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            errors.append(_err(f"{prefix}[{index}]", "", "Mapping", _show(item)))


def _check_mapping(prefix: str, value: Any, errors: list[str],
                   *, expect: str = "Mapping") -> None:
    """``Mapping`` 结构校验（``archive`` / ``receipt``）。"""
    if not isinstance(value, Mapping):
        errors.append(_err(prefix, "", expect, _show(value)))


def _err(prefix: str, field: str, expected: str, actual: str) -> str:
    """统一错误口径：``<kind>.<字段路径>: 期望 X，实得 Y``（kind / 字段 / 期望 / 实得齐）。"""
    where = f"{prefix}.{field}" if field else str(prefix)
    return f"{where}: 期望 {expected}，实得 {actual}"


def _int_expect(minimum: int | None, nullable: bool) -> str:
    text = "int" if minimum is None else f"int(≥{minimum})"
    return f"{text} 或 None" if nullable else text


def _allowed_text(allowed: Sequence[str]) -> str:
    if len(allowed) <= 6:
        return "/".join(allowed)
    return "/".join(allowed[:6]) + f"…（共 {len(allowed)} 个）"


def _is_int(value: Any) -> bool:
    """``bool`` 是 ``int`` 的子类：这里要挡住 ``True`` 被当成 1。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _show(value: Any) -> str:
    text = repr(value)
    if len(text) > 80:
        text = text[:77] + "..."
    return f"{text}({type(value).__name__})"


def _short(value: Any) -> str:
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."


# ------------------------------------------------- 枚举 / 上限：读 owner，不复制
def _owner_states() -> tuple[str, ...]:
    """``slot_fill.STATES``（延迟导入：``slot_fill`` → ``store`` → 本模块 是模块级环）。"""
    from core.tasks import slot_fill

    return tuple(slot_fill.STATES)


def _owner_max_ask_batch() -> int:
    """``slot_fill.MAX_ASK_BATCH``（R-P1 的硬上限 3）。"""
    from core.tasks import slot_fill

    return int(slot_fill.MAX_ASK_BATCH)


def _owner_statuses() -> tuple[str, ...]:
    """``store.WORK_ITEM_STATUSES``（§5.5 的台账状态集）。"""
    from core.tasks import store

    return tuple(store.WORK_ITEM_STATUSES)


def _owner_outcomes() -> tuple[str, ...]:
    """``store.RUN_OUTCOMES``（§4.2 的执行结果集）。"""
    from core.tasks import store

    return tuple(store.RUN_OUTCOMES)
