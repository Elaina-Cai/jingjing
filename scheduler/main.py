import asyncio
import faulthandler
import json
import os
import structlog
import schedule
import time
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any
from concurrent.futures import TimeoutError as FuturesTimeoutError

# ---------------------------------------------------------------- 临时目录固定
# 必须在**任何**数据库连接之前：SQLite 需要临时文件时会用系统临时目录，而调度器
# 由计划任务/启动器拉起时 TEMP 可能已被清理或不可写 —— 那时 SQLite 只报
# ``unable to open database file``（SQLITE_CANTOPEN），新建连接也一样失败。详见
# ``core/infra/tempdir.py`` 与 ``docs/decisions/运维_TEMP不可写导致事件丢失.md``。
from core.infra.tempdir import pin_temp_dir as _pin_temp_dir
from core.infra import duration  # noqa: E402
from core.infra.duration import from_epoch_s, now_ms, sleep_s, to_ms# noqa: E402 - 时长只有一个单位：毫秒（UI-066）
from core.infra import liveness  # noqa: E402 - 心跳路径/判死口径的唯一来源


from core.config.settings import config
# 媒体助手提到模块级：引述消息还原（_resolve_quoted_media_path 一族）要用。
from core.conversation.media import (
    extract_sticker_md5,
    media_path_from_message_row,
    sticker_md5_from_message_row,
)
from core.bootstrap import (
    init_singletons, init_dirs, get_meta_db_path, get_db_path,
    get_llm, get_embedding, get_vector_store, get_tools, build_agent,
    get_runtime,
)
from core.terminal.store_port import get_meta_repo, get_repo, get_unified_query
from core.infra.event_bus import EventBus, Event, EventType, on
from core import features, temporal
from core.runtime import current_account_context
from scheduler import self_stop
from core.routing.agent_registry import AgentRegistry
from core.infra.status import get_status_tracker
from core.ingestion.ingestors import IngestionError, get_ingestion_manager
from db.repository import RepositoryManager
from db.write_guard import declare_writes_allowed
# [去向量] 移除: from db.vector import VectorStore
from db.models import Message
from agents.base import AgentContext
from agents.tools import ToolRegistry
from llm.client import LLMClient
# [去向量] 移除: from llm.embedding import EmbeddingClient
from notify.push import send_notification
from core.infra.logging import init_logging
from safelog import warn

init_logging(log_dir=str(config.paths.log_dir))
log = structlog.get_logger()

# 临时目录必须在**任何**数据库连接之前钉好（首个连接在 init_singletons 里）。放在
# init_logging 之后，是为了让 temp_dir_pinned 进 JSON 日志、而不是只在 stderr 上。
_PINNED_TEMP_DIR = _pin_temp_dir()

DB_PATH = get_db_path()


from core.infra import egress
from core.infra.envflags import flag
from core.infra.jsonio import atomic_write_json


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        warn("scheduler/main.py:72 ValueError", err=repr(__import__("sys").exc_info()[1]))
        return default


def _with_account_context(fn):
    """Resolve and activate AccountContext from the event payload."""
    @wraps(fn)
    async def wrapper(event, *args, **kwargs):
        runtime = get_runtime()
        ctx = runtime.context_for_event(event)
        with runtime.activate(ctx):
            return await fn(event, *args, **kwargs)
    return wrapper


def _build_tool_registry(repo: RepositoryManager) -> ToolRegistry:
    return get_tools()


def register_agents():
    """Keep the generic registry for task/conversation agents.

    The old analysis agents were removed in Track B5; analysis now runs in
    ``core.analysis.workers`` and writes ``analysis_projections`` directly.
    """
    # 2026-10-24：对话 Agent（ConversationalAgent）随旧决策层退役，
    # 这里不再向 AgentRegistry 注册任何生成型 Agent；
    # 生产回复由 loop/ 引擎负责，AgentRegistry 仅保留给任务/子 Agent 用。
    log.info("agent_registry_legacy_retired", hint="conversational agent retired 2026-10-24")


_sync_pipeline_gate_lock = threading.Lock()
_sync_pipeline_running = False
_sync_pipeline_pending = False


def _serialize_sync_pipeline(fn):
    """Run at most one sync pipeline at a time across threads/event loops.

    The watcher thread, the scheduler thread and the Web API all can trigger
    this coroutine.  A concurrent trigger is coalesced into one immediate
    follow-up pass, so a message that landed just after the previous snapshot
    is still picked up in the same scheduling turn instead of waiting for the
    next timer tick.  Two decrypt/cursor writers never run at once.
    """
    async def wrapper(*args, **kwargs):
        global _sync_pipeline_running, _sync_pipeline_pending
        with _sync_pipeline_gate_lock:
            if _sync_pipeline_running:
                _sync_pipeline_pending = True
                log.info("sync_pipeline_coalesced", reason="already_running")
                return
            _sync_pipeline_running = True

        try:
            while True:
                result = await fn(*args, **kwargs)
                with _sync_pipeline_gate_lock:
                    if not _sync_pipeline_pending:
                        # Release the gate while still holding the lock so a
                        # trigger arriving at this instant either marks
                        # _pending or starts a fresh run, never gets lost.
                        _sync_pipeline_running = False
                        return result
                    _sync_pipeline_pending = False
                    log.info("sync_pipeline_rerun", reason="triggered_during_sync")
        except BaseException:
            with _sync_pipeline_gate_lock:
                _sync_pipeline_running = False
            raise

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


async def _run_auto_dream(ctx) -> None:
    try:
        from agents.auto_dream import AutoDream
        from db.unified_query import UnifiedQuery as _UQ
        from core.terminal.store_port import get_account_db_manager as _get_adm
        dream_repo = get_meta_repo()
        dream_adm = _get_adm()
        dream_uq = _UQ(meta_repo=dream_repo, account_db_manager=dream_adm) if dream_adm else None
        dream = AutoDream(dream_repo, unified_query=dream_uq)
        _t0 = time.monotonic()
        log.info("auto_dream_job_start")
        dream_result = await asyncio.to_thread(dream.dream_all)
        auto_merged = 0
        auto_split = 0
        for chat_result in dream_result:
            for mc in chat_result.get("merge_candidates", []):
                if mc.get("score", 0) >= 0.85:
                    if await asyncio.to_thread(dream.execute_merge, mc["source_id"], mc["target_id"]):
                        auto_merged += 1
            for sc in chat_result.get("split_candidates", []):
                if sc.get("score", 0) >= 0.9:
                    if await asyncio.to_thread(dream.execute_split, sc["thread_id"]):
                        auto_split += 1
        log.info("auto_dream_job_done", seconds=round(time.monotonic() - _t0, 2))
        if auto_merged or auto_split:
            log.info("auto_dream_executed", merged=auto_merged, split=auto_split)
    except Exception as e:
        log.warning("auto_dream_failed", error=str(e))


@_serialize_sync_pipeline
async def run_sync_pipeline():
    log.info("sync_pipeline_start")
    status_tracker = get_status_tracker()
    rec = status_tracker.begin_sync()

    runtime = get_runtime()
    manager = get_ingestion_manager()

    for ctx in runtime.all_contexts():
        _t_ing = time.perf_counter()
        _ingest_ms = -1
        try:
            # 2026-10-21：main_loop_gap 量的是主循环一圈（不是事件循环滞后），
            # 一圈里串行等所有平台 = 相加。先给单平台加上限，超了这轮放弃。
            result = await asyncio.wait_for(manager.sync(ctx), timeout=8.0)
            _ingest_ms = int((time.perf_counter() - _t_ing) * 1000)
            if _ingest_ms > 800:
                # 2026-10-21 加的测量：main_loop_gap 与 sync_round_slow 时长相等，
                # 说明主循环被同步段占据；但到底花在「解密/入库/嵌入」哪一段，此前只能猜。
                # 先按平台记一段 ingest 用时，下一次卡顿时这条线直接指出是谁。
                log.warning(
                    "sync_ingest_slow",
                    platform=str(getattr(ctx, "platform", "")),
                    account_id=str(getattr(ctx, "account_id", "")),
                    ingest_ms=_ingest_ms,
                )
        except IngestionError:
            warn("scheduler/main.py:205 IngestionError", err=repr(__import__("sys").exc_info()[1]))
            _ingest_ms = int((time.perf_counter() - _t_ing) * 1000)
        except IngestionError as e:
            msg = str(e)
            log.error("ingest_failed", platform=ctx.platform,
                      account_id=ctx.account_id, error=msg)
            rec.errors.append(msg)
            continue
        except Exception as e:
            msg = f"ingest failed ({ctx.platform}:{ctx.account_id}): {e}"
            log.error("ingest_failed", platform=ctx.platform,
                      account_id=ctx.account_id, error=str(e))
            rec.errors.append(msg)
            continue

        rec.new_messages += result.stored
        if result.errors:
            rec.errors.extend(result.errors)
        if features.enabled(features.EVIDENCE_INDEX):
            # Run even when this sync stored nothing: the cursor may still lag
            # behind because a previous pass hit its batch cap or the process
            # stopped before the next page.
            try:
                from core.evidence import EvidenceIndexer
                meta_repo = get_meta_repo()
                indexer = EvidenceIndexer(meta_repo.evidence, meta_repo.people)
                indexed = indexer.index_pending_for_account(
                    ctx.repo,
                    platform=ctx.platform,
                    account_id=ctx.account_id,
                    cursors=meta_repo.evidence_cursors,
                    batch_size=_env_int("YINYUE_EVIDENCE_INDEX_BATCH_SIZE", 500),
                    max_batches=_env_int("YINYUE_EVIDENCE_INDEX_MAX_BATCHES", 10),
                )
                if indexed:
                    log.info(
                        "evidence_indexed",
                        platform=ctx.platform,
                        account_id=ctx.account_id,
                        count=indexed,
                    )
            except Exception as e:
                log.warning(
                    "evidence_index_failed",
                    platform=ctx.platform,
                    account_id=ctx.account_id,
                    error=str(e)[:160],
                )

        if features.enabled(features.MEDIA_REGISTRY, True):
            # Retry media that had no file yet when it was ingested.  Runs even
            # when this pass stored nothing, because the failure it repairs is
            # "WeChat wrote the file a few seconds after we looked" -- waiting
            # for new traffic would never fix it.  Cheap when nothing is due.
            try:
                from core.conversation.media_registry import resolve_pending

                stats = resolve_pending(ctx.repo.engine, limit=40)
                if stats.get("tried"):
                    log.info(
                        "media_retry_pass",
                        platform=ctx.platform,
                        account_id=ctx.account_id,
                        **stats,
                    )
                if stats.get("resolved"):
                    # task-29：账号库 resolved -> meta 库 missing 的回填桥。
                    # 只在真有行被解析时触发；任何异常只 warning，绝不让 sync pass 挂掉。
                    try:
                        from core.ingestion.media_store import (
                            backfill_media_from_account,
                        )

                        backfill_media_from_account(
                            ctx.repo.engine,
                            platform=ctx.platform,
                            account_id=ctx.account_id,
                            limit=40,
                        )
                        # 2026-10-27：晚到的图只回填了元数据，没人问过"图里是什么"。
                        # 顺手补一小批（每批 4 张，最贵 12 秒/张，失败不抛）。
                        from core.ingestion.media_store import describe_missing_media

                        describe_missing_media(ctx.repo.engine, limit=4)
                    except Exception as e:  # noqa: BLE001
                        log.warning("describe_missing_media_failed", error=str(e)[:160])
                if stats.get("resolved"):
                    # 图晚到：这一轮才终于能看清的图，叫醒她一次让她事后补一句。
                    # 旁白走 run_proactive_turn 的 hint 通道，不改任何 prompt 结构；
                    # 说不说由她决定（她可以输出 [静默]），白名单/静默时段照样生效。
                    try:
                        from agents.late_vision_followup import (
                            maybe_publish_late_followup,
                        )

                        late = maybe_publish_late_followup(
                            ctx.repo.engine,
                            platform=ctx.platform,
                            account_id=ctx.account_id,
                        )
                        if late.get("published"):
                            log.info(
                                "media_late_followup",
                                platform=ctx.platform,
                                account_id=ctx.account_id,
                                **late,
                            )
                    except Exception as e:  # noqa: BLE001
                        log.warning(
                            "media_late_followup_failed",
                            platform=ctx.platform,
                            account_id=ctx.account_id,
                            error=str(e)[:160],
                        )
            except Exception as e:
                log.warning(
                    "media_retry_failed",
                    platform=ctx.platform,
                    account_id=ctx.account_id,
                    error=str(e)[:160],
                )

        if features.enabled(features.OUTBOUND_RECONCILE) and result.stored:
            try:
                from core.conversation import (
                    get_shadow_observer,
                    reconcile_bot_message_ids,
                )
                updated = reconcile_bot_message_ids(
                    get_shadow_observer().store,
                    ctx.repo.send_log,
                    platform=ctx.platform,
                    account_id=ctx.account_id,
                )
                if updated:
                    log.info(
                        "conversation_bot_ids_reconciled",
                        platform=ctx.platform,
                        account_id=ctx.account_id,
                        count=updated,
                    )
            except Exception as e:
                log.warning(
                    "outbound_reconcile_failed",
                    platform=ctx.platform,
                    account_id=ctx.account_id,
                    error=str(e)[:160],
                )


        if result.emitted:
            # Only genuinely new messages trigger the expensive dream post-pass;
            # a historical backfill must stay silent.  Phase 4: run it in the
            # background queue so a slow dream never blocks the next sync/reply.
            try:
                from core.terminal.background import submit_background_job

                async def _dream_job(account_ctx=ctx) -> None:
                    with get_runtime().activate(account_ctx):
                        await _run_auto_dream(account_ctx)

                if not submit_background_job("auto_dream", _dream_job):
                    log.warning("auto_dream_job_dropped",
                                account_id=getattr(ctx, "account_id", ""))
            except Exception as exc:  # noqa: BLE001
                log.warning("auto_dream_submit_failed", error=str(exc)[:160])
            rec.agents_run = len([
                n for n in config.agents.enabled_agents if n != "daily_digest"
            ])

    status_tracker.finish_sync(
        rec,
        new_messages=rec.new_messages,
        agents_run=rec.agents_run,
        analyses=rec.analyses_generated,
    )


