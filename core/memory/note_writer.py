"""写侧 ①³：每轮随手记（0-3 条）——确定性观察器（B 路线）。

背景：长尾记忆（「当时知道 vs 现在知道」、跨多条拼出来的事实、承诺/约定，以及
文件名/数字/日期这类精确 token）在召回链里天然缺一条来源——**当时没人把它记下来**。
本模块是那条「写」的起点：每轮最多抄 0-3 条原话，每条附一个能回原文的 span。

纪律（写时不承诺）：
- 只做确定性观察：正则看见什么抄什么；不概括、不定性、不判断「这件事重不重要」。
- ``note_text`` / ``quote`` 都是源文本的**字面子串**；``span`` 是半开区间，
  读侧拿 ``(message_id, start, end)`` 就能回原文核对（形状复用 ``EvidenceRef``）。
- 每轮 0-3 条，**允许 0 条**；禁止用空话塞满。
- 默认只抓「不可推导」的 token（文件名/日期/数字/承诺/地名/提及 id）；``bare``（整条就是
  一个短词）默认不抓——实测抽样里它几乎全是噪音，要量这一档请显式 ``include_bare=True``。
- ``note_id`` = 内容哈希（chat_id/message_id/start/end/kind/quote）：同一输入永远同一个 id，回填可复现、可重建整库。
- ``mention`` 档只记 @ 昵称里那串平台 id（QQ 号），原样是对方 id，不是用户说的事。
- 不调 LLM、不写库、不进 prompt、无副作用。

三态开关 ``YINYUE_FEATURE_WORTH_REMEMBERING``：
- ``off``（默认）：调用方不调用本模块 => 逐字旧行为；
- ``shadow``：只产出候选 + 日志，不写库、不进 prompt（本轮量的就是这一档）；
- ``on``：接口已备（``NoteCandidate.to_record()``），写库留给接线切片。

唯一读点：本模块的 :func:`worth_remembering_mode`（``features.mode`` 三态，不是
``enabled``）；flag 名只在 ``core/features.py`` 定义一处，本模块只读常量。
见 ``docs/decisions/穷举整改_写侧随手记.md``。
"""

from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import structlog

from core import features
from core.contracts.events import EvidenceRef
from safelog import warn

log = structlog.get_logger()

MODE_OFF = features.MODE_OFF
MODE_SHADOW = features.MODE_SHADOW
MODE_ON = features.MODE_ON
MODES = (MODE_OFF, MODE_SHADOW, MODE_ON)

NOTE_MAX = 3
QUOTE_MAX = 200
BARE_MAX = 12

SOURCE_DETERMINISTIC = "deterministic"
SOURCE_N1 = "n1"

KIND_FILE = "file"
KIND_DATE = "date"
KIND_NUMBER = "number"
KIND_PROMISE = "promise"
KIND_PLACE = "place"
KIND_MENTION = "mention"
KIND_BARE = "bare"
KINDS = (KIND_FILE, KIND_DATE, KIND_NUMBER, KIND_PROMISE, KIND_PLACE, KIND_MENTION, KIND_BARE)

# 每轮只能留 3 条时的取舍序：越「不可推导」越靠前（文件名/数字/日期是别处补不回来的）。
_KIND_PRIORITY = {
    KIND_FILE: 0,
    KIND_DATE: 1,
    KIND_NUMBER: 2,
    KIND_PROMISE: 3,
    KIND_PLACE: 4,
    KIND_MENTION: 5,
    KIND_BARE: 6,
}

