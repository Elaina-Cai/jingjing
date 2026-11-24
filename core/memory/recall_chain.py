"""RecallChain: multi-route recall -> +/-6 row window -> mechanical rerank -> needs_memory.

Three-state flag ``YINYUE_FEATURE_RECALL_CHAIN`` (read only by
:func:`recall_chain_mode` here):

* ``off``    - default. ``recall()`` returns ``[]`` at once: no IO, no embedding,
  no index. Byte-identical to the behaviour before this module existed.
* ``shadow`` - recalls for real (no short circuit) and logs the observation payload
  (``recall_chain_shadow`` + ``query_style_diff``), then returns ``[]``: nothing
  enters the prompt and nothing is written.
* ``on``     - returns one ``MemoryItem`` per window for the memory tool /
  recall readout (2026-10-24: the system-prompt ``[按需记忆]`` block and
  ``agents/conversational.py::_needs_memory_block`` were retired).

Read-only: rows come from ``read_only_engine(get_meta_db_path())``; the FTS5 index and
the vector cache live under ``.test_tmp/recall_chain/`` (override with
``YINYUE_RECALL_CHAIN_DIR``). No network, no new LLM calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import math
import re
# task-10：窗口聚合变体
import sqlite3
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Sequence

import structlog

from core.memory.contracts import MemoryItem
from safelog import warn
from core.infra.envflags import flag, text

log = structlog.get_logger()

WINDOW_SPAN: int = 6
RRF_K: float = 60.0
STRUCT_WEIGHT_DEFAULT: float = 4.00
# 实测（13 题，question 口径）：权重 1/2/4/8 -> win@1 7/9/9/9、win@3 8/9/10/10、
# gold_in 69.2%/76.9%/84.6%/84.6%；4.0 起进平台期，取 4.0。
# task-26：媒体召回路权重（默认档位，与 struct 同构）
MEDIA_WEIGHT_DEFAULT: float = 3.00
# task-28：情节级（滑窗）粒度——连续 EPISODE_SIZE 条消息拼段、50% 重叠。
EPISODE_WEIGHT_DEFAULT: float = 1.50
EPISODE_SIZE: int = 30
EPISODE_STRIDE_RATIO: float = 0.5
EPISODE_CHARS: int = 512
EPISODE_TOPK: int = 3
EPISODE_CACHE_PREFIX: str = "ep"
# task-28b：情节级向量从「行级候选路」上移到「窗口聚合层」——只给窗口加语义先验，
# 不再作为一条 route 灌行（v2 实测：行级灌入会把 top-3 窗口全占满，h@1 -6.25pp）。
EPISODE_PRIOR_ENV = "YINYUE_RECALL_CHAIN_EPISODE_PRIOR"
EPISODE_PRIOR_DEFAULT: float = 0.0
TIE_BREAK_ENV: str = "YINYUE_RECALL_CHAIN_TIEBREAK"
# task-33：类型无关原语（真 BM25 词法层 + 轮/段粒度向量）
# 词法：char 3-gram 与 jieba 两路 **BM25 真打分**（k1/b 全文固定常数，idf 由 DF 统一给），
# 替掉 keyword 路「命中 token 计数 + DF_MAX_RATIO 存活阈值」的手写排序；语义：把嵌入单位
# 从单行换成一轮对话（同一说话人连续消息或 reply 边切 2~6 条）。两路都只有「字面 / 语义怎么
# 算分」的通用公式，不含任何「问句类型 -> 专属词表」。off 时不建 token、不建索引、不载模型。
BM25_K1: float = 1.2
BM25_B: float = 0.75
PRIM_CHAR_N: int = 2
PRIM_WEIGHT_CHAR: float = 0.30
PRIM_WEIGHT_WORD: float = 0.20
TURN_WEIGHT_ENV: str = "YINYUE_RECALL_CHAIN_TURN_WEIGHT"
TURN_WEIGHT_DEFAULT: float = 1.00
TURN_MIN: int = 2
TURN_MAX: int = 6
TURN_CHARS: int = 400
TURN_TOPK: int = 3
TURN_CACHE_PREFIX: str = "turn"
ROUTE_WEIGHTS: dict[str, float] = {
    "episode": EPISODE_WEIGHT_DEFAULT,
    "fts": 0.30,
    "keyword": 0.20,
    "struct": STRUCT_WEIGHT_DEFAULT,
    "media": MEDIA_WEIGHT_DEFAULT,
    # task-33：两路 BM25 词法原语（合计 0.50 = 旧的 keyword 0.20 + fts 0.30）与轮段向量
    "lex3": PRIM_WEIGHT_CHAR,
    "lexw": PRIM_WEIGHT_WORD,
    "turn": TURN_WEIGHT_DEFAULT,
}
QUERY_INSTRUCTION: str = "为这个句子生成表示以用于检索相关文章："
EMBED_MODEL: str = "BAAI/bge-large-zh-v1.5"
EMBED_CHARS: int = 200
ROW_HASH_CHARS: int = 16  # task-22: incremental vector cache
CANDIDATE_TOPK: int = 100
WINDOW_CANDIDATES: int = 200
WINDOW_TOPK: int = 3
CONTENT_MAX: int = 600
ROW_LIMIT: int = 6000
DF_MAX_RATIO: float = 0.05
MATCH_EVENT_TYPES: tuple[str, ...] = ("message", "question", "reply", "request")

# ---- task-11：结构化召回通道（问句类型 -> 单一模式候选集 -> 通道内近因排序）----
# 与 vector/fts/keyword 并列进 fuse；默认 off 时结构通道一行都不产出。
STRUCT_FILE_RE = re.compile(r"\.(pdf|zip|docx|xlsx|pptx|md|py|txt)\s*$", re.I)
STRUCT_DATE_RE = re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*[日号]|\d{1,2}\s*月\s*\d{1,2}\s*[日号]")
STRUCT_Q_FILE = re.compile(r"文件|压缩包|作业|附件|pdf|zip|发我的|发给你的|发的那份|全名")
STRUCT_Q_REP = re.compile(r"重复|一串|连着|这串|那串|数字")
STRUCT_Q_DATE = re.compile(r"几月|几号|哪一天|日期|\d{1,2}\s*月\s*\d{1,2}\s*[日号]")
STRUCT_DIGIT_RUN = re.compile(r"(?<![0-9A-Za-z])\d{6,}(?![0-9A-Za-z])")
STRUCT_AT_RE = re.compile(r"@\S*\d{4,}")
STRUCT_Q_AT = re.compile(r"@|串号|昵称末尾|尾巴")
STRUCT_MIN_DIGITS: int = 6
STRUCT_MAX_UNIT: int = 4

# ---- task-26：媒体召回路（题型闸门 + 通道内近因；数据来自 message_media 基质）----
# 与 struct 同构：先看「问的是不是媒体件」，再看通道内候选；off 时一行都不产出。
MEDIA_Q_RE = re.compile(
    r"文件|附件|压缩包|图片|照片|截图|表情包|录音|语音|视频|表格|课件|作业"
    r"|发的那个|发的那份|发给|全名|叫什么名|文件名"
    r"|pdf|zip|docx|xlsx|pptx|ppt|md|txt",
    re.I,
)
MEDIA_ROW_LIMIT: int = 400

_STOPWORDS = frozenset({"上次", "那个", "什么", "哪个", "来着", "我们", "之前", "多少"})
_PUNCT_RE = re.compile(r"[\s，。！？!?；;、,.…~～\-—_：:（）()「」\"'\[\]]+")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,4}")
_WORD_RE = re.compile(r"[A-Za-z0-9_.\-]{2,}")
_LOCK = threading.RLock()
_EMBEDDER: Any = None

def recall_chain_mode() -> str:
    """The only reader of the three-state flag."""
    from core import features

    return features.mode(features.RECALL_CHAIN, features.MODE_OFF)


def chain_tracing() -> bool:
    return recall_chain_mode() != "off"


def chain_injecting() -> bool:
    return recall_chain_mode() == "on"


def recall_struct_mode() -> str:
    """The only reader of ``YINYUE_FEATURE_RECALL_STRUCT`` (default off)."""
    from core import features

    return features.mode(features.RECALL_STRUCT, features.MODE_OFF)


def struct_tracing() -> bool:
    return recall_struct_mode() != "off"


def struct_injecting() -> bool:
    return recall_struct_mode() == "on"
def recall_primitives_mode() -> str:
    """The only reader of ``YINYUE_FEATURE_RECALL_PRIMITIVES`` (default off)."""
    from core import features

    return features.mode(features.RECALL_PRIMITIVES, features.MODE_OFF)


def primitives_tracing() -> bool:
    return recall_primitives_mode() != "off"


def work_dir() -> Path:
    raw = text("YINYUE_RECALL_CHAIN_DIR").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[2] / ".test_tmp" / "recall_chain"


def _norm(text: Any) -> str:
    return _PUNCT_RE.sub("", str(text or "").lower())


def _fingerprint(texts: Sequence[str]) -> str:
    digest = hashlib.sha1()
    for text in texts:
        digest.update(text.encode("utf-8", "ignore"))
        digest.update(b"\x00")
    return digest.hexdigest()


def keyword_tokens(query: str) -> list[str]:
    """Overlapping 3/2-grams + ascii words: a query is a bag of substrings, not a word list."""
    raw = str(query or "").strip()
    norm = _norm(raw)
    out: list[str] = []
    for size in (3, 2):
        for start in range(0, max(0, len(norm) - size + 1)):
            gram = norm[start : start + size]
            if gram and gram not in _STOPWORDS and gram not in out:
                out.append(gram)
    for word in _WORD_RE.findall(raw):
        if word not in _STOPWORDS and word not in out:
            out.append(word)
    return out or [norm or raw]


def _surviving_tokens(tokens: Sequence[str], bodies: Sequence[str]) -> list[str]:
    """Drop tokens that appear in almost every row (they cannot discriminate)."""
    if not tokens or not bodies:
        return list(tokens)
    total = len(bodies)
    kept: list[str] = []
    for token in tokens:
        hits = sum(1 for body in bodies if token in body)
        if hits and hits / total <= DF_MAX_RATIO:
            kept.append(token)
    if kept:
        return kept
    return sorted(tokens, key=lambda token: (-len(token), token))[:3]


def keyword_ranks(rows: Sequence[dict], query: str) -> list[int]:
    bodies = [_norm(row.get("content")) for row in rows]
    tokens = _surviving_tokens(keyword_tokens(query), [b for b in bodies if b])
    if not tokens:
        return []
    scored: list[tuple[int, int]] = []
    for index, body in enumerate(bodies):
        if not body:
            continue
        hits = sum(1 for token in tokens if token in body)
        if hits:
            scored.append((hits, index))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [index for _hits, index in scored]


def _query_grams(query: str, texts: Sequence[str]) -> list[str]:
    norm = _norm(query)
    grams: list[str] = []
    seen: set[str] = set()
    for size in (4, 3):
        for start in range(0, max(0, len(norm) - size + 1)):
            gram = norm[start : start + size]
            if len(gram) < 3 or gram in seen:
                continue
            seen.add(gram)
            grams.append(gram)
    if not grams:
        return []
    if not texts:
        return grams
    total = len(texts)
    kept: list[str] = []
    for gram in grams:
        hits = sum(1 for text in texts if gram in text)
        if hits and hits / total <= DF_MAX_RATIO:
            kept.append(gram)
    return kept

def _fts_path(key: str) -> Path:
    return work_dir() / f"fts_{key[:16]}.db"


def _fts_conn(path: Path, texts: Sequence[str], fingerprint: str) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("create table if not exists meta (k text primary key, v text)")
    row = conn.execute("select v from meta where k = 'fingerprint'").fetchone()
    if row is None or str(row[0]) != fingerprint:
        conn.execute("drop table if exists fts")
        conn.execute("create virtual table fts using fts5(body, tokenize='trigram')")
        conn.executemany(
            "insert into fts(rowid, body) values (?, ?)",
            [(index + 1, str(text)) for index, text in enumerate(texts)],
        )
        conn.execute("delete from meta")
        conn.execute("insert into meta(k, v) values ('fingerprint', ?)", (fingerprint,))
        conn.commit()
    return conn


def fts_ranks(
    rows: Sequence[dict],
    query: str,
    *,
    texts: Sequence[str] | None = None,
    cache_key: str = "",
) -> list[int]:
    bodies = list(texts) if texts is not None else [_norm(row.get("content")) for row in rows]
    grams = _query_grams(query, bodies)
    if not grams:
        return []
    fingerprint = _fingerprint(bodies)
    key = cache_key or fingerprint
    conn = _fts_conn(_fts_path(key), bodies, fingerprint)
    match = " OR ".join('"' + gram + '"' for gram in grams[:24])
    try:
        rows_out = conn.execute(
            "select rowid from fts where fts match ? order by rank limit ?",
            (match, CANDIDATE_TOPK * 4),
        ).fetchall()
    except sqlite3.Error:
        warn("core/memory/recall_chain.py:302 sqlite3.Error", err=repr(__import__("sys").exc_info()[1]))
        return []
    finally:
        conn.close()
    out: list[int] = []
    for (rowid,) in rows_out:
        index = int(rowid) - 1
        if 0 <= index < len(bodies):
            out.append(index)
    return out


# ---------------------------------------------------------------------------
# task-33：类型无关原语 —— 词法层真 BM25（char 3-gram / jieba 两路）
# ---------------------------------------------------------------------------
def _dedupe(tokens: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        text = str(token or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def prim_char_terms(text: str, *, size: int = PRIM_CHAR_N) -> list[str]:
    """``_norm`` 后的 char N-gram（默认 3-gram）：字面匹配的最小单位，不会切错词。"""
    clean = _norm(text)
    if not clean:
        return []
    if len(clean) <= size:
        return [clean]
    return [clean[pos : pos + size] for pos in range(len(clean) - size + 1)]


def prim_word_terms(text: str) -> list[str]:
    """``_norm`` 之后用 jieba 切词；缺 jieba 时退化为字 run + ascii 词兜底。"""
    clean = _norm(text)
    if not clean:
        return []
    terms: list[str] = []
    try:
        import jieba

        jieba.dt.cache_file = str(work_dir() / "jieba.cache")
        terms = [str(item).strip() for item in jieba.lcut(clean)]
    except Exception:  # noqa: BLE001 - 缺 jieba 不该让召回链挂掉
        warn("core/memory/recall_chain.py:350 Exception", err=repr(__import__("sys").exc_info()[1]))
        terms = []
    if not terms:
        terms = list(_CJK_RUN_RE.findall(clean)) + list(_WORD_RE.findall(clean))
    out: list[str] = []
    for term in terms:
        if not term or term in _STOPWORDS:
            continue
        if len(term) < 2 and not term.isdigit():
            continue
        out.append(term)
    return out


def bm25_scores(
    docs: Sequence[Sequence[str]],
    query_terms: Sequence[str],
    *,
    k1: float = BM25_K1,
    b: float = BM25_B,
) -> list[float]:
    """标准 BM25：idf = ln(1 + (N-df+0.5)/(df+0.5))，k1/b 是全文固定常数。

    与 ``keyword_ranks`` 的区别只有一点：分数来自 tf/idf/文档长度，而不是先把稀有
    token 按 ``DF_MAX_RATIO`` 筛掉再数命中数——阈值与题型都不参与排序。
    """
    total = len(docs)
    if total <= 0:
        return []
    wanted = _dedupe(query_terms)
    if not wanted:
        return [0.0] * total
    lengths = [len(doc) for doc in docs]
    avgdl = (sum(lengths) / total) or 1.0
    wanted_set = set(wanted)
    freqs: list[dict[str, int]] = []
    doc_freq: dict[str, int] = {}
    for doc in docs:
        counts: dict[str, int] = {}
        for token in doc:
            if token in wanted_set:
                counts[token] = counts.get(token, 0) + 1
        freqs.append(counts)
        for token in counts:
            doc_freq[token] = doc_freq.get(token, 0) + 1
    scores = [0.0] * total
    for term in wanted:
        df = doc_freq.get(term, 0)
        if df <= 0:
            continue
        idf = math.log(1.0 + (total - df + 0.5) / (df + 0.5))
        for index, counts in enumerate(freqs):
            tf = counts.get(term, 0)
            if not tf:
                continue
            norm = 1.0 - b + b * lengths[index] / avgdl
            scores[index] += idf * (tf * (k1 + 1.0)) / (tf + k1 * norm)
    return scores


def prim_lexical_ranks(
    rows: Sequence[dict], query: str, *, stats: dict[str, Any] | None = None
) -> dict[str, list[int]]:
    """两路 BM25 名次：``lex3``（char 3-gram）/ ``lexw``（jieba），各是一条 fuse 通道。"""
    if not rows or not str(query or "").strip():
        return {}
    bodies = [str(row.get("content") or "") for row in rows]
    out: dict[str, list[int]] = {}
    for route, doc_fn in (("lex3", prim_char_terms), ("lexw", prim_word_terms)):
        docs = [doc_fn(body) for body in bodies]
        terms = doc_fn(query)
        scores = bm25_scores(docs, terms)
        if not scores:
            continue
        order = sorted(
            (index for index, score in enumerate(scores) if score > 0.0),
            key=lambda index: (-scores[index], index),
        )
        if order:
            out[route] = order
        if stats is not None:
            stats["prim_" + route] = {
                "terms": len(_dedupe(terms)),
                "hits": int(len(order)),
                "top": [
                    {"row": int(index), "score": round(float(scores[index]), 4)}
                    for index in order[:3]
                ],
            }
    return out


# ---------------------------------------------------------------------------
# task-33：轮/段粒度（2~6 条一轮）向量召回
# ---------------------------------------------------------------------------
def _reply_target(row: Mapping[str, Any]) -> str:
    for key in ("parent", "parent_event_id"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def turn_segments(
    rows: Sequence[dict], *, max_size: int = TURN_MAX, min_size: int = TURN_MIN
) -> list[tuple[int, int, str]]:
    """一轮 = 同一说话人的连续消息（换人即切），且一条 reply 边也切；长度夹在 2~6。

    与 episode（固定 30 条滑窗、50% 重叠）的区别：段边界来自对话结构（谁在说话、
    在回谁），不来自固定步长，所以段内语义不该被截断稀释。
    """
    total = len(rows)
    if total < max(2, int(min_size)):
        return []
    limit = max(2, int(max_size))
    spans: list[tuple[int, int]] = []
    begin = 0
    inside: set[str] = set()
    for index in range(total):
        row = rows[index]
        if index > begin:
            speaker = str(row.get("actor") or "")
            previous = str(rows[index - 1].get("actor") or "")
            parent = _reply_target(row)
            if speaker != previous or (index - begin) >= limit or (parent and parent in inside):
                spans.append((begin, index))
                begin = index
                inside = set()
        event_id = str(row.get("event_id") or "")
        if event_id:
            inside.add(event_id)
    spans.append((begin, total))
    merged: list[tuple[int, int]] = []
    for span in spans:
        short = (span[1] - span[0]) < max(2, int(min_size))
        if merged and short and (merged[-1][1] - merged[-1][0]) < limit:
            merged[-1] = (merged[-1][0], span[1])
            continue
        merged.append(span)
    if len(merged) > 1 and (merged[0][1] - merged[0][0]) < max(2, int(min_size)):
        merged[1] = (merged[0][0], merged[1][1])
        merged = merged[1:]
    out: list[tuple[int, int, str]] = []
    for begin_index, end_index in merged:
        blob = _norm(
            " ".join(str(rows[pos].get("content") or "") for pos in range(begin_index, end_index))
        )
        if not blob:
            continue
        out.append((int(begin_index), int(end_index), blob[:TURN_CHARS]))
    return out


def turn_weight() -> float:
    """段级向量通道权重（env 可调，默认 1.0；0 等于关掉这一档）。"""
    try:
        raw = os.environ.get(TURN_WEIGHT_ENV, "")
        return max(0.0, float(raw)) if str(raw or "").strip() else TURN_WEIGHT_DEFAULT
    except (TypeError, ValueError):
        warn("core/memory/recall_chain.py:508 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return TURN_WEIGHT_DEFAULT


def turn_tracing() -> bool:
    return primitives_tracing() and turn_weight() > 0.0


def turn_ranks(
    rows: Sequence[dict],
    query: str,
    *,
    cache_key: str = "",
    model: Any = None,
    topk: int = TURN_TOPK,
    stats: dict[str, Any] | None = None,
) -> list[int]:
    """段级向量召回：段余弦排序取 top-k，再把段展开成成员行（段内按离中心远近）。"""
    if not rows or not str(query or "").strip():
        return []
    import numpy as np

    segments = turn_segments(rows)
    lengths = [end - begin for begin, end, _blob in segments]
    if stats is not None:
        stats["turn_segments"] = len(segments)
        stats["turn_rows_mean"] = round(sum(lengths) / len(lengths), 2) if lengths else 0.0
        stats["turn_rows_max"] = int(max(lengths)) if lengths else 0
    if not segments:
        return []
    target = model
    if target is None:
        if vector_warm_only() and not embedder_ready():
            warm_embedder_async()
            log.warning("recall_chain_turn_cold", stage="model")
            return []
        target = embedder()
    query_vector = _encode_query(
        [QUERY_INSTRUCTION + " " + " ".join(str(query or "").split())],
        target,
        deadline=vector_deadline_seconds(),
    )
    if query_vector is None:
        log.warning("recall_chain_vector_timeout", route="turn")
        return []
    texts = [blob for _, _, blob in segments]
    probe_model = None if vector_warm_only() else target
    block = unit_vectors(texts, cache_key=TURN_CACHE_PREFIX + "_" + cache_key, model=probe_model)
    if block is None or block.known <= 0:
        if stats is not None:
            stats["turn_cache_mode"] = str(getattr(block, "mode", "cold"))
        return []
    live = block.live_indices()
    vectors = block.vectors[block.index_map[live]]
    scores = np.asarray(vectors @ query_vector[0], dtype="float32")
    ranked = sorted(
        ((float(scores[position]), int(live[position])) for position in range(len(live))),
        key=lambda item: (-item[0], item[1]),
    )
    if stats is not None:
        stats["turn_cache_mode"] = str(block.mode)
        stats["turn_known"] = int(block.known)
        stats["turn_missing"] = int(block.missing)
        stats["turn_top"] = [
            {"seg": int(index), "score": round(float(score), 4)} for score, index in ranked[:3]
        ]
    out: list[int] = []
    seen: set[int] = set()
    for _score, seg_index in ranked[: max(1, int(topk))]:
        begin, end, _blob = segments[seg_index]
        center = (begin + end - 1) / 2.0
        for index in sorted(range(begin, end), key=lambda item: (abs(item - center), item)):
            if index in seen:
                continue
            seen.add(index)
            out.append(int(index))
    return out[: CANDIDATE_TOPK * 4]


# ---- task-21: local embed arm + warm-only gate (see docs/decisions/.. §17) ----
EMBED_ARM_ENV: str = "YINYUE_RECALL_CHAIN_EMBED_ARM"
VECTOR_WARM_ONLY_ENV: str = "YINYUE_RECALL_CHAIN_VECTOR_WARM_ONLY"
VECTOR_DEADLINE_ENV: str = "YINYUE_RECALL_CHAIN_VECTOR_DEADLINE"
VECTOR_DEADLINE_DEFAULT: float = 1.0

_EMBED_WARMING = False
_QUERY_INFLIGHT = False


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return flag(name, default)


def embed_arm() -> str:
    """``auto`` (default) tries sentence-transformers, then local_embed."""
    return os.environ.get(EMBED_ARM_ENV, "").strip().lower() or "auto"


def vector_warm_only() -> bool:
    """True in shadow/production: never compute corpus vectors inside a turn."""
    return _env_flag(VECTOR_WARM_ONLY_ENV, True)


def vector_deadline_seconds() -> float:
    try:
        value = float(os.environ.get(VECTOR_DEADLINE_ENV, "") or VECTOR_DEADLINE_DEFAULT)
    except ValueError:
        warn("core/memory/recall_chain.py:617 ValueError", err=repr(__import__("sys").exc_info()[1]))
        return VECTOR_DEADLINE_DEFAULT
    return max(0.0, value)


def embedder_ready() -> bool:
    return _EMBEDDER is not None


def _load_embedder() -> Any:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    arm = embed_arm()
    if arm != "local":
        try:
            from sentence_transformers import SentenceTransformer

            device = text("YINYUE_RECALL_CHAIN_DEVICE").strip() or None
            loaded = SentenceTransformer(EMBED_MODEL, device=device)
            log.info("recall_chain_embed_arm", arm="sentence_transformers")
            return loaded
        except ImportError:
            if arm == "st":
                raise
            log.warning(
                "recall_chain_embed_arm", arm="local_embed", reason="sentence_transformers_missing"
            )
    # [2026-11-02 修 F821] local_embed 臂已随向量能力整体移除，原来到这里
    # `return _local_get_embedder()` 是个 NameError —— 会被上层 except 吞成静默降级。
    # 现在响亮地失败：装 sentence_transformers，或把 embed_arm 切到别的臂。
    raise ImportError(
        "recall_chain 需要 sentence_transformers（EMBED_MODEL=%s，embed_arm=%s）；"
        "local_embed 臂已随去向量移除" % (EMBED_MODEL, arm)
    )


def warm_embedder_async() -> None:
    """Load the model off the turn thread so a cold process never blocks on it."""
    global _EMBED_WARMING
    with _LOCK:
        if _EMBEDDER is not None or _EMBED_WARMING:
            return
        _EMBED_WARMING = True

    def _run() -> None:
        global _EMBEDDER, _EMBED_WARMING
        try:
            with _LOCK:
                if _EMBEDDER is None:
                    _EMBEDDER = _load_embedder()
        except Exception as exc:  # noqa: BLE001
            log.debug("recall_chain_embed_warm_failed", error=str(exc)[:200])
        finally:
            with _LOCK:
                _EMBED_WARMING = False

    threading.Thread(target=_run, name="recall-embed-warm", daemon=True).start()


def _encode_query(texts: Sequence[str], model: Any, *, deadline: float) -> Any:
    """Encode one query under a wall-clock deadline; ``None`` when it misses it."""
    global _QUERY_INFLIGHT
    if deadline <= 0:
        return encode(model, texts)
    with _LOCK:
        if _QUERY_INFLIGHT:
            return None
        _QUERY_INFLIGHT = True
    box: list = []

    def _run() -> None:
        global _QUERY_INFLIGHT
        try:
            box.append(encode(model, texts))
        except Exception as exc:  # noqa: BLE001
            log.warning("recall_chain_vector_encode_failed", error=str(exc)[:200])
        finally:
            with _LOCK:
                _QUERY_INFLIGHT = False

    worker = threading.Thread(target=_run, name="recall-query-embed", daemon=True)
    worker.start()
    worker.join(deadline)
    return box[0] if box else None


# task-21: local embed arm + warm-only gate
# task-21b: warm/_load_embedder 修复
def embedder() -> Any:
    global _EMBEDDER
    with _LOCK:
        if _EMBEDDER is None:
            _EMBEDDER = _load_embedder()
        return _EMBEDDER


def reset_embedder() -> None:
    global _EMBEDDER
    with _LOCK:
        _EMBEDDER = None


def encode(model: Any, texts: Sequence[str], *, batch: int = 64) -> Any:
    import numpy as np

    vectors = model.encode(
        list(texts), batch_size=batch, normalize_embeddings=True, show_progress_bar=False
    )
    return np.asarray(vectors, dtype="float32")


def vector_cache_path(key: str) -> Path:
    return work_dir() / f"vec_{key[:16]}.npz"


def row_hashes(texts: Sequence[str]) -> Any:
    """Per-row sha1[:16] of the embedded text: the incremental cache key (task-22)."""
    import numpy as np

    return np.asarray(
        [
            hashlib.sha1(str(text).encode("utf-8", "ignore")).hexdigest()[:ROW_HASH_CHARS]
            for text in texts
        ],
        dtype=f"<U{ROW_HASH_CHARS}",
    )


@dataclass(frozen=True)
class RowVectorBlock:
    """Cached row vectors aligned to the live corpus by per-row hash (task-22)."""

    vectors: Any  # (M, D) cached vectors, cache order
    index_map: Any  # (N,) int32: live row -> cache row, -1 = missing
    known: int
    missing: int
    mode: str = "exact"  # exact | incremental | computed

    def live_indices(self) -> Any:
        import numpy as np

        return np.nonzero(self.index_map >= 0)[0]


def _read_vector_cache(path: Path) -> Any:
    """(vectors, hashes|None, fingerprint) or None when the cache is unreadable."""
    import numpy as np

    try:
        data = np.load(str(path), allow_pickle=False)
        files = getattr(data, "files", ())
        vectors = np.asarray(data["vectors"], dtype="float32")
        hashes = np.asarray(data["hashes"]) if "hashes" in files else None
        if hashes is not None and len(hashes) != len(vectors):
            hashes = None
        fingerprint = str(data["fingerprint"].item()) if "fingerprint" in files else ""
        return vectors, hashes, fingerprint
    except Exception:  # noqa: BLE001 - 坏缓存当没有
        warn("core/memory/recall_chain.py:771 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


def unit_vectors(
    texts: Sequence[str], *, cache_key: str, model: Any = None
) -> RowVectorBlock | None:
    """Embedding cache for an arbitrary unit list (rows or episode windows).

    Same layout as the row cache: ``vectors`` (M, D) + ``hashes`` (M,) + ``fingerprint``.
    Callers pass a granularity-prefixed ``cache_key`` so granularities never collide.
    """
    import numpy as np

    if not texts:
        return RowVectorBlock(
            np.zeros((0, 1), dtype="float32"), np.zeros(0, dtype="int32"), 0, 0, "computed"
        )
    hashes = row_hashes(texts)
    fingerprint = _fingerprint(texts)
    path = vector_cache_path(cache_key or fingerprint)
    cached_rows = 0
    known = 0
    if path.exists():
        cached = _read_vector_cache(path)
        if cached is not None:
            vectors, cached_hashes, cached_fingerprint = cached
            cached_rows = len(vectors)
            if cached_rows == len(texts) and cached_fingerprint == fingerprint:
                return RowVectorBlock(
                    vectors, np.arange(len(texts), dtype="int32"), len(texts), 0, "exact"
                )
            if cached_hashes is not None:
                table = {str(item): index for index, item in enumerate(cached_hashes)}
                index_map = np.full(len(texts), -1, dtype="int32")
                for live, item in enumerate(hashes):
                    found = table.get(str(item))
                    if found is not None:
                        index_map[live] = found
                known = int((index_map >= 0).sum())
                if known > 0:
                    return RowVectorBlock(
                        vectors, index_map, known, len(texts) - known, "incremental"
                    )
    if model is None and vector_warm_only():
        log.warning(
            "recall_chain_vector_cold",
            cache=path.name,
            rows=len(texts),
            cached_rows=cached_rows,
            live_rows=len(texts),
            known=known,
            missing=len(texts) - known,
        )
        warm_embedder_async()
        return None
    vectors = encode(model or embedder(), texts)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(
            str(path),
            vectors=vectors,
            hashes=hashes,
            fingerprint=np.asarray(fingerprint),
        )
    except Exception:  # noqa: BLE001 - 缓存失败不影响召回
        warn("core/memory/recall_chain.py:836 Exception", err=repr(__import__("sys").exc_info()[1]))
    return RowVectorBlock(
        vectors, np.arange(len(texts), dtype="int32"), len(texts), 0, "computed"
    )


def row_vectors(
    rows: Sequence[dict], *, cache_key: str, model: Any = None
) -> RowVectorBlock | None:
    """Row-granularity vectors (thin wrapper over :func:`unit_vectors`)."""
    texts = [" ".join(str(row.get("content") or "").split())[:EMBED_CHARS] for row in rows]
    return unit_vectors(texts, cache_key=cache_key, model=model)


def vector_ranks(
    rows: Sequence[dict],
    query: str,
    *,
    cache_key: str = "",
    model: Any = None,
    instruction: bool = True,
    stats: dict[str, Any] | None = None,
) -> list[int]:
    if not rows:
        return []
    import numpy as np

    block = row_vectors(rows, cache_key=cache_key, model=model)
    if block is None or block.known <= 0:
        if stats is not None:
            stats["vector_rows_known"] = 0
            stats["vector_rows_missing"] = len(rows)
            stats["vector_cache_mode"] = "cold"
        return []
    if stats is not None:
        stats["vector_rows_known"] = int(block.known)
        stats["vector_rows_missing"] = int(block.missing)
        stats["vector_cache_mode"] = block.mode
    live = block.live_indices()
    vectors = block.vectors[block.index_map[live]]
    head = QUERY_INSTRUCTION if instruction else ""
    target = model
    if target is None:
        if vector_warm_only() and not embedder_ready():
            warm_embedder_async()
            log.warning("recall_chain_vector_cold", stage="model")
            return []
        target = embedder()
    query_vector = _encode_query(
        [head + " ".join(str(query or "").split())], target, deadline=vector_deadline_seconds()
    )
    if query_vector is None:
        log.warning(
            "recall_chain_vector_timeout", deadline_ms=int(vector_deadline_seconds() * 1000)
        )
        return []
    scores = np.asarray(vectors @ query_vector[0], dtype="float32")
    order = np.argsort(-scores)
    picked = order[: CANDIDATE_TOPK * 4]
    return [int(live[index]) for index in picked]


def periodic_digits(text: Any, *, min_digits: int = STRUCT_MIN_DIGITS, max_unit: int = STRUCT_MAX_UNIT) -> bool:
    """True when a >=min_digits digit run repeats a short unit (e.g. 8080808080)."""
    for match in STRUCT_DIGIT_RUN.finditer(str(text or "")):
        run = match.group(0)
        if len(run) < min_digits or len(set(run)) == 1:
            continue
        for unit_len in range(2, max_unit + 1):
            reps = len(run) // unit_len
            if reps < 3:
                continue
            unit = run[:unit_len]
            if run == (unit * reps) + unit[: len(run) - unit_len * reps]:
                return True
    return False


def struct_kind(query: str) -> str:
    """Question type -> one candidate mode (a union of modes drowns the gold row)."""
    text = str(query or "")
    if STRUCT_Q_FILE.search(text):
        return "file"
    if STRUCT_Q_AT.search(text):
        return "at"
    if STRUCT_Q_REP.search(text) or periodic_digits(text):
        return "rep"
    if STRUCT_Q_DATE.search(text):
        return "date"
    return ""


def struct_ranks(rows: Sequence[dict], query: str, *, texts: Sequence[str] | None = None) -> list[int]:
    """Struct route: one mode per question type, recency-ordered inside the mode."""
    if not rows or recall_struct_mode() == "off":
        return []
    kind = struct_kind(query)
    if not kind:
        return []
    if texts is not None:
        bodies = [str(text or "") for text in texts]
    else:
        bodies = [str(row.get("content") or "") for row in rows]
    if kind == "file":
        ordered = [index for index, body in enumerate(bodies) if STRUCT_FILE_RE.search(body)]
    elif kind == "rep":
        ordered = [index for index, body in enumerate(bodies) if periodic_digits(body)]
    elif kind == "at":
        ordered = [index for index, body in enumerate(bodies) if STRUCT_AT_RE.search(body)]
    else:
        ordered = [index for index, body in enumerate(bodies) if STRUCT_DATE_RE.search(body)]
    ordered.sort(reverse=True)  # 通道内近因先验：只排序候选，不裁候选集
    return ordered[:CANDIDATE_TOPK]


@dataclass(frozen=True)
class ChainHit:
    """One recall window: the rows are the substrate, the score is the lens reading."""

    row_index: int
    start: int
    end: int
    score: float
    routes: tuple[str, ...] = ()
    members: tuple[int, ...] = ()
    content: str = ""
    occurred_at: str = ""
    # 写时只存原文与 id；这两个字段是读时透镜的产物（off 档不读，形状不变）
    anchor_event_id: str = ""
    neighbor_event_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "row_index": self.row_index,
            "start": self.start,
            "end": self.end,
            "score": self.score,
            "routes": list(self.routes),
            "members": list(self.members),
            "occurred_at": self.occurred_at,
        }


def row_memory_id(chat_id: str, rows: Sequence[dict], row_index: int, *, mode: str = "") -> str:
    """身份透镜：``on`` 用事件 id，其余档逐字保留行号口径。

    ``row_index`` 是 ``load_rows`` 输出里的**位置**，新消息一进来或 ``ROW_LIMIT`` 一变就全体
    漂移；``event_id`` 不会。``off``/``shadow`` 保留旧键，bench 与影子读数才有对照基线。
    """
    resolved = mode or recall_chain_mode()
    if resolved == "on" and 0 <= int(row_index) < len(rows):
        event_id = str(rows[int(row_index)].get("event_id") or "").strip()
        if event_id:
            return f"recall:{chat_id}:{event_id}"
    return f"recall:{chat_id}:{int(row_index)}"


def anchor_span(row: Mapping[str, Any]) -> list[int]:
    """``[content_start, content_end]`` 读回；``-1/-1``（未知 / m061 之前的老行）读成 ``[]``。"""
    try:
        start = int(row.get("content_start", -1))
        end = int(row.get("content_end", -1))
    except (TypeError, ValueError):
        warn("core/memory/recall_chain.py:999 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return []
    if start < 0 or end < start:
        return []
    return [start, end]


def anchor_identity(
    rows: Sequence[dict], row_index: int, members: Sequence[int]
) -> tuple[str, tuple[str, ...]]:
    """窗口的稳定身份：锚行 event_id + 同窗口邻居的 event_id（顺序沿用成员序）。"""
    anchor = ""
    if 0 <= int(row_index) < len(rows):
        anchor = str(rows[int(row_index)].get("event_id") or "").strip()
    neighbors: list[str] = []
    for index in members:
        if int(index) == int(row_index) or not (0 <= int(index) < len(rows)):
            continue
        event_id = str(rows[int(index)].get("event_id") or "").strip()
        if event_id:
            neighbors.append(event_id)
    return anchor, tuple(neighbors)


def episode_windows(
    rows: Sequence[dict], *, size: int = EPISODE_SIZE, stride_ratio: float = EPISODE_STRIDE_RATIO
) -> list[tuple[int, int, str]]:
    """Sliding message windows for episode-granularity embeddings (50% overlap by default)."""
    total = len(rows)
    if total <= 1:
        return []
    size = max(2, int(size))
    stride = max(1, int(size * float(stride_ratio)))
    spans: list[tuple[int, int]] = []
    start = 0
    while start + size <= total:
        spans.append((start, start + size))
        start += stride
    if not spans or spans[-1][1] < total:
        last = (max(0, total - size), total)
        if not spans or spans[-1] != last:
            spans.append(last)
    out: list[tuple[int, int, str]] = []
    for begin, end in spans:
        blob = _norm(" ".join(str(rows[index].get("content") or "") for index in range(begin, end)))
        out.append((begin, end, blob[:EPISODE_CHARS]))
    return out


def _episode_scored(
    rows: Sequence[dict],
    query: str,
    *,
    cache_key: str = "",
    model: Any = None,
    size: int = EPISODE_SIZE,
    stats: dict[str, Any] | None = None,
) -> list[tuple[int, int, float]]:
    """Score every episode window against the query, best first (task-28b).

    优先用窗口内**行级向量的最大余弦**：窗口 blob 只有 EPISODE_CHARS(512) 字，
    30 行窗口会被截断，语义匹配被稀释；行级缓存不可用时回落到窗口 blob 余弦。
    """
    if not rows or not str(query or "").strip():
        return []
    import numpy as np

    windows = episode_windows(rows, size=size)
    if not windows:
        return []
    target = model
    if target is None:
        if vector_warm_only() and not embedder_ready():
            warm_embedder_async()
            log.warning("recall_chain_episode_cold", stage="model")
            return []
        target = embedder()
    query_vector = _encode_query(
        [QUERY_INSTRUCTION + " " + " ".join(str(query or "").split())],
        target,
        deadline=vector_deadline_seconds(),
    )
    if query_vector is None:
        log.warning("recall_chain_vector_timeout", route="episode")
        return []

    scored: list[tuple[int, int, float]] = []
    probe_model = None if vector_warm_only() else target
    row_block = row_vectors(rows, cache_key=cache_key, model=probe_model)
    if row_block is not None and row_block.known > 0:
        live = row_block.live_indices()
        vectors = row_block.vectors[row_block.index_map[live]]
        row_scores = np.asarray(vectors @ query_vector[0], dtype="float32")
        best = {int(index): float(score) for index, score in zip(live, row_scores)}
        for begin, end, _blob in windows:
            window_best = -1.0
            for index in range(int(begin), int(end)):
                value = best.get(index)
                if value is not None and value > window_best:
                    window_best = value
            if window_best > -1.0:
                scored.append((int(begin), int(end), float(window_best)))
        source = "row_max"
        if stats is not None:
            stats["episode_windows"] = len(windows)
            stats["episode_known"] = int(row_block.known)
            stats["episode_missing"] = int(row_block.missing)
            stats["episode_mode"] = str(row_block.mode)
    else:
        texts = [blob for _, _, blob in windows]
        key = f"{EPISODE_CACHE_PREFIX}{int(size)}_{cache_key}"
        block = unit_vectors(texts, cache_key=key, model=model)
        if stats is not None:
            stats["episode_windows"] = len(windows)
            stats["episode_known"] = int(block.known) if block is not None else 0
            stats["episode_missing"] = int(block.missing) if block is not None else len(windows)
            stats["episode_mode"] = str(block.mode) if block is not None else "cold"
        if block is None or block.known <= 0:
            return []
        live = block.live_indices()
        vectors = block.vectors[block.index_map[live]]
        scores = np.asarray(vectors @ query_vector[0], dtype="float32")
        for position in np.argsort(-scores):
            begin, end, _blob = windows[int(live[int(position)])]
            scored.append((int(begin), int(end), float(scores[int(position)])))
        source = "blob"
    if stats is not None:
        stats["episode_source"] = source
    scored.sort(key=lambda item: (-item[2], item[0]))
    return scored


def episode_ranks(
    rows: Sequence[dict],
    query: str,
    *,
    cache_key: str = "",
    model: Any = None,
    size: int = EPISODE_SIZE,
    topk: int = EPISODE_TOPK,
    stats: dict[str, Any] | None = None,
) -> list[int]:
    """Row expansion of the best episode windows (diagnostics only since task-28b)."""
    scored = _episode_scored(rows, query, cache_key=cache_key, model=model, size=size, stats=stats)
    if not scored:
        return []
    out: list[int] = []
    seen: set[int] = set()
    for begin, end, _score in scored[: max(1, int(topk))]:
        center = (begin + end) // 2
        for index in sorted(range(begin, end), key=lambda item: (abs(item - center), item)):
            if index in seen:
                continue
            seen.add(index)
            out.append(int(index))
    return out[: CANDIDATE_TOPK * 4]


def _query_char_grams(query: str) -> set[str]:
    clean = _norm(query)
    return {clean[index : index + 2] for index in range(max(0, len(clean) - 1))}


def tie_break_order(
    order: Sequence[int],
    rows: Sequence[dict],
    query: str,
    *,
    fused: dict[int, tuple[float, tuple[str, ...]]],
    group_limit: int = 50,
    epsilon: float = 1e-3,
) -> list[int]:
    """Reorder only rows whose fused scores are within ``epsilon`` (never displaces top-1)."""
    head = [int(index) for index in list(order)[: max(2, int(group_limit))]]
    tail = [int(index) for index in list(order)[max(2, int(group_limit)) :]]
    if len(head) < 2:
        return [int(index) for index in order]
    grams = _query_char_grams(query)
    terms = keyword_tokens(query)[:12]
    groups: list[list[int]] = []
    group_scores: list[float] = []
    for index in head:
        score = float(fused.get(index, (0.0, ()))[0])
        if groups and abs(group_scores[-1] - score) <= float(epsilon):
            groups[-1].append(index)
        else:
            groups.append([index])
            group_scores.append(score)

    def mechanical(index: int) -> float:
        body = _norm(rows[index].get("content") or "") if 0 <= index < len(rows) else ""
        ngram = 0.0
        if grams:
            row_grams = {body[pos : pos + 2] for pos in range(max(0, len(body) - 1))}
            ngram = len(grams & row_grams) / len(grams)
        term = 0.0
        if terms:
            term = sum(1 for token in terms if token in body) / len(terms)
        votes = len(fused.get(index, (0.0, ()))[1])
        length_norm = 1.0 / (1.0 + len(body) / 500.0)
        return 2.0 * ngram + 1.0 * term + 0.5 * votes + 0.3 * length_norm

    rebuilt: list[int] = []
    for group in groups:
        if len(group) < 2:
            rebuilt.extend(group)
            continue
        rebuilt.extend(sorted(group, key=lambda index: (-mechanical(index), index)))
    original_top = int(order[0])
    if rebuilt and rebuilt[0] != original_top:
        rebuilt.remove(original_top)
        rebuilt.insert(0, original_top)
    return rebuilt + tail


def fuse(
    route_ranks: dict[str, Sequence[int]],
    *,
    weights: Mapping[str, float] | None = None,
) -> dict[int, tuple[float, tuple[str, ...]]]:
    """Reciprocal-rank fusion: each route votes with its weight, ranks are the only input."""
    table = ROUTE_WEIGHTS if weights is None else weights
    scores: dict[int, float] = {}
    routes: dict[int, list[str]] = {}
    for route, ranks in route_ranks.items():
        weight = table.get(str(route))
        if weight is None:
            continue
        for position, raw_index in enumerate(list(ranks)[:CANDIDATE_TOPK]):
            index = int(raw_index)
            scores[index] = scores.get(index, 0.0) + weight * 100.0 / (RRF_K + position + 1)
            routes.setdefault(index, []).append(str(route))
    return {i: (round(scores[i], 4), tuple(routes.get(i, ()))) for i in scores}


def build_windows(  # task-10：窗口聚合变体
    rows: Sequence[dict],
    fused: dict[int, tuple[float, tuple[str, ...]]],
    *,
    span: int = WINDOW_SPAN,
    topk: int = WINDOW_TOPK,
    tokens: Sequence[str] = (),
    idf: dict[str, float] | None = None,
    order: Sequence[int] | None = None,
    episode_spans: Sequence[tuple[int, int, float]] = (),
) -> list[ChainHit]:
    """Aggregate each hit row's +/-span window and rank windows by score (overlaps allowed).

    task-10 变体：不再贪心去重（同一段上下文可被多个锚行引用，读侧按行去重），
    打分 = 前二成员分 + 0.5*最高成员分 + 0.05*窗口内 query token 的 idf 权重和 + 0.5*通道数。
    """
    if not fused or not rows:
        return []
    weights = idf or {}
    if order is not None:
        candidate_order = [int(index) for index in order if int(index) in fused]
        candidate_order = candidate_order[: max(1, int(WINDOW_CANDIDATES))]
    else:
        candidate_order = sorted(fused, key=lambda i: (-fused[i][0], i))[
            : max(1, int(WINDOW_CANDIDATES))
        ]
    candidates: list[tuple[float, int, int, int, tuple[int, ...], tuple[str, ...]]] = []
    for row_index in candidate_order:
        start = max(0, int(row_index) - max(0, int(span)))
        end = min(len(rows), int(row_index) + max(0, int(span)) + 1)
        window_rows = [i for i in range(start, end) if i in fused]
        members = tuple(
            sorted(window_rows, key=lambda i: (-fused[i][0], abs(i - int(row_index)), i))
        )
        member_scores = sorted((fused[i][0] for i in window_rows), reverse=True)
        top1 = member_scores[0] if member_scores else 0.0
        top2 = sum(member_scores[:2])
        channels = tuple(sorted({route for i in members for route in fused[i][1]}))
        blob = _norm(" ".join(str(rows[i].get("content") or "") for i in range(start, end)))
        idf_weight = sum(weights.get(token, 0.0) for token in tokens if token in blob)
        prior = 0.0
        if episode_spans and episode_tracing():
            for ep_begin, ep_end, ep_score in episode_spans:
                if ep_begin < end and ep_end > start:
                    prior = max(prior, float(ep_score))
        score = (
            top2 + 0.5 * top1 + 0.05 * idf_weight + 0.5 * len(channels)
            + episode_prior() * prior
        )
        candidates.append((round(score, 4), int(row_index), start, end, members, channels))
    candidates.sort(key=lambda item: (-item[0], item[2], item[1]))
    out: list[ChainHit] = []
    for score, row_index, start, end, members, channels in candidates[: max(1, int(topk))]:
        anchor_event_id, neighbor_event_ids = anchor_identity(rows, row_index, members)
        out.append(
            ChainHit(
                row_index=row_index,
                start=start,
                end=end,
                score=score,
                routes=channels,
                members=members,
                anchor_event_id=anchor_event_id,
                neighbor_event_ids=neighbor_event_ids,
            )
        )
    return out


def render_window(rows: Sequence[dict], hit: ChainHit) -> str:
    lines: list[str] = []
    for row in rows[hit.start : hit.end]:
        when = str(row.get("at") or "")[:16]
        who = str(row.get("name") or row.get("actor") or "").strip()
        body = " ".join(str(row.get("content") or "").split())
        if not body:
            continue
        lines.append(f"{when} {who}: {body}".strip())
    return "\n".join(lines)[:CONTENT_MAX]


def recall_episode_mode() -> str:
    """情节级（滑窗）向量召回的三态开关；off 时链行为与 +RECALL_CHAIN 逐字一致。

    已判负（task-28b，2026-10-17）：v2 上 route 版 62.50% / 窗口先验版 62.50%（基线 68.75%）。
    保留代码只是为了留证据与复验工具：默认 off 零 IO，`on` 也不改变路由（prior 默认 0.0），
    **勿据 flag default 调参**。决策页：``docs/decisions/穷举整改_多粒度与rerank.md §6``。
    """
    from core import features

    try:
        return str(features.mode(features.RECALL_EPISODE, features.MODE_OFF) or "").strip().lower()
    except Exception:  # noqa: BLE001 - 开关读失败按 off
        warn("core/memory/recall_chain.py:1326 Exception", err=repr(__import__("sys").exc_info()[1]))
        return str(features.MODE_OFF)


def episode_tracing() -> bool:
    from core import features

    return recall_episode_mode() != features.MODE_OFF


def tie_break_enabled() -> bool:
    return _env_flag(TIE_BREAK_ENV, False)


def episode_prior() -> float:
    """Window-layer weight of the episode prior (task-28b, env-tunable)."""
    try:
        raw = os.environ.get(EPISODE_PRIOR_ENV, "")
        return max(0.0, float(raw)) if str(raw or "").strip() else EPISODE_PRIOR_DEFAULT
    except (TypeError, ValueError):
        warn("core/memory/recall_chain.py:1345 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return EPISODE_PRIOR_DEFAULT


def media_recall_mode() -> str:
    """The only reader of ``YINYUE_FEATURE_MEDIA_RECALL`` (default off)."""
    from core import features

    return features.mode(features.MEDIA_RECALL, features.MODE_OFF)


def media_tracing() -> bool:
    return media_recall_mode() != "off"


def load_media_rows(chat_id: str, *, limit: int = MEDIA_ROW_LIMIT) -> list[dict]:
    """One chat's ``message_media`` rows (read-only; missing table/columns -> ``[]``).

    The table is the task-26 substrate: one row per media part, with ``original_name`` /
    ``text_excerpt`` / ``event_id`` added by migration 60.  ``select *`` keeps this working
    on older databases where those columns do not exist yet (they simply fall back to
    placeholders).
    """
    cid = str(chat_id or "").strip()
    if not cid:
        return []
    try:
        from sqlalchemy import text as sql_text

        from core.bootstrap import get_meta_db_path
        from db.write_guard import read_only_engine
    except Exception:  # noqa: BLE001 - import problems must not affect the turn
        warn("core/memory/recall_chain.py:1376 Exception", err=repr(__import__("sys").exc_info()[1]))
        return []
    try:
        engine = read_only_engine(get_meta_db_path())
        with engine.connect() as conn:
            raw = conn.execute(
                sql_text(
                    "select * from message_media where chat_id = :cid"
                    " order by id desc limit :lim"
                ),
                {"cid": cid, "lim": max(1, int(limit))},
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - a missing table is not an error here
        log.warning("recall_chain_media_unavailable", chat_id=cid, error=str(exc)[:160])
        return []
    out: list[dict] = []
    for row in raw:
        try:
            item = dict(row._mapping)
        except Exception:  # noqa: BLE001
            warn("core/memory/recall_chain.py:1395 Exception", err=repr(__import__("sys").exc_info()[1]))
            continue
        out.append({str(key): value for key, value in item.items()})
    return out


_MEDIA_KIND_LABEL = {
    "image": "图片",
    "emoji": "表情包",
    "file": "文件",
    "video": "视频",
    "voice": "语音",
    "audio": "语音",
}
_NAMED_MEDIA_KINDS = ("file", "video", "voice", "audio")


def _media_label(item: Mapping[str, Any]) -> str:
    """Attachment name, but only when it is a real filename.

    Images/emojis are stored under content-hash names (``c887310d….jpg``), which tell a
    reader nothing and invite mis-attribution; those keep an empty label so the caller
    falls back to a plain kind word.
    """
    kind = str(item.get("kind") or "").strip().lower()
    name = str(item.get("original_name") or "").strip()
    if kind and kind not in _NAMED_MEDIA_KINDS:
        return ""
    return name


def _media_display(item: Mapping[str, Any], *, sender: str = "") -> str:
    """Human-facing media phrase: kind word + filename (when meaningful) + who sent it."""
    kind = str(item.get("kind") or "").strip().lower()
    word = _MEDIA_KIND_LABEL.get(kind, "媒体")
    name = _media_label(item)
    phrase = word + ("「" + name + "」" if name else "")
    who = str(sender or "").strip()
    if who:
        phrase += "（" + who + " 发）"
    return phrase


def annotate_rows_with_media(
    rows: Sequence[dict], media: Sequence[Mapping[str, Any]]
) -> list[dict]:
    """Return copies of ``rows`` whose content carries the media part's name/excerpt.

    Matching is by ``event_id`` first and ``msg_id`` second (the media row is written at
    ingest time, when only the source message id is known).  The window row keeps its
    own text: the annotation is appended, never substituted.
    """
    if not rows or not media:
        return list(rows)
    by_event: dict[str, list[Mapping[str, Any]]] = {}
    by_msg: dict[str, list[Mapping[str, Any]]] = {}
    for item in media:
        event = str(item.get("event_id") or "").strip()
        msg = str(item.get("msg_id") or "").strip()
        if event:
            by_event.setdefault(event, []).append(item)
        if msg:
            by_msg.setdefault(msg, []).append(item)
    out: list[dict] = []
    for row in rows:
        event = str(row.get("event_id") or "").strip()
        msg = str(row.get("msg_id") or row.get("message_id") or "").strip()
        found = list(by_event.get(event) or [])
        if not found and msg:
            # msg_id fallback: only trust it when the sender matches.  A bare id match
            # would paste somebody else's attachment name onto this row (mis-attribution).
            actor = str(row.get("actor") or row.get("sender_id") or "").strip()
            chat = str(row.get("chat_id") or "").strip()
            for item in by_msg.get(msg) or []:
                item_actor = str(item.get("sender_id") or "").strip()
                item_chat = str(item.get("chat_id") or "").strip()
                if actor and item_actor and actor != item_actor:
                    continue
                if chat and item_chat and chat != item_chat:
                    continue
                found.append(item)
        if not found:
            out.append(row)
            continue
        body = " ".join(str(row.get("content") or "").split())
        parts: list[str] = []
        for item in list(found)[:3]:
            label = _media_label(item)
            excerpt = " ".join(str(item.get("text_excerpt") or "").split())
            status = str(item.get("status") or "").strip()
            phrase = _media_display(item, sender=str(row.get("name") or item.get("sender_id") or ""))
            if (label or phrase) and phrase not in body:
                suffix = "" if status in ("", "ready", "ok") else " " + status
                parts.append("〔媒体：%s%s〕" % (phrase, suffix))
            if excerpt and excerpt[:40] not in body:
                parts.append("〔正文：%s〕" % excerpt[:120])
        merged = dict(row)
        if parts:
            merged["content"] = (body + " " + " ".join(parts)).strip()
        merged["media"] = [
            {"id": item.get("id"), "kind": item.get("kind"), "name": _media_label(item)}
            for item in list(found)[:3]
        ]
        out.append(merged)
    return out


MEDIA_EXT_CLASSES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("压缩包", "压缩", "打包"), ("zip", "rar", "7z")),
    (("课表", "表格", "名单"), ("xlsx", "xls", "csv")),
    (("课件", "演示", "幻灯片"), ("pptx", "ppt")),
    (("作业", "论文", "文档", "报告", "讲义"), ("pdf", "docx", "doc", "txt", "md")),
    (("代码", "脚本", "笔记"), ("py", "md", "json", "txt")),
    (("图片", "照片", "截图", "表情包"), ("jpg", "jpeg", "png", "gif", "webp", "bmp")),
    (("录音", "语音", "音频"), ("silk", "amr", "mp3", "wav", "ogg", "m4a")),
    (("视频", "录像"), ("mp4", "mov", "avi", "mkv")),
)
_LATIN_RE = re.compile(r"[a-z0-9]{2,}")


def _name_keys(name: str) -> set[str]:
    """Name fingerprints used for question overlap: CJK bigrams + latin words."""
    text = str(name or "").lower()
    keys: set[str] = set(_LATIN_RE.findall(text))
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        for index in range(len(run) - 1):
            keys.add(run[index : index + 2])
    return keys


def _extension_class_score(name: str, question: str) -> float:
    lowered = str(name or "").lower()
    for cues, suffixes in MEDIA_EXT_CLASSES:
        if any(cue in question for cue in cues) and any(lowered.endswith("." + suffix) for suffix in suffixes):
            return 3.5
    return 0.0


def media_ranks(rows: Sequence[dict], query: str) -> list[int]:
    """Media channel: only when the question asks about a media part.

    Candidates are the rows annotated by :func:`annotate_rows_with_media`.  A row ranks by
    how much of the attachment *name* the question repeats (CJK bigrams / latin words),
    then by the extension class the question names (压缩包 -> .zip, 课表 -> .xlsx), and only
    then by recency.  On corpora where the name already sits in the message text this
    channel agrees with ``struct``; on production placeholders (``[文件]``) it is the only
    route that can see the name at all.
    """
    if not media_tracing():
        return []
    question = _norm(query)
    if not question or not MEDIA_Q_RE.search(str(query or "")):
        return []
    scored: list[tuple[float, int]] = []
    for index, row in enumerate(rows):
        items = row.get("media") or []
        if not items:
            continue
        best = 0.0
        for item in items:
            raw_name = str(item.get("name") or "")
            name = _norm(raw_name)
            if not name:
                best = max(best, 0.5)
                continue
            if name in question:
                best = max(best, 5.0)
                continue
            overlap = _name_keys(raw_name) & _name_keys(str(query or ""))
            if overlap:
                best = max(best, 4.0)
                continue
            class_score = _extension_class_score(raw_name, str(query or ""))
            best = max(best, class_score or 2.0)
        scored.append((best, index))
    scored.sort(key=lambda pair: (-pair[0], -pair[1]))
    strong = [pair for pair in scored if pair[0] >= 3.5]
    if strong:
        return [index for _, index in strong]
    return [index for _, index in scored]


def chain_rank(
    rows: Sequence[dict],
    query: str,
    *,
    model: Any = None,
    cache_key: str = "",
    span: int = WINDOW_SPAN,
    topk: int = WINDOW_TOPK,
    use_vector: bool = True,
    stats: dict[str, Any] | None = None,
) -> list[ChainHit]:
    """Multi-route recall + window aggregation. Pure function: no IO except the temp indexes."""
    if not rows or not str(query or "").strip():
        return []
    texts = [_norm(row.get("content")) for row in rows]
    if stats is not None:
        stats.setdefault("vector_rows_known", 0)
        stats.setdefault("vector_rows_missing", len(rows))
        stats.setdefault("vector_cache_mode", "off" if not use_vector else "unknown")
    if primitives_tracing():
        # task-33：词法层换成两路 BM25 真打分（不再数「命中 token 个数」）
        route_ranks: dict[str, list[int]] = {}
        try:
            route_ranks.update(prim_lexical_ranks(rows, query, stats=stats))
        except Exception as exc:  # noqa: BLE001 - 单路失败不拖垮召回链
            log.warning("recall_chain_route_failed", route="prim", error=str(exc)[:160])
        if stats is not None:
            stats["primitives"] = recall_primitives_mode()
    else:
        route_ranks = {"keyword": keyword_ranks(rows, query)}
        try:
            route_ranks["fts"] = fts_ranks(rows, query, texts=texts, cache_key=cache_key)
        except Exception as exc:  # noqa: BLE001 - 单路失败不拖垮召回链
            log.warning("recall_chain_route_failed", route="fts", error=str(exc)[:160])
            route_ranks["fts"] = []
    if use_vector:
        try:
            route_ranks["vector"] = vector_ranks(
                rows, query, cache_key=cache_key, model=model, stats=stats
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("recall_chain_route_failed", route="vector", error=str(exc)[:160])
            route_ranks["vector"] = []
    if turn_tracing():
        try:
            turn_order = turn_ranks(rows, query, cache_key=cache_key, model=model, stats=stats)
        except Exception as exc:  # noqa: BLE001 - 单路失败不拖垮召回链
            log.warning("recall_chain_route_failed", route="turn", error=str(exc)[:160])
            turn_order = []
        if turn_order:
            route_ranks["turn"] = turn_order
    if struct_tracing():
        try:
            raw_texts = [str(row.get("content") or "") for row in rows]
            struct_order = struct_ranks(rows, query, texts=raw_texts)
        except Exception as exc:  # noqa: BLE001 - 单路失败不拖垮召回链
            log.warning("recall_chain_route_failed", route="struct", error=str(exc)[:160])
            struct_order = []
        if struct_order:
            route_ranks["struct"] = struct_order
    if media_tracing():
        try:
            media_order = media_ranks(rows, query)
        except Exception as exc:  # noqa: BLE001 - 单路失败不拖垮召回链
            log.warning("recall_chain_route_failed", route="media", error=str(exc)[:160])
            media_order = []
        if media_order:
            route_ranks["media"] = media_order
    episode_spans: list[tuple[int, int, float]] = []
    if episode_tracing() and episode_prior() > 0:
        try:
            episode_spans = _episode_scored(
                rows, query, cache_key=cache_key, model=model, stats=stats
            )
        except Exception as exc:  # noqa: BLE001 - 单路失败不拖垮召回链
            log.warning("recall_chain_route_failed", route="episode", error=str(exc)[:160])
            episode_spans = []
        if stats is not None:
            stats["episode_window_top"] = [
                {"begin": int(b), "end": int(e), "score": round(float(s), 4)}
                for b, e, s in episode_spans[:3]
            ]
    route_weights: Mapping[str, float] = ROUTE_WEIGHTS
    if turn_tracing():
        route_weights = {**ROUTE_WEIGHTS, "turn": turn_weight()}
    fused = fuse(route_ranks, weights=route_weights)
    if stats is not None:
        try:
            stats["route_sizes"] = {
                str(route): len(list(ranks)) for route, ranks in route_ranks.items()
            }
            stats["routes"] = {
                str(route): [
                    {
                        "row": int(index),
                        "fused": fused.get(int(index), (0.0, ()))[0],
                        "pos": position,
                    }
                    for position, index in enumerate(list(ranks)[:3])
                ]
                for route, ranks in route_ranks.items()
            }
        except Exception:  # noqa: BLE001 - shadow readout must not affect recall
            warn("core/memory/recall_chain.py:1679 Exception", err=repr(__import__("sys").exc_info()[1]))
    tokens = _surviving_tokens(keyword_tokens(query), [text for text in texts if text])
    idf: dict[str, float] = {}
    total = max(1, len(texts))
    for token in tokens:
        seen = sum(1 for text in texts if token in text)
        idf[token] = round(math.log(1.0 + total / (1.0 + seen)), 4)
    order_override: list[int] | None = None
    if tie_break_enabled() and fused:
        try:
            base_order = sorted(fused, key=lambda i: (-fused[i][0], i))
            order_override = tie_break_order(base_order, rows, query, fused=fused)
            if stats is not None:
                stats["tie_break"] = "on"
        except Exception:  # noqa: BLE001 - tie-break 失败回退原序
            warn("core/memory/recall_chain.py:1694 Exception", err=repr(__import__("sys").exc_info()[1]))
            order_override = None
            if stats is not None:
                stats["tie_break"] = "failed"
    out: list[ChainHit] = []
    for hit in build_windows(
        rows,
        fused,
        span=span,
        topk=topk,
        tokens=tokens,
        idf=idf,
        order=order_override,
        episode_spans=episode_spans,
    ):
        out.append(
            replace(
                hit,
                content=render_window(rows, hit),
            )
        )
    if stats is not None:
        stats["windows"] = len(out)
        stats["window_top"] = float(out[0].score) if out else 0.0
        stats["window_rows"] = [
            {"row": int(hit.row_index), "score": float(hit.score), "routes": list(hit.routes)}
            for hit in out[:3]
        ]
    return out

def _aware(value: Any) -> Any:
    from datetime import datetime

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).strip())
    except ValueError:
        warn("core/memory/recall_chain.py:1733 ValueError", err=repr(__import__("sys").exc_info()[1]))
        return None


def load_names(engine: Any) -> dict[str, str]:
    from sqlalchemy import text as _sql

    out: dict[str, str] = {}
    try:
        with engine.connect() as conn:
            records = conn.execute(
                _sql(
                    "select i.identity_value, e.display_name, i.person_id "
                    "from person_identities i "
                    "left join person_entities e on e.person_id = i.person_id "
                    "where i.identity_type = 'wxid'"
                )
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - 名字只是渲染，拿不到就用 id
        log.warning("recall_chain_names_failed", error=str(exc)[:160])
        return out
    for identity, display, person_id in records:
        value = str(identity or "").strip()
        name = str(display or "").strip()
        if value:
            out.setdefault(value, name)
        # task-49：actor 归一后 ``actor_person_id`` 存的是 person id，名字表必须
        # 同时按 person id 建键，否则 on 档下所有名字都掉成 id 前缀。
        person = str(person_id or "").strip()
        if person and name:
            out.setdefault(person, name)
    return out


_SPAN_COLUMNS: dict[str, bool] = {}


def span_columns_available(engine: Any) -> bool:
    """m061 的 span 两列，按库 URL 只探一次（只读；老库直接说没有）。

    探测结果进程内缓存：迁移如果发生在第一次探测**之后**，本进程会一直读旧形状，直到重启。
    """
    key = str(getattr(engine, "url", "") or "")
    if key in _SPAN_COLUMNS:
        return _SPAN_COLUMNS[key]
    available = False
    try:
        from sqlalchemy import text as _probe

        with engine.connect() as conn:
            names = {
                str(row[1]) for row in conn.execute(_probe("PRAGMA table_info(terminal_events)"))
            }
        available = {"content_start", "content_end"} <= names
    except Exception:  # noqa: BLE001 - 探测失败按老库处理，召回链不许因此挂掉
        warn("core/memory/recall_chain.py:1787 Exception", err=repr(__import__("sys").exc_info()[1]))
        available = False
    _SPAN_COLUMNS[key] = available
    return available


def reply_columns_available(engine: Any) -> bool:
    """``parent_event_id`` 是否存在（task-33 轮/段边界用；只读探测，按库 URL 缓存一次）。"""
    key = "reply:" + str(getattr(engine, "url", "") or "")
    if key in _SPAN_COLUMNS:
        return _SPAN_COLUMNS[key]
    available = False
    try:
        from sqlalchemy import text as _probe

        with engine.connect() as conn:
            names = {
                str(row[1]) for row in conn.execute(_probe("PRAGMA table_info(terminal_events)"))
            }
        available = "parent_event_id" in names
    except Exception:  # noqa: BLE001 - 探测失败按老库处理
        warn("core/memory/recall_chain.py:1807 Exception", err=repr(__import__("sys").exc_info()[1]))
        available = False
    _SPAN_COLUMNS[key] = available
    return available


def load_rows(chat_id: str, *, limit: int = ROW_LIMIT) -> list[dict]:
    """Read one chat's substrate rows, read-only, in the same order the bench uses."""
    cid = str(chat_id or "").strip()
    if not cid:
        return []
    from core.bootstrap import get_meta_db_path
    from db.write_guard import read_only_engine
    from sqlalchemy import text as _sql

    engine = read_only_engine(get_meta_db_path())
    names = load_names(engine)
    columns = "event_id, occurred_at, event_type, actor_person_id, content, chat_type, chat_id"
    if span_columns_available(engine):
        columns += ", content_start, content_end"
    if reply_columns_available(engine):
        # task-33：轮/段边界要用 reply 边；off 时只是多读一列，排序与渲染都不用
        columns += ", parent_event_id"
    statement = _sql(
        "select " + columns + " "
        "from terminal_events where chat_id = :c "
        "and event_type in ('message','question','reply','request') "
        "and content is not null and content != '' "
        "order by occurred_at, sequence limit :n"
    )
    with engine.connect() as conn:
        records = conn.execute(
            statement, {"c": cid, "n": max(1, min(int(limit), 20000))}
        ).mappings().all()
    out: list[dict] = []
    for record in records:
        actor = str(record.get("actor_person_id") or "")
        out.append(
            {
                "event_id": str(record.get("event_id") or ""),
                "at": str(record.get("occurred_at") or ""),
                "type": str(record.get("event_type") or ""),
                "actor": actor,
                "name": names.get(actor, "") or actor[:8],
                "content": str(record.get("content") or ""),
                "chat_type": str(record.get("chat_type") or ""),
                "chat_id": str(record.get("chat_id") or cid),
            }
        )
        row = out[-1]
        for column in ("content_start", "content_end"):
            if column in record:
                try:
                    row[column] = int(record.get(column) or -1)
                except (TypeError, ValueError):
                    warn("core/memory/recall_chain.py:1861 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
                    row[column] = -1
        if "parent_event_id" in record:
            row["parent"] = str(record.get("parent_event_id") or "")
    return out




def _jaccard(left: Sequence[Any], right: Sequence[Any]) -> float:
    first = {str(item) for item in left}
    second = {str(item) for item in right}
    if not first and not second:
        return 1.0
    if not first or not second:
        return 0.0
    return round(len(first & second) / len(first | second), 4)


def bag_query(text: str) -> str:
    """生产式关键词包（与 ``scripts/bench_recall_chain.py::bag_of`` 同规则，去单字）。"""
    body = str(text or "").strip()
    if not body:
        return ""
    try:
        import jieba
        import jieba.posseg as pseg

        jieba.dt.cache_file = str(work_dir() / "jieba.cache")
    except Exception:  # noqa: BLE001 - 没有 jieba 就退回原文
        warn("core/memory/recall_chain.py:1890 Exception", err=repr(__import__("sys").exc_info()[1]))
        return body
    keep: list[str] = []
    try:
        for word, flag in pseg.cut(body):
            token = str(word or "").strip()
            if len(token) < 2:
                continue
            if flag[0] in ("n", "v", "a", "e", "m", "q", "j", "l") or flag in ("eng", "nz"):
                keep.append(token)
    except Exception:  # noqa: BLE001
        warn("core/memory/recall_chain.py:1900 Exception", err=repr(__import__("sys").exc_info()[1]))
        return body
    return " ".join(dict.fromkeys(keep)) or body


#: 原文键（旧键在前；兼容 task-20 的 ``{"raw_inbound": ...}`` Mapping）。
_RAW_KEYS: tuple[str, ...] = (
    "user_message",
    "inbound_content",
    "raw_inbound",
    "inbound_text",
    "raw_text",
    "current",
)


def _raw_from_mapping(value: Any) -> str:
    """从 Mapping 里取原文；取不到再看 ``window_turns`` 的最后一条 user 轮。"""
    if not isinstance(value, Mapping):
        return ""
    for key in _RAW_KEYS:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item
    turns = value.get("window_turns")
    if isinstance(turns, (list, tuple)):
        for turn in reversed(turns):
            if not isinstance(turn, Mapping):
                continue
            if str(turn.get("role") or "") not in ("user", "user_message"):
                continue
            text = turn.get("content")
            if isinstance(text, str) and text.strip():
                return text
    return ""


def raw_inbound(context: Any) -> str:
    """本回合原始 inbound 文本（生产 query 之外的另一口径）。

    读法（兼容旧键；取不到返回空串、**绝不抛**）：

    1) ``context.extra`` 上的原文键（旧键在前）；
    2) ``context`` 本身是 Mapping 时同上（task-20：触发路径 ``_context is None`` 时由
       ``core/context/intent.py`` 补 ``{"raw_inbound": <原话>}``，有值时不动）；
    3) ``extra["window_turns"]`` 里最后一条 user 轮（直回路径的真实来源之一）；
    4) 对象属性 ``user_message`` / ``inbound_content``。
    """
    if context is None:
        return ""
    try:
        text = _raw_from_mapping(getattr(context, "extra", None))
        if text:
            return text
        text = _raw_from_mapping(context)
        if text:
            return text
        for key in ("user_message", "inbound_content"):
            value = getattr(context, key, None)
            if isinstance(value, str) and value.strip():
                return value
    except Exception:  # noqa: BLE001 - 读数失败不影响影子主行
        warn("core/memory/recall_chain.py:1961 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""
    return ""


def suppressed_by_window(chat_id: str) -> bool:
    """窗口接管口径下的读数（2026-10-24：注入块已退役，本函数只供量具/读数）。"""
    try:
        from core.context.chat_window import window_enabled_for

        return bool(window_enabled_for(str(chat_id or "")))
    except Exception:  # noqa: BLE001 - 读数失败按未接管（与旧路径一致）
        warn("core/memory/recall_chain.py:1972 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def _top_row_ids(chat_id: str, hits: Sequence[ChainHit], *, count: int = 3) -> list[str]:
    return [f"recall:{chat_id}:{int(hit.row_index)}" for hit in list(hits)[:count]]


def emission_plan(
    rows: Sequence[dict], hits: Sequence[ChainHit], *, cap: int
) -> list[tuple[int, str, ChainHit]]:
    """注入顺序（与生产 cap 一致）：锚行优先 -> 成员分降序 -> 邻近度兜底。"""
    plan: list[tuple[int, str, ChainHit]] = []
    seen: set[int] = set()

    def _add(hit: ChainHit, row_index: int, role: str) -> None:
        if len(plan) >= cap or row_index in seen or not (0 <= row_index < len(rows)):
            return
        if not str(rows[row_index].get("content") or "").strip():
            return
        seen.add(row_index)
        plan.append((row_index, role, hit))

    for hit in hits[: max(1, WINDOW_TOPK)]:
        _add(hit, hit.row_index, "anchor")
    for hit in hits[: max(1, WINDOW_TOPK)]:
        for index in hit.members:
            _add(hit, index, "context")
    for distance in range(1, WINDOW_SPAN + 1):
        for hit in hits[: max(1, WINDOW_TOPK)]:
            for index in (hit.row_index - distance, hit.row_index + distance):
                if hit.start <= index < hit.end:
                    _add(hit, index, "context")
    return plan


def shadow_payload(
    *,
    chat_id: str,
    query: str,
    rows: Sequence[dict],
    hits: Sequence[ChainHit],
    stats: dict[str, Any] | None,
    elapsed_ms: int,
    plan: Sequence[tuple[int, str, ChainHit]] = (),
    cap: int = 0,
) -> dict[str, Any]:
    """影子观测主行：只读、不注入、不写库、不改 prompt。"""
    sizes = (stats or {}).get("route_sizes") or {}
    routes = (stats or {}).get("routes") or {}
    payload: dict[str, Any] = {
        "mode": "shadow",
        "chat_id": chat_id,
        "query": str(query)[:200],
        "query_len": len(str(query)),
        "rows": len(rows),
        "hits": len(hits),
        "vector_rows_known": int((stats or {}).get("vector_rows_known") or 0),
        "vector_rows_missing": int((stats or {}).get("vector_rows_missing") or 0),
        "vector_cache_mode": str((stats or {}).get("vector_cache_mode") or ""),
        "hits_top": [
            {
                "memory_id": f"recall:{chat_id}:{int(hit.row_index)}",
                "memory_id_event": row_memory_id(chat_id, rows, hit.row_index, mode="on"),
                "row": int(hit.row_index),
                "score": float(hit.score),
                "routes": list(hit.routes),
            }
            for hit in list(hits)[:3]
        ],
        "suppressed_by_window": suppressed_by_window(chat_id),
        "elapsed_ms": int(elapsed_ms),
        "inject_cap": int(cap),
        "inject_plan_len": len(list(plan)),
        "inject_plan": [
            {
                "memory_id": f"recall:{chat_id}:{int(row_index)}",
                "memory_id_event": row_memory_id(chat_id, rows, row_index, mode="on"),
                "row": int(row_index),
                "role": role,
                "score": float(hit.score),
            }
            for row_index, role, hit in list(plan)[:6]
        ],
    }
    try:
        payload["routes_json"] = json.dumps(
            {
                "route_sizes": sizes,
                "routes": routes,
                "windows": (stats or {}).get("windows") or 0,
                "window_top": (stats or {}).get("window_top") or 0.0,
                "window_rows": (stats or {}).get("window_rows") or [],
                "vector": {
                    "known": int((stats or {}).get("vector_rows_known") or 0),
                    "missing": int((stats or {}).get("vector_rows_missing") or 0),
                    "mode": str((stats or {}).get("vector_cache_mode") or ""),
                },
            },
            ensure_ascii=False,
            default=str,
        )
    except Exception:  # noqa: BLE001
        warn("core/memory/recall_chain.py:2074 Exception", err=repr(__import__("sys").exc_info()[1]))
        payload["routes_json"] = "{}"
    return payload


def query_style_diff(
    *,
    chat_id: str,
    query: str,
    rows: Sequence[dict],
    hits: Sequence[ChainHit],
    context: Any = None,
    use_vector: bool = True,
) -> dict[str, Any] | None:
    """生产 query / 关键词包 / 原始 inbound：前 3 条 memory_id 的 Jaccard 对照。"""
    raw = raw_inbound(context)
    if not raw or not str(raw).strip():
        return None
    bag = bag_query(raw)
    prod = str(query or "").strip()
    prod_ids = _top_row_ids(chat_id, hits)

    def _ids(value: str) -> list[str]:
        return _top_row_ids(
            chat_id,
            chain_rank(rows, value, cache_key=chat_id, use_vector=use_vector),
        )

    try:
        raw_ids = prod_ids if str(raw).strip() == prod else _ids(str(raw))
        if bag and bag.strip() == prod:
            bag_ids = prod_ids
        elif bag and bag.strip() == str(raw).strip():
            bag_ids = raw_ids
        elif bag:
            bag_ids = _ids(bag)
        else:
            bag_ids = []
    except Exception as exc:  # noqa: BLE001 - 对照读数失败不影响影子主行
        log.warning("recall_chain_style_diff_failed", error=str(exc)[:160])
        return None
    return {
        "mode": "shadow",
        "chat_id": chat_id,
        "prod_query": prod[:200],
        "bag_query": bag[:200],
        "raw_query": str(raw)[:200],
        "jaccard_prod_raw": _jaccard(prod_ids, raw_ids),
        "jaccard_prod_bag": _jaccard(prod_ids, bag_ids),
        "jaccard_bag_raw": _jaccard(bag_ids, raw_ids),
        "top3_prod": prod_ids,
        "top3_bag": bag_ids,
        "top3_raw": raw_ids,
    }


class RecallChainProvider:
    """MemoryProvider that turns chat windows into row-level MemoryItems.

    The old prompt channel truncated every item to 120 chars (that block was retired
    2026-10-24 with ``_needs_memory_block``); the e2e probe then showed 5 of 6 gold rows
    sat past char 120.  Hence: window is the *retrieval* unit, the row is the *handed-out*
    unit, and the anchor row goes first.
    """

    kind = "recall_chain"

    def __init__(self, *, verify_evidence: bool = False) -> None:
        self.verify_evidence = bool(verify_evidence)

    def _evidence(self, row: dict[str, Any], principal: Any, chat_id: str) -> tuple[str, str]:
        event_id = str(row.get("event_id") or "").strip()
        if not event_id:
            return "", "unverified"
        if not self.verify_evidence:
            return event_id, "not_checked"
        from core.context.cognitive_commit import Scope
        from core.memory import belief_commit

        scope = Scope(
            str(getattr(principal, "platform", "") or ""),
            str(getattr(principal, "bot_account_id", "") or ""),
            chat_id,
        )
        try:
            state = belief_commit.evidence_state(event_id, scope)
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
        use_vector: bool = True,
    ) -> list[MemoryItem]:
        """``use_vector=False``：跳过向量通道（生产 ``.venv`` 无 sentence_transformers 的口径）。"""
        mode = recall_chain_mode()
        if mode == "off":
            return []
        chat_id = str(getattr(principal, "chat_id", "") or "")
        if not chat_id:
            return []
        started = time.perf_counter()
        rows = load_rows(chat_id)
        stats: dict[str, Any] = {}
        if media_tracing():
            try:
                media_rows = load_media_rows(chat_id)
                if media_rows:
                    rows = annotate_rows_with_media(rows, media_rows)
                    stats["media_rows"] = len(media_rows)
                    stats["media_rows_annotated"] = sum(
                        1 for row in rows if row.get("media")
                    )
            except Exception as exc:  # noqa: BLE001 - 媒体路缺席不影响主链
                log.warning("recall_chain_media_failed", error=str(exc)[:160])
        hits = chain_rank(
            rows, query, cache_key=chat_id, stats=stats, use_vector=use_vector
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        top = float(hits[0].score) if hits else 0.0
        log.info(
            "recall_chain_recall",
            mode=mode,
            chat_id=chat_id,
            query=str(query)[:80],
            qlen=len(str(query)),
            rows=len(rows),
            windows=len(hits),
            top=top,
            routes=list(hits[0].routes) if hits else [],
            vec_known=int(stats.get("vector_rows_known") or 0),
            vec_missing=int(stats.get("vector_rows_missing") or 0),
            vec_mode=str(stats.get("vector_cache_mode") or ""),
            media_rows=int(stats.get("media_rows") or 0),
            media_annotated=int(stats.get("media_rows_annotated") or 0),
        )
        if detail is not None:
            try:
                detail["recall_chain"] = {
                    "query": str(query)[:80],
                    "rows": len(rows),
                    "windows": len(hits),
                    "top": top,
                    "top_routes": list(hits[0].routes) if hits else [],
                    "hits_top": [
                        {
                            "memory_id": f"recall:{chat_id}:{int(hit.row_index)}",
                            "row": int(hit.row_index),
                            "score": float(hit.score),
                        }
                        for hit in list(hits)[:3]
                    ],
                    "elapsed_ms": elapsed_ms,
                    "suppressed_by_window": suppressed_by_window(chat_id),
                    "vector_rows_known": int(stats.get("vector_rows_known") or 0),
                    "vector_rows_missing": int(stats.get("vector_rows_missing") or 0),
                    "vector_cache_mode": str(stats.get("vector_cache_mode") or ""),
                }
            except Exception:  # noqa: BLE001
                warn("core/memory/recall_chain.py:2216 Exception", err=repr(__import__("sys").exc_info()[1]))
        cap = max(1, min(int(per_kind or 0) or int(limit or 0) or WINDOW_TOPK, 8))
        plan = emission_plan(rows, hits, cap=cap)
        if mode == "shadow":
            try:
                log.info(
                    "recall_chain_shadow",
                    **shadow_payload(
                        chat_id=chat_id,
                        query=query,
                        rows=rows,
                        hits=hits,
                        stats=stats,
                        elapsed_ms=elapsed_ms,
                        plan=plan,
                        cap=cap,
                    ),
                )
                style = query_style_diff(
                    chat_id=chat_id,
                    query=query,
                    rows=rows,
                    hits=hits,
                    context=context,
                    use_vector=use_vector,
                )
                if style:
                    log.info("query_style_diff", **style)
            except Exception as exc:  # noqa: BLE001 - 影子读数不许影响回合
                log.warning("recall_chain_shadow_failed", error=str(exc)[:160])
        if mode != "on":
            return []
        items: list[MemoryItem] = []
        seen_rows: set[int] = set()

        def _emit(hit: ChainHit, row_index: int, role: str) -> None:
            if len(items) >= cap or row_index in seen_rows or not (0 <= row_index < len(rows)):
                return
            body = str(rows[row_index].get("content") or "").strip()
            if not body:
                return
            event_id, evidence_state = self._evidence(rows[row_index], principal, chat_id)
            if evidence_state == "dead":
                return
            seen_rows.add(row_index)
            window_rows = rows[hit.start : hit.end]
            speakers = {
                str(r.get("name") or r.get("actor") or "").strip() for r in window_rows
            } - {""}
            if len(speakers) == 1:
                speaker = next(iter(speakers))
            elif speakers:
                speaker = "多人"  # window spans several people: never pick one of them
            else:
                speaker = ""
            items.append(
                MemoryItem(
                    memory_id=row_memory_id(chat_id, rows, row_index, mode=mode),
                    kind=self.kind,
                    content=body[:CONTENT_MAX],
                    speaker=speaker,
                    occurred_at=_aware(rows[row_index].get("at")),
                    platform=str(getattr(principal, "platform", "") or ""),
                    bot_account_id=str(getattr(principal, "bot_account_id", "") or ""),
                    chat_id=chat_id,
                    chat_type=str(getattr(principal, "chat_type", "") or ""),
                    source_event_ids=(event_id,) if event_id else (),
                    confidence=0.65,
                    importance=0.65 if role == "anchor" else 0.55,
                    metadata={
                        "source": "recall_chain",
                        "row_index": row_index,
                        "role": role,
                        "window": f"{hit.start}:{hit.end}",
                        "score": hit.score,
                        "routes": list(hit.routes),
                        # 稳定锚：老数据没有 event_id/span 时是 "" / []，键始终在，读侧不用猜
                        "anchor_message_id": hit.anchor_event_id,
                        "anchor_span": anchor_span(rows[hit.row_index]),
                        "neighbors": list(hit.neighbor_event_ids),
                        "evidence_state": evidence_state,
                    },
                )
            )

        # 1) every window's anchor row first: the prompt channel keeps only a few lines, and a
        #    gold row in window #2/#3 must not be crowded out by window #1's context rows.
        for hit in hits[: max(1, WINDOW_TOPK)]:
            _emit(hit, hit.row_index, "anchor")
        # 2) then surrounding rows in anchor-proximity order, round-robin across windows: the
        #    prompt channel keeps only a handful of lines, and a window's gold row usually sits
        #    within a couple of rows of its anchor (e2e: item 2939 was inside window #2 yet its
        #    rows lost the cap to window #1's far context).
        # 2a) 窗口成员按成员分降序（高分行先占位；struct 命中行就是那些金行）
        for hit in hits[: max(1, WINDOW_TOPK)]:
            for index in hit.members:
                _emit(hit, index, "context")
        # 2b) 兜底：窗口内没有通道分数的行，仍按锚行距离轮转补（保持旧行为不退化）
        for distance in range(1, WINDOW_SPAN + 1):
            for hit in hits[: max(1, WINDOW_TOPK)]:
                for index in (hit.row_index - distance, hit.row_index + distance):
                    if hit.start <= index < hit.end:
                        _emit(hit, index, "context")
        return items

# ---- task-23: background cache warmer ----

WARM_INTERVAL_ENV: str = "YINYUE_RECALL_CHAIN_WARM_INTERVAL_MIN"
WARM_INTERVAL_DEFAULT_MIN: float = 10.0
WARM_MAX_NEW_DEFAULT: int = 500
WARM_MAX_NEW_CAP: int = 2000


def warm_interval_minutes() -> float:
    """Minutes between background cache warm rounds (0 or negative = disabled)."""
    raw = os.environ.get(WARM_INTERVAL_ENV, "").strip()
    if not raw:
        return WARM_INTERVAL_DEFAULT_MIN
    try:
        return float(raw)
    except ValueError:
        warn("core/memory/recall_chain.py:2332 ValueError", err=repr(__import__("sys").exc_info()[1]))
        return WARM_INTERVAL_DEFAULT_MIN


def warmer_should_start(interval_min: float, mode: str) -> bool:
    """True only when the warmer is switched on and the recall chain is not off."""
    if str(mode or "").strip().lower() == "off":
        return False
    try:
        return float(interval_min or 0.0) > 0.0
    except (TypeError, ValueError):
        warn("core/memory/recall_chain.py:2342 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return False


def warm_cache_for(
    chat_id: str,
    *,
    rows: Sequence[dict] | None = None,
    max_new: int = WARM_MAX_NEW_DEFAULT,
    model: Any = None,
) -> dict:
    """Top up one chat's row-vector cache in the background (task-23).

    Only rows missing from the cache are embedded (at most ``max_new`` per call); the npz is
    then rewritten with ``vectors`` + ``hashes`` + ``fingerprint``.  Returns
    ``{known, missing_before, added, mode, elapsed_ms}``, or ``{"skipped": reason}`` when
    there is nothing to do right now (empty chat / model still loading / write failed).
    """
    import numpy as np

    started = time.monotonic()

    def _elapsed() -> float:
        return round((time.monotonic() - started) * 1000.0, 1)

    cid = str(chat_id or "").strip()
    if not cid:
        return {"skipped": "no_chat", "elapsed_ms": 0.0}
    source = list(rows) if rows is not None else load_rows(cid)
    if not source:
        return {"skipped": "no_rows", "elapsed_ms": _elapsed()}
    texts = [" ".join(str(row.get("content") or "").split())[:EMBED_CHARS] for row in source]
    hashes = row_hashes(texts)
    fingerprint = _fingerprint(texts)
    path = vector_cache_path(cid)
    with _LOCK:
        cached = _read_vector_cache(path) if path.exists() else None
        base_vectors = None
        cached_hashes = None
        cached_fingerprint = ""
        if cached is not None:
            base_vectors, cached_hashes, cached_fingerprint = cached
        if base_vectors is not None and cached_hashes is None:
            if len(base_vectors) == len(texts) and cached_fingerprint == fingerprint:
                return {
                    "known": len(texts),
                    "missing_before": 0,
                    "added": 0,
                    "mode": "exact",
                    "elapsed_ms": _elapsed(),
                }
            # Legacy cache (no per-row hashes): rebuild from scratch instead of guessing.
            base_vectors = None
        table: dict[str, int] = {}
        if cached_hashes is not None:
            table = {str(item): index for index, item in enumerate(cached_hashes)}
        missing = [index for index, item in enumerate(hashes) if str(item) not in table]
        missing_before = len(missing)
        known_before = len(texts) - missing_before
        had_base = base_vectors is not None
        if not missing:
            exact = had_base and cached_fingerprint == fingerprint and len(base_vectors) == len(texts)
            return {
                "known": known_before,
                "missing_before": 0,
                "added": 0,
                "mode": "exact" if exact else "incremental",
                "elapsed_ms": _elapsed(),
            }
        if model is None and not embedder_ready():
            warm_embedder_async()
            return {
                "skipped": "model_cold",
                "known": known_before,
                "missing_before": missing_before,
                "added": 0,
                "mode": "cold",
                "elapsed_ms": _elapsed(),
            }
        todo = missing[: max(1, min(int(max_new), WARM_MAX_NEW_CAP))]
        try:
            new_vectors = encode(model or embedder(), [texts[index] for index in todo])
        except Exception as exc:  # noqa: BLE001 - the warmer must never kill its thread
            log.warning("recall_chain_cache_warm_failed", chat=cid, error=str(exc)[:200])
            return {
                "skipped": "encode_failed",
                "known": known_before,
                "missing_before": missing_before,
                "added": 0,
                "mode": "error",
                "elapsed_ms": _elapsed(),
            }
        added = len(todo)
        same_space = (
            had_base
            and int(getattr(base_vectors, "ndim", 0)) == 2
            and int(base_vectors.shape[1]) == int(new_vectors.shape[1])
        )
        if same_space:
            merged = np.vstack([base_vectors, new_vectors])
            merged_hashes = [str(item) for item in (cached_hashes if cached_hashes is not None else [])] + [
                str(hashes[i]) for i in todo
            ]
        else:
            # No usable base, or the model changed dimension: start a clean cache instead of
            # mixing two vector spaces (missing rows simply return on later rounds).
            merged = new_vectors
            merged_hashes = [str(hashes[i]) for i in todo]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            np.savez(
                str(path),
                vectors=np.asarray(merged, dtype="float32"),
                hashes=np.asarray(merged_hashes, dtype=f"<U{ROW_HASH_CHARS}"),
                fingerprint=np.asarray(fingerprint),
            )
        except Exception as exc:  # noqa: BLE001 - a failed write must not kill the thread
            log.warning("recall_chain_cache_write_failed", chat=cid, error=str(exc)[:200])
            return {
                "skipped": "write_failed",
                "known": known_before,
                "missing_before": missing_before,
                "added": added,
                "mode": "error",
                "elapsed_ms": _elapsed(),
            }
    return {
        "known": min(len(texts), known_before + added),
        "missing_before": missing_before,
        "added": added,
        "mode": "incremental" if had_base else "computed",
        "elapsed_ms": _elapsed(),
    }
