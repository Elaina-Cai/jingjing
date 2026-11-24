"""「谁对谁说」的寻址边（task-43）：把平台已经给出的证据落成事实。

写侧只做一件事：把一条入站消息里**已有的**寻址证据（私聊 / 引用 / 正文 @）
解析成一行 ``utterance_edges``。读侧（窗口渲染 ``↳回 X``）在
``core/context/chat_window.py``，本模块**不 import 任何渲染代码**——写侧读侧
互相绑架是这类"事实层"改造最常见的翻车方式。

三条纪律
--------
1. **只写不猜**：没有 @、没有引用 ⇒ ``recipient_kind='unknown'`` +
   ``evidence='none'``。绝不把发言人或触发者填成收件人。
2. **幂等**：``(platform, chat_id, msg_id)`` 唯一键 + ``INSERT OR IGNORE``。
   重放同一批消息不会长行；已有行不会被改坏（事实行只追加）。
3. **best-effort**：任何异常只写一条 warning，绝不让入站链路失败。

两个已知的坑（task-42 生产实测，不许"图省事"绕开）
--------------------------------------------------
* 微信 ``messages.quote_reply_user`` **不可用**：主群 572 条引用里 558 条填的是
  chat_id 自身。作者一律走 ``quote_reply_id → messages.msg_id → sender_name``
  （实测 93.4% 可解）。
* 微信 ``messages.mentions`` 恒为空，@ 在**正文**里（``@鲸鲸 信息轰炸…``）：
  靠群成员显示名精确匹配从正文解析（``evidence='at_body'``）。没有这一步，
  微信侧只剩 12% 的引用边，@ 寻址全丢。

开关（三态，默认 off；唯一读点 ``edges_mode``）
-----------------------------------------------
* ``off``    ：不采集、不渲染 = 逐字旧行为（连一行日志都不写）；
* ``shadow`` ：只采集（写表 + 一行 ``utterance_edges_recorded``），不渲染；
* ``on``     ：采集 + 窗口渲染 ``↳回 X``。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping, Sequence

import structlog
from safelog import warn

log = structlog.get_logger()

#: 收件人种类。``self`` 只用于"这条是她自己发的"（写侧不落这类行，留值给回放）。
EDGE_RECIPIENT_KINDS = ("group", "person", "self", "bot", "unknown")

#: 证据种类。``at`` = 平台结构化 mentions；``at_body`` = 正文里的 ``@名字``。
EDGE_EVIDENCES = ("dm", "at", "at_body", "quote", "reply_to", "none")

_MAX_EDGES_PER_BATCH = 2000
_NAMES_TTL_SECONDS = 300.0
_names_cache: dict[tuple[str, str], tuple[float, tuple[str, ...]]] = {}

_AT_SIGN = "@"
#: @ 名字与正文之间的分隔符：微信用 U+0020，部分客户端用 U+2005；全角空格兜底。
_AT_SEPARATORS = ("\u0020", "\u2005", "\u3000", "\t")
_AT_RE = re.compile(_AT_SIGN)
_WS_RE = re.compile(r"\s+")


# --------------------------------------------------------------------- 开关
def edges_mode() -> str:
    """``YINYUE_FEATURE_UTTERANCE_EDGES`` 的**唯一**读点（三态，默认 off）。

    读失败按 ``off``：宁可不采集，也不许在开关读不出来时偷偷改行为。
    """
    try:
        from core import features

        return str(features.mode(features.UTTERANCE_EDGES, features.MODE_OFF))
    except Exception:  # noqa: BLE001 - 开关读不出来 = 不启用
        warn("core/terminal/utterance_edges.py:71 Exception", err=repr(__import__("sys").exc_info()[1]))
        return "off"


def edges_enabled() -> bool:
    """写侧是否采集（``shadow`` / ``on`` 都采集）。"""
    return edges_mode() != "off"


def edges_render_enabled() -> bool:
    """渲染侧是否输出 ``↳回 X``（只有 ``on``）。"""
    return edges_mode() == "on"


# --------------------------------------------------------------------- 小工具
def _norm_id(value: Any) -> str:
    """平台消息 id 统一成字符串（``int`` / ``str`` / ``None`` 都要能对上）。"""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text in {"", "0", "None", "null"} else text


def _as_bool(value: Any) -> bool:
    try:
        return int(value or 0) == 1
    except (TypeError, ValueError):
        warn("core/terminal/utterance_edges.py:97 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return bool(value)


def _bot_names(bot_names: Sequence[str] | None) -> tuple[str, ...]:
    names = [str(n).strip() for n in (bot_names or ()) if str(n or "").strip()]
    if not names:
        try:
            from core.terminal.speaker import BOT_NAME

            names = [BOT_NAME]
        except Exception:  # noqa: BLE001 - 名字拿不到就只按 id 判
            warn("core/terminal/utterance_edges.py:108 Exception", err=repr(__import__("sys").exc_info()[1]))
            names = []
    return tuple(dict.fromkeys(names))


def parse_mentions(raw: Any) -> list[dict]:
    """``messages.mentions``（JSON 文本 / list）→ ``[{"user_id","user_name"}]``。

    平台字段缺失或形状不对时返回空列表（缺字段不报错是写侧硬约束）。
    """
    if not raw:
        return []
    data = raw
    if isinstance(raw, (str, bytes)):
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            warn("core/terminal/utterance_edges.py:124 Exception", err=repr(__import__("sys").exc_info()[1]))
            return []
    if not isinstance(data, (list, tuple)):
        return []
    out: list[dict] = []
    for item in data:
        if isinstance(item, Mapping):
            out.append(
                {
                    "user_id": _norm_id(item.get("user_id") or item.get("wxid")),
                    "user_name": str(item.get("user_name") or item.get("name") or "").strip(),
                }
            )
    return out


def parse_at_names(content: Any, candidates: Sequence[str]) -> tuple[list[dict], bool]:
    """从正文里解析 ``@显示名``，返回 ``(命中列表, 是否歧义)``。

    * 只做**精确匹配**：候选名必须从 ``@`` 之后原样出现；
    * 名字后面必须是 ``U+0020`` / ``U+2005``（或正文结束），否则不算命中；
    * 同一位置能匹上两个候选名（一个是另一个的前缀）⇒ ``ambiguous=True``，
      由调用方落 ``unknown``：重名歧义**不许自己挑一个**。
    """
    text = str(content or "")
    names = sorted(
        {str(n).strip() for n in (candidates or ()) if str(n or "").strip()},
        key=len,
        reverse=True,
    )
    if not text or not names:
        return [], False
    hits: list[dict] = []
    ambiguous = False
    for match in _AT_RE.finditer(text):
        start = match.end()
        matched: list[tuple[str, int]] = []
        for name in names:
            if not text.startswith(name, start):
                continue
            end = start + len(name)
            follow = text[end : end + 1]
            if follow == "" or follow in _AT_SEPARATORS:
                matched.append((name, end))
        if not matched:
            continue
        if len(matched) > 1:
            ambiguous = True
            continue
        name, end = matched[0]
        hits.append(
            {"name": name, "raw": text[match.start() : end], "start": match.start(), "end": end}
        )
    return hits, ambiguous


def _name_of(raw: Mapping[str, Any], fallback: str = "") -> str:
    return str(raw.get("sender_name") or raw.get("name") or fallback or "").strip()


# --------------------------------------------------------------------- 建边
def build_edges(
    messages: Iterable[Any],
    *,
    platform: str,
    account_id: str,
    chat_names: Sequence[str] = (),
    bot_names: Sequence[str] | None = None,
    quoted_lookup: Callable[[str], Mapping[str, Any] | None] | None = None,
) -> list[dict]:
    """纯函数：一批消息 → 待写入的边行（不查库、不写库，可单测）。

    ``quoted_lookup(msg_id)`` 返回被引用那条消息的
    ``{"msg_id","sender_id","sender_name","is_self"}``（查不到返回 ``None``）。
    ``chat_names`` 是这个群的成员显示名（用于正文 @ 解析）。
    """
    bots = _bot_names(bot_names)
    bot_ids = {str(account_id or "").strip()} - {""}
    all_names = tuple(dict.fromkeys(list(chat_names or ()) + list(bots)))
    rows: list[dict] = []
    for raw in messages:
        if not isinstance(raw, Mapping):
            continue
        msg_id = _norm_id(raw.get("msg_id") or raw.get("id"))
        if not msg_id:
            continue
        if _as_bool(raw.get("is_self")):
            # 她自己的消息不是"某人对某人说"：写侧不落行（回放需要时可另建）。
            continue
        chat_id = str(raw.get("chat_id") or "")
        chat_type = str(raw.get("chat_type") or "private")
        speaker_id = str(raw.get("sender_id") or "")
        speaker_name = _name_of(raw)
        content = str(raw.get("content") or "")

        quoted_id = _norm_id(raw.get("quote_reply_id"))
        quoted: Mapping[str, Any] | None = None
        if quoted_id and quoted_lookup is not None:
            try:
                quoted = quoted_lookup(quoted_id)
            except Exception:  # noqa: BLE001 - 引用查不到照样写一行（证据仍成立）
                warn("core/terminal/utterance_edges.py:224 Exception", err=repr(__import__("sys").exc_info()[1]))
                quoted = None
        quoted_is_self = bool(
            quoted
            and (
                _as_bool(quoted.get("is_self"))
                or str(quoted.get("sender_id") or "") in bot_ids
                or _name_of(quoted) in bots
            )
        )
        quoted_speaker_id = "" if quoted_is_self else str((quoted or {}).get("sender_id") or "")
        quoted_speaker_name = ""
        if quoted is not None:
            quoted_speaker_name = _name_of(quoted) or (bots[0] if quoted_is_self else "")

        candidates: list[tuple[int, dict]] = []
        detail: dict[str, Any] = {"signals": []}

        # 1) 私聊：整条消息就是对她说（没有第三个人可选，不需要猜）。
        if chat_type != "group":
            candidates.append(
                (
                    50,
                    {
                        "recipient_kind": "bot",
                        "recipient_id": str(account_id or ""),
                        "recipient_name": bots[0] if bots else "",
                        "evidence": "dm",
                    },
                )
            )
            detail["signals"].append("dm")

        # 2) 平台 mentions（QQ 有值；微信恒空）。
        mentions = parse_mentions(raw.get("mentions"))
        if mentions:
            hit_bot = [
                m
                for m in mentions
                if (m.get("user_id") and m["user_id"] in bot_ids)
                or (m.get("user_name") and m["user_name"] in bots)
            ]
            detail["mentions"] = mentions[:8]
            if hit_bot:
                candidates.append(
                    (
                        45,
                        {
                            "recipient_kind": "bot",
                            "recipient_id": str(hit_bot[0].get("user_id") or ""),
                            "recipient_name": str(hit_bot[0].get("user_name") or (bots[0] if bots else "")),
                            "evidence": "at",
                        },
                    )
                )
            else:
                first = mentions[0]
                candidates.append(
                    (
                        30,
                        {
                            "recipient_kind": "person",
                            "recipient_id": str(first.get("user_id") or ""),
                            "recipient_name": str(first.get("user_name") or ""),
                            "evidence": "at",
                        },
                    )
                )
            detail["signals"].append("at")

        # 3) 正文 @（微信唯一可用的 @ 通道）。
        at_hits, at_ambiguous = parse_at_names(content, all_names)
        if at_ambiguous:
            detail["at_ambiguous"] = True
            detail["signals"].append("at_body_ambiguous")
        if at_hits:
            hit_names = [h["name"] for h in at_hits]
            detail["at_body"] = at_hits[:8]
            if any(n in bots for n in hit_names):
                candidates.append(
                    (
                        44,
                        {
                            "recipient_kind": "bot",
                            "recipient_id": str(account_id or ""),
                            "recipient_name": next(n for n in hit_names if n in bots),
                            "evidence": "at_body",
                        },
                    )
                )
            else:
                candidates.append(
                    (
                        29,
                        {
                            "recipient_kind": "person",
                            "recipient_id": "",
                            "recipient_name": hit_names[0],
                            "evidence": "at_body",
                        },
                    )
                )
            detail["signals"].append("at_body")

        # 4) 引用：引她 = 对她说；引别人 = 对那个人说；引自己 = 没有收件人证据。
        if quoted is not None:
            detail["quoted"] = {
                "msg_id": quoted_id,
                "speaker_id": quoted_speaker_id,
                "speaker_name": quoted_speaker_name,
                "is_self": quoted_is_self,
            }
            if quoted_is_self:
                candidates.append(
                    (
                        40,
                        {
                            "recipient_kind": "bot",
                            "recipient_id": str(account_id or ""),
                            "recipient_name": quoted_speaker_name or (bots[0] if bots else ""),
                            "evidence": "quote",
                        },
                    )
                )
            elif quoted_speaker_id and quoted_speaker_id != speaker_id:
                candidates.append(
                    (
                        35,
                        {
                            "recipient_kind": "person",
                            "recipient_id": quoted_speaker_id,
                            "recipient_name": quoted_speaker_name,
                            "evidence": "quote",
                        },
                    )
                )
            else:
                # 引用自己的话 / 引用里认不出作者：有引用事实，但没有收件人。
                detail["signals"].append("quote_unresolved")
        elif quoted_id:
            detail["signals"].append("quote_unresolved")
        if quoted_id and not any(item["evidence"] == "quote" for _, item in candidates):
            # 有引用事实（quote_reply_id 非空）但认不出收件人：查不到被引用的行、
            # 引的是自己的话、或作者不明。**引用本身是事实**，所以 evidence 记
            # 'quote'；收件人留空 = 不猜（渲染侧只认 kind in ('bot','person')）。
            candidates.append(
                (
                    34,
                    {
                        "recipient_kind": "unknown",
                        "recipient_id": "",
                        "recipient_name": "",
                        "evidence": "quote",
                    },
                )
            )

        chosen: dict[str, Any] = {
            "recipient_kind": "unknown",
            "recipient_id": "",
            "recipient_name": "",
            "evidence": "none",
        }
        if candidates:
            chosen = max(candidates, key=lambda item: item[0])[1]

        rows.append(
            {
                "platform": str(platform or ""),
                "bot_account_id": str(account_id or ""),
                "chat_id": chat_id,
                "chat_type": chat_type,
                "msg_id": msg_id,
                "event_id": "",
                "turn_id": str(raw.get("turn_id") or ""),
                "speaker_id": speaker_id,
                "speaker_name": speaker_name,
                "recipient_kind": chosen["recipient_kind"],
                "recipient_id": str(chosen.get("recipient_id") or ""),
                "recipient_name": str(chosen.get("recipient_name") or ""),
                "quoted_msg_id": quoted_id,
                "quoted_speaker_id": quoted_speaker_id,
                "quoted_speaker_name": quoted_speaker_name,
                "evidence": chosen["evidence"],
                "evidence_detail_json": json.dumps(detail, ensure_ascii=False),
                "created_at": _row_time(raw),
            }
        )
    return rows


def _row_time(raw: Mapping[str, Any]) -> datetime:
    """消息自带时间优先——回填历史时不能把 30 天的边全盖成"今天"。

    只认 ``raw["created_at"]``（account 库字符串，或已是 datetime）；
    解析不出来就 ``datetime.now()``（实时路径语义，绝不因时间格式挡住入库）。
    """
    value = raw.get("created_at")
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip().replace("Z", "")
    if text:
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            warn("core/terminal/utterance_edges.py:428 ValueError", err=repr(__import__("sys").exc_info()[1]))
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.strptime(text, fmt)
                except ValueError:
                    warn("core/terminal/utterance_edges.py:432 ValueError", err=repr(__import__("sys").exc_info()[1]))
                    continue
    return datetime.now()


# --------------------------------------------------------------------- 写库
def record_edges(engine: Any, edges: Sequence[Mapping[str, Any]]) -> int:
    """``INSERT OR IGNORE`` 一批边；返回**真正新增**的行数（幂等）。"""
    if not edges or engine is None:
        return 0
    from sqlalchemy import insert

    from db.meta_models import UtteranceEdge

    inserted = 0
    with engine.begin() as conn:
        for row in edges:
            result = conn.execute(insert(UtteranceEdge).prefix_with("OR IGNORE").values(**dict(row)))
            inserted += 1 if int(result.rowcount or 0) > 0 else 0
    return inserted


def group_display_names(
    engine: Any,
    *,
    platform: str,
    chat_id: str,
    repo: Any | None = None,
    use_cache: bool = True,
) -> tuple[str, ...]:
    """这个群的成员显示名候选（用于正文 ``@`` 解析）。

    来源按可靠性排序：``group_memberships.group_nickname``（群名片）→
    ``person_entities.display_name``。TTL 缓存 5 分钟：这批查询与入站节奏无关，
    每条消息都查一次纯属浪费。
    """
    key = (str(platform or ""), str(chat_id or ""))
    now = time.monotonic()
    if use_cache:
        cached = _names_cache.get(key)
        if cached is not None and now - cached[0] < _NAMES_TTL_SECONDS:
            return cached[1]
    names: list[str] = []
    try:
        from sqlalchemy import text as _sql

        with engine.connect() as conn:
            for row in conn.execute(
                _sql(
                    "select group_nickname from group_memberships "
                    "where platform = :p and chat_id = :c and group_nickname != ''"
                ),
                {"p": key[0], "c": key[1]},
            ):
                names.append(str(row[0] or "").strip())
            for row in conn.execute(
                _sql(
                    "select display_name from person_entities "
                    "where display_name is not null and display_name != '' limit 400"
                )
            ):
                names.append(str(row[0] or "").strip())
    except Exception:  # noqa: BLE001 - 名字拿不到就不做正文 @ 解析
        warn("core/terminal/utterance_edges.py:494 Exception", err=repr(__import__("sys").exc_info()[1]))
        names = []
    if not names and repo is not None:
        try:
            sender_name = getattr(repo, "messages", None)
            del sender_name  # repo 只用于兜底，这里不做全表扫描
        except Exception:  # noqa: BLE001
            warn("core/terminal/utterance_edges.py:500 Exception", err=repr(__import__("sys").exc_info()[1]))
    out = tuple(dict.fromkeys(n for n in names if n))
    if use_cache:
        _names_cache[key] = (now, out)
    return out


def _quoted_lookup_for(repo: Any) -> Callable[[str], Mapping[str, Any] | None] | None:
    """用账号库的 ``MessageRepository`` 造一个 ``msg_id → 被引用行`` 的查询。"""
    messages = getattr(repo, "messages", None)
    find = getattr(messages, "find_by_platform_id", None)
    if find is None:
        return None

    def lookup(msg_id: str) -> Mapping[str, Any] | None:
        try:
            found = find(msg_id)
        except Exception:  # noqa: BLE001
            warn("core/terminal/utterance_edges.py:518 Exception", err=repr(__import__("sys").exc_info()[1]))
            return None
        if found is None:
            return None
        return {
            "msg_id": _norm_id(getattr(found, "msg_id", "")),
            "sender_id": str(getattr(found, "sender_id", "") or ""),
            "sender_name": str(getattr(found, "sender_name", "") or ""),
            "is_self": int(getattr(found, "is_self", 0) or 0) == 1,
        }

    return lookup


def record_utterance_edges(
    messages: Iterable[Any],
    *,
    platform: str,
    account_id: str,
    engine: Any = None,
    repo: Any | None = None,
    bot_names: Sequence[str] | None = None,
) -> int:
    """入站钩子：把这一批消息的寻址边落库（best-effort，绝不抛）。

    ``off`` ⇒ 立刻返回 0，**连一行日志都不写**（off 档逐字旧行为）。
    """
    if edges_mode() == "off":
        return 0
    if engine is None:
        return 0
    try:
        batch = [m for m in messages if isinstance(m, Mapping)]
        if not batch:
            return 0
        chat_id = str(batch[0].get("chat_id") or "")
        names = group_display_names(
            engine, platform=platform, chat_id=chat_id, repo=repo
        )
        rows = build_edges(
            batch,
            platform=platform,
            account_id=account_id,
            chat_names=names,
            bot_names=bot_names,
            quoted_lookup=_quoted_lookup_for(repo) if repo is not None else None,
        )
        inserted = record_edges(engine, rows)
        log.info(
            "utterance_edges_recorded",
            platform=str(platform or ""),
            chat_id=chat_id,
            candidates=len(rows),
            inserted=inserted,
        )
        return inserted
    except Exception as exc:  # noqa: BLE001 - 寻址边永远不许影响入站
        log.warning(
            "utterance_edges_failed",
            platform=str(platform or ""),
            error=str(exc)[:200],
        )
        return 0


__all__ = [
    "EDGE_EVIDENCES",
    "EDGE_RECIPIENT_KINDS",
    "build_edges",
    "edges_enabled",
    "edges_mode",
    "edges_render_enabled",
    "group_display_names",
    "parse_at_names",
    "parse_mentions",
    "record_edges",
    "record_utterance_edges",
]
