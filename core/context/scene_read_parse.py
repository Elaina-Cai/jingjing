# -*- coding: utf-8 -*-
"""N1 载荷里 ``scene_read`` 段的归一与校验（R2 从 ``core/context/intent.py`` 迁出）。

这一个函数只做三件事：**取段、校验、归一**——不判受话、不决定回不回、不落盘。
它是 N1 读数的「形状守门人」：模型给的字段名/类型/取值范围不对时，就把问题记进
``problems`` 返回，而不是抛异常（降级为「没有读场」，回合照走）。

迁出理由：这 208 行原本夹在 ``intent.py`` 的提示词段落之后、归属段之前，三种职责
（提示词、解析、裁决）连成一片；现在它只依赖 ``core.context.scene_read`` 的契约常量
与 ``llm.schema`` 的校验器，可以脱离回合单独跑。

历史口径不变：``intent.scene_read_of`` 仍是同一个函数对象（那里 re-export）。
"""
from __future__ import annotations

from typing import Any, Mapping

#: 载荷里那一段的键名。
SCENE_READ_KEY = "scene_read"
#: 角色分的四把椅子（顺序即渲染顺序）。
SCENE_READ_ROLES = ("her", "owner", "others", "room")


def scene_read_of(payload: Any) -> dict[str, Any] | None:
    """从 N1 载荷里取可选的 ``scene_read`` 段。

    可选段语义：**缺段 -> None**，不把意图层判成 degraded，现有行为逐字节不变；
    段存在但坏 -> 返回 ``{"ok": False, "problems": (...)}``，只标记该段。
    """
    if not isinstance(payload, Mapping):
        return None
    section = payload.get(SCENE_READ_KEY)
    if section is None:
        intents = payload.get("intents")
        if isinstance(intents, Mapping):
            section = intents.get(SCENE_READ_KEY)
    if section is None:
        return None
    if not isinstance(section, Mapping):
        return {"ok": False, "problems": ("scene_read 必须是对象",)}
    # 局部导入：本模块是纯解析，单独 import 它不该把 llm 拖起来。
    from llm import schema

    problems = list(schema.validate_payload(
        section,
        types={"roles": "dict", "mentioned_her": "bool"},
        source="scene_read",
    ))
    roles = section.get("roles")
    if not isinstance(roles, Mapping):
        problems.append("scene_read.roles 必须是对象")
    else:
        for key, value in roles.items():
            if key not in SCENE_READ_ROLES:
                problems.append(f"scene_read.roles 未知键 {key!r}")
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                problems.append(f"scene_read.roles.{key} 必须是 0..1 的数字")
            elif not 0.0 <= float(value) <= 1.0:
                problems.append(f"scene_read.roles.{key} 越界：{value!r}")
    # 收件人是**集合**（2026-10-13）：一条消息可能对多人说（@多人 / "你俩" /
    # 演给一群人），不许压成一个名字。新旧两种形状都认：
    #   ``targets: ["P2", "P3"]``（规范）；``target: "P2"``（旧单值，兼容）。
    targets: list[str] = []
    raw_targets = section.get("targets")
    if raw_targets is None:
        legacy_target = section.get("target")
        if isinstance(legacy_target, str) and legacy_target.strip():
            raw_targets = [legacy_target]
    if raw_targets is not None:
        if isinstance(raw_targets, str):
            problems.append("scene_read.targets 必须是数组（收件人是集合，单人也写数组）")
        elif not isinstance(raw_targets, (list, tuple)):
            problems.append("scene_read.targets 必须是数组")
        else:
            seen_targets: set[str] = set()
            for raw_name in raw_targets:
                if not isinstance(raw_name, str):
                    problems.append(f"scene_read.targets 每项必须是字符串：{raw_name!r}")
                    continue
                name = " ".join(raw_name.split())
                if name and name not in seen_targets:
                    seen_targets.add(name)
                    targets.append(name)

    from core.context.scene_read import OBSERVATION_SOURCES, SCENE_READ_SUBJECTS, normalize_about  # 契约模块

    about_norm = normalize_about(section)
    raw_about = section.get("about")
    if raw_about is not None and not isinstance(raw_about, (str, Mapping)):
        problems.append("scene_read.about 必须是字符串或对象")
    if isinstance(raw_about, Mapping):
        raw_subject = raw_about.get("subject")
        if raw_subject is not None and str(raw_subject).casefold() not in SCENE_READ_SUBJECTS:
            problems.append(f"scene_read.about.subject 未知值 {raw_subject!r}")
        raw_her = raw_about.get("her")
        if raw_her is not None and not isinstance(raw_her, bool):
            problems.append("scene_read.about.her 必须是布尔或 null")
        raw_conf = raw_about.get("confidence")
        if raw_conf is not None and (isinstance(raw_conf, bool) or not isinstance(raw_conf, (int, float))):
            problems.append("scene_read.about.confidence 必须是 0..1 的数字")
        elif raw_conf is not None and not 0.0 <= float(raw_conf) <= 1.0:
            problems.append(f"scene_read.about.confidence 越界：{raw_conf!r}")
        raw_ev = raw_about.get("evidence")
        if raw_ev is not None and not isinstance(raw_ev, (list, tuple)):
            problems.append("scene_read.about.evidence 必须是数组")
    observations: list[dict[str, Any]] = []
    raw_obs = section.get("observations")
    if raw_obs is not None:
        if not isinstance(raw_obs, (list, tuple)):
            problems.append("scene_read.observations 必须是数组")
        else:
            for item in raw_obs:
                if not isinstance(item, Mapping):
                    problems.append("scene_read.observations 每项必须是对象")
                    continue
                kind = str(item.get("kind") or "")
                if not kind:
                    problems.append("scene_read.observations 缺 kind")
                    continue
                value = item.get("value")
                if value is not None and not isinstance(value, str):
                    problems.append(f"scene_read.observations.{kind}.value 必须是字符串")
                conf = item.get("confidence")
                if conf is not None and (isinstance(conf, bool) or not isinstance(conf, (int, float))):
                    problems.append(f"scene_read.observations.{kind}.confidence 必须是 0..1 的数字")
                src_kind = item.get("source")
                if src_kind is not None and src_kind not in OBSERVATION_SOURCES:
                    problems.append(f"scene_read.observations.{kind}.source 未知值 {src_kind!r}")
                observations.append(item)
    thread_continue = section.get("thread_continue")
    if thread_continue is not None and not isinstance(thread_continue, bool):
        problems.append("scene_read.thread_continue 必须是布尔或 null")
        thread_continue = None
    takeover_allowed = section.get("takeover_allowed")
    if takeover_allowed is not None and not isinstance(takeover_allowed, bool):
        problems.append("scene_read.takeover_allowed 必须是布尔或 null")
        takeover_allowed = None

    needs_ask: dict[str, str] | None = None
    memory_state = "unknown"
    raw_ma = section.get("needs_ask")
    if raw_ma is not None:
        if not isinstance(raw_ma, Mapping):
            problems.append("scene_read.needs_ask 必须是对象")
        else:
            ma_question = str(raw_ma.get("question") or "").strip()
            ma_subject = str(raw_ma.get("subject") or "").strip()
            if ma_question:
                needs_ask = {"question": ma_question, "subject": ma_subject}
            else:
                problems.append("scene_read.needs_ask 缺 question")
    _raw_ms = str(section.get("memory_state") or "").strip().lower()
    if _raw_ms in ("have", "partial", "missing", "unknown"):
        memory_state = _raw_ms
    needs_memory: dict[str, Any] | None = None
    raw_nm = section.get("needs_memory")
    if raw_nm is not None:
        from core.context.scene_read import MEMORY_KINDS

        if not isinstance(raw_nm, Mapping):
            problems.append("scene_read.needs_memory 必须是对象")
        else:
            nm_query = str(raw_nm.get("query") or "").strip()
            nm_kinds: list[str] = []
            raw_kinds = raw_nm.get("kinds")
            if raw_kinds is not None:
                if not isinstance(raw_kinds, (list, tuple)):
                    problems.append("scene_read.needs_memory.kinds 必须是数组")
                else:
                    for raw_k in raw_kinds:
                        k = str(raw_k or "")
                        if k not in MEMORY_KINDS:
                            problems.append(f"scene_read.needs_memory.kinds 未知值 {raw_k!r}")
                        elif k not in nm_kinds:
                            nm_kinds.append(k)
            if nm_query:
                needs_memory = {"query": nm_query, "kinds": nm_kinds}
            else:
                problems.append("scene_read.needs_memory 缺 query")
    about = about_norm["subject"]
    updates: list[dict[str, Any]] = []
    raw_updates = section.get("updates")
    if raw_updates is not None:
        if not isinstance(raw_updates, list):
            problems.append("scene_read.updates 必须是数组")
        else:
            for item in raw_updates:
                if not isinstance(item, Mapping):
                    problems.append("scene_read.updates 每项必须是对象")
                    continue
                op = str(item.get("op") or "")
                if op not in ("observe", "abduce", "retract"):
                    problems.append(f"scene_read.updates 未知 op {op!r}")
                    continue
                conf = item.get("confidence")
                if conf is not None and (isinstance(conf, bool) or not isinstance(conf, (int, float))):
                    problems.append("scene_read.updates.confidence 必须是 0..1 的数字")
                    conf = None
                updates.append({
                    "op": op,
                    "key": str(item.get("key") or ""),
                    "value": str(item.get("value") or ""),
                    "confidence": float(conf) if isinstance(conf, (int, float)) else None,
                    "evidence": str(item.get("evidence") or ""),
                    "reason": str(item.get("reason") or ""),
                    # 谁说的（2026-10-13）：世界模型要能回答"谁说的什么"。
                    "speaker": str(item.get("speaker") or ""),
                })
    if problems:
        return {"ok": False, "problems": tuple(str(item) for item in problems)}
    return {
        "ok": True,
        "roles": {key: float(roles.get(key, 0.0)) for key in SCENE_READ_ROLES},
        "mentioned_her": bool(section.get("mentioned_her", False)),
        "targets": tuple(targets),
        # 旧单值位（兼容）：恰好一个收件人时给那个名字，0/多人都给空串——
        # 多收件人请读 ``targets``，这里绝不假装只有一个。
        "target": targets[0] if len(targets) == 1 else "",
        "genre": str(section.get("genre") or "unknown"),
        "owner_state": str(section.get("owner_state") or "unknown"),
        "thread_topic": str(section.get("thread_topic") or ""),
        "about": about,
        "about_her": about_norm["her"],
        "about_confidence": about_norm["confidence"],
        "about_evidence": about_norm["evidence"],
        "observations": tuple(observations),
        "thread_continue": thread_continue,
        "takeover_allowed": takeover_allowed,
        "needs_memory": needs_memory,
        "memory_state": memory_state,
        "needs_ask": needs_ask,
        "updates": updates,
    }
