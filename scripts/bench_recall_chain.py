"""P3 召回链量具（只读、离线）。

两个子命令：

* ``search`` 检索层：题库（``--items``，默认 bench/items.json；v2 形状见 task-14）上 hit@1 / hit@3 / MRR，按 question_kind / category 分组，附带 gold_in 与干扰项进块率。
* ``e2e``    端到端：同一批题，条件 ``prod``（生产原样）vs ``chain``（召回链窗口接管
  最近对话渲染）；互相各跑 >=2 遍，报 exact / token_recall / admission / halluc / chars。
  2026-10-24：按需记忆注入块（原 ``_needs_memory_block``）随读场整族退役，
  e2e 只量检索侧指标，注入列记 n/a。

不改 ``scripts/bench_replay.py`` 与 ``scripts/bench_retrieval.py``：复用前者的
``_read_rows`` / ``_keywords`` / ``_norm`` / ``_make_agent`` / ``_prompt_for`` / ``_chat`` / ``_grade``。
生产库只读（``read_only_engine``），向量模型离线（HF 缓存），不新增 LLM 调用以外的任何写。
"""
from __future__ import annotations

import argparse
import hashlib
import asyncio
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import bench_replay as br  # noqa: E402

from core.memory import recall_chain as rc  # noqa: E402
from core.memory import note_recall as nr  # noqa: E402  ①³ 随手记（task-15）
DEFAULT_ITEMS = ROOT / "bench" / "items.json"  # task-14：--items / v2 形状
OUT_DIR = ROOT / ".test_tmp" / "recall_chain"
SPAN = 6


QUERY_STYLES = ("question", "bag", "fused", "prod")
_PROD_QUERY_PATH = OUT_DIR / "prod_queries.json"


def bag_of(question: str) -> str:
    """生产式关键词包：与 .test_tmp/probe_query_style.py 同一 jieba 规则（去单字）。"""
    try:
        import jieba
        import jieba.posseg as pseg

        jieba.dt.cache_file = str(OUT_DIR / "jieba.cache")
    except Exception:  # noqa: BLE001
        return question
    keep: list[str] = []
    for word, flag in pseg.cut(question):
        word = word.strip()
        if len(word) < 2:
            continue
        if flag[0] in ("n", "v", "a", "e", "m", "q", "j", "l") or flag in ("eng", "nz"):
            keep.append(word)
    return " ".join(dict.fromkeys(keep)) or question


def prod_query(item: dict, question: str) -> str:
    """N1 needs_memory_query 的真实记录（若量具旁边有 prod_queries.json），否则回落 bag。"""
    try:
        mapping = json.loads(_PROD_QUERY_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return bag_of(question)
    value = str(mapping.get(item.get("item_id", "")) or "").strip()
    return value or bag_of(question)


def query_for(item: dict, style: str) -> str:
    question = item["question"]
    if style == "bag":
        return bag_of(question)
    if style == "fused":
        bag = bag_of(question)
        return " ".join(dict.fromkeys([question, *bag.split()])) if bag else question
    if style == "prod":
        return prod_query(item, question)
    return question


def struct_weight() -> float:
    return float(rc.ROUTE_WEIGHTS.get("struct") or 0.0)


@contextmanager
def _chain_mode(value: str):
    """task-14：provider 发射口径的 gold_in 需要在 chain 模式 on 下量。"""
    prev = os.environ.get("YINYUE_FEATURE_RECALL_CHAIN")
    os.environ["YINYUE_FEATURE_RECALL_CHAIN"] = value
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("YINYUE_FEATURE_RECALL_CHAIN", None)
        else:
            os.environ["YINYUE_FEATURE_RECALL_CHAIN"] = prev

@contextmanager
def _struct_mode(value: str):  # task-14a：gold_in 需要 chain 模式 on
    prev = os.environ.get("YINYUE_FEATURE_RECALL_STRUCT")
    os.environ["YINYUE_FEATURE_RECALL_STRUCT"] = value
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("YINYUE_FEATURE_RECALL_STRUCT", None)
        else:
            os.environ["YINYUE_FEATURE_RECALL_STRUCT"] = prev


@contextmanager
def _episode_mode(value: str):  # task-28：情节级（滑窗）向量召回
    prev = os.environ.get("YINYUE_FEATURE_RECALL_EPISODE")
    os.environ["YINYUE_FEATURE_RECALL_EPISODE"] = value
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("YINYUE_FEATURE_RECALL_EPISODE", None)
        else:
            os.environ["YINYUE_FEATURE_RECALL_EPISODE"] = prev


@contextmanager
def _prim_mode(value: str, turn_weight: str | None = None):  # task-33：类型无关原语
    prev = os.environ.get("YINYUE_FEATURE_RECALL_PRIMITIVES")
    prev_weight = os.environ.get("YINYUE_RECALL_CHAIN_TURN_WEIGHT")
    os.environ["YINYUE_FEATURE_RECALL_PRIMITIVES"] = value
    if turn_weight is None:
        os.environ.pop("YINYUE_RECALL_CHAIN_TURN_WEIGHT", None)
    else:
        os.environ["YINYUE_RECALL_CHAIN_TURN_WEIGHT"] = turn_weight
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("YINYUE_FEATURE_RECALL_PRIMITIVES", None)
        else:
            os.environ["YINYUE_FEATURE_RECALL_PRIMITIVES"] = prev
        if prev_weight is None:
            os.environ.pop("YINYUE_RECALL_CHAIN_TURN_WEIGHT", None)
        else:
            os.environ["YINYUE_RECALL_CHAIN_TURN_WEIGHT"] = prev_weight


@contextmanager
def _media_mode(value: str):
    """task-26: switch the media recall route (MEDIA_RECALL) for this block."""
    prev = os.environ.get("YINYUE_FEATURE_MEDIA_RECALL")
    os.environ["YINYUE_FEATURE_MEDIA_RECALL"] = value
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("YINYUE_FEATURE_MEDIA_RECALL", None)
        else:
            os.environ["YINYUE_FEATURE_MEDIA_RECALL"] = prev


_FILE_ONLY_RE = re.compile(
    r"^\s*([^\s:：/\\]{1,160}\.(?:pdf|docx?|xlsx?|pptx?|zip|rar|7z|txt|md|csv|json))\s*$",
    re.I,
)
_PLACEHOLDER_RE = re.compile(r"^\[?(文件|附件)\]?$")
_MEDIA_MIGRATED = False


def seed_media_rows(chat_id: str, rows: list[dict]) -> int:
    """task-26 bench: write the corpus' file names / placeholders as message_media rows.

    Only ever writes inside the isolated root (the db path must contain ``.test_tmp``);
    the production database is refused.  Idempotent: rows written by this bench carry
    ``source_path='bench_seed'`` and are deleted before being rewritten.
    """
    import sqlite3
    from pathlib import Path as _Path

    from core.bootstrap import get_meta_db_path

    db_path = _Path(str(get_meta_db_path()))
    if ".test_tmp" not in str(db_path).lower():
        raise SystemExit("seed_media_rows refuses non-isolated db: %s" % db_path)
    global _MEDIA_MIGRATED
    if not _MEDIA_MIGRATED:
        from db.migrations import run_migrations

        run_migrations(str(db_path))
        _MEDIA_MIGRATED = True
    candidates: list[tuple[str, str, str, str]] = []
    for row in rows:
        body = str(row.get("content") or "")
        event = str(row.get("event_id") or "").strip()
        if not event:
            continue
        match = _FILE_ONLY_RE.match(body)
        if match:
            candidates.append((event, match.group(1), "ready", body[:400]))
            continue
        if _PLACEHOLDER_RE.match(body.strip()):
            candidates.append((event, "", "missing", body[:400]))
    if not candidates:
        return 0
    sql = (
        "insert into message_media (platform, account_id, chat_id, sender_id, kind,"
        " status, md5, session_dir, quote_ref, source_path, local_path, description,"
        " attempts, last_error, created_at, event_id, size_bytes, mime,"
        " original_name, text_excerpt, meta_json)"
        " values ('wechat', '', ?, '', 'file', ?, ?, null, null, 'bench_seed', '',"
        " 'bench_seed', 0, '', datetime('now'), ?, 0, '', ?, ?, '')"
    )
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "delete from message_media where chat_id = ? and source_path = 'bench_seed'",
            (chat_id,),
        )
        for event, name, status, excerpt in candidates:
            digest = hashlib.md5(name.encode("utf-8")).hexdigest()
            conn.execute(
                sql,
                (chat_id, status, digest, event, name, excerpt),
            )
        conn.commit()
    finally:
        conn.close()
    return len(candidates)


