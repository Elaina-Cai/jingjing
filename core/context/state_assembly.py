"""WP-STATE 状态装配 v1：分类 / 聚合 / 渲染 + 对外唯一入口。

模块纪律：
* **纯函数优先**：``classify_move`` 与 ``render_state_block`` 不碰 DB、不读 flag，
  全部可用单测钉死；
* **对外两个入口**（``build_state_block`` / ``record_self_move``）才读 flag、碰仓库；
* **任何异常都降级**：对外入口返回 "" / 静默，绝不把组装或发送弄坏；
* 状态块只允许出现"她自己的行为"（动作类型、次数、回应次数、时间），
  **不出现**用户正文、昵称、群成员、记忆内容。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any, Iterable

import structlog

from core import features, temporal
from safelog import warn

log = structlog.get_logger()

__all__ = [
    "MAX_CHARS",
    "KIND_LABELS",
    "classify_move",
    "render_state_block",
    "situation_facts",
    "is_reset_request",
    "build_state_block",
    "record_self_move",
]

#: 状态块硬上限（超限从最后一行整行丢）。
MAX_CHARS = 400
#: 反馈判定阈值（只按时间戳）。
UNANSWERED_AFTER = timedelta(minutes=30)
IGNORED_AFTER = timedelta(hours=6)
#: 聚合窗口。
WINDOW = timedelta(hours=24)
#: "需要留意"最多几行。
MAX_NOTES = 2

KIND_LABELS: dict[str, str] = {
    "joke": "讲笑话",
    "poem": "写诗",
    "comfort": "接情绪",
    "weather": "查天气",
    "self_praise": "自夸",
    "counting": "数次数",
    "debt": "算账/催",
    "question_back": "追问",
    "other": "闲聊",
}

_DEBT_RE = re.compile(r"欠|账|利滚利|还没还|算账")
_COUNTING_RE = re.compile(r"第[一二三四五六七八九十百0-9]+(次|遍)|几(次|遍)|[0-9]+(次|遍)")
_JOKE_WORDS = ("笑话", "讲个", "笑点", "听好")
_COMFORT_WORDS = ("我在", "陪你", "别急", "先别", "歇会儿", "累", "难受")
_WEATHER_WORDS = ("天气", "度", "多云", "晴", "雨", "带伞", "外套")
_PRAISE_WORDS = ("那当然", "我超", "我最", "厉害", "知道吧", "没丢人", "眼光")

#: 命中即"冷却中"的动作（这两类最敏感：一次就够多）。
_COOLDOWN_KINDS = ("debt", "counting")

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

#: 「翻篇」指令：必须同时命中"动作词 + 范围词"（或短的独立口令），避免误触发。
_RESET_WORDS = ("忘记", "忘掉", "清空", "重置", "翻篇", "重新开始", "从头开始")
_RESET_SCOPES = ("前面", "之前", "以前", "刚才", "以上", "聊天", "对话", "记录", "所有", "全部")
_RESET_STANDALONE = ("翻篇", "重新开始", "从头开始")


def is_reset_request(text: str) -> bool:
    """主人明确要求"翻篇/忘记前面"→ True（确定性、可单测）。

    规则：长度 ≤60；命中「忘记/清空/重置/翻篇/重新开始…」且同时命中范围词
    （前面/之前/刚才/聊天/记录/所有…）；或 ≤8 字的独立口令（"翻篇"等）。
    """
    value = str(text or "").strip()
    if not value or len(value) > 60:
        return False
    if any(word in value for word in _RESET_WORDS) and any(
        scope in value for scope in _RESET_SCOPES
    ):
        return True
    return len(value) <= 8 and any(value.startswith(word) for word in _RESET_STANDALONE)


def classify_move(reply: str, inbound: str = "") -> tuple[str, list[str], int]:
    """回复 → ``(move_kind, bit_keys, expects_reply)``。

    优先级：debt > counting > joke > poem > comfort > weather > self_praise >
    question_back > other。``bit_keys`` 是**所有**命中项（排序去重），
    用于聚合；``move_kind`` 只取第一个命中。
    """
    text = str(reply or "")
    incoming = str(inbound or "")
    hits: list[str] = []
    if _DEBT_RE.search(text):
        hits.append("debt")
    if _COUNTING_RE.search(text):
        hits.append("counting")
    if any(word in text for word in _JOKE_WORDS) or "笑话" in incoming:
        hits.append("joke")
    if "《" in text or "诗" in incoming:
        hits.append("poem")
    if any(word in text for word in _COMFORT_WORDS):
        hits.append("comfort")
    if any(word in text for word in _WEATHER_WORDS) or "天气" in incoming:
        hits.append("weather")
    if any(word in text for word in _PRAISE_WORDS):
        hits.append("self_praise")
    if text.rstrip().endswith(("？", "?")):
        hits.append("question_back")
    if not hits:
        return "other", ["other"], 0
    return hits[0], sorted(set(hits)), 1 if "question_back" in hits else 0


def _age_text(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return "刚刚"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} 小时"
    return f"{hours // 24} 天"


def _engagement_text(state: Any, now: datetime) -> str:
    last_bot = getattr(state, "last_bot_at", None)
    if last_bot is None:
        return ""
    engagement = str(getattr(state, "last_bot_engagement", "") or "none")
    if engagement == "replied":
        return f"已回（{_age_text(now - last_bot)}前）"
    if engagement != "pending":
        return ""
    age = now - last_bot
    if age < UNANSWERED_AFTER:
        return f"等你回（{_age_text(age)}）"
    if age < IGNORED_AFTER:
        return f"还没回（{_age_text(age)}）"
    return f"大概率没接（{_age_text(age)}）"


def _usage_pct() -> float | None:
    """今日 token 消耗占日常参考量的百分比（真量；读不到 → None）。

    数据源：``core.state.drivers.budget_pressure()``（= 今日 token 增量 / 参考量）。
    这是她**本来不知道**的事实——只陈述数字，让她自己决定要不要提、怎么提
    （不是指令、不是结论）。
    """
    try:
        from core.state.drivers import budget_pressure

        pct = float(budget_pressure() or 0.0) * 100.0
        return pct if pct >= 1.0 else None
    except Exception:  # noqa: BLE001 - 状态读不到 = 这一行不出现
        warn("core/context/state_assembly.py:165 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


def situation_facts(*, usage_pct: float | None = None) -> list[str]:
    """纯事实列表：只含数字/时间，不含评价、建议或指令。

    当前唯一增量是「今日消耗」（时间与她的行为史已在块内，不重复注入）；
    读不到真量时该条不出现。她是否用、怎么用，由 persona 自己决定。
    """
    if usage_pct is None:
        return []
    return [f"- 今天的消耗：约日常参考量的 {usage_pct:.0f}%"]


def render_state_block(state: Any, moves: Iterable[Any], now: datetime,
                        *, usage_pct: float | None = None) -> str:
    """渲染 ``[当前局面]`` 块；没有任何可说内容 → ``""``（整块不注入）。"""
    move_list = list(moves or ())
    last_bot = getattr(state, "last_bot_at", None) if state is not None else None
    reset_at = getattr(state, "reset_at", None) if state is not None else None
    # 翻篇边界之前的上一条记录不再展示（边界的语义：之前的不算）。
    if last_bot is not None and reset_at is not None and last_bot < reset_at:
        last_bot = None
    if state is None and not move_list:
        return ""

    lines: list[str] = ["[当前局面]"]

    latest = last_bot
    for stamp in (getattr(state, "last_user_at", None) if state is not None else None,):
        if stamp is not None and (latest is None or stamp > latest):
            latest = stamp
    if latest is not None:
        lines.append(
            f"- 本轮：{_WEEKDAYS[now.weekday()]} {now.strftime('%H:%M')}；"
            f"距上次互动 {_age_text(now - latest)}"
        )

    if last_bot is not None:
        kind = str(getattr(state, "last_bot_move_kind", "") or "other")
        label = KIND_LABELS.get(kind, "闲聊")
        detail = _engagement_text(state, now)
        suffix = f"，{detail}" if detail else ""
        lines.append(f"- 我上一条：{label}，{_age_text(now - last_bot)}前{suffix}")

    # 24 小时聚合（只统计窗口内）
    stats: dict[str, dict[str, int]] = {}
    last_seen: dict[str, datetime] = {}
    for move in move_list:
        sent_at = getattr(move, "sent_at", None)
        if sent_at is None or sent_at < now - WINDOW:
            continue
        for key in (getattr(move, "bit_keys", None) or [getattr(move, "move_kind", "other")]):
            key = str(key or "other")
            bucket = stats.setdefault(key, {"n": 0, "replied": 0})
            bucket["n"] += 1
            if str(getattr(move, "engagement", "") or "") == "replied":
                bucket["replied"] += 1
            if last_seen.get(key) is None or sent_at > last_seen[key]:
                last_seen[key] = sent_at
    if stats:
        ordered = sorted(stats.items(), key=lambda item: (-item[1]["n"], item[0]))[:4]
        parts = []
        for key, bucket in ordered:
            label = KIND_LABELS.get(key, "闲聊")
            text = f"{label}×{bucket['n']}"
            if bucket["replied"]:
                text += f"（收到回应 {bucket['replied']} 次）"
            if key in _COOLDOWN_KINDS:
                text += "（冷却中）"
            parts.append(text)
        lines.append("- 最近 24 小时我用过：" + "、".join(parts))

    notes: list[str] = []
    for key in _COOLDOWN_KINDS:
        bucket = stats.get(key)
        if bucket and bucket["n"] >= 1:
            notes.append(f"{KIND_LABELS.get(key, key)} 24 小时内已用过（冷却中）")
    joke = stats.get("joke")
    if joke and joke["n"] >= 3 and joke["replied"] * 2 < joke["n"]:
        notes.append("笑话梗有点疲劳，换个角度或先放放")
    praise = stats.get("self_praise")
    if praise and praise["n"] >= 3:
        notes.append(f"自夸 24 小时内已用过 {praise['n']} 次，收着点")
    lines.extend(situation_facts(usage_pct=usage_pct))

    if notes:
        lines.append("- 需要留意：" + "；".join(notes[:MAX_NOTES]))

    # 超限：从最后一行整行丢（4→3→2），不截断行内文字
    while len("\n".join(lines)) > MAX_CHARS and len(lines) > 1:
        lines.pop()
    if len(lines) <= 1:
        return ""
    return "\n".join(lines)


# --------------------------------------------------------------------- IO 入口（薄）
def _repo():
    from core.terminal.store_port import get_meta_repo

    manager = get_meta_repo()
    repo = getattr(manager, "state", None)
    if repo is not None:
        return repo
    from db.repo_state import StateRepository

    return StateRepository(manager.engine)


def _scope(context: Any, snapshot: Any) -> tuple[str, str, str]:
    extra = getattr(context, "extra", None) or {}
    target = getattr(getattr(snapshot, "turn", None), "target", None)
    platform = str(
        (extra.get("platform") if hasattr(extra, "get") else "")
        or getattr(target, "platform", "")
        or ""
    )
    account_id = str(
        (extra.get("account_id") if hasattr(extra, "get") else "") or ""
    )
    chat_id = str(
        (extra.get("chat_id") if hasattr(extra, "get") else "")
        or getattr(target, "chat_id", "")
        or ""
    )
    return platform, account_id, chat_id


def _observe_user_turn(context: Any, platform: str, account_id: str, chat_id: str) -> None:
    extra = getattr(context, "extra", None) or {}
    raw = extra.get("message_time") if hasattr(extra, "get") else None
    at = temporal.parse_datetime(raw) if raw else None
    at = at or datetime.now()
    text = str(extra.get("user_message") or "") if hasattr(extra, "get") else ""
    repo = _repo()
    if is_reset_request(text):
        repo.mark_reset(
            platform=platform, account_id=account_id, chat_id=chat_id, at=at,
        )
        log.info("state_reset", chat_id=chat_id, at=at.isoformat(timespec="seconds"))
    repo.observe_user_turn(
        platform=platform, account_id=account_id, chat_id=chat_id, at=at,
    )


def build_state_block(context: Any, snapshot: Any, *, is_group: bool) -> str:
    """唯一读入口：off → ""；shadow → 记录 + 事件 + ""；on → 状态块（可空）。"""
    try:
        mode = features.mode(features.STATE_ASSEMBLY, features.MODE_OFF)
        if mode == features.MODE_OFF:
            return ""
        platform, account_id, chat_id = _scope(context, snapshot)
        if not chat_id:
            return ""
        now = datetime.now()
        _observe_user_turn(context, platform, account_id, chat_id)
        repo = _repo()
        state = repo.get_chat_state(platform=platform, account_id=account_id, chat_id=chat_id)
        since = now - WINDOW
        reset_at = getattr(state, "reset_at", None) if state is not None else None
        if reset_at is not None and reset_at > since:
            since = reset_at
        moves = repo.recent_moves(
            platform=platform, account_id=account_id, chat_id=chat_id, since=since,
        )
        # 2026-10-11 主人拍板：注入范围由「仅主人私聊」放开到**群聊 + 所有私聊**。
        # 原 owner_private 三重与（is_owner / is_group / is_group 参数）已移除。
        # 放开依据：块内只有她自己的行为统计（动作类型/次数/回应次数/时间），
        # 不含用户正文、昵称、群成员、记忆内容；状态按 chat_id 存，天然按会话
        # 隔离，群聊之间互不可见。回滚不改代码：三态开关仍有效
        # （off=完全不跑 / shadow=只记台账不注入 / on=注入）。
        usage_pct = _usage_pct()
        block = render_state_block(state, moves, now, usage_pct=usage_pct)
        if not block:
            return ""
        fields = dict(
            chat_id=chat_id,
            block_len=len(block),
            block_hash=hashlib.sha1(block.encode("utf-8")).hexdigest()[:12],
            moves_24h=len(moves),
            usage_pct=usage_pct,
            last_kind=str(getattr(state, "last_bot_move_kind", "") or ""),
            last_engagement=str(getattr(state, "last_bot_engagement", "") or "none"),
            has_reset=bool(reset_at is not None),
            is_group=bool(is_group),
        )
        if mode == features.MODE_SHADOW:
            log.info("state_assembly_shadow", **fields)
            return ""
        log.info("state_assembly", **fields)
        return block
    except Exception as exc:  # noqa: BLE001 - 状态层失败不许弄坏组装
        log.warning("state_assembly_failed", error=f"{type(exc).__name__}: {exc}"[:160])
        return ""


def record_self_move(context: Any, *, platform: str, account_id: str, chat_id: str, reply: str) -> None:
    """唯一写入口：off → 不写；写失败只告警，不影响发送结果。"""
    try:
        mode = features.mode(features.STATE_ASSEMBLY, features.MODE_OFF)
        if mode == features.MODE_OFF or not chat_id:
            return
        move_kind, bit_keys, expects_reply = classify_move(reply)
        _repo().record_self_move(
            platform=platform, account_id=account_id, chat_id=chat_id,
            reply=reply or "", sent_at=datetime.now(),
            move_kind=move_kind, bit_keys=bit_keys, expects_reply=expects_reply,
        )
        log.info(
            "self_move_recorded",
            chat_id=chat_id,
            move_kind=move_kind,
            bit_keys=bit_keys,
            chars=len(reply or ""),
        )
    except Exception as exc:  # noqa: BLE001 - 台账写失败不许影响发送
        log.warning(
            "state_store_failed",
            chat_id=chat_id,
            where="record_self_move",
            error=f"{type(exc).__name__}: {exc}"[:160],
        )