_FILE_RE = re.compile(
    r"[\w\u4e00-\u9fff\-._()\[\]（）【】]{1,60}?\."
    r"(?:pdf|xlsx?|docx?|pptx?|zip|rar|7z|tar|gz|md|py|txt|png|jpe?g|mp4|mp3)",
    re.IGNORECASE,
)
_DATE_YMD_RE = re.compile(r"(?:\d{4}\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*[日号]")
_DATE_CN_RE = re.compile(r"[零一二三四五六七八九十]{1,3}\s*月\s*[零一二三四五六七八九十]{1,3}\s*[日号]")
_TIME_HHMM_RE = re.compile(r"(?:[01]?\d|2[0-3])\s*[:：]\s*[0-5]\d")
_TIME_CN_RE = re.compile(
    r"(?:今天|明天|后天|今晚|明早|明晚|昨晚|昨天|今早|早上|上午|中午|下午|晚上|凌晨)\s*"
    r"(?:[0-2]?\d|[零一二三四五六七八九十]{1,3})\s*[点时](?:\s*(?:半|[0-5]?\d\s*分))?"
)
_TIME_REL_RE = re.compile(r"(?:还差|差|再有|过了)\s*[零一二三四五六七八九十两\d]{1,3}\s*(?:分钟|小时|秒)")
_LONG_NUM_RE = re.compile(r"(?<!\d)\d{4,}(?!\d)")
_REPEAT_NUM_RE = re.compile(r"(?<!\d)(\d)\1{3,}(?!\d)")
_PROMISE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:答应|承诺|说好)(?:了|过)?[^。！？!?\n]{0,12}(?:要|去|会|帮|给|发|带|买|做|写|查|看|陪|找|问)"),
    re.compile(r"(?<!我)(?:记得|别忘了|别忘记|提醒我)[^。！？!?\n]{0,24}"),
    re.compile(r"(?:我|咱|我们|你)(?:明天|后天|下次|回头|待会|待会儿|等会儿|晚点|晚上|明早|下周|下个月)[^。！？!?\n]{0,24}"),
    re.compile(r"[^。！？!?\n]{0,24}(?:等|待)[^。！？!?\n]{0,12}(?:写完|做完|弄完|交完)[^。！？!?\n]{0,24}"),
    re.compile(r"(?:^|[。！？!?\n])\s*(?:发|寄|给|送|传)我(?:一份|一个|一张|一下)"),
)
_PLACE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:来自|老家|籍贯|住在|现居)\s*([\u4e00-\u9fff]{2,8})"),
    re.compile(r"(?:在|去|到)([\u4e00-\u9fff]{2,8}?)(?:上学|读书|上班|工作|实习|出差)"),
)

# 地名（句式里没有模板时的三式）：靠 `_PLACE_NAMES`/后缀门控，
# 避免「今天的天气」这种把时间词当地名。
_PLACE_GATED_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:我|俺)([\u4e00-\u9fff]{2,4})人"),
    re.compile(r"([\u4e00-\u9fff]{2,4}?)(?:市|省)?的天气"),
    re.compile(r"(?:到|去|回)([\u4e00-\u9fff]{2,4})(?=[，。！？、；：\s别先再就的了])"),
)
_GAP_STOP_RE = re.compile(r"[。！？!?\n]")
_BARE_STOP_RE = re.compile(r"[。！？!?，,；;：:\n\t]")

# 媒体占位 / 引用块：里面的数字串是平台 id（表情包 key、引用作者号），不是用户
# 说出来的事实——抽出来的「数字」在真实回合里几乎全是垃圾（2026-10-16 抽样 20 条里 4 条）。
_MEDIA_TAG_RE = re.compile(
    r"\[(?:图片|表情包|视频|动画表情|语音|链接|位置|聊天记录|合并转发|引用)[^\]]{0,300}\]"
)

# 提及（@昵称+数字）：昵称里那串数字是对方的平台 id（QQ 号），不是用户说的事。
# 同 addressing.py 的 token 口径，只是这里整段涂空。
_MENTION_RE = re.compile(
    r"@[^\s@\u2005，。！？、；：,.!?;:\[\]【】（）()<>《》\"'”‘’]{1,40}"
)

# 链接查询串：`?buvid=…23436` 里的数字不是用户说的事（路径里的文件名仍然保留）。
_URL_QUERY_RE = re.compile(r"[?&][^\s，。！？、；：]{1,200}")
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s，。！？、；：)\]]{1,300}")

# B 站 BV 号：URL 整条会被涂空，但 BV 号是用户发来的可寻址 token，先单独抓出来。
_BV_RE = re.compile(r"\bBV[0-9A-Za-z]{8,12}")
# 学年区间（`2026-2027 学年`）：区间本身就是问题要的那个 token。
_YEARSPAN_RE = re.compile(r"(\d{4}\s*[-–~至]\s*\d{4})\s*学年")

# 链接文件名：路径里的 `report.pdf` 仍是用户发来的东西，先抓再涂。

# @ 昵称里带空格的写法（@某某 1234567）：@ 后 40 字内出现的数字串仍算 mention 档。
_MENTION_ID_RE = re.compile(r"@[^\n]{0,40}?(\d{6,})")


_PLACE_WORDS: Any = None
_PLACE_SUFFIXES: Any = None


