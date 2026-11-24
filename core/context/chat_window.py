"""Chat window builder for the prompt (docs/记忆的定义.md §1).

Read-side only: given a chat, return the text block that goes into the prompt.

  * recent ``full`` messages: verbatim (time + speaker + content, unchanged);
  * older today: skeleton → taper → time buckets → hour digest (never drop a
    message until the last resort, and never invent/rewrite content);
  * previous days: one line per day;
  * speaker attribution follows the terminal_events conventions:
    ``reply``/``thought`` or ``metadata.is_self == 1`` → the bot, otherwise the
    actor (a wxid) resolved through person_identities → person_entities.

Pure and synchronous: no LLM, no writes, deterministic output for equal input.

归属：core/routing/router.py 调 `_group_window_text` → 系统提示窗口块；开关 YINYUE_FEATURE_CHAT_WINDOW + _CHATS/_FULL/_BUDGET；设计 docs/记忆的定义.md §1。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Sequence

from core.terminal.shadow import _same_echo_body
from safelog import warn
from core.infra.envflags import flag, text

BOT_NAME = "鲸鲸"
EVENT_TYPES = ("message", "question", "reply", "request")
DEFAULT_FULL = 40
DEFAULT_DAYS = 7
DEFAULT_BUDGET = 3600
MIN_SNIPPET = 8  # 片段下限（docs §1.2）
MAX_SNIPPET = 12  # 合并成桶时每条片段的默认长度
# 事件 id = uuid4().hex（32 位小写 hex）；用来区分"A1-c 的事件 id"与
# "迁移前的平台 msg_id"（读侧兼容，见 _resolve_parent_msg_id）。
_EVENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class WindowMessage:
    occurred_at: datetime
    speaker: str
    content: str
    is_self: bool = False
    # 拍一拍/系统通知类（2026-09-24 Q1 根治）：不是"某人说的话"，渲染时用
    # （系统）样式，不挂 speaker 前缀，避免模型把不同人的拍数错/张冠李戴。
    is_pat: bool = False
    # 撤回/系统通知（批B）：同样按（系统）样式渲染，不冒充"某人说的话"。
    is_revoke: bool = False
    # ---- 请求→回复→@对象 的链（2026-09-28 @错人修法，读侧派生用）----
    row_id: int | None = None          # terminal_events.id
    event_id: str = ""                 # terminal_events.event_id
    event_type: str = ""               # message/question/reply/request
    msg_id: str = ""                   # 平台消息 id（metadata.msg_id）
    parent_msg_id: str = ""            # 她这条回复指向的目标（平台消息 id）
    author_id: str = ""                # 作者 person id（actor_person_id）
    recipients: tuple[str, ...] = ()   # 她@给了谁（recipient_person_ids_json）
    # task-43：``↳回 X`` 的渲染位（只由 ``utterance_edges`` 的 quote 边填；
    # ``UTTERANCE_EDGES`` 非 on 时永远是空串 = 逐字旧行）。
    reply_hint: str = ""


# --------------------------------------------------------------------- flags
def window_enabled() -> bool:
    return flag("YINYUE_FEATURE_CHAT_WINDOW")


log = logging.getLogger(__name__)


def window_chats() -> set[str]:
    raw = text("YINYUE_CHAT_WINDOW_CHATS")
    return {part.strip() for part in raw.split(",") if part.strip()}


def window_enabled_for(chat_id: str) -> bool:
    """Flag on, and (list empty ⇒ all chats / list set ⇒ only those chats)."""
    if not window_enabled():
        return False
    chats = window_chats()
    if not chats:
        return True
    return str(chat_id or "") in chats


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        raw = (os.environ.get(name) or "").strip()
        return max(low, min(high, int(raw))) if raw else default
    except Exception:
        warn("core/context/chat_window.py:94 Exception", err=repr(__import__("sys").exc_info()[1]))
        return default


def window_full() -> int:
    """最近多少条原文（``YINYUE_CHAT_WINDOW_FULL``，默认 40，5~80）。"""
    return _env_int("YINYUE_CHAT_WINDOW_FULL", DEFAULT_FULL, 5, 80)


def window_budget() -> int:
    """窗口总字数预算（``YINYUE_CHAT_WINDOW_BUDGET``，默认 3600，800~8000）。"""
    return _env_int("YINYUE_CHAT_WINDOW_BUDGET", DEFAULT_BUDGET, 800, 8000)


# --------------------------------------------------------------------- helpers
def _flat(text: Any, limit: int) -> str:
    raw = str(text or "")
    # 与内存窗口口径一致：把 [表情包:md5] 换成可读描述（老 get_recent_text 的行为）
    try:
        from core.preprocess.emoji_recognition import replace_emoji_descriptions

        raw = replace_emoji_descriptions(raw)
    except Exception:
        warn("core/context/chat_window.py:116 Exception", err=repr(__import__("sys").exc_info()[1]))
    flat = " ".join(raw.split())
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "…"


def _is_bot_event(event_type: str, metadata: dict) -> bool:
    from core.terminal.speaker import is_bot_event

    return is_bot_event(event_type, metadata)


def _speaker_of(
    event_type: str,
    actor: str,
    metadata: dict,
    names: dict[str, str],
    actor_account: str = "",
) -> str:
    from core.terminal.speaker import resolve_speaker

    return (
        resolve_speaker(
            event_type,
            actor,
            metadata=metadata,
            names=names,
            actor_account_id=actor_account,
        )
        or "?"
    )


def _metadata_of(row: dict) -> dict:
    try:
        metadata = json.loads(row.get("metadata_json") or "{}")
    except Exception:
        warn("core/context/chat_window.py:154 Exception", err=repr(__import__("sys").exc_info()[1]))
        metadata = {}
    return metadata if isinstance(metadata, dict) else {}


def _event_msg_id_index(rows: Iterable[dict]) -> dict[str, str]:
    """窗口内 ``event_id`` → 平台 ``msg_id``（A1-c 读侧解析父链用）。"""
    index: dict[str, str] = {}
    for row in rows:
        event_id = str(row.get("event_id") or "")
        msg_id = str(_metadata_of(row).get("msg_id") or "")
        if event_id and msg_id:
            index.setdefault(event_id, msg_id)
    return index


def _resolve_parent_msg_id(
    row: dict, metadata: dict, event_msg_ids: dict[str, str]
) -> str:
    """reply 行 → 被回应消息的**平台 msg_id**（兼容新旧两种父链写法）。

    A1-c 之后写入的行：``parent_event_id`` 是事件 id，源平台 msg_id 另存在
    ``metadata.parent_msg_id``（优先取它）。Migration 50 回填过的存量行只有事件
    id，用窗口内 ``event_id → metadata.msg_id`` 映射还原；映射不中且原值不像
    事件 id（32 位 hex）时按迁移前的 legacy 平台 msg_id 直接使用。
    """
    parent_msg_id = str(metadata.get("parent_msg_id") or "")
    if parent_msg_id:
        return parent_msg_id
    raw = str(row.get("parent_event_id") or "")
    if not raw:
        return ""
    mapped = event_msg_ids.get(raw)
    if mapped:
        return mapped
    if _EVENT_ID_RE.match(raw):
        # 已经是事件 id，但父事件不在窗口内（类型/时间被过滤）→ 只留观察，不硬连
        return ""
    return raw


def messages_from_rows(
    rows: Iterable[dict],
    names: dict[str, str] | None = None,
    hints: dict[str, str] | None = None,
) -> list[WindowMessage]:
    """Turn raw terminal_events rows into WindowMessage (public for tests).

    ``hints`` = ``{msg_id: "↳回 X"}``：由 :func:`load_messages` 批量读
    ``utterance_edges`` 得到。这里只负责**带上**渲染位，是否渲染由
    ``core.terminal.utterance_edges.edges_render_enabled()`` 在构造时判定——
    ``off``/``shadow`` 下 hints 恒为空，行内容与旧版逐字相同。
    """
    lookup = names or {}
    hint_lookup = hints or {}
    row_list = list(rows)
    event_msg_ids = _event_msg_id_index(row_list)
    out: list[WindowMessage] = []
    for row in row_list:
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
        except Exception:
            warn("core/context/chat_window.py:215 Exception", err=repr(__import__("sys").exc_info()[1]))
            metadata = {}
        occurred = row.get("occurred_at")
        if isinstance(occurred, str):
            occurred = datetime.fromisoformat(occurred)
        if occurred is None:
            continue
        event_type = str(row.get("event_type") or "message")
        content = str(row.get("content") or "")
        is_pat = _is_pat_content(content)
        is_revoke = _is_revoke_content(content)
        try:
            recipients = tuple(
                str(x)
                for x in (json.loads(row.get("recipient_person_ids_json") or "[]") or [])
                if str(x or "").strip()
            )
        except Exception:
            warn("core/context/chat_window.py:232 Exception", err=repr(__import__("sys").exc_info()[1]))
            recipients = ()
        out.append(
            WindowMessage(
                occurred_at=occurred,
                speaker=_speaker_of(
                    event_type,
                    str(row.get("actor_person_id") or ""),
                    metadata,
                    lookup,
                    str(row.get("actor_account_id") or ""),
                ),
                content=content,
                is_self=_is_bot_event(event_type, metadata),
                is_pat=is_pat,
                is_revoke=is_revoke,
                row_id=row.get("id"),
                event_id=str(row.get("event_id") or ""),
                event_type=event_type,
                msg_id=str(metadata.get("msg_id") or ""),
                parent_msg_id=_resolve_parent_msg_id(row, metadata, event_msg_ids),
                author_id=str(row.get("actor_person_id") or ""),
                recipients=recipients,
                reply_hint=str(hint_lookup.get(str(metadata.get("msg_id") or ""), "")),
            )
        )
    return out



# --------------------------------------------------------------------- loading
# ------------------------------------------------------------------ partition
#: 会话键的分隔符。选 `|` 而不是 `:`：微信 id 里本来就有 `@`、`group:` 前缀里
#: 也有 `:`，用 `:` 拼键会让「微信 chat_id=`group:123`」与「QQ 群 123」撞成一个键。
_CONV_SEP = "|"


def conversation_key(platform: str, account_id: str, chat_id: str) -> str:
    """把一个会话的三元组拼成**唯一**的键（平台 + 账号 + 会话）。

    规范形式是 `core/contracts/events.py::ConversationRef.key` 的**三元组**；本函数
    只是它的**串化**（当字典键、日志字段用），并且必须与它逐字一致
    （`tests/test_check_identity_keys.py` 有 parity 断言）。全仓只许这一处串化——
    第二个拼法会让"同一个会话"出现两种键，是这次会话分窗要防的错。

    为什么不是只用 `chat_id` + 时间：`chat_id` 只在「同一平台、同一账号」里唯一。
    两个微信账号都在同一个群时 `chat_id` 完全相同，只用它分窗会把两个账号的视角
    混成一份；QQ 用 `group:<群号>` 前缀、微信群用 `@chatroom` 后缀，靠**命名约定**
    区分平台，约定一旦变（或某个私聊 id 恰好长成前缀形状）就会串。

    分隔符与 `%` 都做转义，保证 `key` → 三元组可逆（`split_conversation_key`）。
    """
    from core.contracts.events import ConversationRef

    ref = ConversationRef(
        platform=str(platform or ""), account_id=str(account_id or ""),
        chat_id=str(chat_id or ""),
    )
    parts = []
    for value in ref.key:
        text = str(value or "").replace("%", "%25").replace(_CONV_SEP, "%7C")
        parts.append(text)
    return _CONV_SEP.join(parts)


def split_conversation_key(key: str) -> tuple[str, str, str]:
    """`conversation_key` 的逆运算（不可逆输入原样返回，不抛）。"""
    parts = str(key or "").split(_CONV_SEP)
    if len(parts) != 3:
        return ("", "", str(key or ""))
    return tuple(  # type: ignore[return-value]
        part.replace("%7C", _CONV_SEP).replace("%25", "%") for part in parts
    )


def resolve_scope(chat_id: str, *, since: datetime | None = None,
                  engine: Any = None) -> tuple[str, str, str]:
    """这一屏该按哪个 (平台, 账号) 读。返回 ``(platform, account_id, reason)``。

    ``reason`` 四值：

    * ``"explicit"``——调用方已指明（本函数不参与）；
    * ``"single"``——该会话在窗口内只有一个 (平台, 账号) 视角 → 就按它过滤；
    * ``"none"``——一行都没有（新会话 / 回放 / 库没开）→ 不加条件；
    * ``"ambiguous"``——窗口内出现**两个及以上**视角 → **不过滤**（宁可是超集，
      也不静默吞掉另一视角的消息），由调用方补显式 scope 才能定死。

    为什么不默认取"最新那个视角"：那会把另一账号在同一群里更早的消息悄悄排除。
    窗口是"这一屏发生过什么"的事实来源——多一行只是噪音，少一行会让判
    "没看见就说没看见"，所以这里宁松不紧。
    """
    try:
        from core.terminal.store_port import get_meta_repo
        from sqlalchemy import text as _sql

        if engine is None:
            engine = get_meta_repo().engine
        reference = since if isinstance(since, datetime) else datetime.now()
        since_at = reference.replace(tzinfo=None)
        with engine.connect() as conn:
            rows = list(
                conn.execute(
                    _sql(
                        "select distinct platform, bot_account_id from terminal_events "
                        "where chat_id = :chat_id and occurred_at >= :since"
                    ),
                    {"chat_id": str(chat_id), "since": since_at},
                ).mappings()
            )
    except Exception:  # noqa: BLE001 - 作用域判不出来就当"不知道"，绝不拖垮读窗
        warn("core/context/chat_window.py:341 Exception", err=repr(__import__("sys").exc_info()[1]))
        return "", "", "none"
    scopes = [
        (str(r.get("platform") or "").strip(), str(r.get("bot_account_id") or "").strip())
        for r in rows
    ]
    scopes = [sc for sc in scopes if sc[0] or sc[1]]
    if not scopes:
        return "", "", "none"
    if len(scopes) == 1:
        return scopes[0][0], scopes[0][1], "single"
    return "", "", "ambiguous"


def load_messages(chat_id: str, *, days: int = DEFAULT_DAYS,
                  now: datetime | None = None,
                  platform: str = "", account_id: str = "",
                  engine: Any = None) -> list[WindowMessage]:
    """Read-only load from terminal_events (meta DB).  Never raises for callers.

    ``now``：回看起点（缺省 = 墙钟）。**回放/延迟回合必须传它**，否则"窗口"
    会相对当前时刻计算，把回放输入整段排除（2026-10-10 实测：焦点预取把
    2026-10-03 的夹具窗口读成空 → 簇只剩 current → 锁 B 永远拿不到锚点）。

    ``platform`` / ``account_id``：**留空 = 现状（只按 chat_id 过滤，逐字节不变）**；
    传了就再加两个等值条件。多账号或多平台接入时必须传——`chat_id` 只在
    (平台, 账号) 里唯一，只按 chat_id 分窗会把两个账号在同一个群的视角混成一份
    （见 `conversation_key`）。

    ``engine``：仅测试注入用（缺省走 meta 库）。
    """
    try:
        from core.terminal.store_port import get_meta_repo
        from sqlalchemy import text as _sql

        reference = now if isinstance(now, datetime) else datetime.now()
        since = reference - timedelta(days=max(1, int(days)))
        if engine is None:
            engine = get_meta_repo().engine
        where = ["chat_id = :chat_id", "occurred_at >= :since"]
        params: dict[str, Any] = {
            "chat_id": str(chat_id),
            "since": since.replace(tzinfo=None),
        }
        want_platform = str(platform or "").strip()
        want_account = str(account_id or "").strip()
        if not want_platform and not want_account:
            auto_platform, auto_account, reason = resolve_scope(
                chat_id, since=since, engine=engine
            )
            if reason == "single":
                want_platform, want_account = auto_platform, auto_account
            elif reason == "ambiguous":
                log.warning(
                    "window_scope_ambiguous chat_id=%s 两个以上(平台,账号)视角："
                    "不过滤（宁可是超集，不静默吞另一视角）",
                    chat_id,
                )
        if want_platform:
            where.append("platform = :platform")
            params["platform"] = want_platform
        if want_account:
            where.append("bot_account_id = :account_id")
            params["account_id"] = want_account
        with engine.connect() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    _sql(
                        "select id, event_id, occurred_at, event_type, actor_person_id, content, "
                        "parent_event_id, recipient_person_ids_json, metadata_json "
                        "from terminal_events "
                        "where " + " and ".join(where) + " "
                        "and event_type in ('message','question','reply','request') "
                        # 时间已统一（见 event_model.localnow）：按时间排，sequence 只做平手裁决
                        "order by occurred_at, sequence"
                    ),
                    params,
                ).mappings()
            ]
            names: dict[str, str] = {}
            for r in conn.execute(
                _sql(
                    "select pi.identity_value, pe.display_name "
                    "from person_identities pi "
                    "join person_entities pe on pe.person_id = pi.person_id "
                    "where pe.display_name is not null and pe.display_name != ''"
                )
            ).mappings():
                names.setdefault(str(r["identity_value"]), str(r["display_name"]))
            # task-43：``↳回 X`` 的渲染位（off/shadow 恒 ``{}``；只读查询）。
            hints = _edge_hints(conn, _msg_ids_of(rows))
        for row in rows:
            if isinstance(row.get("occurred_at"), str):
                row["occurred_at"] = datetime.fromisoformat(row["occurred_at"])
        return _dedupe_self_echoes(messages_from_rows(rows, names, hints))
    except Exception:
        warn("core/context/chat_window.py:437 Exception", err=repr(__import__("sys").exc_info()[1]))
        return []


def _msg_ids_of(rows: Iterable[dict]) -> list[str]:
    """从 terminal_events 行里取平台 ``msg_id``（``metadata_json.msg_id``）."""
    out: list[str] = []
    for row in rows:
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
        except Exception:
            warn("core/context/chat_window.py:447 Exception", err=repr(__import__("sys").exc_info()[1]))
            metadata = {}
        if not isinstance(metadata, dict):
            continue
        msg_id = str(metadata.get("msg_id") or "").strip()
        if msg_id:
            out.append(msg_id)
    return out


def _edge_hints(conn: Any, msg_ids: Sequence[str]) -> dict[str, str]:
    """批量读 ``utterance_edges``，给出 ``{msg_id: "↳回 X"}``（task-43）。

    口径：
    * 只在 ``UTTERANCE_EDGES == on`` 时读库渲染；off/shadow 返回 ``{}``
      ⇒ 窗口正文与旧版逐字相同。
    * 只渲染 ``evidence='quote'``（引用某人）。``@`` 已经由归属后缀
      ``（→ @X）`` 表达，dm/无证据不渲染，避免同一件事说两遍。
    * 引用的作者名缺失时（``quoted_speaker_name`` 空）只有
      ``recipient_kind == 'bot'`` 能回落成她的名字，其余宁可不渲染也不猜。
    """
    try:
        from core.terminal.utterance_edges import edges_render_enabled

        if not edges_render_enabled():
            return {}
        ids = [str(m) for m in (msg_ids or ()) if str(m or "").strip()]
        if not ids:
            return {}
        from sqlalchemy import text as _sql

        marks = ",".join(f":m{i}" for i in range(len(ids)))
        params = {f"m{i}": value for i, value in enumerate(ids)}
        rows = list(
            conn.execute(
                _sql(
                    "select msg_id, recipient_kind, quoted_speaker_name "
                    "from utterance_edges "
                    "where evidence = 'quote' "
                    "and recipient_kind in ('bot', 'person') "
                    f"and msg_id in ({marks})"
                ),
                params,
            ).mappings()
        )
        bot_name = ""
        try:
            from core.terminal.speaker import BOT_NAME

            bot_name = str(BOT_NAME or "")
        except Exception:  # noqa: BLE001
            warn("core/context/chat_window.py:497 Exception", err=repr(__import__("sys").exc_info()[1]))
            bot_name = ""
        hints: dict[str, str] = {}
        for row in rows:
            name = str(row.get("quoted_speaker_name") or "").strip()
            if not name and str(row.get("recipient_kind") or "") == "bot":
                name = bot_name
            if name:
                hints[str(row.get("msg_id") or "")] = f"↳回 {name}"
        return hints
    except Exception:  # noqa: BLE001 - 渲染位是加成，永远不许阻断窗口
        warn("core/context/chat_window.py:507 Exception", err=repr(__import__("sys").exc_info()[1]))
        return {}


# --------------------------------------------------------------------- render
def _dedupe_self_echoes(rows: Sequence[WindowMessage], window_seconds: int = 900) -> list[WindowMessage]:
    """她的同一句话会被存两遍（消息同步 is_self=1 + 自己写的 reply），去重。

    只在"两边都是她、内容完全相同且长度 ≥8 字、时间差 ≤15 分钟"时去重，
    避免把正常的重复发言（"我靠"连发）误伤。
    """
    kept: list[WindowMessage] = []
    for msg in rows:
        body = " ".join(str(msg.content or "").split())
        if msg.is_self and len(body) >= 8:
            duplicated = False
            dup_at = -1
            for index in range(len(kept) - 1, max(-1, len(kept) - 7), -1):
                prev = kept[index]
                if (
                    prev.is_self
                    # 2026-10-16：与写入侧同一条判据（core.terminal.shadow
                    # _same_echo_body）——同步行带 @对方 前缀、发送侧写入时被
                    # 截断，都不是同一句话的差异，严格相等会漏掉它们。
                    and _same_echo_body(
                        str(msg.content or ""), str(prev.content or "")
                    )
                    and abs((msg.occurred_at - prev.occurred_at).total_seconds()) <= window_seconds
                ):
                    duplicated = True
                    dup_at = index
                    break
            if duplicated:
                # 2026-09-28：同一句话的两个分身，优先留**带链信息**的那条
                # （event_type='reply' 才有 parent_event_id / recipient，@错人修法要用）。
                if str(getattr(msg, "event_type", "") or "") == "reply" and str(
                    getattr(kept[dup_at], "event_type", "") or ""
                ) != "reply":
                    kept[dup_at] = msg
                continue
        kept.append(msg)
    return kept


def _pat_text(msg: WindowMessage) -> str:
    """把 `"X" 拍了拍 "Y" ...` 规范成 `（系统）X 拍了拍 Y ...`（去引号）。"""
    raw = str(msg.content or "")
    # 去掉内容里所有的引号（拍一拍的引号是系统格式，不是对话内容）
    clean = raw.replace('"', "")
    return f"（系统）{clean}"


_REVOKE_RE = re.compile(
    r'<sysmsg type="revokemsg"><revokemsg><content>(.*?)</content>', re.S
)


def _is_revoke_content(content: Any) -> bool:
    """这条 content 是不是微信撤回系统消息（revokemsg XML）。"""
    return bool(_REVOKE_RE.search(str(content or "")))


def revoke_notice_text(text: Any) -> str:
    """纯文本版撤回解析：revokemsg XML → `（系统）X 撤回了一条消息`；非撤回返回 ""。"""
    match = _REVOKE_RE.search(str(text or ""))
    if not match:
        return ""
    body = " ".join(match.group(1).split()).replace('"', "")
    return f"（系统）{body}" if body else "（系统）撤回了一条消息"


def _revoke_text(msg: WindowMessage) -> str:
    """把 revokemsg XML 渲染成 `（系统）X 撤回了一条消息`（去引号）。"""
    return revoke_notice_text(msg.content) or "（系统）撤回了一条消息"


def _system_text(msg: WindowMessage) -> str:
    """系统通知类内容的统一渲染（拍一拍/撤回）；普通消息返回 ""。"""
    if msg.is_pat:
        return _pat_text(msg)
    if msg.is_revoke:
        return _revoke_text(msg)
    return ""


def _attribution_ctx(chat_id: str):
    """这一窗的归属口径（群/私聊 + 她自己的名字）；取不到就 None（用默认）。"""
    try:
        from core.context.attribution import default_context
        from db.models import is_group_chat_id

        return default_context(is_group=bool(is_group_chat_id(str(chat_id or ""))))
    except Exception:  # noqa: BLE001 - 口径拿不到就退回默认（群语义）
        warn("core/context/chat_window.py:599 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


def _fmt_full(msg: WindowMessage, *, ctx: Any = None) -> str:
    """最近原文一行：``[HH:MM] 说话人: 正文`` + canonical 归属后缀。

    2026-09-30 收口：归属后缀由唯一 owner
    （``core.context.attribution.attribution_suffix``）给出——``（→ @Elaina）``
    或 `` [归属不明]``；没有明确目标（无 @ 无引用）不加任何东西。**必须在
    ``_mark_quote`` 之后渲染**：``[引用〔作者不明〕]`` 是信息缺口，不能被当成
    新作者（后缀会是 ``[归属不明]``，而不是某个人的名字）。
    """
    if msg.is_pat or msg.is_revoke:
        # 拍一拍/撤回的正文本身就是"谁对谁做的"，不再加归属后缀（避免重复）。
        return f"[{msg.occurred_at:%H:%M}] {_system_text(msg)}"
    line = f"[{msg.occurred_at:%H:%M}] {msg.speaker}: {_flat(msg.content, 400)}"
    if msg.reply_hint:
        line = f"{line} {msg.reply_hint}"
    try:
        from core.context.attribution import attribution_suffix, default_context

        suffix = attribution_suffix(
            msg, ctx=ctx if ctx is not None else default_context()
        )
    except Exception:  # noqa: BLE001 - 归属渲染失败退回旧行，不阻断窗口
        warn("core/context/chat_window.py:624 Exception", err=repr(__import__("sys").exc_info()[1]))
        suffix = ""
    return line + suffix


_QUOTE_UNATTRIBUTED_RE = re.compile(r"^\[引用\s*[:：]")

# 拍一拍：content 形如 `"P6" 拍了拍 "鲸鲸"` 或 `"X" 拍了拍 "Y" 说"…"`。
# 命中即视为系统通知类，不是"某人说的话"。
_PAT_RE = re.compile(r'^"[^"]*"\s*拍了拍')
# 自己拍的：`我拍了拍 "P6"`（无作者字段，但同样是拍一拍，不是她"说"的话）。
_SELF_PAT_RE = re.compile(r"^(?:我|自己)\s*拍了拍")


def _is_pat_content(content: Any) -> bool:
    """这条 content 是不是拍一拍标题（系统通知），而不是某人说的话。

    只认两种真实线格式（历史全表 108 条含"拍了拍"的记录逐条核过）：
      * ``"X" 拍了拍 "Y"``（群里）/ ``"X" 拍了拍我``（私聊）——带引号；
      * ``我拍了拍 "Y"``（她自己拍的）——无引号但开头是 我/自己。
    其它含"拍了拍"的句子（如"他拍了拍桌子"）是聊天正文，不在此列。
    """
    text = str(content or "").strip()
    if "拍了拍" not in text:
        return False
    if text.startswith('"'):
        return bool(_PAT_RE.match(text))
    return bool(_SELF_PAT_RE.match(text))


def is_pat_content(content: Any) -> bool:
    """①感知与窗口读侧共用同一个拍一拍识别口径（公开别名）。"""
    return _is_pat_content(content)


def is_revoke_content(content: Any) -> bool:
    """①感知与窗口读侧共用同一个撤回识别口径（公开别名）。"""
    return _is_revoke_content(content)


def _mark_quote(msg: WindowMessage) -> WindowMessage:
    """批2b 收口：认不出引用作者的引用行，显式标出来（不装懂）。

    ``[引用 P5: 原话] 正文`` 这种窗口行能拆出作者，保持原样；但
    ``[引用: 原话] 正文``（历史行常有，163 条里 63 条）拆不出作者，
    直接渲染会让模型以为"有人在引用某人的话"却说不出是谁。
    这里把它换成 ``[引用〔作者不明〕: 原话]``，让模型知道这是**信息缺口**
    而不是「没有作者」。渲染层专属：不落库、不改事实。
    """
    if msg.is_self:
        return msg
    if _QUOTE_UNATTRIBUTED_RE.match(msg.content or ""):
        # 用 replace 保留新增的链字段（row/event/recipients…），只改正文
        return replace(
            msg,
            content=_QUOTE_UNATTRIBUTED_RE.sub("[引用〔作者不明〕: ", msg.content, count=1),
        )
    return msg


def _fmt_skeleton(msg: WindowMessage, limit: int) -> str:
    if msg.is_pat or msg.is_revoke:
        return f"[{msg.occurred_at:%H:%M}] {_system_text(msg)}"
    return f"[{msg.occurred_at:%H:%M}] {msg.speaker}: {_flat(msg.content, limit)}"


def _taper(rows: Sequence[WindowMessage], limits: tuple[int, int, int]) -> list[str]:
    """片段随距离衰减 + 同一分钟共用行首时间戳（一条不漏）。"""
    near, mid, far = limits
    total = len(rows)
    out: list[str] = []
    slot: datetime | None = None
    parts: list[str] = []

    def flush() -> None:
        if parts and slot is not None:
            out.append(f"[{slot:%H:%M}] " + " ／ ".join(parts))

    for index, msg in enumerate(rows):
        frac = index / max(1, total - 1)
        limit = far if frac < 1 / 3 else (mid if frac < 2 / 3 else near)
        current = msg.occurred_at.replace(second=0, microsecond=0)
        if slot is None or current != slot:
            flush()
            parts = []
            slot = current
        parts.append(
            _system_text(msg) if (msg.is_pat or msg.is_revoke) else f"{msg.speaker}: {_flat(msg.content, limit)}"
        )
    flush()
    return out


def _buckets(rows: Sequence[WindowMessage], minutes: int = 10, limit: int = 16) -> list[str]:
    """时间桶降密度：桶首尾相接、无重叠无空隙，桶内一条不漏。"""
    out: list[str] = []
    slot: datetime | None = None
    parts: list[str] = []
    for msg in rows:
        current = msg.occurred_at.replace(
            minute=(msg.occurred_at.minute // minutes) * minutes,
            second=0,
            microsecond=0,
        )
        if slot is None or current != slot:
            if parts and slot is not None:
                out.append(f"[{slot:%H:%M}] " + " ／ ".join(parts))
            slot, parts = current, []
        parts.append(
            _system_text(msg) if (msg.is_pat or msg.is_revoke) else f"{msg.speaker}: {_flat(msg.content, limit)}"
        )
    if parts and slot is not None:
        out.append(f"[{slot:%H:%M}] " + " ／ ".join(parts))
    return out


def _hour_digest(rows: Sequence[WindowMessage]) -> list[str]:
    """最后手段（仍然一条不漏）：按小时给"时间 + 条数 + 说话人"。"""
    out: list[str] = []
    slot: datetime | None = None
    speakers: dict[str, int] = {}

    def flush() -> None:
        if slot is None:
            return
        top = "、".join(name for name, _ in sorted(speakers.items(), key=lambda x: -x[1])[:4])
        out.append(f"[{slot:%H:00}] 共 {sum(speakers.values())} 条 · 主要在说：{top}")

    for msg in rows:
        current = msg.occurred_at.replace(minute=0, second=0, microsecond=0)
        if slot is None:
            slot = current
        elif current != slot:
            flush()
            slot, speakers = current, {}
        speakers[msg.speaker] = speakers.get(msg.speaker, 0) + 1
    flush()
    return out


def _day_lines(
    rows: Sequence[WindowMessage],
    *,
    now: datetime | None = None,
    budget_chars: int | None = None,
) -> list[str]:
    """非今天消息的降级渲染（2026-09-24 批 B1 + 2026-09-26 批0 预算）。

    * 昨天/前天（1~2 天）→ 逐条骨架 + 相对日期（谁问谁答看得出来）；
    * 更早（3 天+）→ 每天一行（越远越像结论）。

    批0 修两件事：
      * 原实现完全不吃 ``budget_chars``（实测 3600 字预算输出 20,602 字），
        1~2 天段补降级链：逐条(20) → 逐条(12) → 保留最近 N 条+省略 → 日摘要；
      * 顺序改为旧→新（3 天+ 在前、1~2 天在后），保尾截断先保更新的行。
    """
    moment = now or datetime.now()
    from core import temporal

    recent: list[WindowMessage] = []
    by_day: dict[str, list[WindowMessage]] = {}
    for msg in rows:
        day_diff = (moment.date() - msg.occurred_at.date()).days
        if 1 <= day_diff <= 2:
            recent.append(msg)
        else:
            by_day.setdefault(msg.occurred_at.strftime("%m-%d"), []).append(msg)
    recent.sort(key=lambda m: m.occurred_at)

    def _day_summary(day: str, items: Sequence[WindowMessage]) -> str:
        speakers: dict[str, int] = {}
        for msg in items:
            speakers[msg.speaker] = speakers.get(msg.speaker, 0) + 1
        top = "、".join(name for name, _ in sorted(speakers.items(), key=lambda x: -x[1])[:3])

        def _edge(msg: WindowMessage) -> str:
            # 拍一拍同样按系统样式显示，别在"起/止"里露出引号形态。
            return _system_text(msg) if (msg.is_pat or msg.is_revoke) else _flat(msg.content, 24)

        return (
            f"[{day}] {len(items)} 条 · 主要在说：{top}"
            f" · 起：{_edge(items[0])} · 止：{_edge(items[-1])}"
        )

    def _recent_line(msg: WindowMessage, limit: int) -> str:
        stamp = temporal.format_stamp(msg.occurred_at, moment)
        body = _system_text(msg) if (msg.is_pat or msg.is_revoke) else f"{msg.speaker}: {_flat(msg.content, limit)}"
        return f"[{stamp}] {body}"

    day_out = [_day_summary(day, items) for day, items in sorted(by_day.items())]
    if budget_chars is None:
        return day_out + [_recent_line(m, 60) for m in recent]

    # 1~2 天（更新的）优先，3 天+ 的日摘要保底。
    recent_budget = max(200, int(budget_chars) - _size(day_out))
    recent_lines: list[str] = []
    if recent:
        for limit in (20, 12):
            candidate = [_recent_line(m, limit) for m in recent]
            if _size(candidate) <= recent_budget:
                recent_lines = candidate
                break
        if not recent_lines:
            # 保留最近 N 条，明确标出省略（比"只剩条数"强）。
            kept: list[str] = []
            used = 0
            for msg in reversed(recent):
                line = _recent_line(msg, 12)
                if kept and used + len(line) + 1 > recent_budget:
                    break
                kept.append(line)
                used += len(line) + 1
            kept.reverse()
            omitted = len(recent) - len(kept)
            if kept:
                recent_lines = (
                    [f"…更早 {omitted} 条已省略"] if omitted else []
                ) + kept
            else:
                # 极小预算：退成 1~2 天各一行摘要。
                grouped: dict[str, list[WindowMessage]] = {}
                for msg in recent:
                    grouped.setdefault(msg.occurred_at.strftime("%m-%d"), []).append(msg)
                recent_lines = [
                    _day_summary(day, items) for day, items in sorted(grouped.items())
                ]

    used = _size(recent_lines)
    kept_days_rev: list[str] = []
    for line in reversed(day_out):
        if kept_days_rev and used + len(line) + 1 > budget_chars:
            break
        kept_days_rev.append(line)
        used += len(line) + 1
    kept_days = list(reversed(kept_days_rev))
    omitted_days = len(day_out) - len(kept_days)
    if omitted_days:
        kept_days = [f"…更早 {omitted_days} 天已省略"] + kept_days
    return kept_days + recent_lines


def _take_recent(
    rows: Sequence[WindowMessage],
    max_msgs: int,
    max_chars: int = 2400,
    *,
    ctx: Any = None,
) -> list[WindowMessage]:
    """最近原文：条数与字数双上限（40 条是上限，不是必须）。

    长消息（她自己的长回复）会把窗口撑爆，所以按字数从新往旧取：
    累计超过 ``max_chars`` 就停（至少保 1 条）。
    """
    chosen: list[WindowMessage] = []
    size = 0
    for msg in reversed(list(rows)):
        if len(chosen) >= max(1, int(max_msgs)):
            break
        line = _fmt_full(msg, ctx=ctx)
        if chosen and size + len(line) + 1 > max_chars:
            break
        chosen.append(msg)
        size += len(line) + 1
    chosen.reverse()
    return chosen


def _shrink(rows: Sequence[WindowMessage], budget_chars: int) -> tuple[list[str], str]:
    """按 docs §1.2 降级：骨架(20) → 骨架(10) → 锥形 → 时间桶 → 小时摘要。"""
    if not rows:
        return [], "空"
    # 自适应：在 [MIN_SNIPPET, 20] 里取"塞得下的最大片段长度"，避免档位断崖
    for limit in range(20, MIN_SNIPPET - 1, -1):
        lines = [_fmt_skeleton(m, limit) for m in rows]
        if _size(lines) <= budget_chars:
            return lines, f"逐条骨架 {limit} 字"
    # 仍装不下：合并行（每条片段保持 ≥ MIN_SNIPPET），从细到粗
    for minutes in (10, 30, 60):
        lines = _buckets(rows, minutes=minutes, limit=MAX_SNIPPET)
        if _size(lines) <= budget_chars:
            return lines, f"时间桶（{minutes} 分钟）"
    # 保最近的、明确标出省略（比"只剩条数、没有内容"的摘要强）
    for keep in (3 * len(rows) // 4, len(rows) // 2, len(rows) // 4):
        if keep <= 0:
            continue
        lines = [_fmt_skeleton(m, MIN_SNIPPET) for m in rows[-keep:]]
        if _size(lines) <= budget_chars:
            omitted = len(rows) - keep
            return [f"…更早 {omitted} 条已省略"] + lines, f"保留最近 {keep}/{len(rows)} 条"
    return _hour_digest(rows), "小时摘要"


def _size(lines: Sequence[str]) -> int:
    return sum(len(line) + 1 for line in lines)


# --------------------------------------------------------------------- turns
def turns_enabled() -> bool:
    """轮次通道总开关（``YINYUE_TURNS_MODE``，默认关；一键回退＝设 0）。"""
    return flag("YINYUE_TURNS_MODE")


def turns_enabled_for(chat_id: str) -> bool:
    """轮次模式对某会话是否生效：总开关 + 沿用窗口白名单（不另造名单）。

    与窗口同一份名单是刻意的：``_chat_window_owns`` 只认窗口白名单，两套名单
    会漂移（批4 踩过"两边都没有"的坑，见 tests/test_private_window_context.py）。
    """
    if not turns_enabled():
        return False
    try:
        return bool(window_enabled_for(str(chat_id)))
    except Exception:
        warn("core/context/chat_window.py:937 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def turns_msg_chars() -> int:
    raw = text("YINYUE_TURNS_MSG_CHARS")
    try:
        return max(80, min(4000, int(float(raw)))) if raw else 400
    except Exception:
        warn("core/context/chat_window.py:945 Exception", err=repr(__import__("sys").exc_info()[1]))
        return 400


def turns_timestamp_enabled() -> bool:
    """``YINYUE_TURNS_TIMESTAMP``：轮次内容是否带 ``[HH:MM]`` 前缀（默认关）。

    2026-09-28 S2 实验：带前缀时草稿 6/6 模仿出时间戳（旧载体 0/6）——
    去掉前缀＝从源头消因；时间信号由 [时间感知]/摘要行/工具取时承担。
    出口的 ``guards.strip_leading_timestamp`` 仍保留作防御（防其它来源）。
    """
    return flag("YINYUE_TURNS_TIMESTAMP")


def messages_from_window(
    rows: Sequence[WindowMessage], *, limit: int | None = None
) -> list[dict]:
    """同一份 rows → role 交替的消息序列（轮次通道：只放原文，不截字改写）。

    * 她自己的话 → ``role=assistant``（"这是我说的"由结构承载）；
    * 别人的话 → ``role=user`` + ``name``（"谁说的"由字段承载）；
    * 拍一拍/撤回 → 系统记录（name=系统），不冒充某人的发言；
    * 内容默认**不带**时间戳前缀（``YINYUE_TURNS_TIMESTAMP=on`` 可开，带回旧口径）；
      超长单条按 ``YINYUE_TURNS_MSG_CHARS``（默认 400，与旧窗口 ``_fmt_full`` 同口径）
      截断并带显式省略号。
    """
    cap = turns_msg_chars() if limit is None else max(4, int(limit))
    show_ts = turns_timestamp_enabled()
    out: list[dict] = []
    for msg in rows:
        when = msg.occurred_at.strftime("%H:%M") if msg.occurred_at else "--:--"
        if msg.is_pat or msg.is_revoke:
            body, role, name = _system_text(msg), "user", "系统"
        else:
            body = _flat(msg.content, cap)
            role = "assistant" if msg.is_self else "user"
            name = str(msg.speaker or "").strip() or ("鲸鲸" if msg.is_self else "?")
        content = f"[{when}] {body}".strip() if show_ts else str(body or "").strip()
        if not content:
            continue
        out.append({"role": role, "name": name[:60], "content": content})
    return out


@dataclass
class WindowParts:
    """同一份 rows 派生的两种形态（轮次通道 / 文本通道），供对账使用。"""

    text_full: str = ""                 # 现状形态（含【最近对话·原文】段）
    text_digest: str = ""               # 通道分离形态（去掉最近原文段）
    recent: list[WindowMessage] = field(default_factory=list)
    rows: int = 0
    mode: str = ""


# --------------------------------------------------------------------- public
# 2026-10-26：历史里的 [图片] 每轮都去补描述，代价是 N 次视觉调用；只有「刚到手
# 的那张」值得现问。年龄窗内每轮最多补一张。
_DESCRIBE_MAX_AGE_SECONDS = 600.0
_DESCRIBE_PER_TURN = 1


def _describe_this_turn(msg: WindowMessage, chat_id: str, rendered: str) -> str:
    """给刚到手、还没有描述的图补一句描述（硬超时，失败返回原文）。

    为什么只补「刚到手」的：窗口里的历史 [图片] 可能上百条，每轮都去问视觉模型
    就是上百次调用加几百秒；而真正要回的这一轮，只关心刚发来的那一张。
    """
    try:
        from core.conversation.media import (
            describe_on_turn_enabled,
            ensure_image_description,
        )
        from core.conversation.media_registry import nearby_resolved_path

        if "[图片]" not in rendered or not describe_on_turn_enabled():
            return rendered
        moment = getattr(msg, "occurred_at", None)
        if moment is None:
            return rendered
        from datetime import datetime as _dt

        try:
            age = (_dt.now() - moment).total_seconds()
        except TypeError:
            return rendered
        if age < -60 or age > _DESCRIBE_MAX_AGE_SECONDS:
            return rendered
        path = nearby_resolved_path(None, str(chat_id), moment)
        if not path:
            return rendered
        text = ensure_image_description(path)
        short = " ".join(str(text or "").split())[:60]
        if not short:
            return rendered
        return rendered.replace("[图片]", "[图片: %s]" % short, 1)
    except Exception:  # noqa: BLE001
        warn("core/context/chat_window.py:1040 Exception",
             err=repr(__import__("sys").exc_info()[1]))
        return rendered


def _needs_media_desc(content: str) -> bool:
    """这一行里有需要补描述的媒体吗：字面量 ``[图片]``，或引用的媒体定位串。

    2026-10-27：以前只认 ``[图片]``，于是被引用的媒体行（正文是
    ``[引用 Elaina: chat:ts:1:md5::0]`` 这种定位串）永远拿不到描述。
    """
    text = str(content or "")
    if "[图片]" in text:
        return True
    if "[引用" not in text:
        return False
    try:
        from core.conversation.media_ref import has_locator

        return has_locator(text)
    except Exception:  # noqa: BLE001 - 认不出就当没有，渲染不阻断
        return False


def _enrich_media_descriptions(
    rows: list[WindowMessage], chat_id: str
) -> list[WindowMessage]:
    """把 ``[图片]`` 换成 ``[图片: 描述]``（描述来自媒体注册表；查不到就原样）。

    2026-09-28 媒体可读：图片描述一直存在消息媒体表里（到达那一轮由视觉模型
    生成），只是窗口/轮次渲染从不查它——所以历史里永远是裸 ``[图片]``。
    只在行里有 ``[图片]`` 时才查库，失败一律退回原文（渲染不阻断）。
    """
    if not chat_id:
        return rows
    if not any(_needs_media_desc(getattr(m, "content", "") or "") for m in rows):
        return rows
    try:
        from core.conversation.media import render_media_content
    except Exception:  # noqa: BLE001
        warn("core/context/chat_window.py:1017 Exception", err=repr(__import__("sys").exc_info()[1]))
        return rows
    out: list[WindowMessage] = []
    described = 0
    for msg in rows:
        content = str(getattr(msg, "content", "") or "")
        if not _needs_media_desc(content):
            out.append(msg)
            continue
        try:
            enriched = render_media_content(
                content,
                chat_id=str(chat_id),
                created_at=getattr(msg, "occurred_at", None),
            )
        except Exception:  # noqa: BLE001
            warn("core/context/chat_window.py:1031 Exception", err=repr(__import__("sys").exc_info()[1]))
            enriched = content
        if "[图片:" not in enriched and described < _DESCRIBE_PER_TURN:
            before = enriched
            enriched = _describe_this_turn(msg, chat_id, enriched)
            if enriched != before:
                described += 1
        out.append(replace(msg, content=enriched) if enriched != content else msg)
    return out


def _compose_parts(
    chat_id: str,
    *,
    full: int | None = None,
    days: int = DEFAULT_DAYS,
    budget_chars: int | None = None,
    now: datetime | None = None,
    messages: Sequence[WindowMessage] | None = None,
    loader: Callable[[str, int], list[WindowMessage]] | None = None,
    current: WindowMessage | None = None,
    include_current: bool = True,
) -> WindowParts:
    """Derive BOTH window forms from one read of the rows (docs §1).

    ``messages``/``loader`` are for tests; ``current`` lets a caller append the
    inbound message that may not be committed to the event store yet
    (``include_current=False`` keeps it out: the turns channel delivers the
    current message through the user prompt instead).
    ``full``/``budget_chars`` default to the env-tunable window knobs.
    """
    full = window_full() if full is None else max(1, int(full))
    budget_chars = window_budget() if budget_chars is None else max(200, int(budget_chars))
    rows = list(messages) if messages is not None else None
    if rows is None:
        loader = loader or (lambda cid, d: load_messages(cid, days=d))
        try:
            rows = list(loader(chat_id, days))
        except Exception:
            warn("core/context/chat_window.py:1064 Exception", err=repr(__import__("sys").exc_info()[1]))
            rows = []
    if current is not None and include_current:
        duplicated = any(
            m.speaker == current.speaker
            and m.content.strip() == current.content.strip()
            and abs((current.occurred_at - m.occurred_at).total_seconds()) < 120
            for m in rows[-3:]
        )
        if not duplicated:
            rows.append(current)

    rows = _dedupe_self_echoes(rows)
    rows = [_mark_quote(m) for m in rows]
    rows = _enrich_media_descriptions(rows, chat_id)
    # 归属口径在 _mark_quote **之后**才用（`[引用〔作者不明〕]` 必须被判成
    # unresolved，而不是新作者）；同一窗共用一份 ctx。
    attribution_ctx = _attribution_ctx(chat_id)

    moment = now or datetime.now()
    today = moment.strftime("%Y-%m-%d")
    today_rows = [m for m in rows if m.occurred_at.strftime("%Y-%m-%d") == today]
    older_days = [m for m in rows if m.occurred_at.strftime("%Y-%m-%d") != today]

    full_part = _take_recent(today_rows, max(1, int(full)), ctx=attribution_ctx)
    older_today = today_rows[: max(0, len(today_rows) - len(full_part))]

    lines_full = [_fmt_full(m, ctx=attribution_ctx) for m in full_part]
    # 2026-09-26 批0：预算按「最新优先」分配——今天最近原文 > 今天更早 >
    # 昨天/前天骨架 > 3 天+ 日摘要；窗口文本同时改为旧→新排序，这样调用方
    # （assembler 的 CUT_TAIL 保尾）截断时先保今天，而不是先保最旧。
    remaining = max(0, int(budget_chars) - _size(lines_full))
    lines_older, _mode = _shrink(
        older_today, max(200, remaining // 2) if remaining else 200
    )
    day_budget = max(200, remaining - _size(lines_older))
    day_lines = _day_lines(older_days, now=moment, budget_chars=day_budget)

    # invariants (docs §1.3)
    assert len(full_part) + len(older_today) == len(today_rows), "条数不守恒"
    assert [m.occurred_at for m in older_today + full_part] == [
        m.occurred_at for m in today_rows
    ], "顺序不守恒"
    for line in lines_older:
        assert len(line) >= MIN_SNIPPET, f"片段过短：{line!r}"

    # 顺序＝时间旧→新：任何保尾截断都先保「今天·最近原文」，再今天更早、
    # 昨天/前天、最后更早的日摘要。
    digest_blocks: list[str] = []
    if day_lines:
        digest_blocks.append(f"【近 {max(1, int(days))} 天·更早】\n" + "\n".join(day_lines))
    if lines_older:
        digest_blocks.append("【今天更早·压缩】\n" + "\n".join(lines_older))
    # 通道分离（2026-09-28）：轮次通道只放【最近对话·原文】那一段（整条进出），
    # 文本通道只留压缩/更早部分；两者共用上面同一份 rows / 同一套降级梯子，
    # 由调用方按 turns_enabled_for() 选 text_full 还是 text_digest。
    full_blocks = list(digest_blocks)
    if lines_full:
        full_blocks.append("【最近对话·原文】\n" + "\n".join(lines_full))
    return WindowParts(
        text_full="\n\n".join(full_blocks),
        text_digest="\n\n".join(digest_blocks),
        recent=list(full_part),
        rows=len(rows),
        mode=_mode,
    )


def build_window_parts(
    chat_id: str,
    *,
    full: int | None = None,
    days: int = DEFAULT_DAYS,
    budget_chars: int | None = None,
    now: datetime | None = None,
    messages: Sequence[WindowMessage] | None = None,
    loader: Callable[[str, int], list[WindowMessage]] | None = None,
    current: WindowMessage | None = None,
    include_current: bool = True,
) -> WindowParts:
    """Public: both forms + the exact rows they came from (for trace/audit)."""
    return _compose_parts(
        chat_id,
        full=full,
        days=days,
        budget_chars=budget_chars,
        now=now,
        messages=messages,
        loader=loader,
        current=current,
        include_current=include_current,
    )


def build_chat_window(
    chat_id: str,
    *,
    full: int | None = None,
    days: int = DEFAULT_DAYS,
    budget_chars: int | None = None,
    now: datetime | None = None,
    messages: Sequence[WindowMessage] | None = None,
    loader: Callable[[str, int], list[WindowMessage]] | None = None,
    current: WindowMessage | None = None,
) -> str:
    """Return the window text for ``chat_id`` ('' when nothing to show)."""
    return _compose_parts(
        chat_id,
        full=full,
        days=days,
        budget_chars=budget_chars,
        now=now,
        messages=messages,
        loader=loader,
        current=current,
    ).text_full


def build_window_messages(
    chat_id: str,
    *,
    full: int | None = None,
    days: int = DEFAULT_DAYS,
    budget_chars: int | None = None,
    now: datetime | None = None,
    messages: Sequence[WindowMessage] | None = None,
    loader: Callable[[str, int], list[WindowMessage]] | None = None,
) -> list[dict]:
    """最近原文那段 → role 交替消息序列（不含 current：由 user prompt 携带）。"""
    parts = _compose_parts(
        chat_id,
        full=full,
        days=days,
        budget_chars=budget_chars,
        now=now,
        messages=messages,
        loader=loader,
        current=None,
    )
    return messages_from_window(parts.recent)


__all__ = [
    "WindowMessage",
    "WindowParts",
    "is_pat_content",
    "is_revoke_content",
    "build_chat_window",
    "build_window_messages",
    "build_window_parts",
    "load_messages",
    "messages_from_rows",
    "messages_from_window",
    "turns_enabled",
    "turns_enabled_for",
    "turns_timestamp_enabled",
    "window_chats",
    "window_enabled",
    "window_enabled_for",
]
