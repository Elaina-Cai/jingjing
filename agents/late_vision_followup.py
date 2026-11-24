"""晚到的图：她已经回过话了，图才终于能看清——让她事后补一句。

为什么需要
----------
媒体入库那一刻，微信常常还没把文件写出来（媒体表里 last_error 是
``media path not resolved at ingest``）。那一轮她看到的只是 ``[图片]`` 占位，
于是她老实说"我这边看不到"。几分钟后解析成功、描述也生成了——但描述只是
写进表里，不会自己回到对话：除非对方再说话，而且那张图还在窗口里。

这个模块补中间这一段：解析成功之后主动叫醒她一次，把"你终于看清了这张图"
这件事原样告诉她；说不说、说什么由她自己决定（她可以输出 [静默]）。

为什么是旁白而不是记忆块
------------------------
``run_proactive_turn`` 的 ``hint`` 会作为本轮"别人发来的消息"进 prompt，
和既有 ``DEFAULT_PROACTIVE_PROMPT``／提醒文案同一条通道，不改任何
prompt 分层与白名单。人称沿用既有约定：**"你" = 她，"对方/主人" = 人类**。

三条纪律（与 ``loop/vision.py`` 同一套）
--------------------------------------
1. 一张图只补一次（写进 ``meta_json.late_followup_at``，先落标记再发）；
2. 每次扫描最多发一条，且只认最近 30 分钟内解析成功的图；
3. 永不抛：任何异常只 warning，绝不影响 sync pass。

开关：``YINYUE_IMAGE_LATE_FOLLOWUP=off``。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import text as sql_text

log = structlog.get_logger(__name__)

FOLLOWUP_ENV = "YINYUE_IMAGE_LATE_FOLLOWUP"
DEFAULT_WINDOW_SECONDS = 1800
MAX_PER_SCAN = 1
FOLLOWUP_KINDS = ("image", "quote_image")
_OFF_VALUES = {"0", "off", "false", "no", "none", "disable", "disabled"}


def enabled() -> bool:
    """开关：默认开。``off`` 时整条链路什么都不做。"""
    raw = os.getenv(FOLLOWUP_ENV, "1")
    return str(raw).strip().lower() not in _OFF_VALUES


def _norm_ts(value: Any) -> str:
    """统一成 ``YYYY-MM-DD HH:MM:SS``，好让字符串比较等价于时间比较。"""
    return str(value or "").strip()[:19]


def _meta(raw: Any) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        data = json.loads(str(raw or ""))
    except Exception:  # noqa: BLE001 - 坏 JSON 就当空表
        return {}
    return data if isinstance(data, dict) else {}


def _minutes_since(value: Any, now: datetime) -> int:
    """距现在多少分钟；**拿不到或解析不了就返回 -1（不知道）**。

    2026-11-01：这里原来返回 0，而 0 在下游等于「刚才」—— 时间戳一坏，
    她就对着一张几小时前的旧图说「对方刚才发的」，时间感直接错乱。
    """
    text = _norm_ts(value)
    if not text:
        return -1
    try:
        moment = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001 - 坏时间戳不等于刚刚
        return -1
    return max(0, int((now - moment).total_seconds() // 60))


def _chat_type_of(chat_id: str) -> str:
    return "group" if str(chat_id or "").endswith("@chatroom") else "private"


def build_late_followup_hint(
    *, description: str, minutes_ago: int = 0, chat_type: str = "private",
) -> str:
    """旁白原文。人称：``你`` = 她，``对方`` = 人类。"""
    text = " ".join(str(description or "").split())[:120].rstrip("。．.，,；;、 ·")
    _m = int(minutes_ago)
    if _m < 0:
        lead = "对方之前"          # 时间戳缺失/坏了：宁可含糊，也不谎称「刚才」
    elif _m <= 2:
        lead = "对方刚才"
    else:
        lead = f"对方 {_m} 分钟前"
    where = "在群里" if chat_type == "group" else ""
    return (
        "[系统触发 · 事后才看清的图]\n"
        f"{lead}{where}发过一张图。当时你还没能打开它，你回的是自己没看到。\n"
        f"现在你终于看清了：「{text}」。\n"
        "想接着这件事说一句就说，顺着刚才的话头接，别从头开始；"
        "觉得这时候提它反而奇怪，就输出 [静默]。\n"
        "不要说\"系统\"\"通知\"\"我刚收到\"这类话，就当是你自己这会儿想起来的。"
    )


def _candidates(engine, *, since: datetime, limit: int = 20) -> list[dict]:
    sql = sql_text(
        "select id, chat_id, kind, local_path, description, resolved_at, meta_json "
        "from message_media "
        "where status = 'resolved' "
        "  and kind in ('image', 'quote_image') "
        "  and local_path is not null and local_path <> '' "
        "  and description is not null and trim(description) <> '' "
        "  and resolved_at is not null and resolved_at >= :since "
        "order by resolved_at desc, id desc limit :limit"
    )
    with engine.connect() as conn:
        rows = conn.execute(
            sql, {"since": _norm_ts(since), "limit": int(limit)}
        ).fetchall()
    return [dict(row._mapping) for row in rows]


def _last_self_at(conn, chat_id: str) -> str:
    row = conn.execute(
        sql_text(
            "select max(created_at) as last_at from messages "
            "where chat_id = :chat and is_self = 1"
        ),
        {"chat": str(chat_id)},
    ).fetchone()
    if row is None:
        return ""
    return _norm_ts(dict(row._mapping).get("last_at"))


def _publish(
    *, platform: str, account_id: str, chat_id: str, chat_type: str,
    hint: str, moment: datetime,
) -> bool:
    """走既有的 PROACTIVE_TRIGGER 事件：白名单/静默时段/限流照样生效。"""
    # 2026-10-27：这条事件接的是 2026-10-24 就退役的旧大脑（日志
    # `trigger_failed: 旧大脑 ConversationalAgent 已退役…`），于是"我事后才看清了
    # 那张图"永远到不了她眼前。同一句提示也记进 loop 的自我状态表——那才是现在
    # 真正在跑的引擎，下一拍她自己就读得到。
    try:
        from loop import selfstate

        selfstate.push_vision_hint(
            chat_id,
            hint,
            at=moment.timestamp(),
            scope=(platform, account_id, chat_id),
        )
    except Exception as exc:  # noqa: BLE001 - 记不上也不该影响事件
        log.warning("late_followup_selfstate_failed", error=str(exc)[:160])
    try:
        from core.infra.event_bus import Event, EventBus, EventType

        EventBus.publish(
            Event(
                type=EventType.PROACTIVE_TRIGGER,
                timestamp=moment,
                payload={
                    "platform": platform,
                    "bot_account_id": account_id,
                    "chat_id": chat_id,
                    "chat_type": chat_type,
                    "hint": hint,
                    "source": "late_vision",
                    "reason": "image_resolved_after_reply",
                },
                source="late_vision_followup",
                platform=platform,
                account_id=account_id,
            )
        )
        return True
    except Exception as exc:  # noqa: BLE001 - 绝不打断 sync pass
        log.warning("late_followup_publish_failed", error=str(exc)[:160])
        return False


def maybe_publish_late_followup(
    engine,
    *,
    platform: str = "",
    account_id: str = "",
    now: datetime | None = None,
    window_s: int = DEFAULT_WINDOW_SECONDS,
    max_per_scan: int = MAX_PER_SCAN,
) -> dict:
    """扫一遍"她回过话、图才终于能看清"的行，最多发一条。

    判定（缺一不可）：行已 resolved 且有描述；解析发生在最近 ``window_s`` 内；
    她在**解析之前**说过话（说明那轮她没看到）；她在解析之后还没再说过；
    这张图还没补过。
    """
    stats = {"checked": 0, "published": 0, "skipped": 0, "reason": ""}
    if not enabled():
        stats["reason"] = "disabled"
        return stats
    moment = now or datetime.now()
    try:
        rows = _candidates(
            engine, since=moment - timedelta(seconds=max(60, int(window_s)))
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("late_followup_scan_failed", error=str(exc)[:160])
        stats["reason"] = "scan_failed"
        return stats

    budget = max(1, int(max_per_scan))
    published = 0
    with engine.connect() as conn:
        for row in rows:
            if published >= budget:
                break
            stats["checked"] += 1
            meta = _meta(row.get("meta_json"))
            if meta.get("late_followup_at"):
                stats["skipped"] += 1
                continue
            chat_id = str(row.get("chat_id") or "")
            resolved = _norm_ts(row.get("resolved_at"))
            if not chat_id or not resolved:
                stats["skipped"] += 1
                continue
            last_self = _last_self_at(conn, chat_id)
            if not last_self or last_self >= resolved:
                stats["skipped"] += 1
                continue
            description = " ".join(str(row.get("description") or "").split())[:120]
            if not description:
                stats["skipped"] += 1
                continue
            chat_type = _chat_type_of(chat_id)
            minutes_ago = _minutes_since(resolved, moment)
            # 先落标记再发：宁可少补一次，也不重复打扰。
            meta["late_followup_at"] = moment.isoformat(timespec="seconds")
            meta["late_followup_from"] = resolved
            try:
                conn.execute(
                    sql_text(
                        "update message_media set meta_json = :meta where id = :id"
                    ),
                    {
                        "meta": json.dumps(meta, ensure_ascii=False),
                        "id": int(row.get("id") or 0),
                    },
                )
                conn.commit()
            except Exception as exc:  # noqa: BLE001
                log.warning("late_followup_mark_failed", error=str(exc)[:160])
                stats["skipped"] += 1
                continue
            hint = build_late_followup_hint(
                description=description,
                minutes_ago=minutes_ago,
                chat_type=chat_type,
            )
            if not _publish(
                platform=platform,
                account_id=account_id,
                chat_id=chat_id,
                chat_type=chat_type,
                hint=hint,
                moment=moment,
            ):
                stats["skipped"] += 1
                continue
            published += 1
            log.info(
                "late_followup_published",
                media_id=int(row.get("id") or 0),
                chat_id=chat_id,
                minutes_ago=minutes_ago,
                chars=len(description),
            )
    stats["published"] = published
    return stats