def _embed_arm_used() -> str:
    """task-21: current embed arm name (st / local / off)."""
    try:
        return str(rc.embed_arm())
    except Exception:  # noqa: BLE001
        return ""


def _apply_embed_env(args) -> None:
    """task-21: --embed-arm / --chain-dir / --vector-warm-only -> env (idempotent)."""
    arm = str(getattr(args, "embed_arm", "") or "")
    if arm and arm != "auto":
        os.environ["YINYUE_RECALL_CHAIN_EMBED_ARM"] = arm
    chain_dir = str(getattr(args, "chain_dir", "") or "")
    if chain_dir:
        os.environ["YINYUE_RECALL_CHAIN_DIR"] = chain_dir
    os.environ["YINYUE_RECALL_CHAIN_VECTOR_WARM_ONLY"] = (
        "1" if int(getattr(args, "vector_warm_only", 1) or 0) else "0"
    )



def _arm_suffix(args) -> str:
    """task-21: keep the three vector arms in separate result files."""
    parts = []
    arm = _embed_arm_used()
    if bool(getattr(args, "no_vector", False)):
        parts.append("nov")
    elif arm and arm != "auto":
        parts.append(arm)
    if int(getattr(args, "vector_warm_only", 1) or 0) == 0:
        parts.append("compute")
    if bool(getattr(args, "with_window", False)) or bool(getattr(args, "with_window_cond", False)):
        parts.append("win")
    if bool(getattr(args, "with_media", False)):
        parts.append("media")
    if bool(getattr(args, "with_episode", False)):
        parts.append("ep")
    if bool(getattr(args, "tiebreak", False)):
        parts.append("tb")
    return ("_" + "_".join(parts)) if parts else ""



def items_path(spec: str = "") -> Path:
    """task-14：--items 允许换题库（默认 v1 保持兼容）。"""
    text = str(spec or "").strip()
    return Path(text) if text else DEFAULT_ITEMS


def items_tag(spec: str = "") -> str:
    return items_path(spec).stem


def load_items(limit: int = 0, path: str = "") -> list[dict]:
    items = json.loads(items_path(path).read_text(encoding="utf-8"))
    return items[: int(limit)] if limit else items


def item_kind(item: dict) -> str:
    return str(item.get("question_kind") or item.get("kind") or "?")


def item_window(item: dict) -> str:
    return str(item.get("category") or "?")


