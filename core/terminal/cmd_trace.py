# -*- coding: utf-8 -*-
"""A1-d 命令链接线：把 ``record_execution`` 接到**真实执行落点**。

`docs/archive/任务清单_2026-10-06.md:43` 原文：「把 ``record_execution`` 接到真实执行落点，让
『谁下达 → 执行到哪』在真实流量上可查」。接线前 ``record_execution`` 只有定义
（``core/terminal/command_service.py``）与一个用例（``tests/test_event_chain_links.py``），
**零生产调用方**，于是三条判据在真实流量上一条都不成立。

本模块是那次接线的**唯一**接线点（读点在 ``cmd_trace_mode``，落点在
``record_tool_execution``），被 ``agents/tools/base.py::BaseAgent.act`` 调用——那里是
工具**真正被执行**的那一处（``await tool.execute(...)``），成功与失败两条路径都经过它。

三条判据与它们的 owner（**一律复用，不新建第二套**）
---------------------------------------------------
① ``authorization_records`` 有行
      owner = ``core/terminal/command_service.py::CommandService.record_authorization``
② ``command_records.execution_ids`` 非空
      owner = 同文件 ``CommandService.record_execution``
③ ``terminal_events.authorization_id`` 非空
      owner = ``core/terminal/event_store_db.py::TerminalEventStore.link_event``
      （append-only 的**受控例外**，经 ``core/terminal/shadow.link_event_best_effort`` 兜底）

**命令 id 从哪来**：不新造。入站命令消息在 ``core/terminal/shadow.record_message_events``
里建命令时，已经把 ``command_id`` 回填到**那条消息的事件行**上（A1-a）；这一回合的
工具执行带着 ``extra["parent_event_id"]``（= 触发消息的事件 id, A1-c），所以
``event_store.get(parent_event_id).command_id`` 就是权威命令 id——
一次回查，零新概念。

**授权 id 从哪来**：由 ``command_id`` 派生（``uuid5``）。授权就是**主人自己那条命令**
（``command_records.issuer_role == "master"`` 且执行发生在命令的原始会话里）——
这两条是库里已有的**事实**，不是新判据。同一个命令重复执行拿到**同一个**
``authorization_id``，所以"重复记录"永远不会建第二行授权。

**幂等**（三层，全部落在既有 owner 上）
---------------------------------------
1. 授权行：``upsert_authorization`` 按 ``authorization_id`` 主键查改；id 是
   ``uuid5(command_id)`` ⇒ 同一次执行重复调用写的是同一行；
2. ``execution_ids``：``record_execution`` 自己 ``dict.fromkeys`` 去重追加；
3. 执行事件行：``event_id`` 也是 ``uuid5(command_id, action, 触发消息)`` ⇒
   ``TerminalEventStore.append`` 见到已存在的 ``event_id`` 直接返回**已存那一行**
   （不 INSERT、不换号），因此不会产生第二行。

**开关**：``YINYUE_FEATURE_CMD_TRACE``（三态，默认 ``off``）。三档由
:func:`record_tool_execution` 读一次 ``cmd_trace_mode()`` 后分流：
``off`` 立刻返回——不判定、不建授权、不写事件、不调 ``record_execution``
（与接线前逐字节一致，回滚位）；``shadow`` 只写 structlog 读数，一个字节都不落库；
``on`` 才落库。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import structlog

from core import features
from safelog import warn

log = structlog.get_logger()

#: 授权 id 的派生命名空间（``uuid5(command_id)``）。固定值：换掉它 = 换一套 id，
#: 那就等于第二套 id 体系，本模块刻意只此一处。
_AUTH_NAMESPACE = uuid.UUID("6f1d4b9a-2c7e-5a30-9b41-0d8e2f4a6c11")
#: 执行事件 id 的派生命名空间（``uuid5(command_id, action, parent)``）。
_EVENT_NAMESPACE = uuid.UUID("1b7c0e52-9d34-5f68-a207-3c5e91b4d8f2")
#: 授权记录里 ``final_confirmation`` 的来源标记（谁的哪句话 = 这次授权）。
_CONFIRM_SOURCE = "owner_command_message"


def cmd_trace_mode() -> str:
    """**唯一**读点：``YINYUE_FEATURE_CMD_TRACE`` 的三态（``off``/``shadow``/``on``）。

    ``off`` 是默认（回滚位）。别在别处再写一份 ``features.enabled(...)``：那会把
    ``shadow`` 读成 ``False``，影子门会悄悄变成"不做事"。
    """
    return features.mode(features.CMD_TRACE, features.MODE_OFF)


def cmd_trace_enabled() -> bool:
    """本开关是否**落地**（``on``）。``shadow`` 返回 ``False``，只留痕。

    只给"这一档要不要真写库"的读数用（用例与诊断读它）；落点
    :func:`record_tool_execution` 自己按 ``cmd_trace_mode()`` 三档分流，
    **不是**先过这个布尔再进影子档——那样影子档会被当成 ``off`` 吃掉。
    """
    return cmd_trace_mode() == features.MODE_ON


@dataclass(frozen=True)
class TraceResult:
    """一次接线尝试的读数（供用例与日志读，不参与任何业务判定）。"""

    mode: str = features.MODE_OFF
    reason: str = "off"
    command_id: str = ""
    authorization_id: str = ""
    execution_event_id: str = ""
    execution_ids: tuple[str, ...] = ()
    wrote_authorization: bool = False
    wrote_execution: bool = False
    linked_event_rows: int = 0

    @property
    def traced(self) -> bool:
        return bool(self.command_id)


def _det_id(namespace: uuid.UUID, *parts: str) -> str:
    """确定性 id：同一组输入永远得到同一个 id（幂等的地基）。"""
    return uuid.uuid5(namespace, "\x1f".join(str(p or "") for p in parts)).hex


def authorization_id_for(command_id: str) -> str:
    """``command_id`` → 授权 id（**派生**，不新建命名空间之外的 id）。"""
    return _det_id(_AUTH_NAMESPACE, "yinyue:cmd-auth", command_id)


def execution_event_id_for(command_id: str, action: str, parent_event_id: str = "") -> str:
    """``(command_id, action, 触发消息)`` → 执行事件 id（确定性，重复调用同值）。"""
    return _det_id(_EVENT_NAMESPACE, "yinyue:cmd-exec", command_id, action, parent_event_id)


# --------------------------------------------------------------------- 解析
@dataclass(frozen=True)
class _Scope:
    """这一回合里能拿到的链接事实（一律来自既有列，不猜）。"""

    platform: str = ""
    bot_account_id: str = ""
    chat_id: str = ""
    chat_type: str = ""
    parent_event_id: str = ""
    turn_id: str = ""
    command_id: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.platform and (self.command_id or self.parent_event_id))


def _scope_from_context(context: Any) -> _Scope:
    """从 agent 上下文的 ``extra`` 里取出链接事实（缺失留空，不猜）。"""
    extra = getattr(context, "extra", None) or {}
    if not isinstance(extra, dict):
        extra = {}

    def pick(*keys: str) -> str:
        for key in keys:
            value = extra.get(key)
            if value not in (None, ""):
                return str(value)
        return ""

    account_ctx = getattr(context, "account_context", None)
    platform = pick("platform") or str(getattr(account_ctx, "platform", "") or "")
    account_id = (
        pick("account_id", "reply_account")
        or str(getattr(account_ctx, "account_id", "") or "")
    )
    return _Scope(
        platform=platform,
        bot_account_id=account_id,
        chat_id=pick("chat_id"),
        chat_type="group" if extra.get("is_group") else "private",
        parent_event_id=pick("parent_event_id"),
        turn_id=pick("turn_id"),
        command_id=pick("command_id"),
    )


def _event_store(service: Any | None) -> tuple[Any | None, str]:
    """事件流 store。优先复用命令投影的那一个（同一个 engine，不触发 bootstrap）。

    行为与 ``CommandService._event_store`` 同源：``core.bootstrap.init_singletons()``
    会把整个应用（含迁移）拉起来，接线点绝不能走那条路。
    """
    if service is not None:
        store = service._event_store()  # noqa: SLF001 - 同一个 owner 的读侧，不另建
        if store is not None:
            return store, "command_service"
    try:
        from core.terminal.event_runtime import get_terminal_event_store

        store = get_terminal_event_store()
        if store is not None:
            return store, "event_runtime"
    except Exception as exc:  # noqa: BLE001 - 接线是 best-effort
        warn("core/terminal/cmd_trace.py:188 Exception", err=repr(exc))
        return None, f"error:{str(exc)[:80]}"
    return None, "missing"


def _resolve_command_id(store: Any | None, scope: _Scope) -> str:
    """命令 id：优先 ``extra``（若有），否则回查触发消息事件行的 ``command_id`` 列。

    回查用的是 A1-a 已经建立的那条链（消息事件行 ← 命令投影），**不新造 id**。
    """
    if scope.command_id:
        return scope.command_id
    if not scope.parent_event_id:
        return ""
    get = getattr(store, "get", None)
    if get is None:
        return ""
    event = get(scope.parent_event_id)
    if event is None:
        return ""
    return str(getattr(event, "command_id", "") or "")


def _append_trace_event(
    store: Any | None,
    *,
    event_id: str,
    scope: _Scope,
    action: str,
    ok: bool,
    error: str,
    duration_ms: int,
    command_id: str,
    authorization_id: str,
) -> bool:
    """自有 store 时补写执行事件行（``event_id`` 确定 ⇒ 重复调用不插第二行）。"""
    append = getattr(store, "append", None)
    if append is None:
        return False
    from core.terminal.event_model import Event, EventType, Sensitivity
    from core.terminal.principal import VisibilityScope

    append(
        Event.new(
            event_type=EventType.ACTION,
            platform=scope.platform,
            bot_account_id=scope.bot_account_id,
            chat_id=scope.chat_id,
            chat_type=scope.chat_type,
            actor_person_id=scope.bot_account_id,
            actor_account_id=scope.bot_account_id,
            content=str(action or ""),
            action_payload={
                "action": str(action or ""),
                "ok": bool(ok),
                "error": str(error or "")[:500],
                "duration_ms": int(duration_ms or 0),
                "cmd_trace": True,
            },
            visibility_scope=frozenset({VisibilityScope.OWNER_ONLY}),
            parent_event_id=scope.parent_event_id,
            turn_id=scope.turn_id,
            sensitivity=Sensitivity.NORMAL,
            event_id=event_id,
            command_id=command_id,
            authorization_id=authorization_id,
            metadata={
                "group_id": scope.chat_id if scope.chat_type == "group" else "",
                "cmd_trace": True,
            },
        )
    )
    return True


def _read_back_event(
    store: Any | None,
    *,
    scope: _Scope,
    action: str,
    command_id: str = "",
) -> Any | None:
    """这一回合里**已经**落库的那条 action 事件行（没有就返回 None）。

    ``record_action_event`` 写的 action 行本来就是"这一步被真的执行了"的事实行；
    命中时直接拿它当 ``execution_id``，不再补一条同义行（同一件事不说两遍）。
    命中判据 = 同一回合（``parent_event_id`` 或 ``turn_id`` 相同）+ 同一个动作名
    ——两个都取自库里现成的列，不猜。

    查两次：先按 ``command_id`` 窄查（第一次登记之后，事件行已经带上这个列，
    这是常态）；没命中再按会话回看一次（第一次登记时那条 action 行还没被链接）。
    """
    if store is None or not (scope.parent_event_id or scope.turn_id):
        return None
    query = getattr(store, "query", None)
    if query is None:
        return None
    batches = []
    if command_id:
        batches.append({"platform": scope.platform, "command_id": command_id})
    batches.append(
        {
            "platform": scope.platform,
            "bot_account_id": scope.bot_account_id,
            "chat_id": scope.chat_id,
        }
    )
    for kwargs in batches:
        try:
            rows = list(query(**kwargs))
        except Exception:  # noqa: BLE001 - 查不到就当没有，走补写
            warn("core/terminal/cmd_trace.py:298 Exception", err=repr(__import__("sys").exc_info()[1]))
            continue
        for row in rows:
            if str(getattr(row, "event_type", "") or "") not in ("action", "tool_call"):
                continue
            if str(getattr(row, "content", "") or "") != str(action or ""):
                continue
            parent = str(getattr(row, "parent_event_id", "") or "")
            turn = str(getattr(row, "turn_id", "") or "")
            if scope.parent_event_id and parent == scope.parent_event_id:
                return row
            if not parent and scope.turn_id and turn == scope.turn_id:
                return row
    return None


# --------------------------------------------------------------------- 落点
def record_tool_execution(
    *,
    context: Any,
    action: str,
    ok: bool,
    error: str = "",
    duration_ms: int = 0,
) -> TraceResult:
    """**唯一落点**：把这次工具执行接到命令链上（成功 / 失败都走这里）。

    失败路径同样留痕（``ok=False`` + ``error`` 进事件行、命令状态推进
    ``CommandStatus.FAILED``）：否则「执行到哪」在失败这一步仍然查不到。

    全程 best-effort：任何异常都只记一行日志，**绝不**影响工具返回值与回复
    （与 ``core/terminal/shadow`` 的纪律一致）。返回值只给用例读，不参与判定。
    """
    mode = cmd_trace_mode()
    if mode == features.MODE_OFF:
        # off = 回滚位：不判定、不建授权、不写事件、不调 record_execution，
        # 与接线前**逐字节一致**。这一行是第一道门，连 store 都不碰。
        return TraceResult(mode=mode, reason="off")
    try:
        return _trace(context=context, action=action, ok=ok, error=error,
                      duration_ms=duration_ms, mode=mode)
    except Exception as exc:  # noqa: BLE001 - 接线绝不阻断执行
        log.warning("cmd_trace_failed", action=str(action)[:64], error=str(exc)[:200])
        return TraceResult(mode=mode, reason=f"error:{str(exc)[:120]}")


def _trace(
    *,
    context: Any,
    action: str,
    ok: bool,
    error: str,
    duration_ms: int,
    mode: str,
) -> TraceResult:
    scope = _scope_from_context(context)
    if not scope.usable:
        return TraceResult(mode=mode, reason="no_scope")

    from core.terminal.command_runtime import get_terminal_command_service

    service = get_terminal_command_service()
    store, store_source = _event_store(service)

    command_id = _resolve_command_id(store, scope)
    if not command_id:
        return TraceResult(mode=mode, reason="no_command_id")

    command = None if service is None else service.get(command_id)
    if command is None:
        return TraceResult(mode=mode, reason="unknown_command", command_id=command_id)

    # 授权判据（库里已有的事实，不是新判据）：命令是主人下达的，且这次执行发生在
    # 命令的原始会话（同一平台 / 同一账号 / 同一个会话）里。都不满足 → 不建授权行
    # （如实留痕，不假装有授权）。
    owner_command = str(getattr(command, "issuer_role", "") or "") == "master"
    origin_account = str(getattr(command, "origin_bot_account_id", "") or "")
    same_origin = (
        str(getattr(command, "origin_platform", "") or "") == scope.platform
        and str(getattr(command, "origin_chat_id", "") or "") == scope.chat_id
        and (not origin_account or origin_account == scope.bot_account_id)
    )
    authorization_id = ""
    if owner_command and same_origin:
        authorization_id = authorization_id_for(command_id)
    elif owner_command and not same_origin:
        log.info(
            "cmd_trace_origin_mismatch",
            command_id=command_id[:32],
            origin_chat=str(getattr(command, "origin_chat_id", ""))[:64],
            exec_chat=scope.chat_id[:64],
        )

    if mode == features.MODE_SHADOW:
        # 影子档：只判定 + 只留痕，一个字节都不落库（先跑读数）。
        log.info(
            "cmd_trace_shadow",
            command_id=command_id[:32],
            action=str(action)[:64],
            ok=bool(ok),
            would_authorize=bool(authorization_id),
            store=store_source,
            parent_event_id=scope.parent_event_id[:32],
        )
        return TraceResult(
            mode=mode,
            reason="shadow",
            command_id=command_id,
            authorization_id=authorization_id,
            execution_ids=tuple(getattr(command, "execution_ids", ()) or ()),
        )

    return _write(
        service=service,
        store=store,
        store_source=store_source,
        command=command,
        scope=scope,
        action=action,
        ok=ok,
        error=error,
        duration_ms=duration_ms,
        authorization_id=authorization_id,
        mode=mode,
    )


def _write(
    *,
    service: Any,
    store: Any,
    store_source: str,
    command: Any,
    scope: _Scope,
    action: str,
    ok: bool,
    error: str,
    duration_ms: int,
    authorization_id: str,
    mode: str,
) -> TraceResult:
    command_id = str(command.command_id)

    # ① 授权行（复用既有 owner ``CommandService.record_authorization``）。
    wrote_authorization = False
    if authorization_id and service.authorization(authorization_id) is None:
        service.record_authorization(
            command_id,
            raw_owner_conversation=[
                {
                    "authorization_source": _CONFIRM_SOURCE,
                    "issuer_person_id": str(
                        getattr(command, "issuer_person_id", "") or ""
                    ),
                    "raw_request": str(getattr(command, "raw_request", "") or "")[:500],
                    "command_id": command_id,
                }
            ],
            final_confirmation=f"{_CONFIRM_SOURCE}:{command_id}",
            authorization_id=authorization_id,
        )
        wrote_authorization = True

    # ② 执行事件行：优先复用本回合那条真实的 action 行；没有才补写一条（确定 id）。
    existing = _read_back_event(store, scope=scope, action=action, command_id=command_id)
    execution_event_id = str(getattr(existing, "event_id", "") or "") if existing else ""
    if not execution_event_id:
        execution_event_id = execution_event_id_for(
            command_id, action, scope.parent_event_id
        )
        try:
            _append_trace_event(
                store,
                event_id=execution_event_id,
                scope=scope,
                action=action,
                ok=ok,
                error=error,
                duration_ms=duration_ms,
                command_id=command_id,
                authorization_id=authorization_id,
            )
        except Exception as exc:  # noqa: BLE001 - 事件行补写失败不影响登记
            log.warning(
                "cmd_trace_event_append_failed",
                command_id=command_id[:32],
                error=str(exc)[:160],
            )
            execution_event_id = ""

    # ③ 登记执行（复用既有 owner ``CommandService.record_execution``）。
    from core.terminal.command_ledger import CommandStatus

    status = CommandStatus.EXECUTING if ok else CommandStatus.FAILED
    execution_ids: list[str] = []
    if execution_event_id:
        execution_ids.append(execution_event_id)
    updated = service.record_execution(
        command_id,
        execution_event_ids=execution_ids,
        status=status,
    )

    # ④ 链接列回填（复用 owner ``TerminalEventStore.link_event``）：这一步让
    #    ``terminal_events.authorization_id`` 非空——既补执行事件行，也补授权完成时
    #    已经 append 过的那条触发消息行（与 ``record_authorization`` 同一条路径）。
    #    注意：``link_event`` **同值不写**，所以 ``linked`` 数的是"这次真的改了几行"
    #    ——重复登记时它是 0（幂等的又一个可直接读的读数），不是累计数。
    linked = 0
    if authorization_id:
        linked += int(
            service._link_events_for_command(  # noqa: SLF001 - 同 owner 的读侧
                command_id, authorization_id=authorization_id
            )
            or 0
        )
    for event_id in tuple(getattr(updated, "execution_ids", ()) or ()):
        from core.terminal.shadow import link_event_best_effort

        if link_event_best_effort(
            store,
            event_id,
            command_id=command_id,
            authorization_id=authorization_id,
            turn_id=scope.turn_id,
        ):
            linked += 1

    log.info(
        "cmd_trace_recorded",
        command_id=command_id[:32],
        action=str(action)[:64],
        ok=bool(ok),
        error=str(error or "")[:120],
        execution_ids=len(tuple(getattr(updated, "execution_ids", ()) or ())),
        authorization_id=authorization_id[:16],
        store=store_source,
        reused=bool(existing),
        appended=not bool(existing),
        wrote_authorization=wrote_authorization,
        linked=linked,
    )
    return TraceResult(
        mode=mode,
        reason="ok",
        command_id=command_id,
        authorization_id=authorization_id,
        execution_event_id=execution_event_id,
        execution_ids=tuple(getattr(updated, "execution_ids", ()) or ()),
        wrote_authorization=wrote_authorization,
        wrote_execution=bool(execution_event_id),
        linked_event_rows=linked,
    )
