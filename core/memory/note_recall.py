"""读侧第三条注入路线：随手记（notes）——六透镜 + 确定性融合。

写侧是 ``core/memory/note_writer.py``（写时不承诺：只抄原话 + span + 说话人 + 时间）；
本模块是**读时透镜**：把回填出来的笔记按六个确定性透镜打分排序，取前
``NOTES_MAX_ITEMS`` 条交给记忆工具/召回读侧渲染（2026-10-24：原来由
``agents/conversational.py::_needs_memory_block`` 渲成 system 块，那一族已退役，
现在结果只走工具回执与读数）。写时不承诺、读时才选，这里不新增任何写侧语义。

纪律：
- **span 完整性硬校验**：``row.content[start:end] == quote``；对不上的丢弃并计
  ``span_broken``（只进量具，不进 prompt）。没有 span 的候选一律不许进 prompt。
- **预算**：notes ≤ ``NOTES_MAX_ITEMS`` 行（防挤占 chain_struct / KV 的名额）。
- **可解释**：``detail["note_recall"]`` 留 picked（含每个透镜的分）与 rejected top-3，
  校准权重时不需要重跑模型。
- 三态复用 ``YINYUE_FEATURE_WORTH_REMEMBERING``：off 直接返回（零 IO）/ shadow 只算
  与日志 / on 才返回条目。
- 不调 LLM；只读回填文件（``.test_tmp/notes_backfill/``，可用 ``YINYUE_NOTES_DIR``
  覆盖）与只读 DB（``core.memory.recall_chain.load_rows``）。
"""

from __future__ import annotations

import json
from dataclasses import replace
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import structlog

from core.memory.contracts import MemoryItem
from core import features
from core.memory.note_writer import (
    MODE_OFF as NOTE_MODE_OFF,
    MODE_ON as NOTE_MODE_ON,
    NOTE_MAX,
    worth_remembering_mode,
)
from core.memory import recall_chain as rc
from safelog import warn
from core.infra.envflags import text

log = structlog.get_logger()

NOTE_KIND = "notes"
NOTES_MAX_ENV = "YINYUE_NOTES_MAX_ITEMS"