async def run_daily_digest():
    log.info("daily_digest_start")
    EventBus.publish(Event(EventType.DAILY_DIGEST_TRIGGER, payload={}))
    await EventBus.await_pending(timeout=120)


_last_message_time: dict[str, datetime] = {}


def _update_idle_tracker(event: Event):
    messages = event.payload.get("messages", [])
    for msg in messages:
        chat_id = msg.get("chat_id")
        if chat_id:
            _last_message_time[chat_id] = datetime.now()


async def run_dream_engine(chat_id: str | None = None):
    if not config.dream.enabled:
        return

    log.info("dream_engine_start", chat_id=chat_id or "all")
    repo = get_repo()
    llm = get_llm()
    # [2026-11-02 去向量收尾] embedding/vector_store 随向量能力移除：
    # get_embedding()/get_vector_store() 恒返回 None，AutoDream 也从未读过它们。

    from agents.auto_dream import AutoDream
    from db.unified_query import UnifiedQuery
    from core.terminal.store_port import get_account_db_manager
    _adm = get_account_db_manager()
    _uq = UnifiedQuery(meta_repo=repo, account_db_manager=_adm) if _adm else None
    dream = AutoDream(repo, llm=llm, unified_query=_uq)

    if chat_id:
        chat_ids = [chat_id]
    elif _uq:
        chat_list = _uq.get_chat_list()
        chat_ids = [r.data.wxid for r in chat_list if r.data.is_group]
    else:
        chat_ids = repo.messages.get_chat_ids(chat_type="group")
    if not chat_ids:
        log.info("dream_engine_no_chats")
        return

    total_stats = {"merged": 0, "split": 0, "adopted": 0, "upgraded": 0, "consolidated": 0, "rebalanced": 0}

    for cid in chat_ids:
        try:
            result = await asyncio.to_thread(dream.dream, cid)

            for mc in result.get("merge_candidates", []):
                if mc.get("score", 0) >= config.dream.auto_merge_threshold:
                    if await asyncio.to_thread(dream.execute_merge, mc["source_id"], mc["target_id"]):
                        total_stats["merged"] += 1
                        log.info("dream_auto_merge", source=mc["source_id"], target=mc["target_id"], score=mc["score"])

            for sc in result.get("split_candidates", []):
                if sc.get("score", 0) >= config.dream.auto_split_threshold:
                    if await asyncio.to_thread(dream.execute_split, sc["thread_id"]):
                        total_stats["split"] += 1
                        log.info("dream_auto_split", thread=sc["thread_id"], score=sc["score"])

            if config.dream.auto_adopt_orphans:
                orphans = await asyncio.to_thread(dream.detect_orphan_candidates, cid)
                for o in orphans:
                    if await asyncio.to_thread(dream.execute_adopt, o["orphan_id"], o["target_id"]):
                        total_stats["adopted"] += 1
                        log.info("dream_auto_adopt", orphan=o["orphan_id"], target=o["target_id"])

            if config.dream.auto_upgrade_summaries:
                summary_cands = await dream.detect_summary_upgrade_candidates(cid)
                for s in summary_cands[:3]:
                    res = await dream.execute_summary_upgrade(s["thread_id"])
                    if res.get("ok"):
                        total_stats["upgraded"] += 1
                        log.info("dream_auto_upgrade_summary", thread=s["thread_id"])

            if config.dream.auto_rebalance_budgets:
                budget_result = await asyncio.to_thread(dream.execute_budget_reallocation, cid)
                total_stats["rebalanced"] += budget_result.get("applied", 0)

        except Exception as e:
            log.warning("dream_chat_failed", chat_id=cid, error=str(e))

    if any(total_stats.values()):
        log.info("dream_engine_completed", **total_stats)
        EventBus.publish(Event(
            EventType.DREAM_TRIGGER,
            payload={"stats": total_stats, "chat_ids": chat_ids},
        ))
    else:
        log.info("dream_engine_no_actions")


async def _check_idle_and_dream():
    if not config.dream.enabled:
        return
    now = datetime.now()
    idle_threshold = timedelta(minutes=config.dream.idle_timeout_minutes)
    idle_chats = []
    for cid, last_time in _last_message_time.items():
        if now - last_time >= idle_threshold:
            idle_chats.append(cid)

    if idle_chats:
        log.info("dream_idle_detected", idle_chats=len(idle_chats))
        try:
            from core.terminal.background import submit_background_job
        except Exception:
            warn("scheduler/main.py:464 Exception", err=repr(__import__("sys").exc_info()[1]))
            submit_background_job = None
        for cid in idle_chats[:5]:
            if submit_background_job is not None:
                submit_background_job(
                    f"dream_idle:{cid}",
                    lambda cid=cid: run_dream_engine(chat_id=cid),
                )
            _last_message_time[cid] = now