def row_known_gold(item: dict, rows: list[dict]) -> int:
    """row_index=-1 的条目在检索层没有金行（返回 -1，不计分母）。"""
    source = item.get("source") or {}
    try:
        declared = int(source.get("row_index", 0) or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared < 0:
        return -1
    return gold_index(item, rows)


def distractor_rows(item: dict) -> list[dict]:
    out: list[dict] = []
    for raw in item.get("distractors") or []:
        try:
            index = int(raw.get("row_index", -1))
        except (TypeError, ValueError):
            index = -1
        out.append(
            {
                "row_index": index,
                "content": str(raw.get("content") or ""),
                "kind": str(raw.get("kind") or ""),
            }
        )
    return out


def rate(values: list[bool]) -> dict:
    if not values:
        return {"n": 0, "rate": None}
    return {"n": len(values), "rate": round(sum(1 for v in values if v) / len(values), 4)}


def _fmt_rate(row: dict) -> str:
    return "n/a" if row.get("rate") is None else f"{row['rate'] * 100:5.1f}%"


def same_row(expected: str, actual: str) -> bool:
    probe = str(expected or "").strip()[:40]
    return bool(probe) and probe in str(actual or "")


def gold_index(item: dict, rows: list[dict]) -> int:
    source = item.get("source") or {}
    expected = str(source.get("content") or "")
    index = int(source.get("row_index") or 0)
    if 0 <= index < len(rows) and same_row(expected, rows[index].get("content", "")):
        return index
    for position, row in enumerate(rows):
        if same_row(expected, row.get("content", "")):
            return position
    return index


def r1_order(rows: list[dict], question: str) -> list[int]:
    """prod 关键词排序：score = sum(body.count(kw))，降序、平手取更早的行。"""
    keywords = br._keywords(question)
    scored: list[tuple[int, int]] = []
    for index, row in enumerate(rows):
        body = br._norm(str(row.get("content") or ""))
        score = sum(body.count(keyword) for keyword in keywords)
        if score > 0:
            scored.append((score, index))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [index for _score, index in scored]


def window_rank(order: list[int], gold: int, span: int = SPAN) -> int:
    for position, index in enumerate(order):
        if abs(int(index) - int(gold)) <= span:
            return position + 1
    return 0


def metrics(ranks: list[int]) -> dict:
    total = len(ranks)
    if not total:
        return {"n": 0, "hit@1": 0.0, "hit@3": 0.0, "mrr": 0.0}
    return {
        "n": total,
        "hit@1": sum(1 for rank in ranks if rank == 1) / total,
        "hit@3": sum(1 for rank in ranks if 1 <= rank <= 3) / total,
        "mrr": sum((1.0 / rank) for rank in ranks if rank > 0) / total,
    }


def _fmt(row: dict) -> str:
    return (
        f"{row['name']:14s} n={row['n']:2d} hit@1={row['hit@1']*100:5.1f}% "
        f"hit@3={row['hit@3']*100:5.1f}% MRR={row['mrr']:.3f}"
    )

def chain_rank_of(hits: list, gold: int) -> int:
    for position, hit in enumerate(hits):
        if hit.start <= gold < hit.end:
            return position + 1
    return 0


def index_of_event(rows: list[dict], event_id: str) -> int:
    """event_id -> 当前行号；-1 = 这条事件已不在窗口里（不是"锚错了"）。"""
    if not event_id:
        return -1
    for position, row in enumerate(rows):
        if str(row.get("event_id") or "") == event_id:
            return position
    return -1


def cmd_search(args) -> int:
    """task-14：--items 支持题库 v2（question_kind/distractors/row_index=-1）。"""
    items = load_items(args.limit, args.items)
    tag = items_tag(args.items)
    rows_by_chat: dict[str, list[dict]] = {}
    principal = SimpleNamespace(
        person_id="wxid_owner", chat_id="", chat_type="", platform="wechat", bot_account_id=""
    )
    style = str(getattr(args, "query_style", "question") or "question")
    groups: dict[str, dict] = {}
    detail: list[dict] = []
    skipped: list[str] = []
    started = time.time()
    anchor_mode = str(getattr(args, "anchor_mode", "row") or "row")
    shift = max(0, int(getattr(args, "shift", 0) or 0))
    anchor_moved = 0
    anchor_lost = 0

    def bucket(name: str) -> dict:
        return groups.setdefault(
            name,
            {
                "r1": [],
                "chain": [],
                "nov": [],
                "struct": [],
                "gold_in_chain": [],
                "gold_in_struct": [],
                "distractor_in_chain": [],
                "distractor_in_struct": [],
                "media": [],
                "gold_in_media": [],
                "distractor_in_media": [],
                "episode": [],
                "gold_in_episode": [],
                "distractor_in_episode": [],
                "prim": [],
                "gold_in_prim": [],
                "distractor_in_prim": [],
                "prim_turn": [],
                "gold_in_prim_turn": [],
                "distractor_in_prim_turn": [],
            },
        )

    def emitted_rows(hits: list) -> set[int]:
        out: set[int] = set()
        for hit in hits:
            try:
                out.add(int(hit.metadata.get("row_index", -1)))
            except (TypeError, ValueError):
                continue
        return out

    for item in items:
        chat_id = item["chat_id"]
        if chat_id not in rows_by_chat:
            rows_by_chat[chat_id] = rc.load_rows(chat_id)
        rows = rows_by_chat[chat_id]
        kind = item_kind(item)
        window = item_window(item)
        gold = row_known_gold(item, rows)
        gold_event = str(rows[gold].get("event_id") or "") if 0 <= gold < len(rows) else ""
        if shift and gold >= 0:
            rows = rows[shift:]
            moved_to = index_of_event(rows, gold_event) if gold_event else -1
            if moved_to < 0:
                anchor_lost += 1
            elif anchor_mode == "row":
                if not (0 <= gold < len(rows)) or str(rows[gold].get("event_id") or "") != gold_event:
                    anchor_moved += 1
            if anchor_mode == "message":
                gold = moved_to
        distractors = distractor_rows(item)
        question = item["question"]
        query = query_for(item, style)
        entry = {
            "item_id": item["item_id"],
            "chat_id": chat_id,
            "question_kind": kind,
            "category": window,
            "gold_row": gold,
            "rows": len(rows),
            "query_style": style,
            "query": query,
            "question": question,
            "distractor_rows": [d["row_index"] for d in distractors],
        }
        if gold < 0:
            skipped.append(str(item["item_id"]))
            entry["skipped"] = "row_index=-1（不计检索层分母，仅 e2e）"
            detail.append(entry)
            print(f"  {item['item_id'][-28:]:28s} skipped（row_index=-1，仅 e2e）")
            continue
        principal.chat_id = chat_id
        principal.chat_type = "group" if chat_id.endswith("@chatroom") else "private"
        rank_r1 = window_rank(r1_order(rows, query), gold, args.span)
        rank_nov = 0
        if not args.no_vector:
            with _struct_mode("off"):
                hits_nov = rc.chain_rank(
                    rows, query, cache_key=chat_id, use_vector=False, span=args.span
                )
            rank_nov = chain_rank_of(hits_nov, gold)
        with _chain_mode("on"), _struct_mode("off"):
            hits = rc.chain_rank(
                rows, query, cache_key=chat_id, use_vector=not args.no_vector, span=args.span
            )
            emit_chain = rc.RecallChainProvider().recall(
                principal, query, use_vector=not args.no_vector
            )
        rank_chain = chain_rank_of(hits, gold)
        with _chain_mode("on"), _struct_mode("on"):
            hits_struct = rc.chain_rank(
                rows, query, cache_key=chat_id, use_vector=not args.no_vector, span=args.span
            )
            emit_struct = rc.RecallChainProvider().recall(
                principal, query, use_vector=not args.no_vector
            )
        rank_struct = chain_rank_of(hits_struct, gold)
        rows_chain = emitted_rows(emit_chain)
        rows_struct = emitted_rows(emit_struct)
        distractor_indexes = {d["row_index"] for d in distractors}
        rank_media = 0
        gold_media = False
        dis_media = False
        if bool(getattr(args, "with_media", False)):
            seeded = seed_media_rows(chat_id, rows)
            rows_media = rc.annotate_rows_with_media(rows, rc.load_media_rows(chat_id))
            with _chain_mode("on"), _struct_mode("on"), _media_mode("on"):
                hits_media = rc.chain_rank(
                    rows_media,
                    query,
                    cache_key=chat_id,
                    use_vector=not args.no_vector,
                    span=args.span,
                )
                emit_media = rc.RecallChainProvider().recall(
                    principal, query, use_vector=not args.no_vector
                )
            rank_media = chain_rank_of(hits_media, gold)
            rows_media_emit = emitted_rows(emit_media)
            gold_media = gold in rows_media_emit
            dis_media = bool(distractor_indexes & rows_media_emit)
            entry["media_seeded"] = seeded
            entry["chain_struct_media_routes"] = (
                list(hits_media[0].routes) if hits_media else []
            )
        rank_episode = 0
        gold_episode = False
        dis_episode = False
        if bool(getattr(args, "with_episode", False)):
            prev_tb = os.environ.get("YINYUE_RECALL_CHAIN_TIE_BREAK")
            os.environ["YINYUE_RECALL_CHAIN_TIE_BREAK"] = (
                "1" if bool(getattr(args, "tiebreak", False)) else "0"
            )
            try:
                with _chain_mode("on"), _struct_mode("on"), _episode_mode("on"):
                    entry["episode_mode"] = rc.recall_episode_mode()
                    hits_episode = rc.chain_rank(
                        rows,
                        query,
                        cache_key=chat_id,
                        use_vector=not args.no_vector,
                        span=args.span,
                    )
                    emit_episode = rc.RecallChainProvider().recall(
                        principal, query, use_vector=not args.no_vector
                    )
            finally:
                if prev_tb is None:
                    os.environ.pop("YINYUE_RECALL_CHAIN_TIE_BREAK", None)
                else:
                    os.environ["YINYUE_RECALL_CHAIN_TIE_BREAK"] = prev_tb
            rank_episode = chain_rank_of(hits_episode, gold)
            rows_episode = emitted_rows(emit_episode)
            gold_episode = gold in rows_episode
            dis_episode = bool(distractor_indexes & rows_episode)
            entry["chain_struct_episode_routes"] = (
                list(hits_episode[0].routes) if hits_episode else []
            )
        rank_prim = 0
        gold_prim = False
        dis_prim = False
        turn_stats: dict = {}
        with_prim = bool(getattr(args, "with_prim", False))
        with_turn = bool(getattr(args, "with_turn", False)) and with_prim
        if with_prim:
            with _chain_mode("on"), _struct_mode("on"), _prim_mode("on", "0"):
                hits_prim = rc.chain_rank(
                    rows,
                    query,
                    cache_key=chat_id,
                    use_vector=not args.no_vector,
                    span=args.span,
                )
                emit_prim = rc.RecallChainProvider().recall(
                    principal, query, use_vector=not args.no_vector
                )
            rank_prim = chain_rank_of(hits_prim, gold)
            rows_prim = emitted_rows(emit_prim)
            gold_prim = gold in rows_prim
            dis_prim = bool(distractor_indexes & rows_prim)
            entry["chain_struct_prim_routes"] = (
                list(hits_prim[0].routes) if hits_prim else []
            )
        rank_prim_turn = 0
        gold_prim_turn = False
        dis_prim_turn = False
        if with_turn:
            turn_weight_arg = float(getattr(args, "turn_weight", -1.0) or -1.0)
            with _chain_mode("on"), _struct_mode("on"), _prim_mode(
                "on", None if turn_weight_arg < 0 else str(turn_weight_arg)
            ):
                hits_prim_turn = rc.chain_rank(
                    rows,
                    query,
                    cache_key=chat_id,
                    use_vector=not args.no_vector,
                    span=args.span,
                    stats=turn_stats,
                )
                emit_prim_turn = rc.RecallChainProvider().recall(
                    principal, query, use_vector=not args.no_vector
                )
            rank_prim_turn = chain_rank_of(hits_prim_turn, gold)
            rows_prim_turn = emitted_rows(emit_prim_turn)
            gold_prim_turn = gold in rows_prim_turn
            dis_prim_turn = bool(distractor_indexes & rows_prim_turn)
            entry["chain_struct_prim_turn_routes"] = (
                list(hits_prim_turn[0].routes) if hits_prim_turn else []
            )
            entry["turn_segments"] = turn_stats.get("turn_segments")
            entry["turn_rows_mean"] = turn_stats.get("turn_rows_mean")
            entry["turn_rows_max"] = turn_stats.get("turn_rows_max")
            entry["turn_cache_mode"] = turn_stats.get("turn_cache_mode")
        gold_chain = gold in rows_chain
        gold_struct = gold in rows_struct
        dis_chain = bool(distractor_indexes & rows_chain)
        dis_struct = bool(distractor_indexes & rows_struct)
        for name in ("all", kind, window):
            b = bucket(name)
            b["r1"].append(rank_r1)
            b["chain"].append(rank_chain)
            b["nov"].append(rank_nov)
            b["struct"].append(rank_struct)
            b["gold_in_chain"].append(gold_chain)
            b["gold_in_struct"].append(gold_struct)
            b["distractor_in_chain"].append(dis_chain)
            b["distractor_in_struct"].append(dis_struct)
            b["media"].append(rank_media)
            b["gold_in_media"].append(gold_media)
            b["distractor_in_media"].append(dis_media)
            b["episode"].append(rank_episode)
            b["gold_in_episode"].append(gold_episode)
            b["distractor_in_episode"].append(dis_episode)
            b["prim"].append(rank_prim)
            b["gold_in_prim"].append(gold_prim)
            b["distractor_in_prim"].append(dis_prim)
            b["prim_turn"].append(rank_prim_turn)
            b["gold_in_prim_turn"].append(gold_prim_turn)
            b["distractor_in_prim_turn"].append(dis_prim_turn)
        top = hits[0] if hits else None
        top_struct = hits_struct[0] if hits_struct else None
        entry.update(
            {
                "r1_rank": rank_r1,
                "chain_rank": rank_chain,
                "chain_no_vector_rank": rank_nov,
                "chain_struct_rank": rank_struct,
                "gold_in_chain": gold_chain,
                "gold_in_struct": gold_struct,
                "distractor_in_chain": dis_chain,
                "distractor_in_struct": dis_struct,
                "top_routes": list(top.routes) if top else [],
                "top_span": [top.start, top.end] if top else [],
                "chain_struct_routes": list(top_struct.routes) if top_struct else [],
                "chain_struct_span": [top_struct.start, top_struct.end] if top_struct else [],
                "chain_struct_media_rank": rank_media,
                "gold_in_media": gold_media,
                "distractor_in_media": dis_media,
                "chain_struct_episode_rank": rank_episode,
                "gold_in_episode": gold_episode,
                "distractor_in_episode": dis_episode,
                "chain_struct_prim_rank": rank_prim,
                "gold_in_prim": gold_prim,
                "distractor_in_prim": dis_prim,
                "chain_struct_prim_turn_rank": rank_prim_turn,
                "gold_in_prim_turn": gold_prim_turn,
                "distractor_in_prim_turn": dis_prim_turn,
            }
        )
        detail.append(entry)
        print(
            f"  {item['item_id'][-24:]:24s} {kind:8s} gold={gold:5d} r1={rank_r1:2d} "
            f"chain={rank_chain:2d} struct={rank_struct:2d} "
            f"prim={rank_prim:2d} turn={rank_prim_turn:2d} "
            f"gold_in={int(gold_struct)} dis={int(dis_struct)}"
        )

    def summarize(b: dict) -> dict:
        return {
            "r1": metrics(b["r1"]),
            "chain": metrics(b["chain"]),
            "chain_no_vector": metrics(b["nov"]),
            "chain_struct": metrics(b["struct"]),
            "gold_in_chain": rate(b["gold_in_chain"]),
            "gold_in_struct": rate(b["gold_in_struct"]),
            "distractor_in_chain": rate(b["distractor_in_chain"]),
            "distractor_in_struct": rate(b["distractor_in_struct"]),
            "chain_struct_media": metrics(b["media"]),
            "gold_in_media": rate(b["gold_in_media"]),
            "distractor_in_media": rate(b["distractor_in_media"]),
            "chain_struct_episode": metrics(b["episode"]),
            "gold_in_episode": rate(b["gold_in_episode"]),
            "distractor_in_episode": rate(b["distractor_in_episode"]),
            "chain_struct_prim": metrics(b["prim"]),
            "gold_in_prim": rate(b["gold_in_prim"]),
            "distractor_in_prim": rate(b["distractor_in_prim"]),
            "chain_struct_prim_turn": metrics(b["prim_turn"]),
            "gold_in_prim_turn": rate(b["gold_in_prim_turn"]),
            "distractor_in_prim_turn": rate(b["distractor_in_prim_turn"]),
        }

    overall = summarize(bucket("all"))
    turn_rows = [
        entry for entry in detail if entry.get("turn_segments") is not None
    ]
    segment_stats = {
        "segments_mean": (
            round(sum(int(entry["turn_segments"]) for entry in turn_rows) / len(turn_rows), 1)
            if turn_rows
            else None
        ),
        "rows_mean": (
            round(sum(float(entry.get("turn_rows_mean") or 0) for entry in turn_rows) / len(turn_rows), 2)
            if turn_rows
            else None
        ),
        "rows_max": max((int(entry.get("turn_rows_max") or 0) for entry in turn_rows), default=None),
        "cache_mode": (
            sorted({str(entry.get("turn_cache_mode") or "?") for entry in turn_rows})
            if turn_rows
            else None
        ),
    }
    table = {
        "R1 关键词（query=%s，±6 窗口）" % style: overall["r1"],
        "R2 召回链（向量+fts+关键词，struct off）": overall["chain"],
        "R3 召回链（无向量消融）": overall["chain_no_vector"],
        "R4 召回链+struct（权重 %.2f，冻结）" % struct_weight(): overall["chain_struct"],
        "R5 召回链+struct+media（--with-media）": overall["chain_struct_media"],
        "R6 召回链+struct+episode（--with-episode）": overall["chain_struct_episode"],
    }
    if bool(getattr(args, "with_prim", False)):
        table["R7 召回链+struct+prim（--with-prim）"] = overall["chain_struct_prim"]
    if bool(getattr(args, "with_turn", False)):
        table["R8 召回链+struct+prim+turn（--with-turn）"] = overall["chain_struct_prim_turn"]
    print(
        "\n=== 检索层（%s：题目 %d，有效分母 %d，row_index=-1 跳过 %d；span=%d，query_style=%s）==="
        % (tag, len(items), overall["r1"]["n"], len(skipped), args.span, style)
    )
    for name, row in table.items():
        print("  " + _fmt({"name": name, **row}))
    print(
        "  gold_in（provider 发射口径）：chain=%s struct=%s media=%s episode=%s"
        % (
            _fmt_rate(overall["gold_in_chain"]),
            _fmt_rate(overall["gold_in_struct"]),
            _fmt_rate(overall["gold_in_media"]),
            _fmt_rate(overall["gold_in_episode"]),
        )
    )
    print(
        "  干扰行进块：chain=%s struct=%s media=%s episode=%s"
        % (
            _fmt_rate(overall["distractor_in_chain"]),
            _fmt_rate(overall["distractor_in_struct"]),
            _fmt_rate(overall["distractor_in_media"]),
            _fmt_rate(overall["distractor_in_episode"]),
        )
    )
    if bool(getattr(args, "with_prim", False)):
        print(
            "  prim 读数：gold_in lex=%s turn=%s；干扰行进块 prim=%s turn=%s；"
            "turn 段数 mean=%s / rows_mean=%s / rows_max=%s / cache=%s"
            % (
                _fmt_rate(overall["gold_in_prim"]),
                _fmt_rate(overall["gold_in_prim_turn"]),
                _fmt_rate(overall["distractor_in_prim"]),
                _fmt_rate(overall["distractor_in_prim_turn"]),
                segment_stats.get("segments_mean"),
                segment_stats.get("rows_mean"),
                segment_stats.get("rows_max"),
                segment_stats.get("cache_mode"),
            )
        )
    if anchor_mode != "row" or shift:
        per50 = (anchor_moved * 50.0 / shift) if shift else 0.0
        print(
            "  锚点（mode=%s shift=%d）：行号失效 %d 条（每 50 条新增消息 ≈ %.1f 条），锚丢失 %d 条"
            % (anchor_mode, shift, anchor_moved, per50, anchor_lost)
        )
    print("\n=== 分组（question_kind / category）===")
    for name in sorted(g for g in groups if g != "all"):
        s = summarize(groups[name])
        print(
            "  %-13s n=%2d R1 h@1=%5.1f%% chain h@1=%5.1f%% struct h@1=%5.1f%% h@3=%5.1f%%"
            " media h@1=%5.1f%% episode h@1=%5.1f%% h@3=%5.1f%% gold_in=%s ep_gold=%s"
            % (
                name,
                s["r1"]["n"],
                s["r1"]["hit@1"] * 100,
                s["chain"]["hit@1"] * 100,
                s["chain_struct"]["hit@1"] * 100,
                s["chain_struct"]["hit@3"] * 100,
                s["chain_struct_media"]["hit@1"] * 100,
                s["chain_struct_episode"]["hit@1"] * 100,
                s["chain_struct_episode"]["hit@3"] * 100,
                _fmt_rate(s["gold_in_struct"]),
                _fmt_rate(s["gold_in_episode"]),
            )
        )
    payload = {
        "ts": time.strftime("%Y%m%d_%H%M%S"),
        "items_path": str(items_path(args.items)),
        "items_tag": tag,
        "span": args.span,
        "no_vector": bool(args.no_vector),
        "embed_arm": _embed_arm_used(),
        "chain_dir": str(os.environ.get("YINYUE_RECALL_CHAIN_DIR") or ""),
        "vector_warm_only": bool(int(os.environ.get("YINYUE_RECALL_CHAIN_VECTOR_WARM_ONLY", "0") or 0)),
        "query_style": style,
        "struct_weight": struct_weight(),
        "items": len(items),
        "anchor": {
            "mode": anchor_mode,
            "shift": shift,
            "moved": anchor_moved,
            "lost": anchor_lost,
            "moved_per_50": round((anchor_moved * 50.0 / shift), 2) if shift else 0.0,
        },
        "skipped_row_index_minus_one": skipped,
        "table": table,
        "overall": overall,
        "prim": (
            {
                "enabled": True,
                "with_turn": bool(getattr(args, "with_turn", False)),
                "turn_weight": rc.turn_weight(),
                "char_n": rc.PRIM_CHAR_N,
                "k1": rc.BM25_K1,
                "b": rc.BM25_B,
                "weights": {
                    "lex3": rc.PRIM_WEIGHT_CHAR,
                    "lexw": rc.PRIM_WEIGHT_WORD,
                    "turn": rc.turn_weight(),
                },
                "segments": segment_stats,
            }
            if bool(getattr(args, "with_prim", False))
            else {"enabled": False}
        ),
        "groups": {name: summarize(b) for name, b in groups.items()},
        "detail": detail,
        "seconds": round(time.time() - started, 1),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f"{tag}_{style}" + _arm_suffix(args)
    out = OUT_DIR / (f"search_results_{suffix}.json" if args.items else "search_results.json")
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nsearch_results -> {out}（{payload['seconds']}s）")
    return 0


LIMIT_ENV = (
    "YINYUE_NEEDS_MEMORY_ITEMS",
    "YINYUE_NEEDS_MEMORY_CHARS",
    "YINYUE_NEEDS_MEMORY_ITEM_CHARS",
)


def apply_limits(spec: str) -> str:
    """task-10：设/清 [按需记忆] 上限 env；返回口径串（便于落进结果 JSON）。"""
    for name in LIMIT_ENV:
        os.environ.pop(name, None)
    text = str(spec or "").strip()
    if not text:
        return "6/600/120"
    parts = [part.strip() for part in text.split("/")]
    if len(parts) != 3:
        raise SystemExit("--limits 需要 items/chars/item_chars，例如 8/800/160")
    resolved = []
    for name, value in zip(LIMIT_ENV, parts):
        number = int(value)
        os.environ[name] = str(number)
        resolved.append(str(number))
    return "/".join(resolved)


def _distinct_probes(item: dict, rows: list[dict], gold_row: int) -> list[str]:
    """task-14 干扰项探针：取干扰行正文里 >=4 字的连续中文/数字段，排除金行已有串。"""
    gold_text = str(rows[gold_row].get("content") or "") if 0 <= gold_row < len(rows) else ""
    probes: list[str] = []
    for raw in item.get("distractors") or []:
        content = str(raw.get("content") or "")
        for seg in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{4,}", content):
            if seg not in gold_text and seg not in probes:
                probes.append(seg)
    probes.sort(key=len, reverse=True)
    return probes[:2]


async def _one(
    item: dict, rows: list[dict], condition: str, args, principal, gold_row: int = -1
) -> dict:
    import agents.conversational as conv

    chat_id = item["chat_id"]
    agent, snapshot, _ = br._make_agent(
        chat_id, item["question"], sender=args.sender, owner=True
    )
    is_group = chat_id.endswith("@chatroom")
    agent._inbound_content = item["question"]
    base = br._prompt_for(agent, snapshot, is_group)
    block = ""
    prompt = base
    n_items = 0
    gold_in = False
    distractor_in_block = False
    block_rows: set[int] = set()
    detail: dict = {}
    note_items: list = []
    gold_mid = ""
    notes_write = False
    if condition in (
        "chain",
        "chain_struct",
        "notes",
        "notes_chain_struct",
        "window",
        "window_chain_struct",
        "chain_struct_media",
        "chain_struct_episode",
        "chain_struct_prim",
        "chain_struct_prim_turn",
    ):
        use_window_block = condition in ("window", "window_chain_struct")
        if use_window_block:
            args.with_window_cond = True
        if use_window_block:
            agent._group_recent_context = br._window_text(chat_id)
            base = br._prompt_for(agent, snapshot, is_group)
            prompt = base
        want_chain = condition in (
            "chain",
            "chain_struct",
            "notes_chain_struct",
            "window_chain_struct",
            "chain_struct_media",
            "chain_struct_episode",
            "chain_struct_prim",
            "chain_struct_prim_turn",
        )
        want_media = condition == "chain_struct_media"
        want_prim = condition in ("chain_struct_prim", "chain_struct_prim_turn")
        want_notes = condition in ("notes", "notes_chain_struct")
        os.environ["YINYUE_FEATURE_RECALL_CHAIN"] = "on" if want_chain else "off"
        os.environ["YINYUE_FEATURE_RECALL_STRUCT"] = (
            "on"
            if condition
            in (
                "chain_struct",
                "notes_chain_struct",
                "window_chain_struct",
                "chain_struct_prim",
                "chain_struct_prim_turn",
            )
            else "off"
        )
        # task-33：类型无关原语（词法 BM25 替掉 keyword+fts；语义层可加轮/段向量）
        os.environ["YINYUE_FEATURE_RECALL_PRIMITIVES"] = "on" if want_prim else "off"
        if condition == "chain_struct_prim":
            os.environ["YINYUE_RECALL_CHAIN_TURN_WEIGHT"] = "0"
        elif condition == "chain_struct_prim_turn":
            os.environ.pop("YINYUE_RECALL_CHAIN_TURN_WEIGHT", None)
        else:
            os.environ.pop("YINYUE_RECALL_CHAIN_TURN_WEIGHT", None)
        os.environ["YINYUE_FEATURE_WORTH_REMEMBERING"] = "on" if want_notes else "off"
        os.environ["YINYUE_FEATURE_MEDIA_RECALL"] = "on" if want_media else "off"
        os.environ["YINYUE_FEATURE_RECALL_EPISODE"] = (
            "on" if condition == "chain_struct_episode" else "off"
        )
        os.environ["YINYUE_RECALL_CHAIN_TIE_BREAK"] = (
            "1" if bool(getattr(args, "tiebreak", False)) else "0"
        )
        if want_media:
            try:
                seeded = seed_media_rows(chat_id, rows)
                print(f"   media_seed chat={chat_id} rows={seeded}")
            except SystemExit as exc:
                print(f"   media_seed refused: {exc}")
        try:
            recall_query = query_for(
                item, str(getattr(args, "query_style", "question") or "question")
            )
            chain_items = (
                rc.RecallChainProvider().recall(
                    principal,
                    recall_query,
                    use_vector=not bool(getattr(args, "no_vector", False)),
                )
                if want_chain
                else []
            )
            note_items = (
                nr.NoteRecallProvider().recall(
                    principal, recall_query, limit=6, detail=detail
                )
                if want_notes
                else []
            )
            if condition == "notes":
                items = note_items
            elif condition == "notes_chain_struct":
                items = nr.merge_with_chain(note_items, chain_items)
            else:
                items = chain_items
            for hit in items:
                try:
                    block_rows.add(int(hit.metadata.get("row_index", -1)))
                except (TypeError, ValueError):
                    continue
            distractor_in_block = bool(
                {d["row_index"] for d in distractor_rows(item)} & block_rows
            )
            extra = {
                "chat_id": chat_id,
                "is_group": is_group,
                "needs_memory_hits": [hit.as_dict() for hit in items],
            }
            # 量具前提：默认窗口未接管该会话（生产上窗口接管的会话本就不注入）。
            # 2026-10-24：注入这块读数（`conv._needs_memory_block` + RECALL_IN_WINDOW 门）
            # 随「读场 + 按需记忆」整族退役，量具只保留检索侧指标；注入列记 n/a。
            block = ""
            n_items = len(items)
            if gold_row >= 0:
                gold_text = str(rows[gold_row].get("content") or "").strip()
                gold_mid = str(rows[gold_row].get("event_id") or "")
                gold_in = any(
                    str(it.metadata.get("row_index")) == str(gold_row)
                    or (bool(gold_text[:20]) and gold_text[:20] in str(it.content))
                    or (
                        bool(gold_mid)
                        and str(((it.metadata or {}).get("span") or {}).get("message_id") or "")
                        == gold_mid
                    )
                    for it in items
                )
                if want_notes and gold_mid:
                    notes_write = any(
                        str((note.get("span") or {}).get("message_id") or "") == gold_mid
                        for note in nr.load_notes(chat_id)
                    )
        finally:
            os.environ["YINYUE_FEATURE_RECALL_CHAIN"] = "off"
            os.environ["YINYUE_FEATURE_RECALL_STRUCT"] = "off"
            os.environ["YINYUE_FEATURE_WORTH_REMEMBERING"] = "off"
            os.environ["YINYUE_FEATURE_MEDIA_RECALL"] = "off"
            os.environ["YINYUE_FEATURE_RECALL_EPISODE"] = "off"
            os.environ["YINYUE_FEATURE_RECALL_PRIMITIVES"] = "off"
            os.environ.pop("YINYUE_RECALL_CHAIN_TURN_WEIGHT", None)
            os.environ["YINYUE_RECALL_CHAIN_TIE_BREAK"] = "0"
        if block:
            prompt = conv._append_prompt_block(base, block)
    try:
        answer = await br._chat(agent, prompt, item["question"])
    except Exception as exc:  # noqa: BLE001
        answer = f"[ERROR] {type(exc).__name__}: {exc}"
    grade = br._grade(answer, item, rows)
    probes = _distinct_probes(item, rows, gold_row)
    norm_answer = br._norm(answer)
    grade["condition"] = condition
    grade["block_chars"] = len(block)
    grade["window_chars"] = len(str(getattr(agent, "_group_recent_context", "") or ""))
    grade["injected"] = bool(block)
    grade["n_items"] = n_items
    grade["gold_row"] = gold_row
    grade["gold_known"] = gold_row >= 0
    grade["gold_injected"] = gold_in
    grade["distractor_in_block"] = distractor_in_block
    grade["distractor_probes"] = probes
    grade["answered_distractor"] = any(probe in norm_answer for probe in probes)
    note_detail = detail.get("note_recall", {}) if isinstance(detail, dict) else {}
    grade["notes_write_side"] = bool(notes_write)
    grade["notes_read_side"] = bool(
        gold_mid
        and any(
            str(((it.metadata or {}).get("span") or {}).get("message_id") or "") == gold_mid
            for it in note_items
        )
    )
    grade["notes_injected"] = len(note_items)
    grade["notes_gold_mid"] = gold_mid
    grade["span_broken"] = note_detail.get("span_broken")
    grade["notes_budget"] = note_detail.get("budget")
    return grade


def _pass_rates(runs: list[dict]) -> dict:
    n = max(1, len(runs))
    known = [run for run in runs if run.get("gold_known")]
    gold = sum(1 for run in known if run.get("gold_injected"))
    return {
        "exact_rate": round(sum(1 for run in runs if run["exact"]) / n, 4),
        "gold_injected_rate": round(gold / len(known), 4) if known else None,
        "injected_rate": round(sum(1 for run in runs if run.get("injected")) / n, 4),
        "admission_rate": round(sum(1 for run in runs if run.get("admission")) / n, 4),
        "chars_mean": round(sum(run["chars"] for run in runs) / n, 1),
    }


def _noise_summary(results: list[dict], conditions: list[str], reps: int, enabled: bool) -> dict:
    """噪声底：同配置两遍的 exact 差（pp）；未启用时 headline 标 single-pass。"""
    passes: list[dict] = []
    for condition in conditions:
        for index in range(reps):
            runs = [
                entry["conditions"][condition][index]
                for entry in results
                if index < len(entry["conditions"].get(condition) or [])
            ]
            passes.append({"arm": condition, "pass": index + 1, "n": len(runs), **_pass_rates(runs)})
    ref = conditions[0] if conditions else ""
    ref_passes = [row for row in passes if row["arm"] == ref]
    floor = None
    if enabled and len(ref_passes) >= 2:
        floor = round(abs(ref_passes[0]["exact_rate"] - ref_passes[1]["exact_rate"]) * 100, 1)
    ref_mean = sum(row["exact_rate"] for row in ref_passes) / max(1, len(ref_passes))
    headline: dict[str, dict] = {}
    for condition in conditions:
        rows = [row for row in passes if row["arm"] == condition]
        rates = [row["exact_rate"] for row in rows]
        mean = sum(rates) / max(1, len(rates))
        delta = None
        verdict = "single-pass"
        if floor is not None:
            delta = round((mean - ref_mean) * 100, 1)
            if condition == ref:
                verdict = "reference"
            elif abs(delta) < floor or abs(delta) < 1e-9:
                verdict = "indistinguishable"
            else:
                verdict = "effect"
        headline[condition] = {
            "mean_exact": round(mean, 4),
            "min_exact": min(rates) if rates else None,
            "max_exact": max(rates) if rates else None,
            "delta_vs_reference_pp": delta,
            "verdict": verdict,
        }
    note = ""
    if enabled and floor is not None and floor <= 0.0:
        note = "两遍完全相同（N=0）：小样本下任何 delta 不为 0 都会被判 effect，先加样本再下结论"
    return {
        "noise_floor": enabled,
        "reference_arm": ref,
        "noise_floor_pp": floor,
        "noise_floor_note": note,
        "runs": passes,
        "headline": headline,
    }


def _exact_cell(payload: dict, condition: str) -> str:
    head = (payload.get("headline") or {}).get(condition) or {}
    low, high = head.get("min_exact"), head.get("max_exact")
    if low is None or high is None:
        return "n/a"
    mean = head.get("mean_exact") or 0.0
    if head.get("verdict") == "single-pass":
        return f"{mean * 100:5.1f}% (single-pass)"
    if abs(high - low) < 0.0005:
        return f"{mean * 100:5.1f}%"
    return f"{low * 100:4.1f}-{high * 100:4.1f}% (mean {mean * 100:4.1f}%)"


def _write_per_item_md(payload: dict, path) -> None:
    """逐题表：每股每遍 1 次，保留逐题 n；两遍给 min-max。"""
    reps = int(payload.get("reps") or 1)
    arms = list((payload.get("agg") or {}).keys())
    items_n = payload.get("items")
    tag = payload.get("items_tag")
    style = payload.get("query_style")
    floor = payload.get("noise_floor_pp")
    ref = payload.get("reference_arm")
    lines = [
        f"# 逐题（{items_n} 题 x {reps} 遍；items={tag}；query_style={style}）",
        "",
        f"噪声底 = {floor}pp（参考臂 {ref}）；每格 n = 该题该臂的遍数",
        "",
    ]
    for arm in arms:
        head = (payload.get("headline") or {}).get(arm) or {}
        low, high = head.get("min_exact"), head.get("max_exact")
        mean = head.get("mean_exact")
        delta = head.get("delta_vs_reference_pp")
        verdict = head.get("verdict")
        lines.append(f"## {arm}")
        lines.append(f"exact {low}-{high}（mean {mean}，delta {delta}pp，{verdict}）")
        lines.append("")
        lines.append("| item | kind | cat | n | exact | gold_in | chars | 各遍 exact |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for entry in payload.get("detail") or []:
            runs = entry.get("conditions", {}).get(arm) or []
            n = len(runs)
            if not n:
                continue
            exact = sum(1 for run in runs if run.get("exact"))
            gold = sum(1 for run in runs if run.get("gold_injected"))
            chars = sum((run.get("chars") or 0) for run in runs) / n
            per_pass = "/".join("1" if run.get("exact") else "0" for run in runs)
            item_id = entry.get("item_id")
            kind = entry.get("question_kind")
            cat = entry.get("category")
            lines.append(
                f"| {item_id} | {kind} | {cat} | {n} | {exact}/{n} | {gold}/{n} | {chars:.1f} | {per_pass} |"
            )
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _e2e(args) -> dict:
    """task-14：33 题 v2 + 干扰项效应 + question_kind 分组。"""
    items = load_items(args.limit, args.items)
    tag = items_tag(args.items)
    principal = SimpleNamespace(
        person_id="wxid_owner", chat_id="", chat_type="", platform="wechat", bot_account_id=""
    )
    cache: dict[str, list[dict]] = {}
    original = rc.load_rows

    def _rows(chat_id: str, limit: int = rc.ROW_LIMIT) -> list[dict]:
        if chat_id not in cache:
            cache[chat_id] = original(chat_id, limit=limit)
        return cache[chat_id]

    rc.load_rows = _rows  # type: ignore[assignment]
    reps = max(1, int(getattr(args, "reps", 1) or 1))
    noise_floor_on = bool(getattr(args, "noise_floor", True))
    reps_forced = 0
    if noise_floor_on and reps < 2:
        reps_forced = 2
        reps = 2
    conditions = [name for name in args.only.split(",") if name] or [
        "prod",
        "chain",
        "chain_struct",
    ]
    results: list[dict] = []
    try:
        for item in items:
            chat_id = item["chat_id"]
            rows = _rows(chat_id)
            principal.chat_id = chat_id
            principal.chat_type = "group" if chat_id.endswith("@chatroom") else "private"
            gold = row_known_gold(item, rows)
            entry = {
                "item_id": item["item_id"],
                "category": item.get("category"),
                "question_kind": item_kind(item),
                "kind": item.get("kind"),
                "gold_row": gold,
                "distractor_rows": [d["row_index"] for d in distractor_rows(item)],
                "conditions": {},
            }
            for condition in conditions:
                runs = []
                for pass_index in range(reps):
                    run = await _one(item, rows, condition, args, principal, gold)
                    run["pass"] = pass_index + 1
                    runs.append(run)
                entry["conditions"][condition] = runs
                print(
                    f"  {item['item_id'][-24:]:24s} {item_kind(item):8s} {condition:13s} "
                    + " | ".join(run["answer"][:40].replace("\n", " ") for run in runs)
                )
            results.append(entry)
    finally:
        rc.load_rows = original  # type: ignore[assignment]

    def agg_runs(runs: list[dict]) -> dict:
        known = [run for run in runs if run.get("gold_known")]
        gold_runs = [run for run in known if run.get("gold_injected")]
        in_block = [run for run in runs if run.get("distractor_in_block")]
        return {
            "n": len(runs),
            "exact_rate": round(sum(1 for run in runs if run["exact"]) / max(1, len(runs)), 4),
            "recall_mean": round(sum(run["token_recall"] for run in runs) / max(1, len(runs)), 4),
            "admission_rate": round(sum(1 for run in runs if run["admission"]) / max(1, len(runs)), 4),
            "halluc_mean": round(sum(len(run["halluc_tokens"]) for run in runs) / max(1, len(runs)), 3),
            "chars_mean": round(sum(run["chars"] for run in runs) / max(1, len(runs)), 1),
            "injected_rate": round(sum(1 for run in runs if run.get("injected")) / max(1, len(runs)), 4),
            "gold_known": len(known),
            "gold_injected_rate": (
                round(sum(1 for run in known if run.get("gold_injected")) / len(known), 4)
                if known
                else None
            ),
            "exact_when_gold_injected": (
                round(sum(1 for run in gold_runs if run["exact"]) / len(gold_runs), 4)
                if gold_runs
                else None
            ),
            "distractor_in_block_rate": round(
                sum(1 for run in runs if run.get("distractor_in_block")) / max(1, len(runs)), 4
            ),
            "answered_distractor_rate": round(
                sum(1 for run in runs if run.get("answered_distractor")) / max(1, len(runs)), 4
            ),
            "answered_distractor_when_in_block": (
                round(sum(1 for run in in_block if run.get("answered_distractor")) / len(in_block), 4)
                if in_block
                else None
            ),
            "notes_write_rate": (
                round(sum(1 for run in known if run.get("notes_write_side")) / len(known), 4)
                if known
                else None
            ),
            "notes_read_rate": (
                round(sum(1 for run in known if run.get("notes_read_side")) / len(known), 4)
                if known
                else None
            ),
            "notes_injected_mean": round(
                sum(run.get("notes_injected") or 0 for run in runs) / max(1, len(runs)), 2
            ),
            "span_broken_mean": round(
                sum((run.get("span_broken") or 0) for run in runs) / max(1, len(runs)), 2
            ),
            "exact_when_notes_read": (
                round(
                    sum(1 for run in known if run.get("notes_read_side") and run["exact"])
                    / sum(1 for run in known if run.get("notes_read_side")),
                    4,
                )
                if any(run.get("notes_read_side") for run in known)
                else None
            ),
        }

    agg: dict[str, dict] = {}
    by_kind: dict[str, dict] = {}
    by_category: dict[str, dict] = {}
    for condition in conditions:
        runs = [run for entry in results for run in entry["conditions"][condition]]
        agg[condition] = agg_runs(runs)
        for field, target in (("question_kind", by_kind), ("category", by_category)):
            for name in sorted({str(entry.get(field) or "?") for entry in results}):
                subset = [
                    run
                    for entry in results
                    if str(entry.get(field) or "?") == name
                    for run in entry["conditions"][condition]
                ]
                target.setdefault(name, {})[condition] = agg_runs(subset)
    noise = _noise_summary(results, conditions, reps, noise_floor_on)
    return {
        "ts": time.strftime("%Y%m%d_%H%M%S"),
        "items_path": str(items_path(args.items)),
        "items_tag": tag,
        "reps": reps,
        "reps_forced_by_noise_floor": reps_forced,
        "items": len(items),
        "limits": getattr(args, "limits_used", "6/600/120"),
        "query_style": str(getattr(args, "query_style", "question") or "question"),
        "struct_weight": struct_weight(),
        "chat_window": (
            "injection retired（2026-10-24：按需记忆注入块退役，量具只量检索侧）"
        ),
        "embed_arm": _embed_arm_used(),
        "chain_dir": str(os.environ.get("YINYUE_RECALL_CHAIN_DIR") or ""),
        "vector_warm_only": bool(int(os.environ.get("YINYUE_RECALL_CHAIN_VECTOR_WARM_ONLY", "0") or 0)),
        "agg": agg,
        **noise,
        "by_kind": by_kind,
        "by_category": by_category,
        "detail": results,
    }
def _fmt_pct(value) -> str:
    return "n/a" if value is None else f"{value * 100:5.1f}%"


def cmd_e2e(args) -> int:
    args.limits_used = apply_limits(getattr(args, "limits", ""))
    payload = asyncio.run(_e2e(args))
    tag = payload["items_tag"]
    print(
        f"\n=== 端到端（{payload['items']} 题 × {payload['reps']} 遍；items={tag}；"
        f"query_style={payload['query_style']}）==="
    )
    print(f"  （量具前提：注入块已退役，只量检索侧指标；limits={payload['limits']}）\n")
    for condition, row in payload["agg"].items():
        print(
            f"  {condition:13s} n={row['n']:3d} exact={_exact_cell(payload, condition)} "
            f"recall={row['recall_mean']:.3f} admission={_fmt_pct(row['admission_rate'])} "
            f"halluc={row['halluc_mean']:.2f} chars={row['chars_mean']:5.1f} "
            f"injected={_fmt_pct(row['injected_rate'])} gold_in={_fmt_pct(row['gold_injected_rate'])} "
            f"exact|gold={_fmt_pct(row['exact_when_gold_injected'])} "
            f"dis_in={_fmt_pct(row['distractor_in_block_rate'])} "
            f"ans_dis={_fmt_pct(row['answered_distractor_rate'])} "
            f"ans_dis|in={_fmt_pct(row['answered_distractor_when_in_block'])}"
        )
        if condition in ("notes", "notes_chain_struct"):
            print(
                f"    └ 写侧金行有笔记={_fmt_pct(row['notes_write_rate'])} "
                f"读侧金行进块={_fmt_pct(row['notes_read_rate'])} "
                f"notes_inj={row['notes_injected_mean']} "
                f"span_broken={row['span_broken_mean']} "
                f"exact|notes_read={_fmt_pct(row['exact_when_notes_read'])}"
            )
    print("\n=== 噪声底（同配置两遍） ===")
    if payload.get("reps_forced_by_noise_floor"):
        print("  （--noise-floor 默认开：参考臂要两遍，reps 自动提到 2）")
    floor = payload.get("noise_floor_pp")
    ref = payload.get("reference_arm")
    if floor is None:
        print("  n/a（单遍或 --no-noise-floor）")
    else:
        print(f"  reference={ref} noise_floor_pp={floor:.1f}pp")
        floor_note = payload.get("noise_floor_note")
        if floor_note:
            print("  ! " + floor_note)
        for condition, head in payload["headline"].items():
            low = (head.get("min_exact") or 0.0) * 100
            high = (head.get("max_exact") or 0.0) * 100
            mean = (head.get("mean_exact") or 0.0) * 100
            delta = head.get("delta_vs_reference_pp")
            verdict = head.get("verdict")
            print(
                f"  {condition:13s} exact={low:5.1f}-{high:5.1f}% mean={mean:5.1f}% "
                f"delta={delta}pp {verdict}"
            )
    print("\n=== 分组（question_kind）===")
    for name, rows in sorted(payload["by_kind"].items()):
        parts = [
            f"{cond}: n={data['n']} exact={_fmt_pct(data['exact_rate'])} "
            f"gold_in={_fmt_pct(data['gold_injected_rate'])}"
            for cond, data in rows.items()
        ]
        print(f"  {name:9s} " + " | ".join(parts))
    print("\n=== 分组（category）===")
    for name, rows in sorted(payload["by_category"].items()):
        parts = [
            f"{cond}: n={data['n']} exact={_fmt_pct(data['exact_rate'])} "
            f"gold_in={_fmt_pct(data['gold_injected_rate'])}"
            for cond, data in rows.items()
        ]
        print(f"  {name:14s} " + " | ".join(parts))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / (
        f"e2e_results_{tag}_{payload['query_style']}{_arm_suffix(args)}.json" if args.items else "e2e_results.json"
    )
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md = out.with_suffix(".md")
    _write_per_item_md(payload, md)
    print(f"\ne2e_results -> {out}")
    print(f"per_item_md -> {md}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="P3 recall-chain bench (read-only, offline)")
    sub = parser.add_subparsers(dest="command", required=True)
    search = sub.add_parser("search")
    search.add_argument("--items", default="", help="题库路径（默认 bench/items.json；v2 用 bench/items_v2.json）")
    search.add_argument("--limit", type=int, default=0)
    search.add_argument("--span", type=int, default=SPAN)
    search.add_argument("--no-vector", action="store_true")
    search.add_argument("--query-style", default="question", choices=QUERY_STYLES)
    search.add_argument("--struct-weight", type=float, default=0.0, help="0=不改（默认 4.0）")
    search.add_argument(
        "--anchor-mode", default="row", choices=("row", "message"),
        help="窗口锚：row=按行号（默认，逐字旧行为）；message=按 event_id 锚定（行号漂了也不换目标）",
    )
    search.add_argument(
        "--shift", type=int, default=0,
        help="模拟窗口滚动：评分前丢掉最老的 N 行（默认 0=不丢，逐字旧行为）",
    )
    search.add_argument(
        "--with-episode",
        action="store_true",
        help="task-28：多粒度召回（行级+±6 窗口+情节级 30 行滑窗），新臂 R6",
    )
    search.add_argument(
        "--tiebreak",
        action="store_true",
        help="task-28：同分窗口机械重排（只调同分次序，不改分数）",
    )
    search.add_argument(
        "--with-prim",
        action="store_true",
        help="task-33：类型无关原语（char 3-gram/jieba 两路 BM25 替掉 keyword+fts），新臂 R7",
    )
    search.add_argument(
        "--turn-weight", type=float, default=-1.0,
        help="task-33：段级向量通道权重（-1=用代码默认 1.0；扫权重不改代码）",
    )
    search.add_argument(
        "--with-turn",
        action="store_true",
        help="task-33：段级（一轮 2~6 条）向量召回，新臂 R8（隐含 --with-prim；缓存需先 --turn 预暖）",
    )
    search.add_argument("--with-media", action="store_true",
                        help="task-26: add the media route (seeds message_media rows in the isolated db)")
    search.set_defaults(func=cmd_search)
    e2e = sub.add_parser("e2e")
    e2e.add_argument("--items", default="", help="题库路径（默认 bench/items.json；v2 用 bench/items_v2.json）")
    e2e.add_argument("--limit", type=int, default=0)
    e2e.add_argument("--reps", type=int, default=2)
    e2e.add_argument(
        "--noise-floor", dest="noise_floor", action="store_true", default=True,
        help="参考臂自动跑两遍，报 noise_floor_pp 并对各臂 delta 判 effect|indistinguishable（默认开）",
    )
    e2e.add_argument(
        "--no-noise-floor", dest="noise_floor", action="store_false",
        help="关掉噪声底（只跑一遍时 headline 标 single-pass）",
    )
    e2e.add_argument("--limits", default="", help="items/chars/item_chars，默认 6/600/120")
    e2e.add_argument("--only", default="")
    e2e.add_argument("--sender", default="")
    e2e.add_argument("--query-style", default="question", choices=QUERY_STYLES)
    e2e.add_argument("--struct-weight", type=float, default=0.0, help="0=不改（默认 4.0）")
    e2e.add_argument(
        "--tiebreak",
        action="store_true",
        help="task-28：e2e 条件内开启同分窗口重排",
    )
    e2e.add_argument("--no-vector", action="store_true",
                     help="provider 发射臂也跳过向量（生产 .venv 无 sentence_transformers 的口径）")
    e2e.set_defaults(func=cmd_e2e)
    for _parser in (search, e2e):
        _parser.add_argument("--embed-arm", default="auto", choices=("auto", "st", "local"),
                             help="task-21: st=.venv_it ST upper bound; local=light local embedder"
                                  " (production .venv)")
        _parser.add_argument("--chain-dir", default="",
                             help="task-21: YINYUE_RECALL_CHAIN_DIR for caches / jieba.cache")
        _parser.add_argument("--vector-warm-only", type=int, default=1, choices=(0, 1),
                             help="task-21: 1 = cached vectors only (production); 0 = compute and cache")
    args = parser.parse_args()
    _apply_embed_env(args)
    if not bool(getattr(args, "no_vector", False)):
        try:
            rc.embedder()
            print("embed_arm", _embed_arm_used(), "ready", rc.embedder_ready())
        except Exception as exc:  # noqa: BLE001
            print("embed_preload_failed", type(exc).__name__, exc)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ["YINYUE_FEATURE_RECALL_CHAIN"] = "off"
    if float(getattr(args, "struct_weight", 0.0) or 0.0) > 0:
        rc.ROUTE_WEIGHTS["struct"] = float(args.struct_weight)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
