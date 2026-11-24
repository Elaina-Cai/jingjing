from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from core.infra.event_bus import EventBus, Event, EventType
from core.infra.envflags import flag
from safelog import warn

log = structlog.get_logger()


class IngestionError(RuntimeError):
    """A platform account could not be ingested this round."""


@dataclass
class IngestionResult:
    stored: int = 0
    emitted: int = 0
    backfill_only: bool = False
    errors: list[str] = field(default_factory=list)


class PlatformIngestor:
    """One platform's account-level ingestion adapter."""

    platform: str = ""

    async def sync(self, ctx: Any) -> IngestionResult:
        raise NotImplementedError


def _incremental_decrypt_enabled() -> bool:
    return flag("WX_INCREMENTAL_DECRYPT")


def _trace_terminal_shadow(messages, *,
                           platform: str,
                           account_id: str,
                           repo: Any | None = None) -> list[str]:
    """Terminal event projection into the formal SQLite event stream.

    Best-effort and read-only with respect to the live reply chain.  When
    ``repo`` (account repo with ``event_outbox``) is given the order is:
    直写本批（保留命令记录）→ 销账本批已落地的 outbox 行 → drain 遗留
    pending（退避重试，重试不重放命令）。返回本批消息对应的事件 id。
    """
    try:
        messages = list(messages)
        from core.terminal.event_runtime import (
            get_event_identity_service,
            get_terminal_event_store,
        )
        from core.terminal.command_runtime import get_terminal_command_recorder
        from core.terminal.shadow import record_message_events

        store = get_terminal_event_store(force=True)
        identity_service = get_event_identity_service()
        command_recorder = get_terminal_command_recorder(identity_service)
        record_message_events(
            messages,
            platform=platform,
            account_id=account_id,
            identity_service=identity_service,
            command_recorder=command_recorder,
            return_events=False,
        )

        # task-43：把这一批消息**已经给出的**寻址证据落成 utterance_edges。
        # 三态开关（off 逐字旧行为 / shadow 只写 / on 写+渲染），全程 best-effort：
        # 边是加成，不许影响事件落库与回复链。
        try:
            from core.terminal.utterance_edges import record_utterance_edges

            try:
                from core.config.settings import config as _cfg
                from core.identity.self_names import self_names as _self_names

                _my_names = _self_names(
                    _cfg.get_account_db_path(platform, account_id), account_id=account_id
                )
            except Exception:  # noqa: BLE001 - 取不到名单就交给下游兜底
                _my_names = None
            record_utterance_edges(
                messages,
                platform=platform,
                account_id=account_id,
                engine=getattr(store, "engine", None),
                repo=repo,
                bot_names=_my_names,
            )
        except Exception as exc:  # noqa: BLE001 - 寻址边永远不许影响入站
            log.warning("utterance_edges_hook_failed", error=str(exc)[:200])
        _trace_group_membership(
            messages, platform=platform, account_id=account_id
        )
        if repo is not None and store is not None:
            from core.terminal.outbox import drain_outbox, settle_outbox_batch

            try:
                settle_outbox_batch(
                    repo=repo,
                    platform=platform,
                    account_id=account_id,
                    store=store,
                    messages=messages,
                )
            except Exception as exc:  # noqa: BLE001 - 销账失败留给 drain 幂等重投
                log.warning(
                    "outbox_settle_failed",
                    platform=platform,
                    account_id=account_id,
                    error=str(exc)[:160],
                )
            try:
                drain_outbox(
                    repo=repo,
                    platform=platform,
                    account_id=account_id,
                    store=store,
                    identity_service=identity_service,
                )
            except Exception as exc:  # noqa: BLE001 - 投递失败留给下一次同步
                log.warning(
                    "outbox_drain_failed",
                    platform=platform,
                    account_id=account_id,
                    error=str(exc)[:160],
                )
        lookup = getattr(store, "recorded_message_event_ids", None)
        resolved: dict[tuple[str, str], str] = {}
        if lookup is not None:
            try:
                resolved = lookup(platform=platform, bot_account_id=account_id)
            except Exception:  # noqa: BLE001
                warn("core/ingestion/ingestors.py:132 Exception", err=repr(__import__("sys").exc_info()[1]))
                resolved = {}
        out: list[str] = []
        for raw in messages:
            if not isinstance(raw, dict):
                continue
            key = (
                str(raw.get("chat_id") or ""),
                str(raw.get("msg_id") or raw.get("id") or ""),
            )
            event_id = resolved.get(key, "")
            if event_id:
                out.append(event_id)
        return out
    except Exception as exc:
        log.warning(
            "terminal_shadow_record_failed",
            platform=platform,
            account_id=account_id,
            error=str(exc)[:200],
        )
        return []



