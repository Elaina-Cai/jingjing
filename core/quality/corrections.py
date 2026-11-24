"""旧库兼容纠错：owner 反馈的模型提案经版本/范围核验后同事务消费。

事件、投影、显式替代及样本 learned 状态一并提交；重试使用稳定来源身份，
提交后的响应丢失只恢复收据。无效提炼、源变化及写入失败不消费样本。
旧模型 forget 不再撤销记录；停写库在查询样本和调用模型前退出。
这些摘要仍是未核实的兼容记忆，不因此成为已验证 Belief。
"""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timezone
from typing import Any

import structlog
from safelog import warn

log = structlog.get_logger()

MIN_CONFIDENCE = 0.6
# 长块保护：目标是一条大块记忆（成员清单/档案等）时，短补充不整条覆盖，
# 而是合并回原块（2026-09-22：主人只补一句，原块余人信息全部消失）。
_MERGE_TARGET_CHARS = 120
MAX_PER_MINUTE = 2
_EXTRACT_LIMIT = 5

_RATE_LOCK = threading.RLock()
_RATE: dict[tuple[str,str,str], list[float]] = {}

_SYSTEM_PROMPT = (
    "你是记忆纠错助手。用户正在纠正助手之前说的话。\n"
    "给你：用户的纠正原话、被引用的助手原话（可能为空）、以及候选记忆列表。\n"
    "请判断这次纠正的真实意图，并只输出 JSON：\n"
    '{"action": "upsert|forget|none", "fact": "纠正后的正确事实（action=upsert 时）",\n'
    ' "target_memory_id": "需要被作废的候选记忆 id（没有就空字符串）",\n'
    ' "confidence": 0.0-1.0, "reason": "一句话理由"}\n'
    "规则：\n"
    "- 用户只是否定/要求别再提 → action=forget；\n"
    "- 用户给出了正确说法 → action=upsert，fact 用简短陈述句保留事实；\n"
    "- 判断不了或只是语气词 → action=none；\n"
    "- target_memory_id 只能从候选列表里选，不能编造；\n"
    "- 若候选是一条大块记忆（如成员清单/档案），fact 只写要更正或补充的那部分，"
    "系统会自动把补充合并回原块，不要复制整块。"
)


def _owner_ids(platform: str) -> set[str]:
    try:
        from core.config.settings import config

        owners = config.get_all_owner_accounts() or {}
        return {str(v) for v in (owners.get(platform) or []) if str(v).strip()}
    except Exception:
        warn("core/quality/corrections.py:67 Exception", err=repr(__import__("sys").exc_info()[1]))
        return set()


def _rate_ok(chat_key: tuple[str,str,str]) -> bool:
    now = time.monotonic()
    with _RATE_LOCK:
        stamps = [t for t in _RATE.get(chat_key, []) if now - t < 60.0]
        if len(stamps) >= MAX_PER_MINUTE:
            _RATE[chat_key] = stamps
            return False
        stamps.append(now)
        _RATE[chat_key] = stamps
        return True


def reset_rate_for_tests() -> None:
    with _RATE_LOCK:
        _RATE.clear()