def _place_cut(segment: str) -> str:
    """把「广州今天」/「后天南宁」这类修饰剪掉，只留地名（复用台账同一份地名表）。"""
    global _PLACE_WORDS, _PLACE_SUFFIXES
    if _PLACE_WORDS is None:
        from core.context.message_facts import _PLACE_NAMES, _PLACE_SUFFIXES as suffixes

        _PLACE_WORDS, _PLACE_SUFFIXES = _PLACE_NAMES, suffixes
    for size in (4, 3, 2):
        for i in range(0, len(segment) - size + 1):
            window = segment[i : i + size]
            if window in _PLACE_WORDS or window.endswith(_PLACE_SUFFIXES):
                return window
    return ""


@dataclass(frozen=True)
class NoteCandidate:
    """一条「随手记」候选：原话片段 + 可回原文的 span。

    ``note_text`` 与 ``quote`` 都是源文本的字面子串（B 路线两者同值；
    A 路线可以让 ``note_text`` 更完整，但 ``span`` 必须仍指回原文）。
    """

    note_text: str
    kind: str
    quote: str
    start: int
    end: int
    message_id: str = ""
    speaker: str = ""
    at: str = ""
    chat_id: str = ""
    source: str = SOURCE_DETERMINISTIC

    def span_ref(self, *, account_id: str = "") -> EvidenceRef:
        """转成 P0-2 的 ``EvidenceRef``（半开区间；-1 表示没记 span）。"""
        return EvidenceRef(
            source=self.source,
            message_id=self.message_id,
            account_id=account_id,
            chat_id=self.chat_id,
            quoted_text=self.quote[:QUOTE_MAX],
            start=self.start,
            end=self.end,
        )

    def note_id(self) -> str:
        """内容哈希 id：同一输入两次回填得到同一个 id（可重建、可去重、可对账）。"""
        digest = hashlib.sha1()
        for part in (
            self.chat_id,
            self.message_id,
            str(self.start),
            str(self.end),
            self.kind,
            self.quote,
        ):
            digest.update(part.encode("utf-8", "ignore"))
            digest.update(b"\x00")
        return "note:" + digest.hexdigest()[:16]

    def to_record(self) -> dict[str, Any]:
        """写侧契约形状：``{note_text, span{message_id,start,end,quote}, speaker, at, chat_id, source}``。"""
        return {
            "note_id": self.note_id(),
            "note_text": self.note_text,
            "span": {
                "message_id": self.message_id,
                "start": self.start,
                "end": self.end,
                "quote": self.quote[:QUOTE_MAX],
            },
            "speaker": self.speaker,
            "at": self.at,
            "chat_id": self.chat_id,
            "source": self.source,
            "kind": self.kind,
        }


def worth_remembering_mode(default: str = MODE_OFF) -> str:
    """三态开关读数（唯一正确读法：``features.mode``，不是 ``enabled``）。

    off = 逐字旧行为；shadow = 只把 ``worth_remembering_shadow`` 落日志（量具）；
    on = 候选可供下游消费（本轮只留接口，尚无产品接线）。
    """
    return features.mode(
        features.WORTH_REMEMBERING, default if default in MODES else MODE_OFF
    )


def worth_remembering_shadow() -> bool:
    return worth_remembering_mode() == MODE_SHADOW


def worth_remembering_enabled() -> bool:
    return worth_remembering_mode() == MODE_ON


def _trim(raw: str, start: int, end: int) -> tuple[int, int, str]:
    seg = raw[start:end]
    lead = len(seg) - len(seg.lstrip())
    seg = seg.strip()
    start = start + lead
    return start, start + len(seg), seg


def _find(pattern: re.Pattern[str], text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in pattern.finditer(text)]


def _date_spans(text: str) -> list[tuple[int, int]]:
    """日期 + 相邻时刻合成一个 span：一条时间信息不该被切成两段。"""
    spans: list[tuple[int, int]] = []
    for pattern in (_DATE_YMD_RE, _DATE_CN_RE, _TIME_HHMM_RE, _TIME_CN_RE, _TIME_REL_RE):
        spans.extend(_find(pattern, text))
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged:
            prev_start, prev_end = merged[-1]
            gap = text[prev_end:start]
            if start - prev_end <= 12 and not _GAP_STOP_RE.search(gap):
                merged[-1] = (prev_start, end)
                continue
        merged.append((start, end))
    return merged