def _trace_group_membership(messages, *,
                            platform: str,
                            account_id: str) -> None:
    """Best-effort group join/leave epoch ingestion; never breaks sync."""
    try:
        from core.terminal.store_port import get_meta_repo
        from core.terminal.group_membership_service import GroupMembershipService

        GroupMembershipService(get_meta_repo()).apply_messages(
            messages,
            platform=platform,
            account_id=account_id,
        )
    except Exception:
        warn("core/ingestion/ingestors.py:170 Exception", err=repr(__import__("sys").exc_info()[1]))



def _register_media_rows(messages, *, platform: str, account_id: str, event_ids=None) -> int:
    """采集侧媒体落库（task-26）：best-effort，开关 off 时零 IO。"""
    try:
        from core.ingestion.media_store import register_media_rows

        return register_media_rows(
            messages, platform=platform, account_id=account_id, event_ids=event_ids,
        )
    except Exception as exc:
        log.warning(
            "media_registry_failed",
            platform=platform,
            account_id=account_id,
            error=str(exc)[:200],
        )
        return 0


async def _publish_new_messages(account_repo: Any, *,
                                synced_since: datetime,
                                platform: str,
                                account_id: str,
                                page_size: int = 5000) -> int:
    """Page through every new account row and publish it for agents."""
    offset = 0
    emitted = 0
    while True:
        msgs = await asyncio.to_thread(
            account_repo.messages.get_recent,
            synced_since=synced_since,
            limit=page_size,
            offset=offset,
        )
        if not msgs:
            break
        payload = await asyncio.to_thread(
            lambda rows=msgs: [account_repo.messages.to_dict(m) for m in rows]
        )
        emitted += len(payload)
        terminal_event_ids = _trace_terminal_shadow(
            payload, platform=platform, account_id=account_id,
            repo=account_repo,
        )
        _register_media_rows(
            payload, platform=platform, account_id=account_id,
            event_ids=terminal_event_ids,
        )
        EventBus.publish(Event(
            EventType.NEW_MESSAGES_SYNCED,
            payload={
                "count": len(payload),
                "messages": payload,
                "terminal_event_ids": terminal_event_ids,
            },
            platform=platform,
            account_id=account_id,
        ))
        # 2026-10-21 真机：这里是 main_loop_gap 7.74s / sync_round_slow 的真因。
        # 同步在主循环里 await 后台 handler，每个平台每轮最多 2 秒；
        # 而那 2 秒对「下一轮同步」没有任何用（事件已 publish 进队列）。
        # timeout=0 = 只让 handler 起跑、不在这里等它跑完。
        await EventBus.await_pending(timeout=0)
        offset += len(payload)
        if len(payload) < page_size:
            break
    return emitted