def _lookup_quoted(platform: str, account_id: str, quote_id: str) -> Any:
    if not quote_id:
        return None
    try:
        from core.bootstrap import get_runtime

        ctx = get_runtime().get(platform, account_id)
        if ctx is None:
            return None
        return ctx.repo.messages.find_by_platform_id(quote_id)
    except Exception:
        warn("core/quality/corrections.py:98 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


def _collect_candidates(
    *,
    sample: Any,
    meta_repo: Any,
    quoted: Any,
) -> list[dict]:
    """Candidate memories: 引用的那句话在召回轨迹里出现过的记忆（词面候选）。

    2026-11-01：原来的「再按向量取一轮语义候选」随去向量一起下线，只剩词面这一路。
    """
    candidates: dict[str, dict] = {}

    # 1) exact: what did she actually have in play for that reply?
    if quoted is not None:
        try:
            from core.terminal.memory_gateway import read_recall_trace

            occurred = getattr(quoted, "created_at", None)
            records = read_recall_trace(
                chat_id=str(getattr(sample, "chat_id", "") or ""),
                around=occurred or datetime.now(),
                limit=3,
            )
            for record in records:
                for hit in record.get("hits") or []:
                    memory_id = str(hit.get("memory_id") or "")
                    if memory_id and memory_id not in candidates:
                        candidates[memory_id] = {"source": "trace"}
        except Exception:
            warn("core/quality/corrections.py:137 Exception", err=repr(__import__("sys").exc_info()[1]))

    # 2) 语义候选：2026-10-25 去向量后这条路已下线（原来这里调 vector_recall_ids）。
    #    纠错匹配目前只靠上面的词面候选。

    from core.memory.explicit_write import projection_version

    out: list[dict] = []
    for memory_id, meta in list(candidates.items())[:_EXTRACT_LIMIT * 2]:
        try:
            item = meta_repo.memory_projections.get(memory_id)
        except Exception:
            warn("core/quality/corrections.py:158 Exception", err=repr(__import__("sys").exc_info()[1]))
            item = None
        if item is None:
            continue
        declared = (item.platform,item.bot_account_id,str(item.metadata.get("chat_id") or ""))
        scope = tuple(str(getattr(sample,key,"") or "") for key in ("platform","bot_account_id","chat_id"))
        if not all(scope) or declared != scope or item.sensitivity != "normal":
            continue
        if item.valid_to is not None and item.valid_to <= datetime.now():
            continue
        chat_type = str(getattr(sample,"chat_type","") or "")
        recorded_type = str(item.metadata.get("chat_type") or "")
        if recorded_type and chat_type and recorded_type != chat_type:
            continue
        if chat_type == "group" and (not item.visibility_scope or not set(item.visibility_scope) <= {"public","group_members"}):
            continue
        out.append(
            {
                "memory_id": memory_id,
                "content": str(getattr(item, "content", "") or "")[:300],
                "visibility": list(getattr(item, "visibility_scope", ()) or ()),
                "source": meta.get("source", ""),
                "full_content": str(getattr(item, "content", "") or ""),
                "version": projection_version(item),
            }
        )
        if len(out) >= _EXTRACT_LIMIT:
            break
    return out


def _valid_extraction(value) -> bool:
    if not isinstance(value,dict) or not isinstance(value.get("action"),str) or value["action"] not in {"none","upsert","forget"}:
        return False
    try:
        confidence = float(value["confidence"]) if not isinstance(value["confidence"],bool) else float("nan")
    except (KeyError,ValueError,TypeError):
        return False
    return math.isfinite(confidence) and 0 <= confidence <= 1 and (value["action"] != "upsert" or isinstance(value.get("fact"),str) and bool(value["fact"].strip()))


async def _extract(
    llm: Any,
    *,
    correction_text: str,
    quoted_text: str,
    candidates: list[dict],
) -> dict | None:
    if llm is None:
        return None
    listing = "\n".join(
        f"- {item['memory_id']}: {item['content']}" for item in candidates
    ) or "（无候选记忆）"
    user = (
        f"【用户的纠正原话】\n{correction_text}\n\n"
        f"【被引用的助手原话】\n{quoted_text or '（未引用）'}\n\n"
        f"【候选记忆】\n{listing}"
    )
    try:
        response = await llm.chat(
            system=_SYSTEM_PROMPT,
            user=user,
            agent_name="correction",
            temperature=0.1,
            max_tokens=300,
        )
        parsed = llm.parse_json(response, None)
        return parsed if _valid_extraction(parsed) else None
    except Exception as exc:  # noqa: BLE001
        log.warning("correction_extract_failed", error=str(exc)[:160])
        return None


def _default_visibility(sample: Any, candidates: list[dict]) -> tuple[str, ...]:
    target = _pick_target({}, candidates)
    if target is not None and target.get("visibility"):
        return tuple(str(v) for v in target["visibility"])
    chat_type = str(getattr(sample, "chat_type", "") or "")
    return ("group_members",) if chat_type == "group" else ("owner_only",)


def _pick_target(extraction: dict, candidates: list[dict]) -> dict | None:
    wanted = str(extraction.get("target_memory_id") or "").strip()
    for item in candidates:
        if item["memory_id"] == wanted:
            return item
    return None


def _apply(
    *,
    sample: Any,
    extraction: dict,
    candidates: list[dict],
    meta_repo: Any,
    feedback=None,
) -> str:
    if not _valid_extraction(extraction):
        return "deferred"
    action = extraction["action"]
    if float(extraction["confidence"]) < MIN_CONFIDENCE:
        return "reviewed"

    target = _pick_target(extraction, candidates)
    target_id = target["memory_id"] if target else ""
    if extraction.get("target_memory_id") and target is None:
        return "deferred"

    if action == "forget":
        # 旧模型提炼不具备撤销事实的授权；保留样本，交由带来源与版本的认知提案处理。
        return "reviewed"

    fact = str(extraction.get("fact") or "").strip()
    if not fact:
        return "reviewed"

    # 长块保护：大块记忆 + 短补充 -> 合并（旧块内容保留 + [更正补充]）。
    target_content = str(
        (target or {}).get("full_content") or (target or {}).get("content") or ""
    )
    merged = False
    if (
        target_id
        and len(target_content) > _MERGE_TARGET_CHARS
        and len(fact) <= max(80, int(len(target_content) * 0.6))
    ):
        fact = target_content.rstrip() + "\n[更正补充] " + fact
        merged = True

    from core.memory.feedback_commit import claim_feedback
    feedback = feedback or claim_feedback(sample,meta_repo.feedback_samples)
    from core.terminal.event_model import Event, EventType
    from core.terminal.event_runtime import default_event_store
    from core.terminal.principal import VisibilityScope

    store = default_event_store()
    if store is None:
        return "reviewed"

    scope_values = tuple(target["visibility"]) if target and target.get("visibility") else _default_visibility(sample, candidates)
    scopes = set()
    for value in scope_values:
        try:
            scopes.add(VisibilityScope(str(value)))
        except ValueError:
            warn("core/quality/corrections.py:279 ValueError", err=repr(__import__("sys").exc_info()[1]))
            continue
    if not scopes:
        scopes = {VisibilityScope.OWNER_ONLY}

    metadata = {
        "memory_type": "fact",
        "importance": 0.8,
        "source": "user_correction",
        "explicit_remember": True,
        "supersedes": [target_id] if target_id else [],
        "merged_from": [target_id] if merged else [],
        "partial_update": bool(merged),
        "principal": {
            "platform": str(getattr(sample, "platform", "") or ""),
            "bot_account_id": str(getattr(sample, "bot_account_id", "") or ""),
            "person_id": str(getattr(sample, "person_id", "") or ""),
        },
    }
    event = Event.new(
        event_id=feedback.event_id,
        occurred_at=feedback.created_at,
        event_type=EventType.MEMORY_WRITE,
        platform=str(getattr(sample, "platform", "") or ""),
        bot_account_id=str(getattr(sample, "bot_account_id", "") or ""),
        chat_id=str(getattr(sample, "chat_id", "") or ""),
        chat_type=str(getattr(sample, "chat_type", "") or "private"),
        actor_person_id=str(getattr(sample, "person_id", "") or ""),
        content=fact,
        visibility_scope=frozenset(scopes),
        metadata=metadata,
    )
    from core.memory.explicit_write import commit_explicit
    if target_id and not (target or {}).get("version"):
        return "reviewed"
    receipt = {}
    commit_explicit(event, store=store, repository=meta_repo.memory_projections, feedback=feedback, receipt=receipt,
                    supersedes=((target_id,target["version"]),) if target_id else ())
    return "recovered" if receipt.get("duplicate") else "learned"


async def _process_sample(sample,meta_repo,llm,lease_token: str = ""):
    from core.memory.feedback_commit import claim_feedback,recover_feedback,finish_feedback,database_key,FeedbackConflict
    claim = claim_feedback(sample,meta_repo.feedback_samples)
    if claim.database != database_key(meta_repo.memory_projections.read_engine()):
        raise FeedbackConflict("feedback_projection_database_conflict")
    if recover_feedback(claim,meta_repo.feedback_samples):
        return "recovered"
    platform,account_id,chat_id,sender = claim.identity
    if sender not in _owner_ids(platform):
        finish_feedback(claim,meta_repo.feedback_samples,"skipped_non_owner")
        return "skipped"
    if not _rate_ok((platform,account_id,chat_id)):
        return "rate_limited"
    try:
        meta = json.loads(sample.metadata_json or "{}")
    except (ValueError,TypeError) as exc:
        raise FeedbackConflict("feedback_metadata_invalid") from exc
    if not isinstance(meta,dict):
        raise FeedbackConflict("feedback_metadata_invalid")
    quoted = _lookup_quoted(platform,account_id,str(meta.get("quote_reply_id") or ""))
    if quoted is not None and str(getattr(quoted,"chat_id","") or "") != chat_id:
        quoted = None
    candidates = _collect_candidates(sample=sample,meta_repo=meta_repo,quoted=quoted)
    extraction = await _extract(llm,correction_text=sample.sample_text,
        quoted_text=str(getattr(quoted,"content","") or meta.get("quote_content") or ""),candidates=candidates)
    if not _valid_extraction(extraction):
        return "deferred"
    # Another worker may have committed during the model call; reuse that receipt.
    if recover_feedback(claim,meta_repo.feedback_samples):
        return "recovered"
    if lease_token:
        from core.quality.feedback_lease import held
        if not held(meta_repo.feedback_samples.write_engine(),sample.id,lease_token):
            return "lease_lost"
    outcome = _apply(sample=sample,extraction=extraction,candidates=candidates,meta_repo=meta_repo,feedback=claim)
    if outcome == "reviewed":
        finish_feedback(claim,meta_repo.feedback_samples,"reviewed")
    return outcome


# 每轮 ``limit`` 是**模型/提交预算**，扫描窗口另算：限流和源冲突是廉价失败，
# 跳过它们不能让后面的样本陪葬，也不能把窗口放大成模型调用放大。
_SCAN_FACTOR = 8
_SCAN_MIN = 32


async def process_pending_corrections(*, limit: int = 5, meta_repo: Any = None, llm: Any = None, lease: bool | None = None) -> dict:
    """事务性消费纠错样本；失败、源变化及晚到结果不能冒充已复核。

    积压按最老优先扫描（``list_pending``）；本轮最多做 ``limit`` 次模型/提交，
    但允许扫过更多限流/冲突样本，避免固定最近 N 项或头部坏样本饿死后续。
    """
    stats = {key:0 for key in ("scanned","learned","forgot","reviewed","skipped","recovered","conflicts","deferred","rate_limited","leased","lease_lost")}
    if meta_repo is None:
        from core.terminal.store_port import get_meta_repo
        meta_repo = get_meta_repo()
    from core.memory.legacy_write_freeze import projection_retired
    if projection_retired(meta_repo.memory_projections):
        return {**stats,"status":"retired","reason":"legacy_projection_writes_frozen"}
    from core.memory.feedback_commit import FeedbackConflict
    from core.memory.explicit_write import ExplicitMemoryConflict
    budget = max(1, int(limit))
    window = max(budget * _SCAN_FACTOR, _SCAN_MIN)
    samples = meta_repo.feedback_samples.list_pending(limit=window, source="correction")
    from core.quality import feedback_lease
    use_lease = feedback_lease.enabled() if lease is None else bool(lease)
    lease_engine = None
    if use_lease:
        lease_engine = meta_repo.feedback_samples.write_engine()
        if not feedback_lease.table_exists(lease_engine):
            return {**stats,"status":"lease_unavailable","reason":feedback_lease.TABLE}
    spent = 0
    for sample in samples:
        if spent >= budget:
            break
        stats["scanned"] += 1
        token = ""
        if use_lease:
            token = feedback_lease.try_acquire(lease_engine,sample.id)
            if not token:
                stats["leased"] += 1
                continue
        try:
            try:
                outcome = await _process_sample(sample,meta_repo,llm,lease_token=token)
            except (FeedbackConflict,ExplicitMemoryConflict) as exc:
                stats["conflicts"] += 1
                log.warning("correction_source_conflict",sample=sample.id,reason=str(exc))
                continue
            stats[outcome] += 1
            if outcome != "rate_limited":
                spent += 1
            log.info("correction_processed",sample=sample.id,outcome=outcome)
        finally:
            if token:
                feedback_lease.release(lease_engine,sample.id,token)
    return stats


__all__ = ["process_pending_corrections", "reset_rate_for_tests"]