def _dedupe(spans: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """同一段文字只留一个候选：按取舍序 + 起点排序，被已选 span 覆盖/交叠的丢弃。"""
    ordered = sorted(
        spans,
        key=lambda s: (_KIND_PRIORITY.get(s[2], 99), s[0], -(s[1] - s[0])),
    )
    kept: list[tuple[int, int, str]] = []
    for start, end, kind in ordered:
        if any(not (end <= kstart or start >= kend) for kstart, kend, _ in kept):
            continue
        kept.append((start, end, kind))
    return kept


def observe_message(
    text: object,
    *,
    message_id: str = "",
    speaker: str = "",
    at: str = "",
    chat_id: str = "",
    limit: int = NOTE_MAX,
    include_bare: bool = False,
    source: str = SOURCE_DETERMINISTIC,
    base_offset: int = 0,
) -> tuple[NoteCandidate, ...]:
    """一条消息 -> 0..``limit`` 条候选（确定性，同一输入同一输出）。

    ``include_bare`` 默认 False：整条消息就是一个短词时，确定性观察器分不出
    「值得记的词」和「随口一句」（实测抽样里 bare 几乎全是噪音），所以不抓；
    要量这一档就显式打开（A 路线/量具用）。媒体占位块里的数字串一律不算。

    ``base_offset`` = 这段文本在整条消息里的起点（事件表的 ``content_start``）；
    默认 0 时输出逐字不变。偏移是**对消息**的，不是对某一次截取的窗口的。
    """
    raw = str(text or "")
    if not raw.strip():
        return ()
    base = max(0, int(base_offset or 0))
    # 只在「用户可见文本」上抓：媒体占位/引用块整段涂空，偏移量不变。
    visible = _MENTION_RE.sub(lambda m: " " * len(m.group(0)), raw)
    visible = _MEDIA_TAG_RE.sub(lambda m: " " * len(m.group(0)), visible)
    visible = _URL_QUERY_RE.sub(lambda m: " " * len(m.group(0)), visible)
    found: list[tuple[int, int, str]] = []
    for url_start, url_end in (m.span() for m in _URL_RE.finditer(raw)):
        for s, e in _find(_FILE_RE, raw[url_start:url_end]):
            found.append((url_start + s, url_start + e, KIND_FILE))
        for s, e in _find(_BV_RE, raw[url_start:url_end]):
            found.append((url_start + s, url_start + e, KIND_NUMBER))
    visible = _URL_RE.sub(lambda m: " " * len(m.group(0)), visible)
    found.extend((s, e, KIND_FILE) for s, e in _find(_FILE_RE, visible))
    # 提及里的平台 id（@昵称+QQ 号）：字面就是原文里的 token，单独一档记，
    # 不冒充「用户说出来的数字事实」（bench 里确实要这两个 id，所以不能丢）。
    for match in _MENTION_RE.finditer(raw):
        digits = _LONG_NUM_RE.search(match.group(0))
        if digits and len(digits.group(0)) >= 6:
            start = match.start() + digits.start()
            found.append((start, start + len(digits.group(0)), KIND_MENTION))
    found.extend((s, e, KIND_NUMBER) for s, e in _find(_LONG_NUM_RE, visible))
    for match in _MENTION_ID_RE.finditer(raw):
        start, end = match.span(1)
        found.append((start, end, KIND_MENTION))
    found.extend((s, e, KIND_NUMBER) for s, e in _find(_REPEAT_NUM_RE, visible))
    found.extend((s, e, KIND_DATE) for s, e in _date_spans(visible))
    for match in _YEARSPAN_RE.finditer(raw):
        start, end = match.span(1)
        found.append((start, end, KIND_NUMBER))
    for pattern in _PROMISE_RES:
        for start, end in _find(pattern, visible):
            segment = visible[start:end].rstrip()
            if segment.endswith(("吗", "呢", "么")):
                continue
            found.append((start, end, KIND_PROMISE))
    for pattern in _PLACE_RES:
        for match in pattern.finditer(visible):
            start, end = (match.start(1), match.end(1)) if match.groups() else match.span()
            segment = visible[start:end]
            if "的" in segment or len(segment) > 6:
                continue
            found.append((start, end, KIND_PLACE))
    for pattern in _PLACE_GATED_RES:
        for match in pattern.finditer(visible):
            start, end = match.span(1)
            cut = _place_cut(visible[start:end])
            if not cut:
                continue
            off = visible.index(cut, start, end)
            found.append((off, off + len(cut), KIND_PLACE))
    stripped = raw.strip()
    if (
        include_bare
        and stripped
        and len(stripped) <= BARE_MAX
        and not _BARE_STOP_RE.search(stripped)
        and not _MEDIA_TAG_RE.search(raw)
        and not _MENTION_RE.search(raw)
        and not stripped.startswith("<")
    ):
        offset = raw.find(stripped)
        found.append((offset, offset + len(stripped), KIND_BARE))
    # 平台 id 已经按 mention 记了，就别再当成「用户说出来的数字」。
    id_spans = [(s, e) for s, e, kind in found if kind == KIND_MENTION]
    found = [
        item
        for item in found
        if not (item[2] == KIND_NUMBER and any(s <= item[0] and item[1] <= e for s, e in id_spans))
    ]
    kept = _dedupe(found)
    kept.sort(key=lambda s: (_KIND_PRIORITY.get(s[2], 99), s[0]))
    out: list[NoteCandidate] = []
    for start, end, kind in kept[: max(0, int(limit))]:
        start, end, seg = _trim(raw, start, end)
        if not seg:
            continue
        out.append(
            NoteCandidate(
                note_text=seg[:QUOTE_MAX],
                kind=kind,
                quote=seg[:QUOTE_MAX],
                start=start + base,
                end=end + base,
                message_id=message_id,
                speaker=speaker,
                at=at,
                chat_id=chat_id,
                source=source,
            )
        )
    out.sort(key=lambda c: c.start)
    return tuple(out)


def content_base(row: Any) -> int:
    """行的 ``content_start``（-1/缺失 = 未知，按 0 处理）：span 一律记绝对偏移。"""
    value = row.get("content_start") if isinstance(row, dict) else getattr(row, "content_start", None)
    try:
        start = int(value)
    except (TypeError, ValueError):
        warn("core/memory/note_writer.py:408 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return 0
    return start if start > 0 else 0


def _row_field(row: Any, *names: str) -> str:
    for name in names:
        value = row.get(name) if isinstance(row, dict) else getattr(row, name, None)
        if value:
            return str(value)
    return ""


def observe_turn(
    rows: Iterable[Any],
    *,
    chat_id: str = "",
    limit: int = NOTE_MAX,
    include_bare: bool = False,
) -> tuple[NoteCandidate, ...]:
    """一轮（可能含多条入站行）合起来最多 ``limit`` 条；允许 0 条。"""
    budget = max(0, int(limit))
    out: list[NoteCandidate] = []
    for row in rows:
        if budget <= 0:
            break
        candidates = observe_message(
            _row_field(row, "content", "text"),
            message_id=_row_field(row, "message_id", "event_id"),
            speaker=_row_field(row, "speaker", "name", "actor"),
            at=_row_field(row, "at", "occurred_at"),
            chat_id=chat_id or _row_field(row, "chat_id"),
            include_bare=include_bare,
            limit=budget,
            base_offset=content_base(row),
        )
        out.extend(candidates)
        budget -= len(candidates)
    return tuple(out)


def shadow_record(
    candidates: Sequence[NoteCandidate],
    *,
    chat_id: str = "",
    message_id: str = "",
    logger: Any = None,
) -> dict[str, Any]:
    """shadow 档：只写日志（不写库、不进 prompt），并返回量具要的载荷。

    ``off`` 档调用方就不该走到这里；即便走到，也只返回载荷、不写任何日志。
    """
    records = [c.to_record() for c in candidates]
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "n": len(records),
        "notes": records,
    }
    if worth_remembering_shadow() or worth_remembering_enabled():
        (logger or log).info(
            "worth_remembering_shadow",
            chat_id=chat_id,
            message_id=message_id,
            n=len(records),
            kinds=[c.kind for c in candidates],
            spans=[(c.start, c.end) for c in candidates],
        )
    return payload


__all__ = [
    "content_base",
    "BARE_MAX",
    "KINDS",
    "KIND_MENTION",
    "KIND_BARE",
    "KIND_DATE",
    "KIND_FILE",
    "KIND_NUMBER",
    "KIND_PLACE",
    "KIND_PROMISE",
    "MODE_OFF",
    "MODE_ON",
    "MODE_SHADOW",
    "MODES",
    "NOTE_MAX",
    "NoteCandidate",
    "QUOTE_MAX",
    "SOURCE_DETERMINISTIC",
    "SOURCE_N1",
    "observe_message",
    "observe_turn",
    "shadow_record",
    "worth_remembering_enabled",
    "worth_remembering_mode",
    "worth_remembering_shadow",
]