class WechatAccountIngestor(PlatformIngestor):
    platform = "wechat"

    async def sync(self, ctx: Any) -> IngestionResult:
        from core.config.settings import config
        from core.infra.status import get_status_tracker
        from db.decrypt import get_wx_key, decrypt_and_merge, detect_wx_version
        from db.sync_service import SyncService

        ac = ctx.config
        if not getattr(ac, "wx_db_path", ""):
            return IngestionResult()

        status_tracker = get_status_tracker()
        key = ac.wx_key or await asyncio.to_thread(get_wx_key)
        if not key:
            raise IngestionError(
                f"无法获取微信密钥 (account={ac.account_id})，请确认微信正在运行"
            )
        status_tracker.set_wechat_key(True)

        wx_path = ac.wx_db_path
        out_dir = ac.decrypted_dir or str(
            config.paths.get_account_decrypted_dir(ac.platform, ac.account_id)
        )
        Path(out_dir).mkdir(parents=True, exist_ok=True)

        # 解密/合并与消息扫描都是秒级同步重活。直接在事件循环里 await 会把
        # 调度器占死（2026-09-25 事故：心跳/消息全停、调度器卡死、启动器
        # 反复重启）。全部丢进线程池（asyncio.to_thread），循环只等结果。
        version = await asyncio.to_thread(detect_wx_version, wx_path)

        def _decrypt_pass(use_key: str, incremental: bool):
            return decrypt_and_merge(
                use_key, wx_path, out_dir, version=version,
                incremental=incremental,
            )

        result = await asyncio.to_thread(
            _decrypt_pass, key, _incremental_decrypt_enabled()
        )
        if not result:
            log.warning("decrypt_failed_retry_key", account_id=ac.account_id)
            new_key = await asyncio.to_thread(get_wx_key)
            if new_key and new_key != key:
                log.info("key_re_extracted", key=new_key[:16] + "...",
                         account_id=ac.account_id)
                result = await asyncio.to_thread(_decrypt_pass, new_key, False)
        if not result:
            status_tracker.set_wechat_key(False)
            raise IngestionError(f"数据库解密失败 (account={ac.account_id})，密钥可能已失效")

        msg_db = str(result.get("msg_db", ""))
        contact_db = str(result.get("contact_db", ""))
        if not msg_db or not Path(msg_db).exists():
            raise IngestionError(f"解密后未找到消息数据库 (account={ac.account_id})")

        def _scan_and_store() -> int:
            # SyncService 的构造（迁移/init_db）与扫描放在同一个线程里完成，
            # SQLAlchemy 引擎/Session 全程同线程，避免跨线程使用连接。
            sync = SyncService(
                ac.db_path,
                account_id=ac.account_id,
                platform=ac.platform,
                context=ctx,
            )
            if contact_db and Path(contact_db).exists():
                sync.sync_contacts(contact_db)
            return sync.sync_messages(msg_db)

        sync_start = datetime.now()
        stored = await asyncio.to_thread(_scan_and_store)
        if stored <= 0:
            return IngestionResult(stored=0)

        emitted = await _publish_new_messages(
            ctx.account_repo,
            synced_since=sync_start,
            platform=ac.platform,
            account_id=ac.account_id,
        )
        return IngestionResult(
            stored=stored,
            emitted=emitted,
            backfill_only=emitted == 0,
        )


