"""Append-only decision trace for routing and engagement.

Design goals:

* **Off by default** -- enabled via ``YINYUE_FEATURE_DECISION_TRACE=1``.
* **Never breaks the caller** -- every failure is swallowed and logged.
* **JSONL** -- one line per decision, easy to replay and diff.
* **Legacy-first** -- accepts the current router decision objects so the first
  integration can happen without waiting for the new conversation engine.

The trace is deliberately a sink, not a source of truth.  It is safe to delete
or rotate at any time.

Rotation (2026-10-10，WP-08 边界补齐)：每次 append 前若文件超过
``YINYUE_DECISION_TRACE_MAX_MB``（默认 32），改名为 ``.1``，旧备份顺移，
最多保留 ``YINYUE_DECISION_TRACE_BACKUPS`` 份（默认 3）；<=0 关闭。
读者仍只读当前文件。
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from core import features
from core.contracts.legacy import legacy_to_message_event
from safelog import warn
from core.infra.envflags import text

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_DEFAULT_PATH = Path("logs") / "decision_trace.jsonl"

#: WP-08 8.1：canonical trace v2（固定顶字段 + 预留各段；旧字段全部保留，读侧零破坏）。
SCHEMA_VERSION = 2

_CURRENT_TRACE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "yinyue_trace_id", default="",
)
_PROJECTIONS: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "yinyue_trace_projections", default=(),
)


def new_trace_id() -> str:
    return uuid.uuid4().hex


def current_trace_id() -> str:
    return str(_CURRENT_TRACE.get() or "")


def mark_projection(name: str) -> None:
    """Record that a sub-trace sink wrote a row in this bound turn."""
    key = str(name or "").strip()
    if not key:
        return
    current = _PROJECTIONS.get()
    if key not in current:
        _PROJECTIONS.set(tuple(current) + (key,))


def current_projections() -> tuple[str, ...]:
    return tuple(_PROJECTIONS.get())


@contextmanager
def use_trace_id(trace_id: str):
    """Bind one trace_id + empty projection set for the current async turn."""
    token = _CURRENT_TRACE.set(str(trace_id or ""))
    proj_token = _PROJECTIONS.set(())
    try:
        yield
    finally:
        _PROJECTIONS.reset(proj_token)
        _CURRENT_TRACE.reset(token)


def ensure_trace_id(context: Any = None) -> str:
    """Return context.trace_id / bound id / new id, and stamp it on the context."""
    trace_id = ""
    if context is not None:
        trace_id = str(getattr(context, "trace_id", "") or "")
    trace_id = trace_id or current_trace_id()
    if not trace_id:
        trace_id = new_trace_id()
    if context is not None and not getattr(context, "trace_id", ""):
        try:
            context.trace_id = trace_id
        except Exception:  # noqa: BLE001 - trace 不拖垮回合
            warn("core/observability/decision_trace.py:98 Exception", err=repr(__import__("sys").exc_info()[1]))
    return trace_id


def decision_trace_enabled() -> bool:
    return features.enabled(features.DECISION_TRACE)


def decision_trace_path() -> Path:
    """canonical trace 落盘路径：显式 env > ``LOG_DIR`` > 默认 ``logs/``。

    2026-10-10 修：测试进程只隔离 ``LOG_DIR``，而本函数原先只认
    ``YINYUE_DECISION_TRACE_PATH``——跑测试会把 intent_detail 等 detail 行写进
    生产 canonical（实测一天 515 行里 363 行是测试占位会话）。默认跟随 ``LOG_DIR``
    后，runner 的既有隔离自动生效；生产 ``.env`` 的 ``LOG_DIR=./logs`` 路径不变。
    """
    raw = text("YINYUE_DECISION_TRACE_PATH")
    if raw:
        return Path(raw)
    log_dir = (os.environ.get("LOG_DIR") or "").strip()
    if log_dir:
        return Path(log_dir) / "decision_trace.jsonl"
    return _DEFAULT_PATH


_DEFAULT_TRACE_MAX_MB = 32.0
_DEFAULT_TRACE_BACKUPS = 3


def _trace_rotation_config() -> tuple[int, int]:
    try:
        max_mb = float(
            text("YINYUE_DECISION_TRACE_MAX_MB") or _DEFAULT_TRACE_MAX_MB
        )
    except (TypeError, ValueError):
        warn("core/observability/decision_trace.py:133 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        max_mb = _DEFAULT_TRACE_MAX_MB
    try:
        backups = int(
            text("YINYUE_DECISION_TRACE_BACKUPS") or _DEFAULT_TRACE_BACKUPS
        )
    except (TypeError, ValueError):
        warn("core/observability/decision_trace.py:139 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        backups = _DEFAULT_TRACE_BACKUPS
    if max_mb <= 0 or backups <= 0:
        return 0, 0
    return int(max_mb * 1024 * 1024), backups


def _rotate_if_needed(target: Path) -> None:
    """Size-based rotation; best effort, never raises (caller holds _LOCK)."""
    max_bytes, backups = _trace_rotation_config()
    if max_bytes <= 0:
        return
    try:
        if not target.exists() or target.stat().st_size < max_bytes:
            return
    except OSError:
        warn("core/observability/decision_trace.py:154 OSError", err=repr(__import__("sys").exc_info()[1]))
        return
    oldest = target.with_name(target.name + ".%d" % backups)
    if oldest.exists():
        try:
            oldest.unlink()
        except OSError:
            warn("core/observability/decision_trace.py:160 OSError", err=repr(__import__("sys").exc_info()[1]))
    for index in range(backups - 1, 0, -1):
        src = target.with_name(target.name + ".%d" % index)
        dst = target.with_name(target.name + ".%d" % (index + 1))
        if src.exists():
            try:
                os.replace(src, dst)
            except OSError:
                warn("core/observability/decision_trace.py:168 OSError", err=repr(__import__("sys").exc_info()[1]))
    try:
        os.replace(target, target.with_name(target.name + ".1"))
    except OSError:
        warn("core/observability/decision_trace.py:172 OSError", err=repr(__import__("sys").exc_info()[1]))


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return str(value)


def record_decision(
    record: Mapping[str, Any],
    *,
    force: bool = False,
    path: Path | None = None,
) -> bool:
    """Append one decision record.

    ``force=True`` is for tests and explicit diagnostics; production callers
    should rely on the feature flag.
    """
    if not force and not decision_trace_enabled():
        return False

    payload = dict(record)
    payload.setdefault("ts", datetime.now().isoformat(timespec="milliseconds"))
    payload.setdefault("trace_id", current_trace_id() or uuid.uuid4().hex)

    target = path or decision_trace_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, default=_json_default)
        with _LOCK:
            _rotate_if_needed(target)
            with target.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return True
    except Exception as exc:  # pragma: no cover - trace must never break traffic
        log.debug("decision_trace_write_failed: %s", exc)
        return False


def record_detail(
    kind: str,
    payload: Mapping[str, Any],
    *,
    path: Path | None = None,
) -> bool:
    """WP-08 8.1d：把子 trace 细节行并入 canonical 单文件（保留旧 record 形状 + kind）。"""
    row = dict(payload or {})
    row["kind"] = str(kind or row.get("kind") or "")
    row.setdefault("schema", SCHEMA_VERSION)
    row.setdefault("trace_id", current_trace_id() or new_trace_id())
    return record_decision(row, force=True, path=path or decision_trace_path())


def _context_value(context: Any, key: str, default: Any = "") -> Any:
    if context is None:
        return default
    extra = getattr(context, "extra", None) or {}
    try:
        return extra.get(key, default)
    except AttributeError:
        warn("core/observability/decision_trace.py:234 AttributeError", err=repr(__import__("sys").exc_info()[1]))
        return default


def _decision_value(decision: Any, key: str, default: Any = "") -> Any:
    if decision is None:
        return default
    value = getattr(decision, key, default)
    return value if value is not None else default


def _iter_pairs(
    messages: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    decisions: Sequence[Any] | Iterable[Any],
) -> Iterable[tuple[Mapping[str, Any], Any]]:
    messages = list(messages)
    decisions = list(decisions)
    for index, msg in enumerate(messages):
        decision = decisions[index] if index < len(decisions) else None
        yield msg, decision


def _decision_action(decision: Any) -> str:
    action = str(getattr(decision, "action", "") or "")
    if action:
        return action
    if decision is None:
        return "silent"
    return "reply_now" if bool(getattr(decision, "should_reply", False)) else "silent"


def _decision_chain(decision: Any) -> list[str]:
    raw = getattr(decision, "trigger_reasons", ()) or ()
    chain = [str(item) for item in raw if str(item or "")]
    if chain:
        return chain
    reason = str(_decision_value(decision, "reason", "") or "")
    return [reason] if reason else []


def build_decision_trace_row(
    *,
    trace_id: str,
    decision: Any,
    event: Mapping[str, Any],
    context: Any = None,
    source: str = "",
    turn_id: str = "",
    sender_name: str = "",
    message_time: Any = None,
    route_label_text: str = "",
) -> dict[str, Any]:
    """Build one canonical v2 row (fixed sections; legacy keys kept additive)."""
    event_dict = dict(event or {})
    action = _decision_action(decision)
    reason = str(_decision_value(decision, "reason", "") or "")
    chain = _decision_chain(decision)
    relevance = getattr(decision, "relevance", None)
    if hasattr(relevance, "to_dict"):
        try:
            relevance_dict = dict(relevance.to_dict())
        except Exception:  # noqa: BLE001
            warn("core/observability/decision_trace.py:295 Exception", err=repr(__import__("sys").exc_info()[1]))
            relevance_dict = {}
    elif isinstance(relevance, Mapping):
        relevance_dict = dict(relevance)
    else:
        relevance_dict = {}
    from core.routing.reason_text import render_reason_text

    reason_text = render_reason_text(
        decision,
        sender_name=str(sender_name or ""),
        message_time=message_time,
        route_label_text=str(route_label_text or ""),
        source_override=str(source or ""),
    )
    legacy_decision = {
        "should_reply": bool(_decision_value(decision, "should_reply", False)),
        "reason": reason,
        "reason_text": reason_text,
        "action": action,
        "chain": chain,
        "reply_platform": str(_decision_value(decision, "reply_platform", "")),
        "reply_account": str(_decision_value(decision, "reply_account", "")),
        "reply_account_id": str(_decision_value(decision, "reply_account_id", "")),
        "reply_chat_id": str(_decision_value(decision, "reply_chat_id", "")),
        "is_group": bool(_decision_value(decision, "is_group", False)),
        "needs_judge": bool(_decision_value(decision, "needs_judge", False)),
    }
    trigger = str(getattr(context, "trigger", "") or "") if context is not None else ""
    trigger_msg_id = getattr(context, "trigger_msg_id", None) if context is not None else None
    effective_source = str(source or _decision_value(decision, "source", "") or "direct")
    return {
        "schema": SCHEMA_VERSION,
        "kind": "route_decision",
        "trace_id": str(trace_id or ""),
        "turn_id": str(turn_id or trace_id or ""),
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "chat_id": str(event_dict.get("chat_id") or _context_value(context, "chat_id", "") or ""),
        "platform": str(event_dict.get("platform") or _context_value(context, "platform", "") or ""),
        "account_id": str(event_dict.get("account_id") or _context_value(context, "account_id", "") or ""),
        "source": effective_source,
        "facts": {
            "event_id": str(event_dict.get("event_id") or ""),
            "msg_id": str(event_dict.get("msg_id") or ""),
            "sender_id": str(event_dict.get("sender_id") or ""),
            "sender_name": str(event_dict.get("sender_name") or ""),
            "is_group": bool(event_dict.get("is_group")),
            "trigger": trigger,
            "trigger_msg_id": trigger_msg_id,
        },
        "decision": legacy_decision,
        "relevance": {
            "action": str(relevance_dict.get("action") or ""),
            "reason": str(relevance_dict.get("reason") or ""),
        },
        "understanding": {
            "admission": "",
            "intent": str(_decision_value(decision, "active_intent", "") or ""),
            "addressee": "",
        },
        "generation": {
            "silent": action == "silent",
            "policy_action": "",
        },
        "outbound": {"sent": None, "length": None},
        "projections": {},
        # legacy compatibility (existing readers)
        "event": event_dict,
        "context": {"trigger": trigger, "trigger_msg_id": trigger_msg_id},
    }


def trace_route_decisions(
    messages: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    decisions: Sequence[Any] | Iterable[Any],
    context: Any = None,
    *,
    trace_id: str = "",
    force: bool = False,
    path: Path | None = None,
) -> int:
    """Record one canonical v2 row per routing decision (direct path)."""
    if not force and not decision_trace_enabled():
        return 0
    if not messages:
        return 0
    trace_id = str(trace_id or "") or ensure_trace_id(context)
    written = 0
    try:
        pairs = list(_iter_pairs(messages, decisions))
        total = len(pairs)
        platform = str(_context_value(context, "platform", "") or "")
        account_id = str(_context_value(context, "account_id", "") or "")
        for index, (msg, decision) in enumerate(pairs):
            if not isinstance(msg, Mapping):
                continue
            event = legacy_to_message_event(
                msg,
                platform=platform or str(msg.get("source") or ""),
                account_id=account_id or str(msg.get("account_id") or ""),
            )
            row = build_decision_trace_row(
                trace_id=trace_id,
                decision=decision,
                event=event.to_dict(),
                context=context,
                source="direct",
            )
            row["decision_index"] = index
            row["decision_count"] = total
            if record_decision(row, force=force, path=path):
                written += 1
    except Exception as exc:  # pragma: no cover - trace must never break traffic
        log.debug("decision_trace_failed: %s", exc)
    return written


def trace_trigger_decision(
    decision: Any,
    *,
    question: Any = None,
    chat_id: str = "",
    trace_id: str = "",
    force: bool = False,
    path: Path | None = None,
) -> bool:
    """Canonical v2 row for the group-trigger path (which bypasses route())."""
    if not force and not decision_trace_enabled():
        return False
    trace_id = str(trace_id or "") or current_trace_id() or new_trace_id()
    event = {
        "platform": "wechat",
        "account_id": "",
        "chat_id": str(chat_id or ""),
        "event_id": "",
        "msg_id": str(getattr(question, "msg_id", "") or ""),
        "sender_id": str(getattr(question, "sender_id", "") or ""),
        "sender_name": str(getattr(question, "sender_name", "") or ""),
        "content": str(getattr(question, "content", "") or "")[:40],
        "is_group": True,
    }
    row = build_decision_trace_row(
        trace_id=trace_id,
        decision=decision,
        event=event,
        source="trigger",
        sender_name=str(getattr(question, "sender_name", "") or ""),
        message_time=getattr(question, "created_at", None),
    )
    return record_decision(row, force=force, path=path)

def read_decisions(
    limit: int = 100,
    *,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """Read the most recent trace rows (debugging / evaluation helper)."""
    target = path or decision_trace_path()
    if not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with target.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    warn("core/observability/decision_trace.py:464 json.JSONDecodeError", err=repr(__import__("sys").exc_info()[1]))
                    continue
    except OSError:
        warn("core/observability/decision_trace.py:466 OSError", err=repr(__import__("sys").exc_info()[1]))
        return []
    return rows[-max(0, limit):]

def turns_summary(
    turns: Any,
    *,
    source: str = "",
    window_hash: str = "",
    window_full_hash: str = "",
    rows_hash: str = "",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """WP-08 8.1c：turns 摘要（原 turns_trace 的读数字段，搬进 canonical outcome.facts）。"""
    items = list(turns or [])
    payload = {
        "n": len(items),
        "chars": sum(len(str(item.get("content") or "")) for item in items),
        "hash": hashlib.sha1(
            json.dumps(items, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:12],
        "roles": "".join(
            "A" if str(item.get("role")) == "assistant" else "U" for item in items
        )[:120],
        "source": str(source or ""),
        "window_hash": str(window_hash or ""),
        "window_full_hash": str(window_full_hash or ""),
        "rows_hash": str(rows_hash or ""),
    }
    if extra:
        payload.update(dict(extra))
    return payload


def build_turn_outcome_row(
    ctx: Any,
    *,
    trace_id: str,
    reply: Any = None,
    sent: Any = None,
    length: int = 0,
    error: str = "",
) -> dict[str, Any]:
    """Build the end-of-turn outcome row (same trace_id; joins sub-traces)."""
    context = getattr(ctx, "outcome_context", None) or getattr(ctx, "context", None)
    decision = getattr(ctx, "decision", None)
    extra = getattr(context, "extra", None) or {}
    action = _decision_action(decision)
    reason = str(_decision_value(decision, "reason", "") or "")
    chain = _decision_chain(decision)
    event = {
        "chat_id": str(extra.get("chat_id") or getattr(ctx, "chat_id", "") or ""),
        "platform": str(extra.get("platform") or "wechat"),
        "account_id": str(extra.get("account_id") or ""),
        "event_id": "",
        "msg_id": str(getattr(getattr(ctx, "question", None), "msg_id", "") or ""),
        "sender_id": str(extra.get("sender_id") or getattr(getattr(ctx, "question", None), "sender_id", "") or ""),
        "sender_name": str(extra.get("sender_name") or getattr(getattr(ctx, "question", None), "sender_name", "") or ""),
        "is_group": bool(extra.get("is_group") or getattr(ctx, "origin", "") == "trigger"),
    }
    if sent is None:
        try:
            sent = bool(extra.get("_last_sent_ok"))
        except Exception:  # noqa: BLE001
            warn("core/observability/decision_trace.py:529 Exception", err=repr(__import__("sys").exc_info()[1]))
            sent = None
    # N1 退役后 ``context.intent_result`` 不再有生产者：这两列恒空（保留列位是为了
    # 报表 / 回放的历史形状不变），不再从已消失的结论对象上读。
    intent_value = ""
    addressee_text = ""
    policy_decision = getattr(context, "policy_decision", None)
    policy_action = getattr(policy_decision, "action", "") if policy_decision is not None else ""
    if hasattr(policy_action, "value"):
        policy_action = policy_action.value
    question = getattr(ctx, "question", None)
    sender_name = str(
        extra.get("sender_name") or getattr(question, "sender_name", "") or ""
    )
    message_time = extra.get("message_time") or getattr(question, "created_at", None)
    row = build_decision_trace_row(
        trace_id=trace_id,
        decision=decision,
        event=event,
        context=context,
        source=str(getattr(ctx, "origin", "") or "direct"),
        sender_name=sender_name,
        message_time=message_time,
    )
    row["kind"] = "turn_outcome"
    row["understanding"] = {
        "admission": "",
        "intent": str(intent_value or ""),
        "addressee": addressee_text,
    }
    row["generation"] = {
        "silent": action == "silent",
        "policy_action": str(policy_action or ""),
    }
    row["outbound"] = {
        "sent": bool(sent) if sent is not None else None,
        "length": int(length or 0),
    }
    row["projections"] = {name: trace_id for name in current_projections()}
    outcome_state = getattr(ctx, "outcome", None)
    if isinstance(outcome_state, dict) and outcome_state.get("turns"):
        row["facts"]["turns"] = dict(outcome_state["turns"])
    row["error"] = str(error or "")[:200]
    return row


def trace_turn_outcome(
    ctx: Any,
    *,
    reply: Any = None,
    sent: Any = None,
    length: int = 0,
    error: str = "",
    force: bool = False,
    path: Path | None = None,
) -> bool:
    """Append the end-of-turn outcome row (same trace_id; canonical only)."""
    if not force and not decision_trace_enabled():
        return False
    trace_id = str(getattr(ctx, "trace_id", "") or "") or current_trace_id()
    if not trace_id:
        return False
    if not length and isinstance(reply, str):
        length = len(reply)
    row = build_turn_outcome_row(
        ctx, trace_id=trace_id, reply=reply, sent=sent, length=length, error=error,
    )
    return record_decision(row, force=force, path=path)


#: WP-08 8.1 读者登记表（6 套 JSONL 的写者/读者/状态；新增读者改这里并同步测试）。
TRACE_SINKS: dict[str, dict[str, Any]] = {
    "decision_trace.jsonl": {
        "writer": "core.observability.decision_trace",
        "canonical": True,
        "readers": (
            "core/conversation/report.py",
            "core/conversation/tune.py",
            "scripts/review_strict_additions.py",
            "scripts/score_two_path_quality.py",
            "scripts/understanding_baseline.py",
            "scripts/watch_shadow_smoke.ps1",
        ),
        "status": "canonical",
        "deletion_ready": False,
    },
    "admission_trace.jsonl": {
        "writer": "core.observability.decision_trace (detail rows)",
        "canonical": False,
        "readers": ("scripts/admission_board.py",),
        "status": "merged_into_canonical",
        "deletion_ready": True,
    },
    "intent_trace.jsonl": {
        "writer": "core.observability.decision_trace (detail rows)",
        "canonical": False,
        "readers": (),
        "status": "merged_into_canonical",
        "deletion_ready": True,
    },
    "context_trace.jsonl": {
        "writer": "core.observability.decision_trace (detail rows)",
        "canonical": False,
        "readers": (
            "core/context/assembler.py::read_context_trace",
            "scripts/inspect_turns.py",
            "scripts/observe_rollout.py",
            "scripts/replay_log_cases.py",
        ),
        "status": "merged_into_canonical",
        "deletion_ready": True,
    },
    "turns_trace.jsonl": {
        "writer": None,
        "canonical": False,
        "readers": (),
        "status": "retired_wp081c",
        "deletion_ready": True,
    },
}
