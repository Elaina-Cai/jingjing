"""RoomState（批2b）：把「谁在跟谁说话」算成结构化事实。

纯函数、无 LLM、无写入：输入房间最近消息（含当前这条），输出
``RoomSituation`` 与可直接进 prompt 的 ``[局况]`` 文本。

它回答四件事（都来自结构证据，不猜）：
  * 谁在场（近 5 分钟发言者）；
  * 最近的对话是谁问谁答（相邻问答 / 引用）；
  * 最新这句是不是「某人回答另一个人」，以及有没有点到她；
  * 有谁在等回应、她刚说过什么。

用途：
  * 批2b：作为上下文（软）——让模型不再从一堆原文里自己猜指向；
  * 批2c：作为拦截规则的结构输入（硬）——别人互答轮且未提到她时可跳过生成。

批2b 双轨收口（2026-09-23）：本模块原先只吃内存 buffer（30 条），而 prompt
里的窗口块走 ``chat_window``（terminal_events，40 条原文 + 压缩）。两处视图
不一致时，「局况」和「她看到的原文」会对不上——**最坏的情况是 gate 依据一份
比模型看到的更浅的样本去拦**。``WindowMessage`` 现在被接受为一等输入
（见 ``_speaker``/``_is_self``/``_dt`` 的读取口径与 ``_as_room_row``）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence
from safelog import warn

BOT_NAME = "鲸鲸"
SCAN_LIMIT = 24
# 时间窗口（近 5 分钟发言者 / 她刚说过 90s / 问答 60s）现由 core.timing 唯一持有。

_QUESTION_TAILS = ("?", "？", "吗", "呢")
# 复读治理（P2.5 层1）：媒体占位行不含信息，不能占「你刚说过/你最近说过」的位置。
_MEDIA_PREFIXES = (
    "[表情包", "[图片", "[媒体消息", "[动画表情", "[语音", "[视频", "[文件", "[链接", "[位置", "[聊天记录",
)
# 归一化（去空白与标点）：用于"同一句话被存两遍"（双写回声）的去重。
_ECHO_NORM_RE = re.compile(r"[\s，。！？；：、（）【】《》…—～·,.!?;:'\"()\[\]<>-]+")
# 「实义 2-gram」的停用字集：只用来剔除"你们/到底/是不是"这类无信息组合，
# 保留"拎回/宿舍/供着"这类能说明"在说同一件事"的词。
_STOP_CHARS = frozenset(
    "你我他她它们的是在了有和与就都也还不没一个什么怎吗呢吧啊呀那这要会能去来说看得过对好很太真谁啥哪"
    "为以而但所之其又再才只把被让给跟同向从于上下里中外前后天等嗯哦噢哎诶唉哈嘿着地得么"
    "，。！？；：、（）【】《》…—～·,.!?;:'\"()[]<>-— \t\n"
)
# 弱 2-gram（口头禅/连接件）：它们会把"两件不同的事"串成一组（例：句尾都有
# 「行吧」），逐出分组键。分组原则仍是"共享实义词才算同一件事"。
_WEAK_GRAMS = frozenset(
    {
        "行吧", "好吧", "那你们", "你们", "我们", "他们", "到底", "就是", "还是",
        "然后", "因为", "所以", "其实", "真的", "不是", "没有", "什么", "怎么",
        "这样", "那样", "现在", "已经", "可以", "应该", "可能", "这个", "那个",
        "一个", "这么", "那么", "而是", "但是", "如果", "虽然", "不过", "而且",
        "至于", "关于", "对于", "为了", "来说", "的话", "一样", "只是", "还要",
        "不用", "不要", "看看", "说说", "一下", "一点", "有点", "也是", "真是",
        "算了", "行了", "对了", "没有", "还能", "还是", "反正", "或者", "只是",
    }
)


def _get(msg: Any, name: str, default: Any = None) -> Any:
    if isinstance(msg, dict):
        return msg.get(name, default)
    return getattr(msg, name, default)


def _dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            warn("core/context/room_state.py:76 ValueError", err=repr(__import__("sys").exc_info()[1]))
            return None
    return None


def _text(msg: Any) -> str:
    return str(_get(msg, "content", "") or "")


def _speaker(msg: Any) -> str:
    # WP-03：说话人解析的唯一 owner = core/terminal/speaker.resolve_speaker_from_row。
    from core.terminal.speaker import resolve_speaker_from_row

    return resolve_speaker_from_row(msg, default="?")


def _is_self(msg: Any) -> bool:
    return bool(_get(msg, "is_self", False))


def _authored_by(msg: Any) -> str:
    """谁写下了这条记录（未解析——不猜身份，只用记录里的字段）。"""
    if _is_self(msg):
        return BOT_NAME
    return _speaker(msg)


def _created_at(msg: Any) -> datetime | None:
    """buffered 用 ``created_at``；``WindowMessage`` 用 ``occurred_at``。"""
    return _dt(_get(msg, "created_at")) or _dt(_get(msg, "occurred_at"))


def _as_room_row(msg: Any, *, now: datetime | None = None) -> dict:
    """把一个窗口消息规范化成局况行（批2b 双轨收口的接缝）。

    窗口消息只带一段无作者的引用片段（那是 prompt 的呈现格式），而局况判定
    需要作者。若这条消息的原始前缀是落库时的单行引用标记
    ``[引用 X: …] 正文``，就把它拆回 (作者, 正文)——**引用解析归 canonical**
    （``core.conversation.addressing``）；拆不开就保留原文并标
    ``quote_attributable=False``，由调用方决定（``build_window_room_situation``
    对拆不开的引用轮直接跳过，宁可少说，不猜）。
    """
    from core.conversation.addressing import (
        has_quote_prefix,
        quote_of,
        split_quote_line,
        strip_quote_block,
    )

    text = _text(msg)
    author = _authored_by(msg)
    base = {
        "sender_name": author,
        "created_at": _created_at(msg),
        "is_self": _is_self(msg),
    }
    quoted = quote_of(text)
    if quoted.present:
        quoted_author, body = split_quote_line(text)
        stripped = strip_quote_block(text)
        if stripped == text and body and body != text:
            # 引用块没闭合：退回"前缀之后即正文"的旧口径。
            stripped = body
        return {
            **base,
            "content": stripped.strip(" ：:。") or text,
            "quoted_author": quoted_author,
            "quote_attributable": bool(quoted_author),
        }
    if has_quote_prefix(text):
        # 有引用意图但认不出作者：标记不可归因。
        return {**base, "content": text, "quoted_author": "", "quote_attributable": False}
    return {**base, "content": text}


def _clip(text: Any, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
def _is_media_placeholder(text: str) -> bool:
    """媒体/空占位行（表情包、图片…）：不含信息，不进「你最近说过」。"""
    flat = str(text or "").strip()
    return not flat or flat.startswith(_MEDIA_PREFIXES)


def _content_bigrams(text: str) -> set[str]:
    """2-gram 集合：只保留"含中文实义字、且不是口头禅"的组合。

    先剔除 ``@某某``（收件人称呼，canonical 的 :func:`addressing.strip_mentions`）
    与纯英文片段（字母组合巧合太多），让分组键回到"拎回/宿舍/供着"这类真正说明
    "在说同一件事"的词上。
    """
    from core.conversation.addressing import strip_mentions

    flat = strip_mentions(text)
    flat = re.sub(r"\s+", "", flat)
    grams: set[str] = set()
    for index in range(max(0, len(flat) - 1)):
        pair = flat[index : index + 2]
        if pair in _WEAK_GRAMS:
            continue
        if not any("\u4e00" <= char <= "\u9fff" for char in pair):
            continue
        if any(char not in _STOP_CHARS for char in pair):
            grams.add(pair)
    return grams


def _bigram_key(text: Any) -> frozenset[str]:
    """分组键（N3 聚类内核的**唯一**取键处；``_content_bigrams`` 的历史别称）。

    ``_content_bigrams`` 保留原名给既有调用方/用例，实现只此一份——N3
    （``core/context/threads.py``）与 [局况] 的「你最近说过」折叠共用它，
    **不许**再出现第二套 2-gram 取键（规格 §七「收编清单」铁律②）。
    """
    return frozenset(_content_bigrams(str(text or "")))


#: 阈值：键共享到哪个程度才算「同一件事」。默认 1（沿用 ``_fold_echo_texts``
#: 的历史口径）；N3 传同一常数，避免两处各写一个字面量。
KEY_SHARE_MIN = 1


def share_key(left: Any, right: Any) -> bool:
    """两条文本是不是「同一件事」：实义 2-gram **至少共享 ``KEY_SHARE_MIN`` 个**。

    口径来自 ``_fold_echo_texts``（只比代表、不做链式合并；宁可少折不多折）：
    这里把那条判据从"折叠函数内部"提成**一个可被 N3 复用的公开函数**——
    N3（``core/context/threads.py``）与 [局况] 折叠因此永远是同一套判据。
    """
    left_key = left if isinstance(left, frozenset) else _bigram_key(left)
    right_key = right if isinstance(right, frozenset) else _bigram_key(right)
    return len(left_key & right_key) >= KEY_SHARE_MIN


def fold_groups(
    entries: Sequence[Any],
    *,
    key: Any = _bigram_key,
    max_share: int = KEY_SHARE_MIN,
) -> list[list[int]]:
    """通用「按代表折叠」内核：返回**分组下标**（每组保留输入顺序）。

    这是 N3 与 [局况] 共用的**唯一**聚类内核（规格 §七 收编清单第 ① 条：
    ``room_state._content_bigrams`` + ``_fold_echo_texts`` **升格**为 owner 的
    聚类内核，不许第二套）。规则与 ``_fold_echo_texts`` 逐字一致：
    按输入顺序扫描，能挂到**任一组代表**上就挂（取第一个匹配的组），
    否则自己当一组的新代表；**只比代表、不做链式合并**。

    ``key`` 取键、``max_share`` 判定阈值都可覆盖，但默认值就是历史口径——
    调用方不许各写一个字面量（阈值重定义 = 第二套判据）。
    """
    reps: list[frozenset[str]] = []
    groups: list[list[int]] = []
    for index, entry in enumerate(entries):
        entry_key = key(entry)
        for position, group in enumerate(groups):
            if len(reps[position] & entry_key) >= int(max_share):
                group.append(index)
                break
        else:
            groups.append([index])
            reps.append(entry_key)
    return groups


def _fold_echo_texts(texts: Sequence[str]) -> list[tuple[str, int]]:
    """把她的发言折叠成「同一件事」组：组代表=最新一条，计数=组内条数。

    分组判据 = 与**组代表**（最新一条）共享任意一个实义 2-gram。
    只比代表、不做链式合并——否则「小八」这类桥接词会把不同的事串成一组。
    宁可少折不多折：词没重合就分开列。

    实现走 :func:`fold_groups`（N3 与 [局况] 共用的聚类内核）——本函数只做
    "组代表 + 计数"的呈现，判据本身不在这里。
    """
    items = [str(text or "") for text in texts]
    groups = fold_groups(items)
    return [(items[group[0]], len(group)) for group in groups]


def _is_question(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if value.endswith(_QUESTION_TAILS):
        return True
    try:
        from core.conversation.addressing import is_question_or_appeal

        return bool(is_question_or_appeal(value))
    except Exception:
        warn("core/context/room_state.py:266 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def _quote_facts_of(text: Any):
    """``[引用 X: …]`` 的引用事实（canonical；认不出作者时 resolvable=False）。"""
    from core.conversation.addressing import quote_of

    return quote_of(text)


def _has_quote_prefix(text: Any) -> bool:
    """行首是不是引用块（canonical；含认不出作者的形态）。"""
    from core.conversation.addressing import has_quote_prefix

    return has_quote_prefix(text)


def _split_quote(text: Any) -> tuple[str, str]:
    """``[引用 X: 原话] 正文`` → ``(作者, 正文)``（canonical）。"""
    from core.conversation.addressing import split_quote_line

    return split_quote_line(text)


def _attribution_for(text: Any, bot_name: str):
    """一条文本的 canonical 归属（局况内部用；纯文本、无 IO）。"""
    from core.context.attribution import default_context

    return default_context(self_name=str(bot_name or "")).resolve({"content": text})


def _mentions_bot(text: str, bot_name: str) -> bool:
    """这条消息是不是在说她（canonical：@她 / 引用她 / 直呼她名字）。

    2026-09-30 收口：原来只查 ``@鲸鲸`` 字面量，现在交给唯一 owner
    ``core.conversation.addressing.resolve_attribution``；引用块**里面**的
    ``@鲸鲸`` 不算（那是对别人说的历史上下文）。
    """
    attribution = _attribution_for(text, bot_name)
    return bool(attribution is not None and attribution.is_self_addressed)


def _quotes_bot(text: str, bot_name: str) -> bool:
    """这条消息引用的对象是不是她自己（认不出作者的引用不算）。"""
    from core.context.attribution import quote_facts

    quoted, is_self = quote_facts(text, self_names=(str(bot_name or ""),))
    return bool(quoted.present and quoted.resolvable and is_self)


@dataclass(frozen=True)
class RoomTurn:
    """One structural exchange found in the room."""

    kind: str  # "qa" | "quote"
    asker: str
    answerer: str
    question: str
    answer: str


@dataclass(frozen=True)
class RoomSituation:
    speakers: tuple[str, ...] = ()
    turns: tuple[RoomTurn, ...] = ()
    addressed_to_bot: bool = False
    addressed_reason: str = ""
    pending: tuple[str, ...] = ()
    last_self: str = ""
    self_ago: str = ""
    latest_speaker: str = ""
    latest_is_answer: bool = False
    latest_asker: str = ""
    latest_kind: str = ""
    # @错人修法（2026-09-28）：最近一条"请求→你的答复"事实行（无则空）。
    answer_line: str = ""

    def render(self, *, limit: int = 440) -> str:
        """Render the ``[局况]`` block (facts only; behaviour rules live elsewhere)."""
        lines = ["[局况]"]
        if self.speakers:
            lines.append("- 在场：" + "、".join(self.speakers[:5]))
        if self.turns:
            turn = self.turns[0]
            if turn.kind == "quote":
                lines.append(
                    f"- 最近对话：{turn.answerer} 引用 {turn.asker} 的话回应"
                    f"「{_clip(turn.answer, 26)}」"
                )
            else:
                lines.append(
                    f"- 最近对话：{turn.asker} 问「{_clip(turn.question, 22)}」，"
                    f"{turn.answerer} 答「{_clip(turn.answer, 22)}」"
                )
        if self.addressed_to_bot:
            lines.append(f"- 最新消息点到了你：{_clip(self.addressed_reason, 30)}")
        elif self.latest_is_answer and self.latest_asker:
            lines.append(
                f"- 最新这句是 {self.latest_speaker} 在对 {self.latest_asker} 说话，"
                "没有提到你。"
            )
        elif self.latest_speaker:
            lines.append(f"- 最新消息（{self.latest_speaker}）没有提到你。")
        if self.answer_line:
            # @错人修法（2026-09-28）：把"谁问的→你答没答→答给谁"作为事实行给她
            lines.append(self.answer_line)
        if self.pending:
            waiting = "；".join(_clip(item, 26) for item in self.pending[:2])
            lines.append(f"- 有人还在等回应：{waiting}")
        if self.last_self:
            ago = f"{self.self_ago}前" if self.self_ago else "刚才"
            lines.append(f"- 你刚说过：{ago}「{_clip(self.last_self, 22)}」")
        return "\n".join(lines)[:limit]


def _recent_speakers(rows: Sequence[Any], now: datetime, bot_name: str) -> tuple[str, ...]:
    from core.timing import get as _timing_get

    recent_window = timedelta(seconds=_timing_get().room_recent)
    seen: dict[str, datetime] = {}
    for msg in rows:
        if _is_self(msg):
            continue
        stamp = _created_at(msg)
        if stamp is None or now - stamp > recent_window:
            continue
        name = _speaker(msg)
        if name and name not in (bot_name, "?"):
            seen[name] = stamp
    ordered = sorted(seen.items(), key=lambda item: item[1], reverse=True)
    return tuple(name for name, _ in ordered[:6])


def _find_turns(rows: Sequence[Any], bot_name: str) -> tuple[RoomTurn, ...]:
    turns: list[RoomTurn] = []
    # 只扫最近 SCAN_LIMIT 条；注意 range 下界要用 -1，否则当 rows 短于
    # SCAN_LIMIT 时 start 会等于 0，index 0 永远扫不到（批2c 回放时发现：
    # 「问」恰好落在第 0 条时整轮问答识别不出来）。
    start = max(0, len(rows) - SCAN_LIMIT)
    for index in range(len(rows) - 1, start - 1, -1):
        msg = rows[index]
        if _is_self(msg):
            continue
        text = _text(msg)
        # 窗口行（批2b 收口）：作者已由 ``_as_room_row`` 解析出来时用它，
        # 解析不出作者（认不出引用对象）就跳过——不猜。
        if "quoted_author" in (msg if isinstance(msg, dict) else {}):
            if not _get(msg, "quote_attributable"):
                continue
            author = str(_get(msg, "quoted_author") or "") or "?"
            turns.append(
                RoomTurn(
                    kind="quote",
                    asker=author,
                    answerer=_speaker(msg),
                    question="",
                    answer=text,
                )
            )
            continue
        quote = _quote_facts_of(text)
        if quote.present:
            if not quote.resolvable:
                # 认不出引用对象（`[引用〔作者不明〕]`）→ 不猜，不产出这一轮。
                continue
            _, body = _split_quote(text)
            turns.append(
                RoomTurn(
                    kind="quote",
                    asker=quote.author_name or "?",
                    answerer=_speaker(msg),
                    question="",
                    answer=body.strip(" ：:。") or text,
                )
            )
            continue
        if _is_question(text) and index + 1 < len(rows):
            nxt = rows[index + 1]
            if not _is_self(nxt) and _speaker(nxt) != _speaker(msg):
                turns.append(
                    RoomTurn(
                        kind="qa",
                        asker=_speaker(msg),
                        answerer=_speaker(nxt),
                        question=text,
                        answer=_text(nxt),
                    )
                )
    # 最新在前；去掉完全重复
    deduped: list[RoomTurn] = []
    for turn in turns:
        if turn not in deduped:
            deduped.append(turn)
    return tuple(deduped[:3])


def build_room_situation(
    messages: Iterable[Any],
    *,
    pending: Sequence[str] = (),
    bot_name: str = BOT_NAME,
    now: datetime | None = None,
    current: Any | None = None,
) -> RoomSituation:
    """Build the room situation from recent messages (old → new).

    ``current`` appends the inbound message that may not be in the buffer yet.
    Never raises for odd input; returns an empty situation when there is
    nothing to say.
    """
    try:
        rows = [msg for msg in messages]
        if current is not None:
            last_text = _text(rows[-1]) if rows else None
            last_time = _created_at(rows[-1]) if rows else None
            cur_time = _created_at(current)
            duplicated = (
                last_text is not None
                and last_text == _text(current)
                and last_time is not None
                and cur_time is not None
                and abs((cur_time - last_time).total_seconds()) < 120
            )
            if not duplicated:
                rows.append(current)
        if not rows:
            return RoomSituation()

        moment = now or _created_at(rows[-1]) or datetime.now()
        speakers = _recent_speakers(rows, moment, bot_name)
        turns = _find_turns(rows, bot_name)

        latest = rows[-1]
        # 自己是最新一条时不渲染「没提到你」（那是自相矛盾行）。
        latest_speaker = "" if _is_self(latest) else _speaker(latest)
        addressed = False
        reason = ""
        for msg in reversed(rows[-3:]):
            if _is_self(msg):
                continue
            text = _text(msg)
            if bool(_get(msg, "mention_self", False)) or _mentions_bot(text, bot_name):
                addressed, reason = True, f"@{bot_name} {text}"
                break
            quoted_author = str(_get(msg, "quoted_author") or "")
            # 窗口行经 _as_room_row 规范化后，`[引用 X: …]` 前缀被剥进
            # quoted_author；只查 text 会漏掉「引用她」（2b 收口后的回归）。
            if _quotes_bot(text, bot_name) or (bot_name and bot_name in quoted_author):
                addressed, reason = True, f"引用了你：{text}"
                break

        latest_is_answer = False
        latest_asker = ""
        latest_kind = ""
        if not _is_self(latest) and turns:
            top = turns[0]
            if top.answerer == latest_speaker and top.answer == _text(latest):
                latest_is_answer = True
                latest_asker = top.asker
                latest_kind = top.kind

        last_self = ""
        self_ago = ""
        from core.timing import get as _timing_get

        self_ago_window = timedelta(seconds=_timing_get().self_ago)
        for msg in reversed(rows):
            if not _is_self(msg):
                continue
            stamp = _created_at(msg)
            if stamp is not None and moment - stamp <= self_ago_window:
                last_self = _text(msg)
                seconds = int((moment - stamp).total_seconds())
                self_ago = (
                    "不到 1 分钟" if seconds < 60 else f"{seconds // 60} 分钟"
                )
            break

        answer_line = ""
        try:
            from core.context.answer_links import derive_answer_links

            links = derive_answer_links(rows)
            if links:
                latest_link = max(
                    links,
                    key=lambda item: item.reply_time or datetime.min,
                )
                q_time = (
                    latest_link.request_time.strftime("%H:%M")
                    if latest_link.request_time else "--:--"
                )
                a_time = (
                    latest_link.reply_time.strftime("%H:%M")
                    if latest_link.reply_time else "--:--"
                )
                answer_line = (
                    f"- 最近问答：{latest_link.request_speaker or '?'} {q_time} 问"
                    f"「{_clip(latest_link.request_content, 22)}」，"
                    f"你 {a_time} 已答（@{latest_link.target_name or latest_link.request_speaker or '?'}）"
                )
        except Exception:
            warn("core/context/room_state.py:568 Exception", err=repr(__import__("sys").exc_info()[1]))
            answer_line = ""

        return RoomSituation(
            speakers=speakers,
            turns=turns,
            addressed_to_bot=addressed,
            addressed_reason=reason,
            pending=tuple(str(item) for item in pending if str(item).strip()),
            last_self=last_self,
            self_ago=self_ago,
            latest_speaker=latest_speaker,
            latest_is_answer=latest_is_answer,
            latest_asker=latest_asker,
            latest_kind=latest_kind,
            answer_line=answer_line,
        )
    except Exception:
        warn("core/context/room_state.py:585 Exception", err=repr(__import__("sys").exc_info()[1]))
        return RoomSituation()


def build_window_room_situation(
    chat_id: str,
    *,
    pending: Sequence[str] = (),
    bot_name: str = BOT_NAME,
    now: datetime | None = None,
    current: Any | None = None,
    days: int | None = None,
    limit: int | None = None,
    loader: Any | None = None,
) -> RoomSituation | None:
    """批2b 双轨收口的入口：**同一份窗口数据**同时供 prompt 与局况使用。

    与 ``build_room_situation`` 的区别只在数据来源——它读 ``chat_window``
    （terminal_events，与 ``[群聊最近消息]`` 块同源），而不是内存 buffer。
    这样「她看到的原文」和「局况算出的结构」永远出自同一份样本。

    返回 ``None`` 表示这轮不该用窗口（开关关 / 会话不在白名单 / 读取失败），
    调用方应回落到缓冲区版本——**失败的语义是回落，不是放行**，因为
    判据 owner（WP-04 起）在 （已退役的 ``skip_room`` 判据，2026-10-23 删）；``None`` 本身会返回不拦。
    """
    try:
        from core.context.chat_window import load_messages, window_enabled_for

        if not window_enabled_for(str(chat_id or "")):
            return None
        if callable(loader):
            # 注入优先：复现/回放可按历史时点截断喂数（历史 pending 不可得时仍可用）。
            rows = list(loader())
        else:
            rows = list(
                load_messages(str(chat_id), days=int(days) if days else 7)
            )
        if not rows:
            return None
        keep = int(limit) if limit else 2 * SCAN_LIMIT
        rows = rows[-max(4, keep):]
        normalized = [_as_room_row(msg, now=now) for msg in rows]
        if current is not None:
            normalized.append(_as_room_row(current, now=now))
        return build_room_situation(
            normalized,
            pending=pending,
            bot_name=bot_name,
            now=now,
        )
    except Exception:
        warn("core/context/room_state.py:635 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


__all__ = [
    "BOT_NAME",
    "RoomSituation",
    "RoomTurn",
    "build_room_situation",
    "build_window_room_situation",
]
