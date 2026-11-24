"""P3 gauge: multi-route retrieval baseline (read-only).

Same eval set as the replay gauge (bench/items.json, 13 real questions, 2 chats).
Each item is retrieved with **only the question text** over all message rows of
that chat; the gold row is item["source"]["row_index"] on the very row list that
scripts/bench_replay.py::_read_rows returns.

Routes:
  R1    keyword substring counts (br._keywords + br._norm) = current production
        baseline; "R1win" additionally reports the production +-6 row window hit.
  R2    FTS5 tokenize='trigram' (temp DB under .test_tmp/bench_retrieval/)
  R3    FTS5 unicode61 over jieba-segmented rows
  R4    local BAAI/bge-large-zh-v1.5 (offline, cosine, brute force)
  R5    hybrid: R2/R3/R4 top-10 union + mechanical rerank

Read-only: the production DB is only ever opened through bench_replay's
read_only_engine path; every index this script builds lives in
.test_tmp/bench_retrieval/. Offline: HF_HUB_OFFLINE=1. No LLM calls.

Usage:
  .venv_it\\Scripts\\python.exe -X utf8 scripts\\bench_retrieval.py
  ... --only R1,R2,R3 --limit 400
  ... --only R4 --chats 10000000003@chatroom
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

_OUT = ROOT / ".test_tmp" / "bench_retrieval"
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("YINYUE_FEATURE_MEMORY_VECTOR_RECALL", "0")
os.environ.setdefault("LOG_DIR", str(_OUT / "logs"))

import bench_replay as br  # noqa: E402  (read-only helpers, reused on purpose)

MODEL_NAME = "BAAI/bge-large-zh-v1.5"
ROUTES = ("R1", "R2", "R3", "R4", "R4i", "R5", "R5i")
WINDOW_SPAN = 6        # production injects a +-6 row window around the best row
BGE_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："  # official bge-zh retrieval query prefix
EMBED_CHARS = 200      # bge max is 512 tokens; keep the head of each row
TRIGRAM_MAX = 24       # cap the OR-ed 3-grams per query
CAND_TOPK = 10         # per-route candidates for the hybrid
RANK_TOPK = 20
WEIGHTS = {"R2": 0.30, "R3": 0.35, "R4": 0.45}
DF_MAX_RATIO = 0.05  # DF filter: drop query tokens that occur in >5% of rows

_MODEL = None


def _log(msg: str) -> None:
    print(msg, flush=True)


def _slug(text: str) -> str:
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:10]


def load_items(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit("items.json is not a list")
    return data


def read_corpora(chats: Sequence[str], limit: int) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for chat in chats:
        t0 = time.time()
        rows = br._read_rows(chat, limit=limit)
        out[chat] = rows
        _log(f"[corpus] {chat} rows={len(rows)} read_s={time.time() - t0:.1f}")
    return out


def gold_index(item: dict[str, Any], rows: list[dict[str, Any]]) -> int | None:
    """Gold row index.

    Fast path: rows[item["source"]["row_index"]] must be this item event_id.
    Synthetic ":win:" items store a window offset in source.row_index, so fall
    back to a scan by event_id, then to the first row matching source.content.
    """
    eid = str(item["item_id"]).rsplit(":", 1)[-1]
    gi = int(item["source"]["row_index"])
    if 0 <= gi < len(rows) and str(rows[gi].get("event_id") or "") == eid:
        return gi
    for i, row in enumerate(rows):
        if str(row.get("event_id") or "") == eid:
            _log(f"[fix] gold by event_id {item['item_id']} row_index={gi} -> {i}")
            return i
    raw = str(item["source"].get("content") or "")
    if raw:
        for i, row in enumerate(rows):
            if str(row.get("content") or "") == raw:
                _log(f"[fix] gold by content {item['item_id']} row_index={gi} -> {i}")
                return i
    if 0 <= gi < len(rows):
        _log(f"[warn] gold unresolved {item['item_id']}: keeping row_index {gi}")
        return gi
    return None


def norm_row(text: str) -> str:
    return br._norm(str(text or ""))

def r1_scores(question: str, rows: list[dict[str, Any]]) -> list[tuple[int, float]]:
    kws = br._keywords(question)
    scored: list[tuple[int, float]] = []
    for i, row in enumerate(rows):
        text = norm_row(row.get("content"))
        score = float(sum(text.count(k) for k in kws))
        if score > 0:
            scored.append((i, score))
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored


def r1_window(question: str, rows: list[dict[str, Any]], span: int = 6) -> int:
    _block, centre = br._retrieve(rows, question, span=span)
    return int(centre)


def _fts_build(path: Path, texts: Sequence[str], tokenize: str) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.execute(f"create virtual table fts using fts5(content, tokenize={tokenize})")
    conn.executemany(
        "insert into fts(rowid, content) values (?, ?)",
        [(i + 1, text) for i, text in enumerate(texts)],
    )
    conn.commit()
    return conn


def _fts_rank(conn: sqlite3.Connection, match_query: str, topk: int) -> list[tuple[int, float]]:
    if not match_query:
        return []
    sql = "select rowid, bm25(fts) as score from fts where fts match ? order by rank limit ?"
    try:
        cur = conn.execute(sql, (match_query, topk))
    except sqlite3.OperationalError as exc:
        _log(f"[fts] query failed ({type(exc).__name__}: {exc})")
        return []
    return [(int(row[0]) - 1, float(row[1])) for row in cur.fetchall()]


def _document_frequency(token: str, docs: Sequence[Any]) -> int:
    return sum(1 for doc in docs if token in doc)


def rare_by_df(tokens: Sequence[str], docs: Sequence[Any]) -> list[str]:
    """Keep query tokens whose document frequency is at most DF_MAX_RATIO.

    Statistical replacement for a hand-written stopword list: generic question
    words ("whichever", "remember", "you said") are frequent in the corpus and
    would otherwise drown the BM25 ranking. Falls back to the input when every
    token is frequent.
    """
    if not docs:
        return list(tokens)
    limit = max(int(len(docs) * DF_MAX_RATIO), 1)
    keep = [token for token in tokens if _document_frequency(token, docs) <= limit]
    return keep or list(tokens)


def trigram_tokens(question: str) -> list[str]:
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", norm_row(question))
    grams: list[str] = []
    for i in range(max(len(text) - 2, 0)):
        gram = text[i : i + 3]
        if gram not in grams:
            grams.append(gram)
        if len(grams) >= TRIGRAM_MAX:
            break
    return grams


def trigram_query(question: str, docs: Sequence[Any] | None = None) -> str:
    grams = trigram_tokens(question)
    if docs is not None:
        grams = rare_by_df(grams, docs)
    return " OR ".join('"%s"' % gram for gram in grams)


def jieba_tokens(text: str) -> list[str]:
    import jieba

    # 临时落点唯一入口：仓库内 .test_tmp/（系统 TEMP 不可写时会静默回落到仓库根）。
    _jieba_cache = _OUT / "jieba.cache"
    _jieba_cache.parent.mkdir(parents=True, exist_ok=True)
    jieba.dt.cache_file = str(_jieba_cache)
    out: list[str] = []
    for word in jieba.lcut(str(text or "")):
        word = word.strip()
        if len(word) >= 2 and not re.fullmatch(r"[^0-9A-Za-z\u4e00-\u9fff]+", word):
            out.append(word)
    return out


def jieba_query(question: str, docs: Sequence[Any] | None = None) -> str:
    tokens = jieba_tokens(question)[:12]
    if docs is not None:
        tokens = rare_by_df(tokens, docs)
    return " OR ".join('"%s"' % token for token in tokens)


def _model():
    global _MODEL
    if _MODEL is None:
        import torch
        from sentence_transformers import SentenceTransformer

        want = os.environ.get("BENCH_EMBED_DEVICE") or ("cuda" if torch.cuda.is_available() else "cpu")
        t0 = time.time()
        _MODEL = SentenceTransformer(MODEL_NAME, device=want)
        _log(f"[vector] {MODEL_NAME} on {want} in {time.time() - t0:.1f}s")
    return _MODEL


def _cache_read(path: Path) -> tuple[list[str], Any]:
    import numpy as np

    if not path.exists():
        return [], np.zeros((0, 0), dtype="float32")
    data = np.load(str(path), allow_pickle=False)
    return [str(key) for key in data["keys"].tolist()], data["vecs"]


def _cache_write(path: Path, keys: list[str], vecs: Any) -> None:
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(str(path), keys=np.array(keys, dtype="U40"), vecs=np.asarray(vecs, dtype="float32"))


def vector_index(rows: list[dict[str, Any]]) -> Any:
    import numpy as np

    texts = [str(row.get("content") or "")[:EMBED_CHARS] for row in rows]
    keys = [hashlib.sha1(text.encode("utf-8")).hexdigest() for text in texts]
    cache = _OUT / "vector_cache.npz"
    old_keys, old_vecs = _cache_read(cache)
    store: dict[str, Any] = {key: old_vecs[i] for i, key in enumerate(old_keys)}
    missing = [i for i, key in enumerate(keys) if key not in store]
    if missing:
        t0 = time.time()
        fresh = _model().encode(
            [texts[i] for i in missing],
            batch_size=32,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        for slot, i in enumerate(missing):
            store[keys[i]] = np.asarray(fresh[slot], dtype="float32")
        _log(f"[vector] embedded {len(missing)} new rows in {time.time() - t0:.1f}s")
        _cache_write(cache, list(store.keys()), np.stack([store[key] for key in store]))
    return np.stack([store[key] for key in keys])


def vector_rank(
    question: str, index: Any, *, instruction: bool = False
) -> list[tuple[int, float]]:
    import numpy as np

    text = BGE_QUERY_PREFIX + question if instruction else question
    query = _model().encode([text], normalize_embeddings=True, show_progress_bar=False)[0]
    sims = index @ np.asarray(query, dtype="float32")
    order = np.argsort(-sims)[:RANK_TOPK]
    return [(int(i), float(sims[int(i)])) for i in order]


def build_indexes(rows: list[dict[str, Any]], chat: str, need: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "trigram" in need or "jieba" in need:
        out["norm_docs"] = [norm_row(row.get("content")) for row in rows]
    if "trigram" in need:
        out["trigram"] = _fts_build(
            _OUT / f"fts_trigram_{_slug(chat)}.db", out["norm_docs"], "'trigram'"
        )
    if "jieba" in need:
        token_rows = [jieba_tokens(row.get("content")) for row in rows]
        out["token_docs"] = [frozenset(tokens) for tokens in token_rows]
        out["jieba"] = _fts_build(
            _OUT / f"fts_jieba_{_slug(chat)}.db",
            [" ".join(tokens) for tokens in token_rows],
            "'unicode61'",
        )
    if "vector" in need:
        out["vector"] = vector_index(rows)
    return out


def hybrid_rank(
    question: str, rows: list[dict[str, Any]], idx: dict[str, Any], *, instruction: bool = False
) -> list[tuple[int, float]]:
    candidates: dict[int, float] = {}
    ranked_by_channel = {
        "R2": _fts_rank(idx["trigram"], trigram_query(question, idx.get("norm_docs")), CAND_TOPK),
        "R3": _fts_rank(idx["jieba"], jieba_query(question, idx.get("token_docs")), CAND_TOPK),
        "R4": vector_rank(question, idx["vector"], instruction=instruction)[:CAND_TOPK],
    }
    for name, ranked in ranked_by_channel.items():
        for pos, (i, _score) in enumerate(ranked):
            candidates[i] = candidates.get(i, 0.0) + WEIGHTS[name] * (1.0 / (pos + 1))
    span = max(len(rows) - 1, 1)
    out: list[tuple[int, float]] = []
    for i, score in candidates.items():
        recency = 0.15 * (i / span)
        length_pen = 0.10 * min(len(str(rows[i].get("content") or "")) / 200.0, 1.0)
        out.append((i, score + recency - length_pen))
    out.sort(key=lambda pair: (-pair[1], pair[0]))
    return out


def _rank_of(gold: int, ranked: Sequence[tuple[int, float]]) -> int:
    for pos, (i, _score) in enumerate(ranked, 1):
        if i == gold:
            return pos
    return 0


def route_rank(route: str, question: str, rows: list[dict[str, Any]], idx: dict[str, Any]):
    if route == "R1":
        return r1_scores(question, rows)
    if route == "R2":
        return _fts_rank(idx["trigram"], trigram_query(question, idx.get("norm_docs")), RANK_TOPK)
    if route == "R3":
        return _fts_rank(idx["jieba"], jieba_query(question, idx.get("token_docs")), RANK_TOPK)
    if route == "R4":
        return vector_rank(question, idx["vector"])
    if route == "R4i":
        return vector_rank(question, idx["vector"], instruction=True)
    if route == "R5":
        return hybrid_rank(question, rows, idx)
    if route == "R5i":
        return hybrid_rank(question, rows, idx, instruction=True)
    return []

def main() -> int:
    parser = argparse.ArgumentParser(description="multi-route retrieval baseline (read-only)")
    parser.add_argument("--items", default=str(ROOT / "bench" / "items.json"))
    parser.add_argument("--only", default=",".join(ROUTES))
    parser.add_argument("--chats", default="")
    parser.add_argument("--limit", type=int, default=6000)
    parser.add_argument("--out", default=str(_OUT / "results.json"))
    args = parser.parse_args()

    canon = {name.upper(): name for name in ROUTES}
    routes = [
        canon.get(chunk.strip().upper(), chunk.strip().upper())
        for chunk in args.only.split(",")
        if chunk.strip()
    ]
    items = load_items(Path(args.items))
    if args.chats:
        wanted = {chat.strip() for chat in args.chats.split(",") if chat.strip()}
        items = [item for item in items if str(item["chat_id"]) in wanted]
    chats = sorted({str(item["chat_id"]) for item in items})
    corpora = read_corpora(chats, args.limit)

    need: set[str] = set()
    if any(route in routes for route in ("R2", "R5", "R5i")):
        need.add("trigram")
    if any(route in routes for route in ("R3", "R5", "R5i")):
        need.add("jieba")
    if any(route in routes for route in ("R4", "R4i", "R5", "R5i")):
        need.add("vector")

    indexes: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    for chat in chats:
        try:
            indexes[chat] = build_indexes(corpora[chat], chat, need)
        except Exception as exc:  # noqa: BLE001
            indexes[chat] = {}
            for route in routes:
                errors.setdefault(route, f"{type(exc).__name__}: {exc}")
            _log(f"[index] {chat} failed: {type(exc).__name__}: {exc}")

    stats = {route: {"n": 0, "hit1": 0, "hit3": 0, "rr": 0.0, "win1": 0, "win3": 0} for route in routes}
    detail: list[dict[str, Any]] = []
    for item in items:
        chat = str(item["chat_id"])
        rows = corpora[chat]
        idx = indexes.get(chat, {})
        gold = gold_index(item, rows)
        line: dict[str, Any] = {"item_id": item["item_id"], "chat": chat, "gold": gold, "ranks": {}}
        if gold is None:
            line["skipped"] = "gold row outside corpus (use a larger --limit)"
            detail.append(line)
            continue
        for route in routes:
            if route in errors:
                continue
            if route == "R2" and "trigram" not in idx:
                continue
            if route == "R3" and "jieba" not in idx:
                continue
            if route in ("R4", "R4i", "R5", "R5i") and "vector" not in idx:
                continue
            if route in ("R5", "R5i") and not all(
                key in idx for key in ("trigram", "jieba", "vector")
            ):
                continue
            ranked = route_rank(route, str(item["question"]), rows, idx)
            pos = _rank_of(gold, ranked)
            near1 = bool(ranked) and abs(ranked[0][0] - gold) <= WINDOW_SPAN
            near3 = any(abs(entry[0] - gold) <= WINDOW_SPAN for entry in ranked[:3])
            line["ranks"][route] = pos
            line.setdefault("windows", {})[route] = [near1, near3]
            stats[route]["n"] += 1
            if pos == 1:
                stats[route]["hit1"] += 1
            if 1 <= pos <= 3:
                stats[route]["hit3"] += 1
            if pos:
                stats[route]["rr"] += 1.0 / pos
            if near1:
                stats[route]["win1"] += 1
            if near3:
                stats[route]["win3"] += 1
        centre = r1_window(str(item["question"]), rows)
        line["r1_window_centre"] = centre
        line["r1_window_hit"] = abs(gold - centre) <= 6
        detail.append(line)

    _log("")
    _log("%-6s %4s %8s %8s %8s %8s %8s  %s" % ("route", "n", "hit@1", "hit@3", "MRR", "win@1", "win@3", "note"))
    for route in routes:
        row = stats[route]
        note = errors.get(route, "")
        n = int(row["n"])
        if n:
            _log(
                "%-6s %4d %8.3f %8.3f %8.3f %8.3f %8.3f  %s"
                % (
                    route,
                    n,
                    row["hit1"] / n,
                    row["hit3"] / n,
                    row["rr"] / n,
                    row["win1"] / n,
                    row["win3"] / n,
                    note,
                )
            )
        else:
            _log("%-6s %4d %8s %8s %8s %8s %8s  %s" % (route, 0, "-", "-", "-", "-", "-", note))

    window_rows = [line for line in detail if "r1_window_hit" in line]
    window_hits = [line for line in window_rows if line["r1_window_hit"]]
    if window_rows:
        _log(
            "%-6s %4d %8.3f  (production +-6 row window around the R1 argument)"
            % ("R1win", len(window_rows), len(window_hits) / len(window_rows))
        )

    _log("")
    _log("per-item detail (rank 0 = miss)")
    _log("%-48s %5s %s R1win" % ("item_id", "gold", " ".join("%4s" % route for route in routes)))
    for line in detail:
        ranks = " ".join("%4d" % line["ranks"].get(route, 0) for route in routes)
        win = "yes" if line.get("r1_window_hit") else "no"
        gold = line["gold"] if line["gold"] is not None else -1
        _log("%-48s %5d %s %s" % (line["item_id"], gold, ranks, win))

    payload = {
        "items": len(items),
        "limit": args.limit,
        "chats": chats,
        "routes": routes,
        "stats": {
            route: {
                "n": stats[route]["n"],
                "hit@1": (stats[route]["hit1"] / stats[route]["n"]) if stats[route]["n"] else None,
                "hit@3": (stats[route]["hit3"] / stats[route]["n"]) if stats[route]["n"] else None,
                "mrr": (stats[route]["rr"] / stats[route]["n"]) if stats[route]["n"] else None,
                "win@1": (stats[route]["win1"] / stats[route]["n"]) if stats[route]["n"] else None,
                "win@3": (stats[route]["win3"] / stats[route]["n"]) if stats[route]["n"] else None,
                "error": errors.get(route, ""),
            }
            for route in routes
        },
        "r1_window": {"n": len(window_rows), "hits": len(window_hits)},
        "detail": detail,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"[out] {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