def notes_max_items() -> int:
    """notes 预算上限：默认 ``NOTES_MAX_ITEMS``（3，防挤占）；环境变量只用于量具对比，不改产品默认。"""
    raw = str(os.environ.get(NOTES_MAX_ENV, "")).strip()
    if not raw:
        return NOTES_MAX_ITEMS   # 2026-11-24：.env 里留空串时别每拍 warn 一次（实测一天几十条噪声）
    try:
        value = int(raw)
    except (TypeError, ValueError):
        warn("core/memory/note_recall.py:54 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return NOTES_MAX_ITEMS
    if value < 1 or value > 8:
        return NOTES_MAX_ITEMS
    return value
NOTES_MAX_ITEMS = 3
CONTENT_MAX = 118
REJECTED_TOP = 3
CHAIN_TOPK = 20
RECENT_ROWS = 3
RECENT_ROWS_ENV = "YINYUE_NOTES_RECENT_ROWS"
GATE_SCORE_ENV = "YINYUE_NOTES_GATE_SCORE"


def recent_rows_count() -> int:
    """指代窗口行数（默认 3）：量具可用 ``YINYUE_NOTES_RECENT_ROWS``（1..6，非法回落 3）。"""
    raw = str(os.environ.get(RECENT_ROWS_ENV, "")).strip()
    if not raw:
        return RECENT_ROWS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        warn("core/memory/note_recall.py:73 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return RECENT_ROWS
    if value < 1 or value > 6:
        return RECENT_ROWS
    return value


def notes_gate_score() -> float | None:
    """chain 够不够强的门控阈值（默认关闭 = None）：量具用 ``YINYUE_NOTES_GATE_SCORE``。"""
    raw = os.environ.get(GATE_SCORE_ENV, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        warn("core/memory/note_recall.py:87 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return None
    return value


NOTES_GATE_CUES: tuple[str, ...] = ("上次", "那个", "之前", "记得", "叫什么", "哪张", "谁")
NOTES_GATE_COV_ENV = "YINYUE_NOTES_GATE_COV"
NOTES_GATE_COV_MAX = 0.35


def notes_gate_mode() -> str:
    """双条件机械闸档位（off/shadow/on，默认 off）：本模块是唯一三态读点。"""
    mode = features.mode(features.NOTES_GATE, NOTE_MODE_OFF)
    return mode if isinstance(mode, str) and mode else NOTE_MODE_OFF


def notes_gate_cov_max() -> float:
    """覆盖率阈值（默认 0.35）：量具可用 ``YINYUE_NOTES_GATE_COV`` 覆盖（0..1，非法回落）。"""
    raw = os.environ.get(NOTES_GATE_COV_ENV, "").strip()
    if not raw:
        return NOTES_GATE_COV_MAX
    try:
        value = float(raw)
    except (TypeError, ValueError):
        warn("core/memory/note_recall.py:110 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return NOTES_GATE_COV_MAX
    if value < 0.0 or value > 1.0:
        return NOTES_GATE_COV_MAX
    return value


_PUNCT_RE = re.compile(r"[\s\u3000!-/:-@\[-`{-~，。！？、；：（）【】《》”‘’…·—～]+")


def _norm_text(value: object) -> str:
    return _PUNCT_RE.sub("", str(value or "")).lower()


def query_coverage(query: str, window_text: str) -> float:
    """问句 token 在窗口文本里的覆盖率（1 = 全部出现）；token 口径同召回链 ``rc.keyword_tokens``。"""
    tokens = [str(token) for token in rc.keyword_tokens(str(query or "")) if str(token).strip()]
    if not tokens:
        return 0.0
    haystack = _norm_text(window_text)
    if not haystack:
        return 0.0
    hits = sum(1 for token in tokens if _norm_text(token) and _norm_text(token) in haystack)
    return round(hits / len(tokens), 4)


def gate_decision(query: str, window_text: str) -> dict[str, Any]:
    """双条件：cue 词命中 **且** ``cov < cov_max`` 才 fire（单独任一条件都不够）。"""
    question = str(query or "")
    cues = [cue for cue in NOTES_GATE_CUES if cue in question]
    cov = query_coverage(question, window_text)
    cov_max = notes_gate_cov_max()
    return {
        "cue": bool(cues),
        "cue_hits": cues,
        "cov": cov,
        "cov_max": cov_max,
        "fire": bool(cues) and cov < cov_max,
    }

REFERENT_CUES: tuple[str, ...] = (
    "上次", "那个", "之前", "记得", "叫什么", "哪张", "谁", "提过", "说过", "回忆", "忘了",
)
REFERENT_OVERLAP_ENV = "YINYUE_NOTES_REFERENT_OVERLAP"
REFERENT_SCORE_ENV = "YINYUE_NOTES_REFERENT_CHAIN_SCORE"
REFERENT_OVERLAP_MAX = 0.20
REFERENT_CHAIN_SCORE_MAX = 6.0


def _env_ratio(name: str, default: float, *, low: float = 0.0, high: float = 1.0) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        warn("core/memory/note_recall.py:165 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return default
    if value < low or value > high:
        return default
    return value


def referent_overlap_max() -> float:
    """指代判定用的 n-gram 重叠上限（默认 0.20，可 env 覆盖）。"""
    return _env_ratio(REFERENT_OVERLAP_ENV, REFERENT_OVERLAP_MAX)


def referent_chain_score_max() -> float:
    """指代判定用的 chain 首条分上限（默认 6.0，可 env 覆盖）。"""
    return _env_ratio(REFERENT_SCORE_ENV, REFERENT_CHAIN_SCORE_MAX, low=0.0, high=100.0)


def referent_overlap3(question: str, content: str, n: int = 3) -> float:
    """问句字符 n-gram 被 content 覆盖的比例（与 ``.test_tmp/task15/overlap_probe.py`` 同口径）。"""
    q = str(question or "")
    if len(q) < n:
        return 0.0

    def grams(text: str) -> set[str]:
        return {text[i:i + n] for i in range(max(0, len(text) - n + 1))}

    base = grams(q)
    if not base:
        return 0.0
    return round(len(base & grams(str(content or ""))) / len(base), 4)


def is_referent_question(
    question: str,
    *,
    chain_content: str = "",
    chain_score: float | None = None,
    cues: Sequence[str] = REFERENT_CUES,
    overlap_max: float | None = None,
    chain_score_max: float | None = None,
) -> bool:
    """是否「指代题」：线索词命中 **且** chain 首条重叠 <= 上限 **且** chain 首条分 < 上限。

    纪律：闸门写在纯函数 ``merge_notes_first`` **之外**（调用方/provider 侧判），
    这样 merge 的语义可以被单独钉死；非指代题必须走逐字旧路径。
    """
    text = str(question or "")
    if not any(cue in text for cue in cues):
        return False
    limit_overlap = referent_overlap_max() if overlap_max is None else float(overlap_max)
    if referent_overlap3(text, chain_content) > limit_overlap:
        return False
    if chain_score is not None:
        limit_score = referent_chain_score_max() if chain_score_max is None else float(chain_score_max)
        if float(chain_score) >= limit_score:
            return False
    return True


def recent_text(rows: Sequence[dict[str, Any]], context: Any = None) -> str:
    """当前回合的最近窗口文本：context 显式给的优先（recent_text/recent_rows），否则取窗口末尾 N 行。"""
    limit = recent_rows_count()
    if isinstance(context, dict):
        provided = str(context.get("recent_text") or "").strip()
        if provided:
            return provided
        window = context.get("recent_rows") or context.get("window_rows")
        if isinstance(window, (list, tuple)) and window:
            parts = [str(row.get("content") or "") if isinstance(row, dict) else str(row) for row in window[-limit:]]
            return " ".join(part for part in parts if part).strip()
    return " ".join(str(row.get("content") or "") for row in list(rows)[-limit:]).strip()


def speaker_key_of(principal: Any, rows: Sequence[dict[str, Any]], context: Any = None) -> str:
    """当前回合说话人 id：依次看 context dict → context 属性 → principal 属性 → 窗口最后一行 actor。

    生产路径里 principal 是同一个从不变的权限主体（不带发话人），所以最后一步落到窗口最新一行——那正是刚进来的这条消息。"""
    keys = ("speaker_key", "speaker", "sender_id", "sender_name", "current_sender_id", "current_speaker")
    if isinstance(context, dict):
        for name in keys:
            key = str(context.get(name) or "").strip()
            if key:
                return key
    elif context is not None:
        for name in keys:
            key = str(getattr(context, name, "") or "").strip()
            if key:
                return key
    for attr in ("person_id", "sender_id", "speaker_key", "speaker", "sender_name"):
        key = str(getattr(principal, attr, "") or "").strip()
        if key:
            return key
    if rows:
        return str(rows[-1].get("actor") or "").strip()
    return ""

# 六个透镜的权重（合成 = Σ 权重 × 该透镜的 [0,1] 读数）。放一处、可整表覆盖，
# 便于校准：数字本身不是判定逻辑，读时排序才是。
NOTE_LENS_WEIGHTS: dict[str, float] = {
    "kind": 3.0,
    "overlap": 2.0,
    "locality": 1.5,
    "chain": 1.0,
    "speaker": 1.0,
    "recency": 0.5,
}

_Q_FILE = re.compile(r"文件|压缩包|作业|附件|pdf|zip|docx?|xlsx?|全名|发我的|发给你的|发的那份", re.I)
_Q_DATE = re.compile(r"几月|几号|哪一天|日期|什么时候|哪年|\d{1,2}\s*月\s*\d{1,2}\s*[日号]")
_Q_NUM = re.compile(r"多少|几|数字|号码|串号|一串|连着|重复|编号|金额")
_Q_PLACE = re.compile(r"哪里|哪儿|在哪|什么地方|哪个城市|哪座城市|城市|哪个省|哪个市|哪个地方|地名|哪的|哪儿人|来自")
# 注意：不能把「记得」当承诺信号——「你还记得…吗？」是回忆动词（v3 里有题面就是这句）。
_Q_PROMISE = re.compile(r"答应|说好|约定|承诺|提醒我|别忘了|约好")
_Q_MENTION = re.compile(r"@|昵称|串号|尾巴|末尾")

_CID_SAFE = re.compile(r"[^0-9A-Za-z_.@-]+")


def notes_dir() -> Path:
    """回填目录：``YINYUE_NOTES_DIR`` 优先，否则仓库内 ``.test_tmp/notes_backfill``。"""
    raw = text("YINYUE_NOTES_DIR").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[2] / ".test_tmp" / "notes_backfill"


def store_path(chat_id: str, *, directory: Path | None = None) -> Path:
    safe = _CID_SAFE.sub("_", str(chat_id or "").strip()) or "unknown"
    return (directory or notes_dir()) / f"notes_{safe}.jsonl"


def load_notes(
    chat_id: str,
    *,
    directory: Path | None = None,
    cache: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """读一个会话的回填笔记（文件顺序即回填顺序，确定性）。缺失文件 = 空。"""
    key = str(chat_id or "")
    if cache is not None and key in cache:
        return cache[key]
    path = store_path(key, directory=directory)
    notes: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except Exception:  # noqa: BLE001 - 坏行跳过，不拖垮整次读
                    warn("core/memory/note_recall.py:316 Exception", err=repr(__import__("sys").exc_info()[1]))
                    continue
                if isinstance(record, dict):
                    notes.append(record)
    except FileNotFoundError:
        warn("core/memory/note_recall.py:320 FileNotFoundError", err=repr(__import__("sys").exc_info()[1]))
        notes = []
    except Exception:  # noqa: BLE001 - 回填缺失/不可读不该弄坏一轮对话
        warn("core/memory/note_recall.py:322 Exception", err=repr(__import__("sys").exc_info()[1]))
        notes = []
    if cache is not None:
        cache[key] = notes
    return notes


def backfill_chat(
    rows: Sequence[dict[str, Any]],
    *,
    include_bare: bool = False,
    limit: int = NOTE_MAX,
) -> list[dict[str, Any]]:
    """纯函数：一个会话的窗口行 -> 笔记记录（同一输入两次逐字节相同）。

    ``rows`` 用 ``recall_chain.load_rows`` 的形状（``event_id/at/name/actor/content``）。
    ``row_index`` 一并写进记录：量具靠它判断「金行有没有被回填出 note」。
    """
    from core.memory import note_writer as nw

    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        text = str(row.get("content") or "")
        if not text:
            continue
        candidates = nw.observe_message(
            text,
            message_id=str(row.get("event_id") or ""),
            speaker=str(row.get("name") or row.get("actor") or ""),
            at=str(row.get("at") or ""),
            chat_id=str(row.get("chat_id") or ""),
            include_bare=include_bare,
            limit=limit,
        )
        for candidate in candidates:
            record = candidate.to_record()
            record["actor"] = str(row.get("actor") or "")
            record["row_index"] = index
            out.append(record)
    return out


def question_kinds(query: str) -> tuple[str, ...]:
    """问题题型 -> 可能命中的笔记档（多标签；认不出就空）。"""
    text = str(query or "")
    kinds: list[str] = []
    if _Q_FILE.search(text):
        kinds.append("file")
    if _Q_DATE.search(text):
        kinds.append("date")
    if _Q_NUM.search(text):
        kinds.append("number")
    if _Q_PLACE.search(text):
        kinds.append("place")
    if _Q_PROMISE.search(text):
        kinds.append("promise")
    if _Q_MENTION.search(text):
        kinds.append("mention")
    return tuple(kinds)


def _parse_at(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for candidate in (text.replace(" ", "T", 1), text):
        try:
            return datetime.fromisoformat(candidate)
        except Exception:  # noqa: BLE001
            warn("core/memory/note_recall.py:390 Exception", err=repr(__import__("sys").exc_info()[1]))
            continue
    return None


def _short_time(value: Any) -> str:
    moment = _parse_at(value)
    if moment is None:
        return str(value or "")[:16]
    return moment.strftime("%m-%d %H:%M")


def index_by_message(rows: Sequence[dict[str, Any]]) -> dict[str, tuple[int, dict[str, Any]]]:
    """按 event_id 建索引：窗口会漂（活库会插入更早的行），行号只是快照。"""
    out: dict[str, tuple[int, dict[str, Any]]] = {}
    for index, row in enumerate(rows):
        key = str(row.get("event_id") or "")
        if key and key not in out:
            out[key] = (index, row)
    return out


def _message_id_of(note: dict[str, Any]) -> str:
    """笔记落盘时 message_id 在 span 里（兼容有人把它放顶层）。"""
    span = note.get("span") if isinstance(note.get("span"), dict) else {}
    return str(span.get("message_id") or note.get("message_id") or "")


def locate_note(
    note: dict[str, Any],
    rows: Sequence[dict[str, Any]],
    index: dict[str, tuple[int, dict[str, Any]]] | None = None,
) -> tuple[int, dict[str, Any]] | None:
    """先按 message_id 定位（稳定），再退回存储的行号（快照）。"""
    message_id = _message_id_of(note)
    if index is not None and message_id:
        found = index.get(message_id)
        if found is not None:
            return found
    try:
        row_index = int(note.get("row_index", -1))
    except (TypeError, ValueError):
        warn("core/memory/note_recall.py:431 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return None
    if 0 <= row_index < len(rows):
        return row_index, rows[row_index]
    return None


def _content_base(row: dict[str, Any]) -> int:
    """行的 ``content_start``（-1/缺失 = 未知，按 0 处理）。"""
    try:
        start = int(row.get("content_start", -1))
    except (TypeError, ValueError):
        warn("core/memory/note_recall.py:442 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return 0
    return start if start > 0 else 0


def validate_note_ex(
    note: dict[str, Any],
    rows: Sequence[dict[str, Any]],
    index: dict[str, tuple[int, dict[str, Any]]] | None = None,
) -> tuple[str, str]:
    """span 硬校验 -> (失败原因, 命中方式)；``""`` = 通过。

    命中方式：``span``（偏移直接对上）、``relative``（偏移是相对 content 的，
    加上 ``content_start`` 后对上）、``relocated``（偏移对不上但 quote 在正文里唯一，
    只认这一处；计 ``span_relocated`` 量具，不算 broken）。
    """
    located = locate_note(note, rows, index)
    if located is None:
        return "row_missing", ""
    _position, row = located
    message_id = _message_id_of(note)
    row_id = str(row.get("event_id") or "")
    if not message_id or (row_id and message_id != row_id):
        return "message_id_mismatch", ""
    span = note.get("span") if isinstance(note.get("span"), dict) else {}
    quote = str(span.get("quote") or note.get("quote") or "")
    try:
        start = int(span.get("start", note.get("start", -1)))
        end = int(span.get("end", note.get("end", -1)))
    except (TypeError, ValueError):
        warn("core/memory/note_recall.py:471 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return "span_not_int", ""
    body = str(row.get("content") or "")
    if not quote or start < 0 or end <= start:
        return "span_out_of_range", ""
    base = _content_base(row)
    if base and end - base <= len(body) and start >= base and body[start - base : end - base] == quote:
        return "", "relative"
    if end <= len(body) and body[start:end] == quote:
        return "", "span"
    hit = body.find(quote)
    if hit >= 0 and body.find(quote, hit + 1) < 0:
        return "", "relocated"
    if hit >= 0:
        return "quote_ambiguous", ""
    return "quote_mismatch", ""


def validate_note(
    note: dict[str, Any],
    rows: Sequence[dict[str, Any]],
    index: dict[str, tuple[int, dict[str, Any]]] | None = None,
) -> str:
    """span 完整性硬校验：返回 "" 表示通过，否则返回失败原因。"""
    return validate_note_ex(note, rows, index)[0]
def _lens_kind(note: dict[str, Any], wanted: Sequence[str]) -> float:
    if not wanted:
        return 0.0
    return 1.0 if str(note.get("kind") or "") in wanted else 0.0


def _lens_overlap(note: dict[str, Any], query: str) -> float:
    tokens = [token for token in rc.keyword_tokens(query) if token]
    if not tokens:
        return 0.0
    body = str(note.get("quote") or note.get("note_text") or "")
    hits = sum(1 for token in tokens if token in body)
    return min(1.0, hits / float(len(tokens)))


def _lens_locality(
    note: dict[str, Any],
    rows: Sequence[dict[str, Any]],
    index: dict[str, tuple[int, dict[str, Any]]] | None = None,
) -> float:
    located = locate_note(note, rows, index)
    if located is None or not rows:
        return 0.0
    position, _row = located
    distance = max(0, len(rows) - 1 - position)
    return 1.0 / (1.0 + distance / 200.0)


def _lens_speaker(
    note: dict[str, Any],
    query: str,
    speaker_hint: str = "",
    *,
    speaker_key: str = "",
    recent_text: str = "",
) -> float:
    """说话人透镜：与当前回合说话人同人 = 1.0；名字出现在问句/最近窗口 = 0.5；否则 0。"""
    note_keys = {
        str(note.get("actor") or "").strip(),
        str(note.get("speaker") or "").strip(),
    }
    if speaker_key and speaker_key in note_keys:
        return 1.0
    pool = f"{query or ""} {recent_text or ""}"
    for name in {str(note.get("speaker") or "").strip(), str(speaker_hint or "").strip()}:
        if len(name) >= 2 and name in pool:
            return 0.5
    return 0.0


def _lens_recency(note: dict[str, Any], rows: Sequence[dict[str, Any]]) -> float:
    if len(rows) < 2:
        return 0.0
    moment = _parse_at(note.get("at"))
    first = _parse_at(rows[0].get("at"))
    last = _parse_at(rows[-1].get("at"))
    if moment is None or first is None or last is None or last <= first:
        return 0.0
    return max(0.0, min(1.0, (moment - first).total_seconds() / (last - first).total_seconds()))


def _lens_chain(
    note: dict[str, Any],
    chain_rows: set[int],
    index: dict[str, tuple[int, dict[str, Any]]] | None = None,
) -> float:
    located = locate_note(note, None, None) if index is None else locate_note(note, None, index)
    if located is None:
        return 0.0
    position, _row = located
    return 1.0 if position in chain_rows else 0.0


def score_note(
    note: dict[str, Any],
    rows: Sequence[dict[str, Any]],
    *,
    query: str = "",
    wanted: Sequence[str] = (),
    chain_rows: set[int] | None = None,
    speaker_hint: str = "",
    speaker_key: str = "",
    recent_text: str = "",
    index: dict[str, tuple[int, dict[str, Any]]] | None = None,
    weights: dict[str, float] | None = None,
) -> tuple[float, dict[str, float]]:
    """六透镜读数 -> (总分, 每透镜贡献)，全部确定性。"""
    table = weights or NOTE_LENS_WEIGHTS
    parts = {
        "kind": _lens_kind(note, wanted),
        "overlap": _lens_overlap(note, query),
        "locality": _lens_locality(note, rows, index),
        "chain": _lens_chain(note, chain_rows or set(), index),
        "speaker": _lens_speaker(note, query, speaker_hint, speaker_key=speaker_key, recent_text=recent_text),
        "recency": _lens_recency(note, rows),
    }
    total = sum(float(table.get(name, 0.0)) * value for name, value in parts.items())
    return round(total, 6), {name: round(value, 6) for name, value in parts.items()}


def write_backfill(
    chats: Sequence[str],
    *,
    directory: Path | None = None,
    include_bare: bool = False,
    rows_loader: Any = None,
) -> dict[str, Any]:
    """把若干会话的回填写盘（JSONL + manifest），返回 manifest。"""
    target = directory or notes_dir()
    target.mkdir(parents=True, exist_ok=True)
    loader = rows_loader or rc.load_rows
    manifest: dict[str, Any] = {
        "schema": 1,
        "include_bare": bool(include_bare),
        "window": "recall_chain.load_rows(limit=ROW_LIMIT)",
        "chats": {},
    }
    for chat_id in chats:
        rows = list(loader(chat_id))
        records = backfill_chat(rows, include_bare=include_bare)
        path = store_path(chat_id, directory=target)
        body = "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records)
        path.write_text(body, encoding="utf-8", newline="")
        kinds: dict[str, int] = {}
        for record in records:
            key = str(record.get("kind") or "")
            kinds[key] = kinds.get(key, 0) + 1
        import hashlib

        manifest["chats"][chat_id] = {
            "rows": len(rows),
            "notes": len(records),
            "kinds": dict(sorted(kinds.items())),
            "sha1": hashlib.sha1(body.encode("utf-8")).hexdigest()[:16],
            "file": path.name,
        }
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="",
    )
    return manifest


def _render_content(note: dict[str, Any]) -> str:
    who = str(note.get("speaker") or note.get("actor") or "?").strip()
    content = f"[{_short_time(note.get('at'))} {who}] {str(note.get('quote') or note.get('note_text') or '').strip()}"
    if len(content) > CONTENT_MAX:
        content = content[: CONTENT_MAX - 1] + "…"
    return content


class NoteRecallProvider:
    """读时透镜 provider：kind = ``notes``（回填笔记 -> 预算内条目）。"""

    kind = NOTE_KIND

    def __init__(
        self,
        *,
        directory: Path | None = None,
        verify_evidence: bool = False,
    ) -> None:
        self.directory = directory
        self.verify_evidence = bool(verify_evidence)

    def _evidence(self, row: dict[str, Any], principal: Any, chat_id: str) -> tuple[str, str]:
        event_id = str(row.get("event_id") or "").strip()
        if not event_id:
            return "", "unverified"
        if not self.verify_evidence:
            return event_id, "not_checked"
        platform = str(getattr(principal, "platform", "") or "")
        account_id = str(getattr(principal, "bot_account_id", "") or "")
        if not all((platform, account_id, chat_id)):
            return event_id, "unknown"
        from core.context.cognitive_commit import Scope
        from core.memory import belief_commit

        try:
            state = belief_commit.evidence_state(
                event_id,
                Scope(platform, account_id, chat_id),
            )
        except Exception:
            state = "unknown"
        return event_id, state

    def recall(
        self,
        principal: Any,
        query: str = "",
        *,
        limit: int = 8,
        per_kind: int | None = None,
        context: Any = None,
        detail: dict[str, Any] | None = None,
    ) -> list[MemoryItem]:
        mode = worth_remembering_mode()
        if mode == NOTE_MODE_OFF:
            return []
        chat_id = str(getattr(principal, "chat_id", "") or "")
        if not chat_id:
            return []
        chat_type = str(getattr(principal, "chat_type", "") or "")
        rows = list(rc.load_rows(chat_id))
        notes = load_notes(chat_id, directory=self.directory)
        index = index_by_message(rows)
        recent = recent_text(rows, context)
        gate_mode = notes_gate_mode()
        gate = gate_decision(query, recent)
        cap = max(1, min(int(limit or 0) or notes_max_items(), notes_max_items()))
        wanted = question_kinds(query)
        recent = recent_text(rows, context)
        speaker_key = speaker_key_of(principal, rows, context)
        chain_query = f"{query or ""} {recent}".strip()
        chain_rows = set(rc.keyword_ranks(rows, chain_query)[:CHAIN_TOPK]) if chain_query else set()
        scored: list[
            tuple[float, dict[str, Any], dict[str, float], str, str]
        ] = []
        broken: list[dict[str, Any]] = []
        relocated = 0
        evidence_dead = 0
        for note in notes:
            reason, how = validate_note_ex(note, rows, index)
            if reason:
                broken.append({"note_id": str(note.get("note_id") or ""), "reason": reason})
                continue
            if how == "relocated":
                relocated += 1
            located = locate_note(note, rows, index)
            row = located[1] if located is not None else {}
            event_id, evidence_state = self._evidence(row, principal, chat_id)
            if evidence_state == "dead":
                evidence_dead += 1
                continue
            total, parts = score_note(
                note,
                rows,
                query=query,
                wanted=wanted,
                chain_rows=chain_rows,
                index=index,
                speaker_key=speaker_key,
                recent_text=recent,
            )
            scored.append((total, note, parts, event_id, evidence_state))
        scored.sort(key=lambda item: (-item[0], -_order_time(item[1]), str(item[1].get("note_id") or "")))
        picked = scored[:cap]
        items: list[MemoryItem] = []
        for total, note, parts, event_id, evidence_state in picked:
            message_id = _message_id_of(note)
            items.append(
                MemoryItem(
                    memory_id=str(note.get("note_id") or f"{NOTE_KIND}:{chat_id}:{note.get('row_index')}"),
                    kind=NOTE_KIND,
                    content=_render_content(note),
                    occurred_at=_parse_at(note.get("at")),
                    chat_id=chat_id,
                    chat_type=chat_type,
                    speaker=str(note.get("speaker") or ""),
                    source_event_ids=(event_id,) if event_id else (),
                    source_message_ids=(message_id,) if message_id else (),
                    confidence=0.7,
                    importance=max(0.0, min(1.0, total / 10.0)),
                    metadata={
                        "source": NOTE_KIND,
                        "note_id": str(note.get("note_id") or ""),
                        "row_index": (locate_note(note, rows, index) or (None, {}))[0],
                        "stored_row_index": note.get("row_index"),
                        "span": note.get("span") or {},
                        "note_kind": str(note.get("kind") or ""),
                        "evidence_state": evidence_state,
                        "score": total,
                        "lenses": parts,
                    },
                )
            )
        if isinstance(detail, dict):
            detail["note_recall"] = {
                "mode": mode,
                "chat_id": chat_id,
                "rows": len(rows),
                "notes_total": len(notes),
                "valid": len(scored),
                "span_broken": len(broken),
                "span_relocated": relocated,
                "evidence_dead": evidence_dead,
                "broken_sample": broken[:REJECTED_TOP],
                "budget": cap,
                "context": {
                    "speaker_key": speaker_key,
                    "recent_chars": len(recent),
                    "recent_rows": recent_rows_count(),
                    "chain_query_chars": len(chain_query),
                    "gate_score": notes_gate_score(),
                },
                "picked": [
                    {"note_id": str(note.get("note_id") or ""), "score": total, "lenses": parts}
                    for total, note, parts, _event_id, _evidence_state in picked
                ],
                "rejected": [
                    {"note_id": str(note.get("note_id") or ""), "score": total, "lenses": parts,
                     "lost_to": str(picked[0][1].get("note_id") or "") if picked else ""}
                    for total, note, parts, _event_id, _evidence_state
                    in scored[cap : cap + REJECTED_TOP]
                ],
                "question_kinds": list(wanted),
                "gate": gate,
            }
        if gate_mode != NOTE_MODE_OFF:
            gate["would_merge"] = bool(gate["fire"])
            gate["would_pick"] = [
                str(note.get("note_id") or "")
                for _total, note, _parts, _event_id, _evidence_state in picked
            ]
            log.info(
                "notes_gate_would_fire",
                chat_id=chat_id,
                mode=mode,
                gate_mode=gate_mode,
                cue=bool(gate["cue"]),
                cue_hits=list(gate["cue_hits"]),
                cov=float(gate["cov"]),
                cov_max=float(gate["cov_max"]),
                fire=bool(gate["fire"]),
                would_merge=bool(gate["would_merge"]),
                would_pick=len(gate["would_pick"]),
                would_pick_ids=list(gate["would_pick"]),
            )
        if mode != "on":
            log.info(
                "note_recall_shadow",
                chat_id=chat_id,
                mode=mode,
                notes=len(notes),
                rows=len(rows),
                query=str(query or "")[:80],
                picked=len(items),
                gate_fire=bool(gate["fire"]),
            )
            return []
        log.info(
            "note_recall",
            mode=mode,
            chat_id=chat_id,
            rows=len(rows),
            notes=len(notes),
            valid=len(scored),
            span_broken=len(broken),
            span_relocated=relocated,
            picked=len(items),
            question_kinds=list(wanted),
        )
        if gate_mode == NOTE_MODE_ON and not gate["fire"]:
            return []
        return items


def _order_time(note: dict[str, Any]) -> float:
    moment = _parse_at(note.get("at"))
    return moment.timestamp() if moment else 0.0


def _chain_top_score(chain_items: Sequence[MemoryItem]) -> float | None:
    """chain 臂最高分（metadata.score）；读不到返回 None（不门控）。"""
    best: float | None = None
    for item in chain_items:
        meta = item.metadata if isinstance(item.metadata, dict) else {}
        try:
            value = float(meta.get("score"))
        except (TypeError, ValueError):
            warn("core/memory/note_recall.py:822 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            continue
        if best is None or value > best:
            best = value
    return best


ROUTE_NOTES = "notes"
ROUTE_CHAIN_FILL = "chain_fill"
NOTES_FIRST_TOTAL = 6


def _route_tagged(item: MemoryItem, route: str) -> MemoryItem:
    """给条目盖上通道标记（不改内容），供逐题归因。"""
    meta = dict(item.metadata or {})
    meta["route"] = route
    return replace(item, metadata=meta)


def merge_notes_first(
    notes: Sequence[MemoryItem],
    chain_items: Sequence[MemoryItem],
    *,
    notes_cap: int | None = None,
    total_cap: int = NOTES_FIRST_TOTAL,
) -> list[MemoryItem]:
    """A 案（replace + fill）：notes 先占 <=notes_cap 条，余位按 chain 顺位补。

    与 ``merge_with_chain`` 的唯一区别是**次序**：本函数把 notes 放前面、chain 只填空位；
    每条命中带 ``metadata["route"]``（``notes`` / ``chain_fill``），供逐题归因。
    """
    cap = notes_max_items() if notes_cap is None else max(0, int(notes_cap))
    limit = max(cap, int(total_cap))
    note_rows = {
        str((item.metadata or {}).get("row_index"))
        for item in list(notes)[:cap]
        if isinstance(item.metadata, dict) and (item.metadata or {}).get("row_index") is not None
    }
    merged = [_route_tagged(item, ROUTE_NOTES) for item in list(notes[:cap])]
    for item in chain_items:
        if len(merged) >= limit:
            break
        meta = item.metadata if isinstance(item.metadata, dict) else {}
        if str(meta.get("row_index")) in note_rows:
            continue
        merged.append(_route_tagged(item, ROUTE_CHAIN_FILL))
    return merged

def merge_with_chain(
    notes: Sequence[MemoryItem],
    chain_items: Sequence[MemoryItem],
    *,
    gate_score: float | None = None,
) -> list[MemoryItem]:
    """chain 优先、notes 只补弱链：chain 最高分 >= 门控时只发 chain（notes 不挤预算）。

    门控默认关闭（``gate_score=None`` 且 ``YINYUE_NOTES_GATE_SCORE`` 未设）＝旧行为：同 span 去重、verbatim note 优先、notes ≤ 上限。
    """
    gate = notes_gate_score() if gate_score is None else float(gate_score)
    if gate is not None:
        top = _chain_top_score(chain_items)
        if top is not None and top >= gate:
            return list(chain_items)
    note_rows = {
        str((item.metadata or {}).get("row_index"))
        for item in notes
        if isinstance(item.metadata, dict) and (item.metadata or {}).get("row_index") is not None
    }
    merged = list(notes[:notes_max_items()])
    for item in chain_items:
        meta = item.metadata if isinstance(item.metadata, dict) else {}
        if str(meta.get("row_index")) in note_rows:
            continue
        merged.append(item)
    return merged




__all__ = [
    "NOTE_KIND",
    "ROUTE_NOTES",
    "ROUTE_CHAIN_FILL",
    "NOTES_FIRST_TOTAL",
    "merge_notes_first",
    "REFERENT_CUES",
    "is_referent_question",
    "referent_overlap3",
    "NOTES_MAX_ITEMS",
    "NOTES_MAX_ENV",
    "notes_max_items",
    "RECENT_ROWS",
    "RECENT_ROWS_ENV",
    "GATE_SCORE_ENV",
    "notes_gate_score",
    "recent_rows_count",
    "recent_text",
    "speaker_key_of",
    "NOTE_LENS_WEIGHTS",
    "NoteRecallProvider",
    "backfill_chat",
    "load_notes",
    "merge_with_chain",
    "notes_dir",
    "question_kinds",
    "score_note",
    "store_path",
    "validate_note",
    "write_backfill",
]