class ProviderAccountIngestor(PlatformIngestor):
    """Generic adapter for providers that implement ``receive()``."""

    def __init__(self, platform: str = ""):
        self.platform = platform

    async def sync(self, ctx: Any) -> IngestionResult:
        provider = ctx.provider
        if provider is None:
            return IngestionResult()
        # 只有「鲸鲸号」（role=yinyue）参与接收：主人号/导入待分配账号只用于发送或
        # 其他用途（两个角色的 provider 都订阅同一 WS，都收会重复回复）。
        # 新平台若要接收，请在配置里写明 role=yinyue。
        if str(getattr(ctx, "role", "") or "") != "yinyue":
            return IngestionResult()
        try:
            if not provider.is_connected:
                if not await provider.connect():
                    return IngestionResult()
        except Exception as exc:
            raise IngestionError(
                f"provider connect failed ({ctx.platform}:{ctx.account_id}): {exc}"
            ) from exc

        from core.ingestion.message_bridge import inbound_to_message

        account_repo = ctx.account_repo
        try:
            await provider.sync_directory(account_repo)
        except Exception as exc:  # noqa: BLE001 - 目录同步失败不挡消息
            log.warning("provider_directory_sync_failed",
                        platform=ctx.platform, account_id=ctx.account_id,
                        error=str(exc)[:160])
        batch: list[dict] = []
        max_batch = 200
        errors: list[str] = []
        try:
            async for inbound in provider.receive():
                try:
                    inbound.extra["account_id"] = ctx.account_id
                    msg = inbound_to_message(inbound)
                    msg.source = ctx.platform
                    msg.account_id = ctx.account_id
                    msg.synced_at = datetime.now()
                    if account_repo.messages.get_by_id(msg.id) is not None:
                        continue
                    if account_repo.messages.exists(
                            msg.msg_id,
                            chat_id=msg.chat_id,
                            source=ctx.platform,
                    ):
                        continue
                    account_repo.messages.add(msg)
                    batch.append(account_repo.messages.to_dict(msg))
                except Exception as exc:
                    errors.append(str(exc)[:160])
                    log.warning("provider_message_persist_failed",
                                platform=ctx.platform,
                                account_id=ctx.account_id,
                                error=str(exc)[:160])
                if len(batch) >= max_batch:
                    break
        except Exception as exc:
            log.warning("provider_receive_failed", platform=ctx.platform,
                        account_id=ctx.account_id, error=str(exc)[:160])
            errors.append(str(exc)[:160])

        emitted = 0
        if batch:
            emitted = len(batch)
            terminal_event_ids = _trace_terminal_shadow(
                batch, platform=ctx.platform, account_id=ctx.account_id,
                repo=account_repo,
            )
            EventBus.publish(Event(
                EventType.NEW_MESSAGES_SYNCED,
                payload={
                    "count": len(batch),
                    "messages": batch,
                    "terminal_event_ids": terminal_event_ids,
                },
                platform=ctx.platform,
                account_id=ctx.account_id,
            ))
            # 只给事件一个很短的起跑窗口：async handler（含整条回复处理链）
            # 会进入 await_pending 的等待集合，此前 timeout=60 让整个同步轮次
            # 等回复跑完——实测主循环被卡 53 秒、连发消息全部排队。短窗口即可。
            # 2026-10-21 真机：这里是 main_loop_gap 7.74s / sync_round_slow 的真因。
            # 同步在主循环里 await 后台 handler，每个平台每轮最多 2 秒；
            # 而那 2 秒对「下一轮同步」没有任何用（事件已经 publish 进队列了）。
            # timeout=0 = 只让 handler 起跑、不在这里等它跑完。
            await EventBus.await_pending(timeout=0)
        return IngestionResult(
            stored=len(batch), emitted=emitted, errors=errors,
        )


class IngestionManager:
    """Registry of platform ingestion adapters."""

    def __init__(self):
        self._ingestors: dict[str, PlatformIngestor] = {}
        self._fallback_provider: PlatformIngestor | None = None

    def register(self, ingestor: PlatformIngestor) -> None:
        self._ingestors[ingestor.platform] = ingestor
        log.info("ingestor_registered", platform=ingestor.platform,
                 ingestor=type(ingestor).__name__)

    def get(self, platform: str) -> PlatformIngestor | None:
        return self._ingestors.get(platform)

    def for_context(self, ctx: Any) -> PlatformIngestor | None:
        ingestor = self._ingestors.get(ctx.platform)
        if ingestor is not None:
            return ingestor
        if ctx.provider is not None:
            return ProviderAccountIngestor(ctx.platform)
        return None

    async def sync(self, ctx: Any) -> IngestionResult:
        ingestor = self.for_context(ctx)
        if ingestor is None:
            return IngestionResult()
        return await ingestor.sync(ctx)


_INGESTION_MANAGER: IngestionManager | None = None


def get_ingestion_manager() -> IngestionManager:
    global _INGESTION_MANAGER
    if _INGESTION_MANAGER is None:
        _INGESTION_MANAGER = IngestionManager()
        _INGESTION_MANAGER.register(WechatAccountIngestor())
    return _INGESTION_MANAGER


__all__ = [
    "IngestionError",
    "IngestionResult",
    "PlatformIngestor",
    "WechatAccountIngestor",
    "ProviderAccountIngestor",
    "IngestionManager",
    "get_ingestion_manager",
]