def _principal_for_message(msg, account_id: str, platform: str = ""):
    """Resolve the deterministic PrincipalContext for one inbound message.

    Authority is always on; when the backing service is unavailable the
    authority layer returns a fail-closed UNKNOWN principal rather than
    falling back to nickname / owner-id lookup.
    """
    try:
        from core.terminal.authority import active_principal
    except Exception:
        warn("scheduler/main.py:486 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None
    chat_type = "group" if msg.get("chat_type") == "group" else "private"
    chat_id = str(msg.get("chat_id") or "")
    return active_principal(
        platform=str(platform or msg.get("source") or "wechat"),
        account_id=str(account_id or ""),
        chat_id=chat_id,
        chat_type=chat_type,
        sender_id=str(msg.get("sender_id") or ""),
        sender_name=str(msg.get("sender_name") or ""),
        group_id=chat_id if chat_type == "group" else "",
    )


def _resolve_parent_event_id(
    msg: dict, *, platform: str, bot_account_id: str, chat_id: str
) -> str:
    """把被回应的消息解析成**事件 id**；解析不出来返回空。

    A1-c：采集侧已在事件行写入时把 ``event_id`` / ``turn_id`` 回写到消息行
    （core/terminal/shadow.record_message_events），所以正常路径直接取。旧路径
    或测试替身没有这两个键时按四元组查一次库；再不行就留空——**绝不把平台
    msg_id 当父事件 id 写进去**（那会让父链全 orphan，读侧兼容见 chat_window）。
    """
    event_id = str(msg.get("event_id") or "")
    if event_id:
        return event_id
    external_msg_id = str(msg.get("msg_id") or "")
    if not external_msg_id:
        return ""
    try:
        from core.terminal.shadow import get_default_store

        store = get_default_store()
        lookup = getattr(store, "get_by_external_msg_id", None)
        if lookup is None:
            return ""
        found = lookup(
            platform=str(platform or ""),
            bot_account_id=str(bot_account_id or ""),
            chat_id=str(chat_id or ""),
            external_msg_id=external_msg_id,
        )
        return str(getattr(found, "event_id", "") or "")
    except Exception:
        warn("scheduler/main.py:531 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


def _shadow_record_reply(reply: str, msg: dict, agent_context, source: str, account_id: str) -> None:
    """Record one outbound reply into the terminal event stream."""
    try:
        if not reply:
            return
        from core.terminal.shadow import record_reply_event
        chat_id = str(msg.get("chat_id") or "")
        chat_type = str(msg.get("chat_type") or "private")
        sender_id = str(msg.get("sender_id") or "")
        platform = str(source or "")
        bot_account_id = str(account_id or "")
        principal = None
        try:
            from core.terminal.authority import active_principal

            principal = active_principal(
                platform=platform,
                account_id=bot_account_id,
                chat_id=chat_id,
                chat_type=chat_type,
                sender_id=sender_id,
                sender_name=str(msg.get("sender_name") or ""),
                group_id=chat_id if chat_type == "group" else "",
            )
        except Exception:
            warn("scheduler/main.py:559 Exception", err=repr(__import__("sys").exc_info()[1]))
            principal = None
        if principal is None:
            from core.terminal.shadow_runtime import resolve_shadow_principal

            principal = resolve_shadow_principal(
                platform=platform,
                account_id=bot_account_id,
                chat_id=chat_id,
                chat_type=chat_type,
                sender_id=sender_id,
                sender_name=str(msg.get("sender_name") or ""),
                group_id=chat_id if chat_type == "group" else "",
            )
        record_reply_event(
            content=reply,
            platform=platform,
            bot_account_id=bot_account_id,
            chat_id=chat_id,
            chat_type=chat_type,
            person_id=sender_id,
            # A1-c：父链接写事件 id（不是平台 msg_id）；源 msg_id 另存 metadata。
            parent_event_id=_resolve_parent_event_id(
                msg,
                platform=platform,
                bot_account_id=bot_account_id,
                chat_id=chat_id,
            ),
            parent_msg_id=str(msg.get("msg_id") or ""),
            turn_id=str(
                (getattr(agent_context, "extra", None) or {}).get("turn_id")
                or msg.get("turn_id")
                or ""
            ),
            principal=principal,
        )
    except Exception:
        warn("scheduler/main.py:595 Exception", err=repr(__import__("sys").exc_info()[1]))


def should_register_pat_media(
    *,
    should_reply: bool,
    pat_deferred: bool,
    has_images: bool,
    reason: str,
    has_sticker_context: bool,
) -> bool:
    """Pat-first, image-later (cross batch): register the "maybe an image next" expectation.

    A pat is a weak "look at me" signal, so keep a short expectation for the
    sender's next image (``kind="pat"``, 90s TTL).  flag ``pat_grace`` 打开时，
    群聊**纯拍**会被挂起（``should_reply=False``，等 6s 宽限窗口到点再成回合）
    ——但"拍是弱信号、给对方留一个『可能发图』的预期"这条语义不能丢：挂起期间
    同样登记，否则"拍一下再发图"会丢掉"他刚拍过我"的上下文。

    ``pat_deferred`` 由调用点算好（``not should_reply`` 且 ``pat_grace_enabled()``）：
    这里只做判定，不读 env、不 import 开关，行为可被逐格断言。

    **R-A12 P1b（2026-10-06）**：``UNIT_MEDIA`` 打开时这条登记**退役**——调用点在
    进来之前就问过 ``pat_media_registration_retired()``，本函数因此在这条路上不再被
    调用（判据本身保持逐字节不变：它仍是 ``UNIT_MEDIA=off`` 的**唯一**口径，也是
    退役前后可逐格对账的那一份）。

    判定口径（搬自原内联条件，逐字节不变）：
    ``decision`` 存在 且（直回 或 挂起登记）且无图 且 reason 含 ``"pat"``
    且没有 sticker 上下文。
    """
    return bool(
        (should_reply or pat_deferred)
        and not has_images
        and "pat" in reason
        and not has_sticker_context
    )


def pat_media_registration_retired() -> bool:
    """``pat→media`` 的 ``kind="pat"`` 登记**是否退役**（R-A12 P1b，2026-10-06）。

    规格 `docs/语义单元规格.md` §6.1 第 3 行把它判**退役**：那条登记要证的事情是
    「拍是弱信号，给他留一个『可能发图』的预期」——**那正是"媒体信号并入同一单元"**。
    单元路径按 ``(chat, actor, 时间邻近)`` 收口，拍与随后那张图是同一个人、同一个窗口
    里的两个成员，装进**同一个**单元、只成一个回合（退役覆盖证据逐场景对账见
    ``tests/test_unit_media.py`` 的 ``test_pat_media_registration_is_covered_by_the_unit``）。

    唯一判据是 ``core.group_state.unit_media_enabled()``（它自己要求 ``SEMANTIC_UNIT``
    也开着：没有装配器就没有"媒体进单元"这条路径，那时退役就是纯粹的覆盖缺口）。
    读不到开关 = ``False`` = **不退役**（回滚位，与接线前逐字节一致）。
    """
    try:
        from core.group_facts import unit_media_enabled

        return bool(unit_media_enabled())
    except Exception:  # noqa: BLE001 - 读不到开关=保持现状
        warn("scheduler/main.py:652 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def _resolve_quoted_media_path(
    msg, batch_messages, source: str, account_id: str, repo,
) -> str:
    """Resolve ``[引用 图片]`` back to the quoted message's cached media."""
    # ``quote_reply_id`` is the platform message id (what the quote points
    # at); ``reply_to`` is an internal integer id and only a fallback.
    quoted_id = str(
        msg.get("quote_reply_id") or msg.get("reply_to") or ""
    ).strip()
    if not quoted_id:
        return ""

    def _remember_sticker(row_or_dict, sticker: str) -> None:
        sticker = str(sticker or "").strip().lower()
        if not sticker:
            return
        msg["_quoted_sticker_id"] = sticker
        try:
            from core.preprocess.emoji_recognition import get_emoji_description
            description = get_emoji_description(sticker)
        except Exception:
            warn("scheduler/main.py:1228 Exception", err=repr(__import__("sys").exc_info()[1]))
            description = ""
        if description:
            msg["_sticker_description"] = description

    def _path_of(row_or_dict) -> str:
        from core.conversation.media import is_media_message
        if isinstance(row_or_dict, dict):
            _remember_sticker(row_or_dict, extract_sticker_md5(row_or_dict))
            path = str(row_or_dict.get("media_path") or "").strip()
            if not path:
                return ""
            if is_media_message(
                row_or_dict.get("msg_type_name"),
                row_or_dict.get("content") or "",
                path,
            ):
                return path
            return ""
        _remember_sticker(row_or_dict, sticker_md5_from_message_row(row_or_dict))
        return media_path_from_message_row(row_or_dict)

    # Same sync batch first (the quoted image may not be flushed yet).
    for other in batch_messages or ():
        if not isinstance(other, dict):
            continue
        if str(other.get("msg_id") or "") != quoted_id:
            continue
        path = _path_of(other)
        if path:
            return path

    try:
        if (
            repo is not None
            and hasattr(repo, "messages")
            and hasattr(repo.messages, "find_by_platform_id")
        ):
            row = repo.messages.find_by_platform_id(quoted_id)
            path = _path_of(row)
            if path:
                return path
        # Older payloads only carry the internal reply_to integer.
        raw_reply = str(msg.get("reply_to") or "").strip()
        if (
            raw_reply.isdigit()
            and repo is not None
            and hasattr(repo, "messages")
            and hasattr(repo.messages, "get_by_id")
        ):
            row = repo.messages.get_by_id(int(raw_reply))
            path = _path_of(row)
            if path:
                return path
    except Exception as exc:
        log.debug("quoted_media_lookup_failed",
                  quoted_id=quoted_id, error=str(exc)[:120])
    return ""


def _authoritative_owner_ids(messages, account_id: str, default_platform: str = "") -> set[str]:
    """Owner *sender ids* for one sync batch, resolved by Authority only.

    This is not an authorization decision: it only tells the media-adjacency
    helper whose private bare-image turn should be attached.  It never reads
    ``config.get_all_owner_accounts`` / ``owner_wxids``; a batch that cannot be
    resolved deterministically simply produces an empty set.
    """
    owners: set[str] = set()
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        sender_id = str(msg.get("sender_id") or "")
        if not sender_id:
            continue
        principal = _principal_for_message(
            msg,
            account_id,
            platform=str(default_platform or msg.get("source") or "wechat"),
        )
        if principal is not None and principal.is_owner:
            owners.add(sender_id)
    return owners


@on(EventType.NEW_MESSAGES_SYNCED)
@_with_account_context
async def on_new_messages_claim_ledger(event: Event):
    """M2-4 claim 台账**写侧**：把"谁在何时自报/转述/推测了什么"真的落库。

    落点选在摄取链的入口（本事件 = 消息进得来的那一处），理由：

    * 这一批消息**带原始溯源**（``sender_id`` / ``chat_id`` / ``msg_id`` / ``is_self``），
      正是 ``claim_ledger`` 的溯源列要的东西——离源头最近，不需要第二套归属解析；
    * 与 ``person_observations`` 吃**同一批消息**（同一个 canonical handler 的入口），
      是既有 provenance 通道上的一个写口，不是第四份事实仓库。

    三条边界（照既有 ``on_new_messages_person_memory`` 的口径）：

    * **开关在 owner 模块**（``message_facts.ledger_tracing()``）：``off`` 时下面那个
      函数第一行就返回，**连 repo 都不取**（不查库、零 IO、逐字节一致）；
    * **后台队列**：记账不许挡住摄取与回复（写库是同步的 SQL，放主循环会拖慢同步轮次）；
    * **异常只记日志**：摄取链是消息进得来的唯一一条路，记账失败不许把它带走。
    """
    account_ctx = current_account_context()
    platform = str(getattr(event, "platform", "") or "")
    account_id = str(getattr(event, "account_id", "") or "")
    messages = (getattr(event, "payload", {}) or {}).get("messages", []) or []
    if not messages:
        return
    try:
        from core.terminal.background import submit_background_job
        from scheduler.person_memory import record_claims_for_messages

        async def _job() -> None:
            with get_runtime().activate(account_ctx):
                record_claims_for_messages(
                    messages, platform=platform, account_id=account_id,
                )

        if not submit_background_job("claim_ledger", _job):
            log.warning("claim_ledger_job_dropped", count=len(messages))
    except Exception as exc:  # noqa: BLE001 - 记账不许打断摄取
        log.warning("claim_ledger_handler_failed", error=str(exc)[:160])


@on(EventType.NEW_MESSAGES_SYNCED)
@_with_account_context
async def on_new_messages_person_memory(event: Event):
    # Shadow/opt-in extraction.  The core service and all prompts live in
    # core.people; this handler is only the scheduler wiring.
    account_ctx = current_account_context()
    try:
        from core.terminal.background import submit_background_job
        from scheduler.person_memory import process_new_messages

        async def _job() -> None:
            with get_runtime().activate(account_ctx):
                await process_new_messages(event)

        if not submit_background_job("person_memory", _job):
            log.warning("person_memory_job_dropped")
    except Exception as e:
        log.warning("person_memory_handler_failed", error=str(e)[:160])


async def _run_analysis_pipeline(
    *,
    messages: list,
    event: Event | None = None,
    account_ctx=None,
) -> None:
    """Track B5: analysis runs exclusively from the terminal event stream."""
    from core.analysis.workers import run_event_analysis_worker

    stats = await run_event_analysis_worker(
        messages=messages,
        account_ctx=account_ctx,
    )
    log.info("analysis_from_events_done", messages=len(messages or []), **stats)


@on(EventType.NEW_MESSAGES_SYNCED)
@_with_account_context
async def on_new_messages(event: Event):
    _update_idle_tracker(event)
    messages = event.payload.get("messages", [])
    if not messages:
        return

    # 2026-11-01（第 3 批）：分析完就到此为止 —— 回复由 loop/ 引擎负责。
    # 原来 persona.enabled 时还会调一次 `_route_and_reply`，可那个旧入口
    # 2026-10-24 就退役了（只 warn 一次然后 return None），这里的调用是
    # 纯空转，还顺带让 repo/llm/tools 三个取值活着陪它演。
    account_ctx = current_account_context()
    try:
        from core.terminal.background import submit_background_job

        queued = submit_background_job(
            "analysis_pipeline",
            lambda: _run_analysis_pipeline(
                messages=messages,
                account_ctx=account_ctx,
            ),
        )
        if not queued:
            log.warning("analysis_pipeline_dropped", count=len(messages))
    except Exception as exc:  # noqa: BLE001
        log.warning("analysis_pipeline_submit_failed", error=str(exc)[:160])


@on(EventType.DAILY_DIGEST_TRIGGER)
@_with_account_context
async def on_daily_digest(event: Event):
    from core.analysis.workers import run_daily_digest_worker

    stats = await run_daily_digest_worker()
    log.info("daily_digest_from_events", **stats)
    if stats.get("written"):
        send_notification("微信助理", "今日日报已生成")


@on(EventType.PROACTIVE_TRIGGER)
async def on_proactive_trigger(event: Event):
    """Explicit trigger path for agent/owner-initiated proactive contact.

    This does not schedule itself; when the future "when to speak" worker
    decides an opening exists, it publishes this event.  Whitelist and rate
    limits are enforced before the model turn and again inside OutboundGateway.
    """
    payload = dict(getattr(event, "payload", None) or {})
    platform = str(
        payload.get("platform") or getattr(event, "platform", "") or ""
    ).strip()
    account_id = str(
        payload.get("bot_account_id")
        or payload.get("account_id")
        or getattr(event, "account_id", "")
        or ""
    ).strip()
    chat_id = str(payload.get("chat_id") or "").strip()
    if not platform or not chat_id:
        log.warning("proactive_trigger_missing_target", payload_keys=sorted(payload))
        return
    try:
        from agents.proactive_trigger import run_proactive_turn

        result = await run_proactive_turn(
            platform=platform,
            bot_account_id=account_id,
            chat_id=chat_id,
            chat_type=str(payload.get("chat_type") or "private"),
            hint=str(payload.get("hint") or ""),
            source="event",
        )
        log.info("proactive_trigger_done", **result.as_dict())
    except Exception as exc:  # noqa: BLE001 - background event must not crash loop
        log.warning("proactive_trigger_failed", error=str(exc)[:200])


@on(EventType.KEY_EXPIRED)
def on_key_expired(event: Event):
    send_notification("微信助理", "微信密钥已失效，请重启微信后重新运行")


@on(EventType.ANALYSIS_COMPLETED)
def on_analysis_completed(event: Event):
    agent_name = event.payload.get("agent_name", "unknown")
    confidence = event.payload.get("confidence", 0)
    summary = event.payload.get("summary", "")

    if confidence >= 0.8 and summary:
        log.info("high_confidence_analysis", agent=agent_name, confidence=confidence)
        send_notification(
            "微信助理",
            f"[{agent_name}] {summary[:80]}",
        )


@on(EventType.DREAM_TRIGGER)
def on_dream_completed(event: Event):
    stats = event.payload.get("stats", {})
    if not stats:
        return
    parts = []
    if stats.get("merged"):
        parts.append(f"合并{stats['merged']}个线程")
    if stats.get("split"):
        parts.append(f"分裂{stats['split']}个线程")
    if stats.get("adopted"):
        parts.append(f"收养{stats['adopted']}个孤儿")
    if stats.get("upgraded"):
        parts.append(f"升级{stats['upgraded']}个摘要")
    if stats.get("consolidated"):
        parts.append(f"固化{stats['consolidated']}个记忆")
    if stats.get("rebalanced"):
        parts.append(f"重分配{stats['rebalanced']}个预算")
    if parts:
        log.info("dream_summary", summary="，".join(parts))


async def run_todo_overdue_check():
    repo = get_repo()
    overdue = repo.todos.get_overdue()
    if not overdue:
        return
    log.info("todo_overdue_check", count=len(overdue))
    for t in overdue[:5]:
        due_str = t.due_at.strftime("%m月%d日 %H:%M") if t.due_at else "未知"
        send_notification("待办提醒", f"「{t.title}」已逾期（截止 {due_str}）")

REMINDER_MAX_ATTEMPTS = 3


def _reminder_repeat_plan(repo, reminder) -> tuple[bool | None, datetime | None]:
    """权威侧回传：这一条投递成功后**是否仍会被重排** + 下一次提醒时间。

    判定就是 ``reminders.mark_done`` 内部那句 ``_calc_next_repeat(r.remind_at,
    r.repeat_rule)``（``db/repo_reminder.py``）：同一个函数、同一条行、同一份输入，
    所以观测侧拿到的是**权威侧的结论**，不是从 ``repeat_spec_json`` 反推的猜测。
    后者会把映射认不出的规则（``12小时`` / ``2.5h``）当成一次性 → 投影落 ``done``
    而 reminders 还在重复（P1-b）。

    返回 ``(None, None)`` = 问不到结论（替身 repo / 未来换实现）：观测侧按「可能仍
    会重复」保守处理，绝不写 ``done``。本函数是 total 的：任何异常都吞掉并记日志，
    绝不让探针影响投递。
    """
    try:
        rule = str(getattr(reminder, "repeat_rule", "") or "").strip()
        if not rule:
            return False, None          # 没有重复规则：mark_done 必置 done
        calc = getattr(getattr(repo, "reminders", None), "_calc_next_repeat", None)
        if not callable(calc):
            return None, None
        next_at = calc(getattr(reminder, "remind_at", None), rule)
    except Exception as exc:  # noqa: BLE001 - 探针失败一律按「未知」处理
        log.warning("reminder_repeat_probe_failed", error=str(exc)[:160])
        return None, None
    return (next_at is not None), (next_at if isinstance(next_at, datetime) else None)


def _work_item_run_started(repo, reminder) -> dict | None:
    """P1 投递观测（§4.2：一次投递 = 一次 run）。

    **只观测，不改投递**：flag 关、台账行不存在、写库失败都只留日志并返回 ``None``，
    ``run_reminder_delivery`` 的重试与状态判定完全不受影响。
    """
    try:
        from core.tasks import store

        return store.record_run_started(
            repo,
            legacy_reminder_id=getattr(reminder, "id", None),
            trigger_source="timer",
        )
    except Exception as exc:  # noqa: BLE001 - 观测失败不影响投递
        log.warning("work_item_write_failed", error=str(exc)[:160])
        return None


def _work_item_run_finished(
    repo, run, *, sent: bool, reason: str, attempts: int, reply_text: str = "",
    repeat_continues: bool | None = None, repeat_due_at: datetime | None = None,
) -> None:
    """P1 投递结果观测：镜像 outcome / 尝试次数 / 失败分类（同样 best-effort）。

    ``repeat_continues`` / ``repeat_due_at`` 是权威侧回传的「是否继续 + 下一次时间」
    （见 :func:`_reminder_repeat_plan`）：观测侧不自己判断重复口径。
    """
    if not run:
        return
    try:
        from core.tasks import store

        store.record_run_finished(
            repo,
            run,
            outcome="sent" if sent else "failed",
            reply_text=reply_text,
            error_class="" if sent else "transient",
            error_message="" if sent else str(reason or ""),
            attempts=int(attempts or 0),
            repeat_continues=repeat_continues,
            repeat_due_at=repeat_due_at,
        )
    except Exception as exc:  # noqa: BLE001 - 观测失败不影响投递
        log.warning("work_item_write_failed", error=str(exc)[:160])


@dataclass(frozen=True)
class _ReminderDelivery:
    """一次投递的结论（扫描 job 与意图 tick 共用同一条实现）。"""

    sent: bool
    reason: str = ""
    reply_text: str = ""
    attempts: int = 0
    #: True = 权威状态已终局（mark_done / mark_failed），调用方不必再调度。
    terminal: bool = False
    repeat_continues: bool | None = None
    next_due_at: datetime | None = None


def _reminder_scope(row) -> Any:
    """提醒行 → 会话坐标；缺任一标识返回 None（不猜）。"""
    from core.context.cognitive_commit import Scope

    platform = str(getattr(row, "source_platform", "") or "")
    account = str(getattr(row, "source_account_id", "") or "")
    chat = str(getattr(row, "source_chat_id", "") or "")
    if not (platform and account and chat):
        return None
    chat_type = str(getattr(row, "source_chat_type", "") or "private")
    return Scope(
        platform=platform,
        account_id=account,
        chat_id=chat,
        chat_type=chat_type,
        group_id=chat if chat_type == "group" else "",
    )


async def _deliver_reminder(repo, r, *, trigger_source: str = "timer") -> _ReminderDelivery:
    """投递一条提醒（唯一实现点；``run_reminder_delivery`` 与意图 tick 都走这里）。

    与旧循环体逐字同源：空内容 / 缺地址 / act 未接 → mark_failed（缺地址与 act
    另推桌面通知）；发送成功 → mark_done（重复规则由权威侧顺延）；失败 → 记
    attempts，到 ``REMINDER_MAX_ATTEMPTS`` 止损 mark_failed + 通知。
    ``trigger_source`` 只进 work_item 观测，不影响投递判据。
    """
    content = str(r.content or "").strip()
    if not content:
        repo.reminders.mark_failed(r.id)
        log.warning("reminder_empty_content", reminder_id=r.id)
        return _ReminderDelivery(sent=False, reason="empty_content", terminal=True)
    if not r.source_chat_id:
        # 修复前创建的记录没有地址，补投不了：标记失败并告警，不静默。
        repo.reminders.mark_failed(r.id)
        log.warning("reminder_missing_source", reminder_id=r.id)
        send_notification(
            "提醒无法送达", f"「{content}」创建时没有记录会话，无法送达",
        )
        return _ReminderDelivery(sent=False, reason="missing_source", terminal=True)
    if (getattr(r, "purpose", "") or "remind") == "act":
        # 字段先留、执行器后接：不允许把 act 当提醒悄悄发出去。
        repo.reminders.mark_failed(r.id)
        log.warning("reminder_act_executor_missing", reminder_id=r.id)
        send_notification(
            "任务无法执行",
            f"「{content}」是到点执行类任务，执行器还没接，已标记失败",
        )
        return _ReminderDelivery(sent=False, reason="act_executor_missing", terminal=True)

    from agents.reminder_trigger import parse_mention_names, run_reminder_turn

    mention_names = (
        parse_mention_names(getattr(r, "mention_names", None))
        or parse_mention_names(getattr(r, "mention_name", None))
    )
    attempts = repo.reminders.record_attempt(r.id)
    # P1 观测：run_started 在投递之前落，run_finished 在两个分支各落一次；
    # 三个调用点都自带 try/except（P2-d）：观测器异常绝不影响投递。
    try:
        work_run = _work_item_run_started(repo, r)
    except Exception as exc:  # noqa: BLE001 - 观测失败不影响投递
        log.warning("work_item_write_failed", error=str(exc)[:160])
        work_run = None
    reply_text = ""
    try:
        result = await run_reminder_turn(
            platform=r.source_platform or "wechat",
            bot_account_id=r.source_account_id or "",
            chat_id=r.source_chat_id,
            chat_type=r.source_chat_type or "private",
            content=content,
            mention_names=mention_names,
        )
        sent = bool(result.sent)
        reason = str(result.reason or "")
        reply_text = str(getattr(result, "reply", "") or "")
    except Exception as exc:  # noqa: BLE001 - job must not kill the loop
        warn("scheduler/main.py:1706 Exception", err=repr(exc))
        sent = False
        reason = f"exception: {str(exc)[:120]}"

    if sent:
        # 权威侧回传「是否继续」：必须在 mark_done 之前取，输入（remind_at /
        # repeat_rule）与 mark_done 读到的同一份；mark_done 后行已顺延。
        repeat_continues, repeat_due_at = _reminder_repeat_plan(repo, r)
        repo.reminders.mark_done(r.id)
        try:
            _work_item_run_finished(
                repo, work_run, sent=True, reason=reason,
                attempts=attempts, reply_text=reply_text,
                repeat_continues=repeat_continues, repeat_due_at=repeat_due_at,
            )
        except Exception as exc:  # noqa: BLE001 - 观测失败不影响投递
            log.warning("work_item_write_failed", error=str(exc)[:160])
        log.info("reminder_delivered", reminder_id=r.id,
                 attempts=attempts, reason=reason or "sent")
        return _ReminderDelivery(
            sent=True, reason=reason, reply_text=reply_text, attempts=attempts,
            terminal=True, repeat_continues=repeat_continues,
            next_due_at=repeat_due_at if repeat_continues else None,
        )

    log.warning("reminder_delivery_failed", reminder_id=r.id,
                attempts=attempts, reason=reason)
    try:
        _work_item_run_finished(
            repo, work_run, sent=False, reason=reason, attempts=attempts,
        )
    except Exception as exc:  # noqa: BLE001 - 观测失败不影响投递
        log.warning("work_item_write_failed", error=str(exc)[:160])
    if attempts >= REMINDER_MAX_ATTEMPTS:
        repo.reminders.mark_failed(r.id)
        send_notification(
            "提醒送达失败",
            f"「{content}」重试 {attempts} 次仍未送达（{reason}）",
        )
        return _ReminderDelivery(
            sent=False, reason=reason, reply_text=reply_text,
            attempts=attempts, terminal=True,
        )
    return _ReminderDelivery(
        sent=False, reason=reason, reply_text=reply_text,
        attempts=attempts, terminal=False,
    )


#: 到点意图超过这个时限仍 open → 视为 tick 停摆，扫描器立刻兜底投递（不静默积压）。
#: 正常重试周期（3 次 × 1 分钟）远小于它，不会抢 tick 的活。
INTENTION_CLAIM_STALE_AFTER_SECONDS = 600


def _reminder_claimed_by_intention(reminder) -> bool:
    """``INTENTION_TICK=on`` 且该提醒仍有**新鲜** open 意图 → 投递由 tick 独占。

    读不到世界模型 / 检查异常 / 意图已超时未收口，一律返回 False（fail open）：
    宁可扫描器多投一次，也不能让提醒因为意图台账坏了或 tick 停摆而静默不投。
    """
    try:
        from core.context.intention_bridge import (
            open_reminder_intention_keys,
            parse_reminder_key,
        )
        from core.context.intention_tick import tick_mode

        if tick_mode() != features.MODE_ON:
            return False
        keys = open_reminder_intention_keys(
            getattr(reminder, "source_chat_id", ""), getattr(reminder, "id", None),
        )
        if not keys:
            return False
        cutoff = (
            datetime.now() - timedelta(seconds=INTENTION_CLAIM_STALE_AFTER_SECONDS)
        ).strftime("%Y-%m-%d %H:%M")
        return any(parse_reminder_key(key)[1] >= cutoff for key in keys)
    except Exception as exc:  # noqa: BLE001
        log.warning("intention_claim_check_failed", error=str(exc)[:160])
        return False


async def run_reminder_delivery():
    """把到点的提醒送回它被创建时所在的会话。

    2026-09-28：在此之前 reminders 只落库、没有送达目标，create_reminder
    能承诺"到点提醒"却必然静默。现在每条提醒自带来源会话，这里每分钟扫
    一次：成功 → mark_done（重复规则顺延）；失败 → 记一次尝试，到止损线
    置 failed 并推桌面通知，不让坏地址无限重扫。

    2026-10-12（Step 12）：``INTENTION_TICK=on`` 且这条提醒还有 open 意图时，
    投递让位给意图 tick（同一条 :func:`_deliver_reminder`，不产生第二条投递链）；
    其余情况（默认 off / shadow / 没有意图）仍由本 job 投递。
    """
    repo = get_repo()
    try:
        due = repo.reminders.list_pending()
    except Exception as exc:  # noqa: BLE001 - scanner must not kill the loop
        log.warning("reminder_scan_failed", error=str(exc)[:160])
        return
    if not due:
        return

    from core.context.intention_bridge import reminder_due_text as _due_text

    for r in list(due)[:20]:
        if _reminder_claimed_by_intention(r):
            log.info(
                "reminder_delivery_deferred_to_intention",
                reminder_id=getattr(r, "id", None),
                chat_id=str(getattr(r, "source_chat_id", "") or ""),
            )
            continue
        # 投递前记下「这一跳」的时刻：重复提醒 mark_done 后行会顺延，
        # 事后取不到原值，执行证据只能靠它认。
        _due_before = _due_text(getattr(r, "remind_at", ""))
        delivery = await _deliver_reminder(repo, r)
        if delivery.terminal:
            # 权威到终局 → 关掉镜像意图（shadow 投递 / tick 异常后的兜底路径）。
            # on 且意图还 open 时本循环已让位，所以这里不会和 tick 抢同一条。
            try:
                from core.context.intention_bridge import close_reminder_intention

                close_reminder_intention(
                    getattr(r, "source_chat_id", ""), getattr(r, "id", None),
                    at=datetime.now().strftime("%Y-%m-%d %H:%M"),
                    result=f"delivered_by_scanner:{delivery.reason or 'sent'}",
                    evidence_ref=(("reminder:%s@%s" % (getattr(r, "id", ""), _due_before))
                                  if _due_before else ("reminder:%s" % getattr(r, "id", ""))),
                    scope=_reminder_scope(r),
                )
            except Exception as exc:  # noqa: BLE001 - 收口失败不影响投递
                log.warning("intention_close_failed", error=str(exc)[:160])


def _reminder_by_id(repo, reminder_id):
    """按 id 读一条提醒；替身没有 ``get()`` 时退回 ``list_all`` 扫描。"""
    reminders = getattr(repo, "reminders", None)
    getter = getattr(reminders, "get", None)
    if callable(getter):
        return getter(reminder_id)
    lister = getattr(reminders, "list_all", None)
    if callable(lister):
        for row in lister(limit=200):
            if getattr(row, "id", None) == reminder_id:
                return row
    return None


async def _execute_reminder_intention(chat_id: str, intent):
    """意图 tick 的 reminder 执行器：复用唯一投递实现，把结论翻译成重试策略。

    * reminder 已 done → 成功关闭（可能是扫描器兜底投过了，绝不重发）；
    * 可重试失败 → ``retryable=True`` 留 open，下一 tick 重试；
    * 到止损线 → ``_deliver_reminder`` 已 mark_failed，本函数给终局结论关闭；
    * 重复提醒成功 → 按下一到点时刻另开新 key（旧 key 由 run_due_retryable 关闭）。
    """
    from core.context.intention_bridge import open_reminder_intention, parse_reminder_key
    from core.context.intention_runner import IntentionExecution
    from core.context.intention_tick import tick_now_text

    reminder_id, _due = parse_reminder_key(getattr(intent, "key", ""))
    kind = str(getattr(intent, "kind", "") or "")
    if reminder_id is None:
        log.warning("intention_no_executor",
                    key=str(getattr(intent, "key", "") or ""), kind=kind)
        return IntentionExecution(ok=False, result=f"no_executor:{kind or 'unknown'}")
    repo = get_repo()
    row = _reminder_by_id(repo, reminder_id)
    if row is None:
        log.warning("intention_reminder_missing", reminder_id=reminder_id, chat_id=chat_id)
        return IntentionExecution(ok=False, result=f"reminder_missing:{reminder_id}")
    status = str(getattr(row, "status", "") or "")
    # 执行事实的可引用 id：提醒行 + 这一次的到点时刻。关闭时引用它，
    # 不拿「一句话」或空串凑证据；带 @due 才分得清「重复提醒这一跳投过」
    # 与「行已被顺延到下一次」。
    evidence_ref = ("reminder:%s@%s" % (reminder_id, _due)) if _due else ("reminder:%s" % reminder_id)
    if status == "done":
        return IntentionExecution(ok=True, result="already_done", evidence_ref=evidence_ref)
    if status == "failed":
        return IntentionExecution(ok=False, result="already_failed", evidence_ref=evidence_ref)
    if status != "pending":
        return IntentionExecution(
            ok=False, result=f"reminder_status:{status}", evidence_ref=evidence_ref
        )

    remind_at = getattr(row, "remind_at", None)
    if isinstance(remind_at, datetime):
        if remind_at.strftime("%Y-%m-%d %H:%M") > tick_now_text():
            # 权威行已被顺延到未来（重复提醒等）而意图已到点：本轮不投，等下一 tick。
            return IntentionExecution(
                ok=False, result="not_due_yet", retryable=True,
                attempts=int(getattr(row, "attempts", 0) or 0),
            )

    outcome = await _deliver_reminder(repo, row, trigger_source="timer")
    if not outcome.sent:
        return IntentionExecution(
            ok=False, result=outcome.reason or "failed",
            retryable=not outcome.terminal, attempts=outcome.attempts,
            evidence_ref=evidence_ref if outcome.terminal else "",
        )
    if outcome.next_due_at is not None:
        try:
            open_reminder_intention(
                chat_id=chat_id,
                reminder_id=reminder_id,
                due_at=outcome.next_due_at,
                evidence=str(getattr(row, "content", "") or ""),
                at=tick_now_text(),
                scope=_reminder_scope(row),
                source_refs=tuple(getattr(intent, "evidence_refs", ()) or ()),
            )
        except Exception as exc:  # noqa: BLE001 - 下一次登记失败不吞掉本次成功
            log.warning("intention_repeat_reschedule_failed", error=str(exc)[:160])
    return IntentionExecution(
        ok=True, result=outcome.reason or "sent", evidence_ref=evidence_ref
    )


async def _cancel_revoked_reminder(intent):
    """Intention → reminder 权威行取消；返回 False 时意图继续占用以防补发。"""
    from core.context.intention_bridge import parse_reminder_key

    reminder_id, _due = parse_reminder_key(getattr(intent, "key", ""))
    if reminder_id is None:
        return True
    repo = get_repo()
    reminders = getattr(repo, "reminders", None)
    cancel = getattr(reminders, "cancel", None)
    if not callable(cancel):
        return False
    return bool(cancel(reminder_id))


async def run_intention_tick():
    """调度 job（每分钟）：到点意图 → 执行 → 关闭 / 重试（群聊v2 Step 12）。

    flag 关时 :func:`core.context.intention_tick.run_tick` 立即返回，零扫描零执行。
    """
    from core.context.intention_tick import run_tick

    try:
        return await run_tick(
            _execute_reminder_intention,
            on_source_revoked=_cancel_revoked_reminder,
        )
    except Exception as exc:  # noqa: BLE001 - job 不拖垮调度循环
        log.warning("intention_tick_failed", error=str(exc)[:160])
        return None


async def run_consolidation() -> None:
    """巩固环（批D Step 38）：高显著情节幂等写入长期记忆；off 零动作。"""
    try:
        from core.quality.consolidation import run_consolidation as _run

        await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001 - 后台任务不得拖垓调度
        log.warning("consolidation_failed", error=str(exc)[:160])


# 投影落后多少条事件就告警（每 5 分钟一批 200 条，正常滞后远小于此值）
PROJECTION_LAG_WARN = 400
_projection_lag_notified_at = 0.0


def _projection_lag() -> tuple[int, int]:
    """返回 (cursor, 最新事件序号)：投影落后 = 两者之差。"""
    try:
        from sqlalchemy import text as _sql

        from core.terminal.store_port import get_meta_repo

        with get_meta_repo().engine.connect() as conn:
            cursor = conn.execute(
                _sql(
                    "select last_sequence from memory_projection_cursors "
                    "where key = 'terminal_event'"
                )
            ).scalar()
            newest = conn.execute(_sql("select max(sequence) from terminal_events")).scalar()
        return int(cursor or 0), int(newest or 0)
    except Exception:
        warn("scheduler/main.py:1972 Exception", err=repr(__import__("sys").exc_info()[1]))
        return 0, 0


async def run_memory_projection():
    """Project a bounded batch of terminal events into memory projections.

    投影不只是"翻旧账"用的：群名单块里的班级/职务细节、纠错学习找目标、
    名单同步、审计导出都读它（见 docs/记忆的定义.md §9）。所以它只加滞后告警，
    未迁移的作业不降频、不停。有效持久化停写闸表示旧投影已退役，
    此时保留游标，也不再将停止投影视作滞后故障。
    """
    global _projection_lag_notified_at
    try:
        from core.terminal.memory_runtime import project_pending_memory

        stats = await asyncio.to_thread(project_pending_memory, limit=200)
        if not stats:
            return
        if stats.get("status") == "retired":
            log.info("memory_projection_retired", reason=stats.get("reason"), cursor=stats.get("cursor", 0))
            return
        if stats.get("projected"):
            log.info(
                "memory_projection_done",
                scanned=stats.get("scanned", 0),
                projected=stats.get("projected", 0),
                cursor=stats.get("cursor", 0),
            )
        elif stats.get("error"):
            log.warning("memory_projection_failed", error=stats.get("error"))

        cursor, newest = _projection_lag()
        lag = max(0, newest - cursor)
        if lag > PROJECTION_LAG_WARN:
            log.warning("memory_projection_lagging", lag=lag, cursor=cursor, newest=newest)
            now = time.time()
            if now - _projection_lag_notified_at > 3600:
                _projection_lag_notified_at = now
                _notify(
                    "微信助理",
                    f"警告：记忆投影落后 {lag} 条事件（cursor={cursor} / 最新={newest}）。"
                    "名单细节、纠错学习、名单同步会滞后。",
                )
    except Exception as exc:  # noqa: BLE001 - background job must not break loop
        log.warning("memory_projection_failed", error=str(exc)[:160])


# 沉淀提案（P2，docs/记忆的定义.md §3）：每天最多一次，只写 pending 等人审
_MEMORY_PROPOSAL_INTERVAL_HOURS = 20
_MEMORY_PROPOSAL_MAX_CHATS = 5


def _last_proposal_at():
    from sqlalchemy import text as _sql

    from core.terminal.store_port import get_meta_repo

    try:
        with get_meta_repo().engine.connect() as conn:
            return conn.execute(
                _sql("select max(created_at) from memory_proposals")
            ).scalar()
    except Exception:
        warn("scheduler/main.py:2031 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


def _active_chats(hours: int = 24, min_events: int = 5, limit: int = _MEMORY_PROPOSAL_MAX_CHATS):
    """最近活跃、消息量够的会话（按事件数降序）。"""
    from sqlalchemy import text as _sql

    from core.terminal.store_port import get_meta_repo

    try:
        with get_meta_repo().engine.connect() as conn:
            rows = conn.execute(
                _sql(
                    "select chat_id, count(*) n from terminal_events "
                    "where occurred_at >= datetime('now', 'localtime', :since) "
                    "and event_type in ('message','question','request') "
                    "group by chat_id having n >= :min_events "
                    "order by n desc limit :limit"
                ),
                {
                    "since": f"-{max(1, int(hours))} hours",
                    "min_events": max(1, int(min_events)),
                    "limit": max(1, int(limit)),
                },
            ).mappings().all()
        return [str(r["chat_id"]) for r in rows if str(r["chat_id"] or "")]
    except Exception:
        warn("scheduler/main.py:2058 Exception", err=repr(__import__("sys").exc_info()[1]))
        return []


async def run_memory_proposals() -> None:
    """把最近活跃会话抽成"沉淀候选"（pending），等人 approve/reject。

    每天最多跑一次：用库里的最后一次提案时间做幂等，机器半夜关机也不会漏。
    只写 memory_proposals（提案表），**不写任何记忆**——生效仍要主人点头。
    """
    try:
        from datetime import datetime, timedelta

        last = _last_proposal_at()
        if last:
            try:
                when = datetime.fromisoformat(str(last))
                if datetime.now() - when < timedelta(hours=_MEMORY_PROPOSAL_INTERVAL_HOURS):
                    return
            except ValueError:
                warn("scheduler/main.py:2077 ValueError", err=repr(__import__("sys").exc_info()[1]))

        # 批3b：反复被检索命中的群术语 → lexicon 提案（只写 pending；开关默认关）
        try:
            from core import features
            from core.quality.memory_sinking import propose_sinking

            if features.enabled(features.MEMORY_SINKING, False):
                sink_stats = propose_sinking(days=14)
                if sink_stats.get("staged"):
                    log.info("memory_sinking_done", **sink_stats)
        except Exception as exc:  # noqa: BLE001
            log.warning("memory_sinking_schedule_failed", error=str(exc)[:160])

        chats = _active_chats()
        if not chats:
            log.info("memory_proposals_scan", chats=0, why="no_active_chat")
            return
        from core.bootstrap import get_llm
        from core.quality.memory_proposals import propose_from_chat

        llm = get_llm()
        stats: dict[str, int] = {"chats": 0, "staged": 0}
        # 2026-11-22：以前只有 staged>0 才留痕，于是「这条链三周没产出」既没有报错也没有
        # 任何读数，只能靠猜。现在每轮都记一次扫描量（会话数/事件数/模型给的候选数）。
        scan = {"chats_seen": 0, "events": 0, "raw": 0, "skipped": 0}
        for chat_id in chats:
            result = await propose_from_chat(chat_id, hours=24, llm=llm)
            scan["chats_seen"] += 1
            for key in ("events", "raw", "skipped"):
                scan[key] += int(result.get(key) or 0)
            if result.get("staged"):
                stats["chats"] += 1
                stats["staged"] += int(result["staged"])
        log.info("memory_proposals_scan", **scan)
        if stats["staged"]:
            log.info("memory_proposals_done", **stats)
            _notify(
                "微信助理",
                f"记忆提案：新增 {stats['staged']} 条候选（{stats['chats']} 个会话），"
                "等你审：python scripts\\memory_proposals.py --list",
            )
    except Exception as exc:  # noqa: BLE001 - background job must not break loop
        log.warning("memory_proposals_failed", error=str(exc)[:160])


async def run_correction_learning() -> None:
    """Layer-2 follow-up: owner corrections become memory writes/invalidations."""
    from core import features

    if not features.enabled(features.CORRECTION_LEARNING, True):
        return
    try:
        from core.terminal.store_port import get_meta_repo
        from core.bootstrap import get_llm
        from core.quality.corrections import process_pending_corrections

        stats = await process_pending_corrections(
            meta_repo=get_meta_repo(), llm=get_llm()
        )
        if stats.get("scanned"):
            log.info("correction_learning_done", **stats)
    except Exception as exc:  # noqa: BLE001
        log.warning("correction_learning_failed", error=str(exc)[:160])


async def run_roster_sync() -> None:
    """Owner-confirmed roster -> per-person group entries + owner-only canonical."""
    try:
        from core.terminal.store_port import get_meta_repo
        from core.quality.roster_sync import process_pending_roster_confirms

        stats = await process_pending_roster_confirms(
            meta_repo=get_meta_repo()
        )
        if stats.get("scanned"):
            log.info("roster_sync_done", **stats)
    except Exception as exc:  # noqa: BLE001
        log.warning("roster_sync_failed", error=str(exc)[:160])


async def run_proactive_decision_scan() -> None:
    """Phase 4/5: run one automatic proactive-opportunity scan.

    The scan is cheap and deterministic.  When it publishes
    ``PROACTIVE_TRIGGER``, this coroutine waits for the trigger turn(s) to
    finish before returning.  That matters because the scheduler loop only runs
    while an ``_run_async(...)`` call is active; a fire-and-forget background
    job gets frozen between ticks and the model turn never reaches
    ``proactive_trigger_done``.
    """
    try:
        from core.terminal.proactive_decision import (
            get_proactive_decision_worker,
        )

        worker = get_proactive_decision_worker()
        if worker is None:
            log.warning("proactive_decision_worker_unavailable")
            return
        result = await worker.run_cycle()
        if result.get("dispatched"):
            log.info("proactive_decision_cycle_done", **{
                key: value for key, value in result.items()
                if key != "opportunities"
            })
    except Exception as exc:  # noqa: BLE001 - scheduler loop must keep going
        log.warning("proactive_decision_scan_failed", error=str(exc)[:160])


# REC-1/REC-2（2026-09-29）：outbox drain 与对账器接进调度周期。
# 在此之前 drain 只在同步路径触发（同步没有新消息 → pending 永远不销账），
# 对账器只有手工 CLI（跨库丢事件要等人想起来才会被发现）。
TERMINAL_RECONCILE_WINDOW_HOURS = 24
TERMINAL_RECONCILE_MAX_MESSAGES = 2000
TERMINAL_RECONCILE_PAGE_SIZE = 500
# 缺口告警去重窗口：同一段（同 PROJECTION_LAG_WARN 的写法）避免周期 job 刷屏。
TERMINAL_RECONCILE_ALERT_INTERVAL = 3600
# drain 缺口告警同样一小时去重（同一分钟一跑，不刷屏）。
OUTBOX_ALERT_INTERVAL = 3600
_terminal_reconcile_notified_at = 0.0
_outbox_notified_at = 0.0
_OUTBOX_COUNTERS = ("picked", "delivered", "retried", "failed", "missing_message")
_RECONCILE_COUNTERS = (
    "scanned", "matched", "missing", "inserted", "failed", "skipped_no_msg_id",
)
# 注册期只读一次 flag（schedule_jobs），免重启承诺还得靠协程体每轮再判一次；
# 这里记「已经记过日志的 job」，只在 开→关 切换时打一行，避免每分钟刷屏。
_jobs_disabled_logged: set[str] = set()

# REC-1/REC-2 两个 job「默认开」的**唯一来源**：注册期（schedule_jobs）与运行期
# （协程体开头的 _job_disabled_by_flag）都读它。两处各自硬编码 True 是 shotgun
# surgery：改默认值要同时改几处、漏一处就出现"注册了却每轮自我关闭"或反过来。
_JOBS_ON_BY_DEFAULT = frozenset({
    features.OUTBOX_MAINTENANCE,
    features.TERMINAL_RECONCILE,
})


def _job_disabled_by_flag(job: str, flag: str, default: bool) -> bool:
    """运行期判 flag：True 表示本 job 已关闭，调用方直接 return。

    ``default`` 由调用方按 :data:`_JOBS_ON_BY_DEFAULT` 显式给（**必填**：默认值
    硬编码在签名里就等于又开了一处来源）。True 表示这个 flag 是"默认开"的 job。
    """
    if features.enabled(flag, default):
        _jobs_disabled_logged.discard(job)   # 重新打开：清记录，下次关闭再记一行
        return False
    if job not in _jobs_disabled_logged:
        _jobs_disabled_logged.add(job)
        log.info("job_disabled_by_flag", job=job, flag=flag)
    return True


def _outbox_counts_or_empty(account_repo: Any) -> dict:
    """取一个账号库的 outbox 计数（REC-3）；失败只记日志，不拖垮 drain。"""
    from db.outbox_store import outbox_status_counts

    try:
        return outbox_status_counts(account_repo.engine, now=datetime.now())
    except Exception as exc:  # noqa: BLE001 - 指标失败不能停掉维护
        log.warning("outbox_status_failed", error=str(exc)[:160])
        return {}


def _drain_outbox_all_accounts() -> dict:
    """（同步）逐账号 drain outbox；单账号异常只记日志，不拖累其它账号。"""
    from core.terminal.event_runtime import (
        get_event_identity_service,
        get_terminal_event_store,
    )
    from core.terminal.outbox import drain_outbox

    totals = {"accounts": 0, "picked": 0, "delivered": 0, "retried": 0,
              "failed": 0, "missing_message": 0, "inflight": 0, "stale": 0,
              "gaps": []}
    store = get_terminal_event_store(force=True)
    if store is None:
        log.warning("outbox_maintenance_no_store")
        return totals
    identity_service = get_event_identity_service()
    for ctx in get_runtime().all_contexts():
        try:
            report = drain_outbox(
                repo=ctx.account_repo,
                platform=ctx.platform,
                account_id=ctx.account_id,
                store=store,
                identity_service=identity_service,
            )
        except Exception as exc:  # noqa: BLE001 - 单账号失败不能停掉维护
            log.warning("outbox_drain_failed", platform=ctx.platform,
                        account_id=ctx.account_id, error=str(exc)[:160])
            continue
        totals["accounts"] += 1
        for key in _OUTBOX_COUNTERS:
            totals[key] += int(getattr(report, key, 0) or 0)
        # drain 之后的落库快照（REC-3/REC-8）：inflight>0 = 另一条路径正持有
        # 租约（本地此刻正常），stale>0 = 租约过期没人收尾（要人看）。
        counts = _outbox_counts_or_empty(ctx.account_repo)
        for key in ("inflight", "stale"):
            totals[key] += int(counts.get(key) or 0)
        if report.failed or report.missing_message or counts.get("stale"):
            # 告警文案要能指到账号：总量之外留下是谁的缺口。
            totals["gaps"].append({
                "platform": ctx.platform,
                "account_id": ctx.account_id,
                "failed": int(report.failed or 0),
                "missing_message": int(report.missing_message or 0),
                "inflight": int(counts.get("inflight") or 0),
                "stale": int(counts.get("stale") or 0),
            })
    return totals


async def run_outbox_maintenance() -> None:
    """REC-1：周期 drain，让「同步没有新消息」时的 pending 行也能被销账。

    与同步流水线互斥（``_sync_pipeline_running``）**只是省开销**：这个进程内
    粗判有竞态窗口（判过之后再 ``to_thread``），窗口里两条路径会同时 drain。
    正确性由 REC-8 的行级 claim/租约保证——``attempts`` 在夺取时原子 +1、同一
    行只有一个执行者能投递，跳过与否都不影响计数。
    """
    global _outbox_notified_at
    if _job_disabled_by_flag("outbox_maintenance", features.OUTBOX_MAINTENANCE,
                             features.OUTBOX_MAINTENANCE in _JOBS_ON_BY_DEFAULT):
        return
    if _sync_pipeline_running:
        log.info("outbox_maintenance_skipped", reason="sync_running")
        return
    try:
        totals = await asyncio.to_thread(_drain_outbox_all_accounts)
    except Exception as exc:  # noqa: BLE001 - 周期 job 不能打断调度循环
        log.warning("outbox_maintenance_failed", error=str(exc)[:160])
        return
    if totals.get("picked"):
        # 无事可做时不打 info：1 分钟一跑，日志已经被它涨到 16MB。
        log.info("outbox_maintenance_done", **totals)
    failed = int(totals.get("failed") or 0)
    missing_message = int(totals.get("missing_message") or 0)
    inflight = int(totals.get("inflight") or 0)
    stale = int(totals.get("stale") or 0)
    # stale 不是新的通知触发条件（2026-09-29 决定：它只进 /api/status 与 doctor），
    # 但缺口文案里带上它——收到 failed/missing_message 告警时能一眼看到租约过期。
    if not (failed or missing_message):
        return
    gaps = list(totals.get("gaps") or [])
    log.warning("outbox_maintenance_gaps", failed=failed,
                missing_message=missing_message, inflight=inflight,
                stale=stale, gaps=gaps)
    now = time.time()
    if now - _outbox_notified_at <= OUTBOX_ALERT_INTERVAL:
        return
    detail = "、".join(
        f"{gap['platform']}/{gap['account_id']}（failed={gap['failed']}, "
        f"missing_message={gap['missing_message']}, "
        f"inflight={gap.get('inflight', 0)}, stale={gap.get('stale', 0)}）"
        for gap in gaps[:5]
    ) or "账号明细缺失"
    if _notify(
        "微信助理",
        f"警告：事件 outbox 有缺口——投递失败 {failed} 行、消息缺失 "
        f"{missing_message} 行、在投 {inflight} 行、租约过期 {stale} 行：{detail}。"
        f"事件流存在缺口，需要人工排查。",
    ) is not False:
        # 时间戳放在调用之后：推送确认失败时不能吃掉 1 小时去重窗口。
        _outbox_notified_at = time.time()


def _reconcile_all_accounts(*, apply: bool) -> dict:
    """（同步）逐账号对账。窗口固定近 24h：周期 job 是止血，不是考古。"""
    from core.terminal.event_runtime import (
        get_event_identity_service,
        get_terminal_event_store,
    )
    from core.terminal.reconciler import reconcile_account

    totals = {"accounts": 0, "scanned": 0, "matched": 0, "missing": 0,
              "inserted": 0, "failed": 0, "skipped_no_msg_id": 0}
    store = get_terminal_event_store(force=True)
    if store is None:
        log.warning("terminal_reconcile_no_store")
        return totals
    identity_service = get_event_identity_service()
    since = datetime.now() - timedelta(hours=TERMINAL_RECONCILE_WINDOW_HOURS)
    for ctx in get_runtime().all_contexts():
        try:
            report = reconcile_account(
                repo=ctx.account_repo,
                platform=ctx.platform,
                account_id=ctx.account_id,
                store=store,
                identity_service=identity_service,
                apply=apply,
                since=since,
                page_size=TERMINAL_RECONCILE_PAGE_SIZE,
                max_messages=TERMINAL_RECONCILE_MAX_MESSAGES,
            )
        except Exception as exc:  # noqa: BLE001 - 单账号失败不能停掉对账
            log.warning("terminal_reconcile_failed", platform=ctx.platform,
                        account_id=ctx.account_id, error=str(exc)[:160])
            continue
        totals["accounts"] += 1
        for key in _RECONCILE_COUNTERS:
            totals[key] += int(getattr(report, key, 0) or 0)
        if report.scanned >= TERMINAL_RECONCILE_MAX_MESSAGES:
            # 打满上限 = 窗口最旧那段没扫到（分页是「新→旧」），不能静默截断。
            log.warning(
                "terminal_reconcile_truncated",
                platform=ctx.platform,
                account_id=ctx.account_id,
                scanned=report.scanned,
                max_messages=TERMINAL_RECONCILE_MAX_MESSAGES,
                window_hours=TERMINAL_RECONCILE_WINDOW_HOURS,
                since=since.isoformat(),
            )
        if report.missing:
            log.warning(
                "terminal_reconcile_gaps",
                platform=ctx.platform,
                account_id=ctx.account_id,
                scanned=report.scanned,
                missing=report.missing,
                inserted=report.inserted,
                failed=report.failed,
                apply=apply,
                by_chat=dict(sorted(report.by_chat.items(), key=lambda kv: -kv[1])[:8]),
                samples=report.samples[:5],
            )
    return totals


async def run_terminal_reconcile() -> None:
    """REC-2：周期对账。默认只读扫描，补写由 ``TERMINAL_RECONCILE_APPLY`` 决定。"""
    global _terminal_reconcile_notified_at
    if _job_disabled_by_flag("terminal_reconcile", features.TERMINAL_RECONCILE,
                             features.TERMINAL_RECONCILE in _JOBS_ON_BY_DEFAULT):
        return
    apply = features.enabled(features.TERMINAL_RECONCILE_APPLY)
    try:
        totals = await asyncio.to_thread(_reconcile_all_accounts, apply=apply)
    except Exception as exc:  # noqa: BLE001 - 周期 job 不能打断调度循环
        log.warning("terminal_reconcile_failed", error=str(exc)[:160])
        return
    log.info("terminal_reconcile_done", apply=apply, **totals)
    if not totals.get("missing"):
        return
    now = time.time()
    if now - _terminal_reconcile_notified_at <= TERMINAL_RECONCILE_ALERT_INTERVAL:
        return
    if _notify(
        "微信助理",
        f"警告：对账发现 {totals['missing']} 条消息没有对应事件"
        f"（近 {TERMINAL_RECONCILE_WINDOW_HOURS}h 扫描 {totals['scanned']} 条，"
        f"apply={apply}）。",
    ) is not False:
        # 时间戳放在调用之后：推送确认失败时不能吃掉 1 小时去重窗口。
        _terminal_reconcile_notified_at = time.time()


_scheduler_loop = asyncio.new_event_loop()


def _scheduler_loop_main() -> None:
    asyncio.set_event_loop(_scheduler_loop)
    _scheduler_loop.run_forever()


# 2026-09-25：loop 常驻化——此前 loop 只在 run_until_complete 期间运行，
# 导致 WS（QQ 收发/心跳）只在「同步那几秒」被顺带推进：消息延迟、心跳停、
# 连接反复重连。现在由独立线程常驻驱动，WS/异步任务可实时推进。
_loop_thread = threading.Thread(target=_scheduler_loop_main, daemon=True,
                                name="scheduler-loop")
_loop_thread.start()

_db_watcher = None
_db_watchers: list = []


_JOB_TIMEOUT_ENV = "YINYUE_JOB_TIMEOUT_SECONDS"
_JOB_TIMEOUT_DEFAULT = 180.0
_JOB_STUCK_RESTART_ENV = "YINYUE_JOB_STUCK_RESTART_SECONDS"
_JOB_STUCK_RESTART_DEFAULT = 900.0
_HEARTBEAT_PATH = Path(liveness.path("scheduler"))
_HEARTBEAT_INTERVAL_MS = to_ms("10s")
_LOOP_STARTED_AT = time.time()
_current_job: dict[str, Any] = {"name": "", "started_at": 0.0}
_main_loop_state: dict[str, Any] = {"ticks": 0, "last_tick_at": 0.0}
_watchdog_state: dict[str, Any] = {"lock": threading.Lock()}


def job_timeout_seconds(name: str = "") -> float:
    """单个 job 的超时（秒）；<=0 表示不设超时（回到旧的无限等待行为）。

    2026-10-17 事故：某个 job 的协程永不返回，主线程卡在 ``future.result()``，
    主循环（同步/停止哨兵/心跳）全部停摆，只能靠提权杀掉进程。现在默认有超时。
    """
    raw = os.environ.get(_JOB_TIMEOUT_ENV, "").strip()
    try:
        value = float(raw) if raw else _JOB_TIMEOUT_DEFAULT
    except ValueError:
        warn("scheduler/main.py:2470 ValueError", err=repr(__import__("sys").exc_info()[1]))
        value = _JOB_TIMEOUT_DEFAULT
    if value <= 0:
        return 0.0
    if name.startswith("run_sync_pipeline"):
        value *= 2.0  # 同步管线本身最重（解密/扫描已在线程里，但要给足余量）
    return value


def job_stuck_restart_seconds() -> float:
    """主循环/job 卡住多久后强制退出让 autostart 重启；<=0 关闭。"""
    raw = os.environ.get(_JOB_STUCK_RESTART_ENV, "").strip()
    try:
        return float(raw) if raw else _JOB_STUCK_RESTART_DEFAULT
    except ValueError:
        warn("scheduler/main.py:2484 ValueError", err=repr(__import__("sys").exc_info()[1]))
        return _JOB_STUCK_RESTART_DEFAULT


def current_job() -> dict[str, Any]:
    with _watchdog_state["lock"]:
        return dict(_current_job)


_LOOP_ENGINE_PORT = 39871
_loop_engine_last_check = 0.0
_loop_engine_starts = 0


def _loop_engine_alive(timeout: float = 0.3) -> bool:
    """引擎还活着吗：连一下它自己绑的端口。

    2026-10-24：不用心跳文件判活——心跳是引擎自己写的，进程僵死时文件还是新的；
    端口（127.0.0.1:39871）由内核管，进程一死就释放，这是唯一的硬判据。
    """
    import socket

    sk = socket.socket()
    sk.settimeout(timeout)
    try:
        sk.connect(("127.0.0.1", _LOOP_ENGINE_PORT))
        return True
    except OSError:
        # 2026-10-27：连不上（拒绝或超时）都算"不在"，但**不再记 warn** ——
        # 这台机器上连一个没人监听的本地端口会 timeout 而不是 refused，
        # 以前每小时二十多条噪音就是这么来的。判否由调用方决定要不要拉起来。
        return False
    finally:
        sk.close()


def _loop_py() -> str:
    """引擎用的解释器：优先 pythonw.exe（无控制台窗口，用户看不到黑框）。

    python.exe 是 console 子进程，即使 DETACHED_PROCESS 在某些 shell 下也会闪一个窗口；
    pythonw.exe 根本没有控制台。找不到就退回 sys.executable。
    """
    import os as _os
    import sys as _s
    exe = _s.executable
    cand = _os.path.join(_os.path.dirname(exe), "pythonw.exe")
    return cand if _os.path.exists(cand) else exe


def _ensure_loop_engine() -> None:
    """新大脑引擎看门狗：它挂了就拉起来（最多 60 秒查一次）。

    为什么要有这一条：2026-10-24 引擎被我反复手动重启/杀进程，
    每次都要人发现"她不吭声"再叫我去查。调度器自己在跑，
    顺手替她看着是最省事的做法。拉不起来只记日志，不影响调度器。
    """
    global _loop_engine_last_check, _loop_engine_starts

    now = time.monotonic()
    if now - _loop_engine_last_check < 60:
        return
    _loop_engine_last_check = now
    if _loop_engine_alive():
        return
    try:
        if stop_requested():
            return
    except Exception:  # noqa: BLE001
        warn("scheduler/main.py:2535 Exception", err=repr(__import__("sys").exc_info()[1]))
    try:
        import subprocess as _sp
        import sys as _sys
        root = str(Path(__file__).resolve().parents[1])
        # 引擎的输出不许扔 DEVNULL：2026-10-28 引擎因模块级 NameError 起不来，
        # traceback 全进了黑洞，只剩「她不说话」。这里改成追加到 logs/loop_engine.out。
        _out_fp = open(os.path.join(root, "logs", "loop_engine.out"), "ab", buffering=0)  # noqa: SIM115
        try:
            _sp.Popen(
                [_loop_py(), "-m", "loop.run", "--send"],
                cwd=root,
                stdout=_out_fp,
                stderr=_sp.STDOUT,
                creationflags=0x00000008 | 0x00000200,   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                close_fds=True,
            )
        finally:
            _out_fp.close()
        _loop_engine_starts += 1
        log.warning("loop_engine_respawned", starts=_loop_engine_starts)
    except Exception as exc:  # noqa: BLE001
        log.error("loop_engine_respawn_failed", error=repr(exc))


_DESK_LAST_CHECK = 0.0
_DESK_STARTS = 0
_DESK_LOCK = os.path.join(str(Path(__file__).resolve().parents[1]), "logs", "desk.lock")


def _desk_alive() -> bool:
    """发送工（`python -m loop.desk`）还活着吗：认它自己写的 `logs/desk.lock` 里的 pid。

    为什么不用端口：desk 不绑端口，它只轮询 `outbound_queue`，端口判活在这儿不成立。
    为什么要有这一条：2026-10-30 23:01 `all.stop` 把 `desk_supervisor` 关掉之后，
    **没有任何东西负责再拉起它** —— 出口关了 13 小时，她「说」的 12 句话全堆在
    `outbound_queue` 里烂成 failed。用户当时看到的是「她不回我」，其实是没人送。
    """
    try:
        import psutil
        raw = open(_DESK_LOCK, encoding="utf-8").read().strip()
        return bool(raw) and psutil.pid_exists(int(raw))
    except Exception:  # noqa: BLE001
        return False


def _ensure_desk() -> None:
    """发送工链看门狗：60 秒查一次，不在就拉 `scripts/desk_supervisor.py`。

    为什么拉监管器而不是直接拉 desk：重起/退避/单实例那套逻辑已经在监管器里，
    再写一份迟早会走样。监管器自己有命名互斥体（`Local\\YinyueDeskSupervisorMutex`），
    所以重复拉也只是空转一次，不会出双份。
    """
    global _DESK_LAST_CHECK, _DESK_STARTS

    now = time.monotonic()
    if now - _DESK_LAST_CHECK < 60:
        return
    _DESK_LAST_CHECK = now
    if _desk_alive():
        return
    try:
        if stop_requested():
            return
    except Exception:  # noqa: BLE001
        warn("desk_supervise_failed", err=repr(__import__("sys").exc_info()[1]))
    try:
        import subprocess as _sp
        root = str(Path(__file__).resolve().parents[1])
        _out_fp = open(os.path.join(root, "logs", "desk_supervisor.out"), "ab", buffering=0)  # noqa: SIM115
        try:
            _sp.Popen(
                [_loop_py(), os.path.join(root, "scripts", "desk_supervisor.py")],
                cwd=root,
                stdout=_out_fp,
                stderr=_sp.STDOUT,
                creationflags=0x00000008 | 0x00000200,   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                close_fds=True,
            )
        finally:
            _out_fp.close()
        _DESK_STARTS += 1
        log.warning("desk_respawned", starts=_DESK_STARTS)
    except Exception as exc:  # noqa: BLE001
        log.error("desk_respawn_failed", error=repr(exc))


def _loop_tick() -> None:
    _main_loop_state["ticks"] += 1
    _main_loop_state["last_tick_at"] = time.time()


def _dump_stacks(reason: str) -> str:
    """把全线程栈落到 logs/job_stack_<ts>.txt（事故复盘用，不阻塞）。"""
    path = Path(__file__).resolve().parents[1] / "logs" / f"job_stack_{int(time.time())}.txt"
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"# {reason} at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            faulthandler.dump_traceback(file=fh, all_threads=True)
        return str(path)
    except Exception:  # noqa: BLE001 - 复盘失败不能反过来搞崩进程
        warn("scheduler/main.py:2568 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


def _run_async(coro, *, name: str = "", timeout: float | None = None):
    """把协程提交到常驻调度 loop（独立线程）并等待结果；超时打栈、取消、放弃这一轮。"""
    limit = job_timeout_seconds(name) if timeout is None else float(timeout)
    with _watchdog_state["lock"]:
        _current_job.update({"name": name or "anonymous", "started_at": time.time()})
    try:
        future = asyncio.run_coroutine_threadsafe(coro, _scheduler_loop)
        return future.result(timeout=limit or None)
    except FuturesTimeoutError:
        stack = _dump_stacks(f"job_timeout:{name}")
        log.warning("job_timeout", job=name, seconds=round(limit, 1), stack=stack)
        try:
            future.cancel()
        except Exception:  # noqa: BLE001
            warn("scheduler/main.py:2585 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None
    finally:
        with _watchdog_state["lock"]:
            _current_job.update({"name": "", "started_at": 0.0})


def heartbeat_payload() -> dict[str, Any]:
    """心跳载荷。**时长一律毫秒整数**（UI-066）：`at_ms` / `beat_ms` 是契约字段，
    其余是自由字段，但也统一用 `_ms` 结尾，别再混秒。"""
    job = current_job()
    now_ms = duration.now_ms()
    started_ms = duration.from_epoch_s(_LOOP_STARTED_AT)
    last_tick_ms = duration.from_epoch_s(_main_loop_state["last_tick_at"] or 0.0)
    job_started_ms = duration.from_epoch_s(job["started_at"]) if job["started_at"] else 0
    return {
        "pid": os.getpid(),
        "started_at_ms": started_ms,
        "uptime_ms": max(0, now_ms - started_ms),
        "job": job["name"],
        "job_elapsed_ms": max(0, now_ms - job_started_ms) if job_started_ms else 0,
        "main_loop_ticks": int(_main_loop_state["ticks"]),
        "main_loop_age_ms": max(0, now_ms - last_tick_ms) if last_tick_ms else 0,
        # 契约字段：at_ms=最后一次心跳（epoch 毫秒）；beat_ms=我承诺的节拍
        "at_ms": now_ms,
        "beat_ms": _HEARTBEAT_INTERVAL_MS,
    }


def write_heartbeat_once() -> dict[str, Any]:
    """原子写心跳文件（路径来自 core/infra/liveness.py；外部看门狗读它判断进程是否还活着）。"""
    payload = heartbeat_payload()
    atomic_write_json(_HEARTBEAT_PATH, payload)
    return payload


def heartbeat_restart_reason(payload: dict[str, Any], threshold_ms: int) -> str:
    """返回需要硬重启的原因（'' 表示健康）。`threshold_ms` 与载荷里一样是毫秒。"""
    if threshold_ms <= 0:
        return ""
    job = str(payload.get("job") or "")
    elapsed = int(payload.get("job_elapsed_ms") or 0)
    if job and elapsed >= threshold_ms:
        return f"job:{job}"
    age = int(payload.get("main_loop_age_ms") or 0)
    if int(payload.get("main_loop_ticks") or 0) > 0 and age >= threshold_ms:
        return "main_loop"
    return ""


def _heartbeat_loop() -> None:
    while True:
        try:
            payload = write_heartbeat_once()
            reason = heartbeat_restart_reason(
        payload, duration.to_ms("%ds" % int(job_stuck_restart_seconds())))
            if reason:
                stack = _dump_stacks(f"stuck:{reason}")
                log.error("stuck_hard_restart", reason=reason, stack=stack, **payload)
                os._exit(3)
        except Exception as exc:  # noqa: BLE001 - 心跳线程不许死
            log.debug("heartbeat_failed", error=str(exc)[:120])
        time.sleep(duration.sleep_s(_HEARTBEAT_INTERVAL_MS))


def start_heartbeat() -> threading.Thread:
    thread = threading.Thread(target=_heartbeat_loop, daemon=True, name="loop-heartbeat")
    thread.start()
    return thread


_wechat_dirty = threading.Event()


async def _mark_wechat_dirty() -> None:
    """db_watcher 回调：只置脏标记，由主循环合并触发同步。

    微信每次 DB 变动都直接跑整条同步管线会把事件循环占死（2026-09-25 事故）；
    现在只置标记，主循环每轮（30 秒）合并处理一次，兼顾实时与稳定。
    """
    _wechat_dirty.set()


def _start_db_watcher():
    global _db_watcher, _db_watchers
    from core.ingestion.db_watcher import WeChatDbWatcher

    for ac in config.all_account_configs():
        if ac.platform != "wechat" or ac.role != "yinyue":
            continue
        if not ac.wx_db_path or not os.path.isdir(ac.wx_db_path):
            log.warning("db_watcher_no_path", db_path=ac.wx_db_path, account_id=ac.account_id)
            continue
        w = WeChatDbWatcher(
            db_storage_path=ac.wx_db_path,
            # 2026-09-25 事故：微信每次 DB 变动直接跑整条同步管线（含解密），
            # 频繁变化时把事件循环占死（心跳/消息全停、调度器卡死、启动器反复
            # 重启）。2026-09-26 根治：解密/扫描已移出事件循环
            # （core/ingestion/ingestors.py，asyncio.to_thread）；回调恢复
            # 置脏标记语义，由主循环 Event.wait 实时合并触发。
            on_change_callback=_mark_wechat_dirty,
            debounce_seconds=0.5,
            account_id=ac.account_id,
            platform=ac.platform,
        )
        w.start(loop=_scheduler_loop)
        _db_watchers.append(w)
        log.info("db_watcher_enabled", path=ac.wx_db_path, account_id=ac.account_id)

    if not _db_watchers:
        log.warning("db_watcher_no_accounts", msg="没有找到任何本机微信账号配置，数据库监控未启动")
        _notify("微信助理", "警告：数据库监控未启动（没有可用微信账号配置），鲸鲸收不到新消息。")


def log_background_metrics() -> None:
    """Phase 4: periodic queue-depth / drop counter snapshot + Web handoff."""
    try:
        from core.terminal.background import (
            write_background_queue_metrics_snapshot,
        )

        metrics = write_background_queue_metrics_snapshot()
        log.info("background_queue_metrics", **metrics)
    except Exception as exc:  # noqa: BLE001
        log.debug("background_queue_metrics_failed", error=str(exc)[:120])


def schedule_jobs():
    interval = config.sync.interval_minutes
    schedule.every(interval).minutes.do(lambda: _run_async(run_sync_pipeline(), name="run_sync_pipeline"))
    schedule.every().day.at("21:00").do(lambda: _run_async(run_daily_digest(), name="run_daily_digest"))
    schedule.every(30).minutes.do(lambda: _run_async(run_todo_overdue_check(), name="run_todo_overdue_check"))
    # 群聊v2 Step 12（交接 §5 TODO 1）：到点意图执行。flag off（默认）时
    # coroutine 只读一次 env 立即返回，零扫描零执行。注册在扫描器之前：
    # shadow 先记一条审计，扫描器随后投递并收口镜像意图（每个到点只记一次）。
    schedule.every(1).minutes.do(lambda: _run_async(run_intention_tick(), name="run_intention_tick"))
    schedule.every(1).minutes.do(lambda: _run_async(run_reminder_delivery(), name="run_reminder_delivery"))
    schedule.every(5).minutes.do(log_background_metrics)
    # Phase 5 D3: automatic scanning is always on; the YINYUE_PROACTIVE_*
    # rhythm/whitelist parameters remain the operational controls.
    proactive_interval = _env_int("YINYUE_PROACTIVE_SCAN_INTERVAL_MINUTES", 30)
    schedule.every(proactive_interval).minutes.do(
        lambda: _run_async(run_proactive_decision_scan(), name="run_proactive_decision_scan")
    )
    log.info("proactive_decision_scheduled", interval_min=proactive_interval)
    schedule.every(5).minutes.do(lambda: _run_async(run_memory_projection(), name="run_memory_projection"))
    schedule.every(5).minutes.do(lambda: _run_async(run_correction_learning()))
    schedule.every(1).hours.do(lambda: _run_async(run_memory_proposals()))
    schedule.every(10).minutes.do(lambda: _run_async(run_roster_sync()))
    log.info("memory_projection_scheduled", interval_min=5)
    if config.dream.enabled:
        dream_interval = config.dream.periodic_interval_minutes
        async def _enqueue_periodic_dream() -> None:
            from core.terminal.background import submit_background_job

            submit_background_job("dream_periodic", lambda: run_dream_engine())

        schedule.every(dream_interval).minutes.do(
            lambda: _run_async(_enqueue_periodic_dream())
        )
        log.info("jobs_scheduled", sync_interval_min=interval, digest_time="21:00",
                 dream_interval_min=dream_interval, dream_idle_min=config.dream.idle_timeout_minutes)
    else:
        log.info("jobs_scheduled", sync_interval_min=interval, digest_time="21:00", dream="disabled")

    # REC-1/REC-2：周期 drain / 对账。两个 job 各自一个 flag，运行时读 env，
    # 不重启也能关（YINYUE_FEATURE_OUTBOX_MAINTENANCE=0 等）：这里只决定「注册
    # 不注册」，已经注册的那份靠协程体开头的 _job_disabled_by_flag 每轮再判。
    # 「默认开」这一条事实只在 _JOBS_ON_BY_DEFAULT 写一次。
    if features.enabled(features.OUTBOX_MAINTENANCE,
                        features.OUTBOX_MAINTENANCE in _JOBS_ON_BY_DEFAULT):
        drain_interval = _env_int("YINYUE_OUTBOX_DRAIN_INTERVAL_MINUTES", 1)
        schedule.every(drain_interval).minutes.do(
            lambda: _run_async(run_outbox_maintenance())
        )
        log.info("outbox_maintenance_scheduled", interval_min=drain_interval)
    if features.enabled(features.TERMINAL_RECONCILE,
                        features.TERMINAL_RECONCILE in _JOBS_ON_BY_DEFAULT):
        reconcile_interval = _env_int("YINYUE_TERMINAL_RECONCILE_INTERVAL_MINUTES", 30)
        reconcile_apply = features.enabled(features.TERMINAL_RECONCILE_APPLY)
        schedule.every(reconcile_interval).minutes.do(
            lambda: _run_async(run_terminal_reconcile())
        )
        log.info("terminal_reconcile_scheduled", interval_min=reconcile_interval,
                 apply=reconcile_apply)


def _incremental_decrypt_enabled() -> bool:
    return flag("WX_INCREMENTAL_DECRYPT")


def _check_ollama() -> bool:
    st = get_status_tracker()
    try:
        with egress.client(lane="local", timeout_ms=3_000) as _c:
            resp = _c.get("http://localhost:11434/api/tags")
        ok = resp.status_code == 200
        st.set_ollama(ok)
        return ok
    except Exception:
        warn("scheduler/main.py:2786 Exception", err=repr(__import__("sys").exc_info()[1]))
        st.set_ollama(False)
        return False


def _prewarm_ocr() -> None:
    """Build the shared RapidOCR engine off the send path.

    The first OCR call in a fresh process spends roughly a second loading the
    models, and that second used to land inside the first reply's search
    verification.  Running it once the scheduler is up moves it out of the way.
    """
    try:
        from core.execution import wx_vision

        wx_vision.get_ocr()
        log.info("ocr_prewarmed")
    except Exception as exc:
        log.warning("ocr_prewarm_failed", error=str(exc))





def stop_requested() -> bool:
    """哨兵在场、且**晚于本进程启动**才算数（陈旧哨兵不误停新进程）。"""
    return self_stop.stop_requested()


_SCHEDULER_LOCK = None


def _acquire_scheduler_lock():
    """Prevent two scheduler processes from decrypting/syncing at once.

    A second process otherwise hits [WinError 32] when it tries to overwrite
    the merged SQLite file the first process still has open.
    """
    global _SCHEDULER_LOCK
    lock_path = Path("logs/scheduler.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        log.error("scheduler_already_running", lock=str(lock_path))
        return None
    _SCHEDULER_LOCK = handle
    try:
        # 谁在跑这份角色，只认角色自己写下的一行 pid（见 core/infra/roles.py）
        Path("logs/scheduler.pid").write_text(str(os.getpid()), encoding="utf-8")
    except OSError as exc:
        log.warning("scheduler_pid_write_failed", err=repr(exc))
    return handle


def _notify(title: str, message: str) -> bool:
    """Best-effort push; alerting must never break the scheduler.

    推送失败必须留痕：静默吞异常是项目禁忌（告警没了，日志也查不到）。返回
    是否真的推出去——``False`` 是确认失败，调用方据此不消费告警去重窗口。
    """
    try:
        send_notification(title, message)
        return True
    except Exception as exc:  # noqa: BLE001 - 推送失败不能打断调度
        log.warning("notify_failed", title=title, error=str(exc)[:160])
        return False


# ---- task-23: recall-chain vector cache warmer ----

_RECALL_CACHE_WARM_MAX_CHATS = 8


def _warm_interval() -> float:
    """Minutes between warm rounds; 0 or negative disables the thread (task-23)."""
    from core.memory import recall_chain as _rc

    return _rc.warm_interval_minutes()


def _recall_cache_chats() -> list[str]:
    """Chats worth warming: recently active ones, else the chat-window allowlist."""
    try:
        chats = _active_chats(hours=24, min_events=5, limit=_RECALL_CACHE_WARM_MAX_CHATS)
    except Exception as exc:  # noqa: BLE001 - the warmer must degrade, not crash
        log.warning("recall_chain_cache_chats_failed", error=str(exc)[:160])
        chats = []
    if chats:
        return chats
    try:
        from core.context.chat_window import window_chats

        return sorted(window_chats())
    except Exception as exc:  # noqa: BLE001
        log.warning("recall_chain_cache_chats_fallback_failed", error=str(exc)[:160])
        return []


def _recall_cache_warm_loop(interval_min: float) -> None:
    from core.memory import recall_chain as _rc

    interval = max(0.0, float(interval_min)) * 60.0
    log.info("recall_chain_cache_warm_started", interval_min=round(interval / 60.0, 2))
    while not stop_requested():
        for chat_id in _recall_cache_chats():
            if stop_requested():
                return
            attempt = 0
            while True:
                try:
                    stats = _rc.warm_cache_for(chat_id)
                except Exception as exc:  # noqa: BLE001 - one bad chat must not kill the thread
                    log.warning(
                        "recall_chain_cache_warm_failed", chat=chat_id, error=str(exc)[:200]
                    )
                    break
                if stats.get("skipped") != "model_cold":
                    log.info(
                        "recall_chain_cache_warm",
                        chat=chat_id,
                        mode=stats.get("mode"),
                        known=stats.get("known"),
                        missing_before=stats.get("missing_before"),
                        added=stats.get("added"),
                        elapsed_ms=stats.get("elapsed_ms"),
                    )
                    break
                attempt += 1
                if attempt > 3:
                    log.warning("recall_chain_cache_warm_cold_give_up", chat=chat_id)
                    break
                log.info("recall_chain_cache_warm_cold", chat=chat_id, retry=attempt)
                time.sleep(20)
                if stop_requested():
                    return
            time.sleep(0.5)
        waited = 0.0
        while waited < interval and not stop_requested():
            nap = min(5.0, interval - waited)
            time.sleep(nap)
            waited += nap


def start_recall_cache_warmer() -> threading.Thread | None:
    """Start the daemon thread that keeps row-vector caches fresh (task-23)."""
    from core.memory import recall_chain as _rc

    interval = _warm_interval()
    try:
        mode = features.mode(features.RECALL_CHAIN, features.MODE_OFF)
    except Exception:  # noqa: BLE001 - a broken flag must not break scheduler start
        warn("scheduler/main.py:2964 Exception", err=repr(__import__("sys").exc_info()[1]))
        mode = features.MODE_OFF
    if not _rc.warmer_should_start(interval, mode):
        log.info("recall_chain_cache_warm_disabled", interval_min=interval, mode=mode)
        return None
    thread = threading.Thread(
        target=_recall_cache_warm_loop,
        args=(interval,),
        daemon=True,
        name="recall-cache-warm",
    )
    thread.start()
    return thread


def main():
    log.info("yinyue_start")
    if _acquire_scheduler_lock() is None:
        print("另一个鲸鲸调度器实例已在运行，本进程退出。")
        return

    init_dirs()
    # 独立自停心跳（2026-10-17 事故）：主循环被一次同步轮压住时，
    # 哨兵也必须在 grace 秒内把进程停掉（不再等主循环轮询）。
    self_stop.install()
    # Every flag, once per start.  Asking "is feature X on?" used to mean
    # reading .env, then the code, then guessing which default applied; the
    # answer belongs in the log.
    try:
        flags = features.snapshot()
        log.info("feature_flags", **{f"flag_{k}": v for k, v in sorted(flags.items())})
    except Exception as exc:  # noqa: BLE001
        log.warning("feature_flags_log_failed", error=str(exc)[:120])
    # Recorded once per start: the affect layer is flag-split on purpose, and a
    # shadow run has to be distinguishable from a live one in the log alone.
    # Without this the "injected" fields of two different builds read the same.
    log.info(
        "affect_flags",
        affect=features.enabled(features.AFFECT),
        affect_prompt=features.enabled(features.AFFECT_PROMPT),
    )
    # **生产迁移的显式声明（唯一入口）**：``init_singletons(allow_production=True)``
    # 是本进程"我要迁移生产库"的声明点（默认 False = 拒绝，见
    # ``core.bootstrap.assert_migrations_allowed``）。为什么是参数而不是 env：
    # 环境变量会被脚本/探测随手带上，等于没拦；写在这一行就要求改动入口本身。
    # 2026-10-02 18:12:43 事故（非测试进程把生产库推到 schema 56）就是缺这一行。
    #
    # **写库守卫的显式声明（同一处，同一次启动的同一个意图）**：下面这一行声明
    # "本进程可写生产库"（默认 False = 拒绝，见 ``db.write_guard``）。缺了它，服务进程
    # 会在 ``init_singletons`` 构造 meta 库可写句柄时被守卫拒掉；有了它，**跑着的服务
    # 不受任何影响**。测试进程与脚本不声明 ⇒ 拿不到生产库的可写句柄（它们的库是隔离的，
    # 守卫对非生产路径一律放行）。为什么也是参数而不是 env：同上的理由。
    declare_writes_allowed(declared_by="scheduler.main")
    init_singletons(allow_production=True)

    # 重启恢复：上次写失败留在世界模型里的执行关闭，补写进认知库（幂等，失败不阻断）。
    try:
        from core.context.goal_reconcile import import_all_pending

        _recovered = {k: v for k, v in import_all_pending().items() if v}
        if _recovered:
            log.info("goal_import_recovered", chats=_recovered)
    except Exception as exc:  # noqa: BLE001 - 恢复失败不阻断启动
        log.warning("goal_import_failed", err=repr(exc)[:160])

    # 旧账本承诺补成目标（同一条幂等；没有会话坐标的旧行跳过）。
    try:
        from loop.promises import import_legacy as _import_promise_goals

        _promise_count = _import_promise_goals()
        if _promise_count:
            log.info("promise_legacy_imported", count=_promise_count)
    except Exception as exc:  # noqa: BLE001 - 迁移失败不阻断启动
        log.warning("promise_legacy_import_failed", err=repr(exc)[:160])

    # 每天第一次启动保留一行「L0 还差几天」：放行闸的判据在 scripts/stage_gate.py，
    # 但没人会在第 7 天记得去跑一条脚本。一次一行、绝不阻断启动。
    try:
        from core.observability.startup import startup_note

        startup_note()
    except Exception as exc:  # noqa: BLE001
        log.info("daily_note_failed error=%s", type(exc).__name__)


    register_agents()
    # Speech identity/conduct seed is imported by core.bootstrap.init_singletons
    # (SpeechBootstrap.ensure_seed), which also writes a revision and makes
    # the new persona/binding/rule tables the runtime source of truth.

    _check_ollama()

    from core.terminal.background import set_background_worker_loop

    set_background_worker_loop(_scheduler_loop)
    schedule_jobs()
    start_heartbeat()

    # 2026-10-25：web 改成独立子进程（线程里起 uvicorn 会"端口 bind 了但不应答"）
    time.sleep(2)

    log.info("first_sync_start")
    _run_async(run_sync_pipeline(), name="run_sync_pipeline")

    _start_db_watcher()
    start_recall_cache_warmer()

    threading.Thread(target=_prewarm_ocr, daemon=True, name="ocr-prewarm").start()

    log.info("scheduler_running")
    _notify("微信助理", "鲸鲸已启动")
    try:
        _last_ollama_check = 0.0
        _last_dream_check = 0.0
        _loop_t0 = time.monotonic()
        while True:
            _gap = time.monotonic() - _loop_t0
            if _gap > 4:
                log.warning("main_loop_gap", seconds=round(_gap, 2))
            _loop_t0 = time.monotonic()
            _loop_tick()
            if stop_requested():
                # 心跳线程也在等这条路——标了它就不要再强退。
                self_stop.mark_graceful_exit()
                log.info("scheduler_stopped",
                         reason=self_stop.stop_reason() or "stop_sentinel",
                         sentinel=str(getattr(self_stop, "SENTINEL_PATH", "")))
                print("鲸鲸已停止（主人自停哨兵），再见～")
                break
            _rp_t0 = time.monotonic()
            schedule.run_pending()
            _rp_dt = time.monotonic() - _rp_t0
            if _rp_dt > 1.5:
                log.warning("run_pending_slow", seconds=round(_rp_dt, 2))
            _ensure_loop_engine()
            _ensure_desk()
            # 微信 db_watcher 变化（_wechat_dirty）或 provider（QQ 等）队列
            # 非空时合并触发一次同步。等待用 Event.wait 而非 sleep(30)：
            # 微信一有变化立刻唤醒；静止时最多 2 秒也醒一次查 QQ 队列。
            # （同步管线内部已把解密/扫描移出事件循环，频繁触发不再卡死。）
            _pending = False
            try:
                from core.bootstrap import get_runtime as _get_rt
                _pending = any(
                    getattr(c.provider, "pending", 0)
                    for c in _get_rt().all_contexts()
                    if c.provider is not None
                )
            except Exception:  # noqa: BLE001
                warn("scheduler/main.py:3092 Exception", err=repr(__import__("sys").exc_info()[1]))
                _pending = False
            if _pending or _wechat_dirty.is_set():
                _wechat_dirty.clear()
                _sync_t0 = time.monotonic()
                _run_async(run_sync_pipeline(), name="run_sync_pipeline")
                _sync_dt = time.monotonic() - _sync_t0
                if _sync_dt > 1.5:
                    log.warning("sync_round_slow", seconds=round(_sync_dt, 2))
                continue
            _wechat_dirty.wait(timeout=2)
            _now = time.monotonic()
            if _now - _last_ollama_check >= 600:
                _last_ollama_check = _now
                _ollama_t0 = time.monotonic()
                _check_ollama()
                _ollama_dt = time.monotonic() - _ollama_t0
                if _ollama_dt > 1.5:
                    log.warning("ollama_check_slow", seconds=round(_ollama_dt, 2))
            if config.dream.enabled and _last_message_time and _now - _last_dream_check >= 60:
                _last_dream_check = _now
                _dream_t0 = time.monotonic()
                _run_async(_check_idle_and_dream())
                _dream_dt = time.monotonic() - _dream_t0
                if _dream_dt > 1.5:
                    log.warning("dream_check_slow", seconds=round(_dream_dt, 2))
    except KeyboardInterrupt:
        self_stop.mark_graceful_exit()
        log.info("scheduler_stopped", reason="keyboard_interrupt")
        print("\n鲸鲸已停止，再见～")
    except Exception as exc:  # noqa: BLE001 - report and let the watchdog restart us
        log.error("scheduler_crashed", error=str(exc)[:200])
        _notify("微信助理", f"鲸鲸调度器异常退出：{type(exc).__name__}: {str(exc)[:160]}")
        raise
