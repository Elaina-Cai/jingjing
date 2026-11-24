r"""task-28 read-only baseline: mechanical rerank of fused top-20 on v2.

Archived evidence for the decision that reranking is NOT the lever (see
`docs/decisions/穷举整改_多粒度与rerank.md`, section baseline).

Read-only: loads rows through `recall_chain.load_rows` (read-only engine), never writes.
Usage: `.venv_it\Scripts\python.exe -X utf8 scripts\probe_rerank_baseline.py`
"""
from __future__ import annotations

import json
import math
import os
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("YINYUE_FEATURE_RECALL_STRUCT", "on")
os.environ.setdefault("YINYUE_RECALL_CHAIN_EMBED_ARM", "local")
os.environ.setdefault("YINYUE_RECALL_CHAIN_VECTOR_WARM_ONLY", "1")
os.environ.setdefault("YINYUE_RECALL_CHAIN_DIR", str(ROOT / "data" / "recall_chain"))
os.environ.setdefault("YINYUE_LOCAL_EMBED_THREADS", "4")

from core.memory import recall_chain as rc  # noqa: E402

ITEMS = ROOT / "bench" / "items_v2.json"
TOP_N = 20
K = 50


def load_items() -> list[dict]:
    data = json.loads(ITEMS.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else list(data.get("items") or [])


def gold_index(item: dict, rows: list[dict]) -> int:
    src = item.get("source") or {}
    raw_index = int(src.get("row_index", -1))
    content = str(src.get("content") or "")
    probe = content[:40].strip()
    if not probe:
        return raw_index if raw_index >= 0 else -1
    for index, row in enumerate(rows):
        if probe in str(row.get("content") or ""):
            return index
    return -1


def char_grams(text: str, sizes=(2, 3)) -> set[str]:
    clean = "".join(ch for ch in text if not ch.isspace())
    out: set[str] = set()
    for size in sizes:
        for i in range(len(clean) - size + 1):
            out.add(clean[i : i + size])
    return out


def token_set(text: str) -> set[str]:
    return set(rc._norm(text).split()) | set(char_grams(text, (2,)))


def idf_map(texts: list[str], tokens: set[str]) -> dict[str, float]:
    total = max(1, len(texts))
    out: dict[str, float] = {}
    for token in tokens:
        df = sum(1 for body in texts if token in body)
        out[token] = math.log(1.0 + total / (1.0 + df))
    return out


def rerank(
    rows: list[dict],
    texts: list[str],
    query: str,
    candidates: list[int],
    route_hits: dict[str, set[int]],
    weights: dict[str, float],
) -> list[tuple[int, float]]:
    q_grams = char_grams(query, (2, 3))
    q_tokens = token_set(query)
    idf = idf_map([texts[i] for i in candidates], q_tokens)
    max_idf = max(idf.values()) if idf else 1.0
    scored: list[tuple[int, float]] = []
    cand_set = set(candidates)
    for index in candidates:
        body = texts[index]
        row_grams = char_grams(body, (2, 3))
        ngram = (len(q_grams & row_grams) / len(q_grams)) if q_grams else 0.0
        if q_tokens:
            hit_tokens = [tok for tok in q_tokens if tok in body]
            term = len(hit_tokens) / len(q_tokens)
            df_len = (sum(idf.get(tok, 0.0) for tok in hit_tokens) / (len(hit_tokens) + 1e-9)) / max_idf
        else:
            term = 0.0
            df_len = 0.0
        votes = sum(1 for hits in route_hits.values() if index in hits) / max(1, len(route_hits))
        neighbors = sum(1 for other in cand_set if other != index and abs(other - index) <= 6)
        proximity = min(1.0, neighbors / 5.0)
        length_norm = 1.0 / (1.0 + len(body) / 500.0)
        score = (
            weights["ngram"] * ngram
            + weights["term"] * term
            + weights["votes"] * votes
            + weights["proximity"] * proximity
            + weights["df_len"] * (0.5 * df_len + 0.5 * length_norm)
        )
        scored.append((index, score))
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored


def window_feature_scores(texts: list[str], hits: list, question: str) -> dict[int, float]:
    grams = char_grams(question, sizes=(2,))
    terms = [term for term in re.split(r"[\s,，。；;：:、/]+", question) if len(term) >= 2]
    scores: dict[int, float] = {}
    for hit in hits:
        text = " ".join(texts[index] for index in range(hit.start, hit.end))
        text_grams = set()
        for index in range(max(0, len(text) - 1)):
            text_grams.add(text[index:index + 2])
        gram_cov = len(grams & text_grams) / max(1, len(grams))
        term_cov = sum(text.count(term) for term in terms) / max(1, len(terms))
        votes = len(hit.routes)
        density = len(hit.members) / max(1, hit.end - hit.start)
        scores[hit.row_index] = (
            2.0 * gram_cov
            + 1.0 * min(term_cov, 1.0)
            + 1.0 * votes
            + 0.5 * density
            - 0.1 * (hit.end - hit.start)
        )
    return scores


def rank_of(order: list[int], gold: int) -> int:
    for pos, index in enumerate(order, 1):
        if index == gold:
            return pos
    return 0


def metrics(ranks: list[int]) -> dict:
    n = len(ranks)
    if not n:
        return {"n": 0}
    hit1 = sum(1 for r in ranks if r == 1) / n
    hit3 = sum(1 for r in ranks if 1 <= r <= 3) / n
    mrr = sum((1.0 / r) if r else 0.0 for r in ranks) / n
    return {
        "n": n,
        "hit@1": round(hit1, 4),
        "hit@3": round(hit3, 4),
        "mrr": round(mrr, 4),
        "ranked_out": sum(1 for r in ranks if r == 0),
        "beyond3": sum(1 for r in ranks if r == 0 or r > 3),
    }

def main() -> int:
    # warm the embedder once: WARM_ONLY=1 then passes embedder_ready() and the
    # vector route uses the on-disk per-row cache without recomputing corpora.
    model = rc.embedder()
    print("[warm] embedder ready=" + str(bool(model)) + " arm=" + str(rc.embed_arm()))
    items = load_items()
    by_chat: dict[str, list[dict]] = {}
    for item in items:
        by_chat.setdefault(str(item.get("chat_id") or ""), []).append(item)

    weights = {"ngram": 0.30, "term": 0.25, "votes": 0.20, "proximity": 0.15, "df_len": 0.10}
    alt = {"ngram": 0.55, "term": 0.45, "votes": 0.0, "proximity": 0.0, "df_len": 0.0}

    current_ranks: list[int] = []
    top20_ranks: list[int] = []
    alt_ranks: list[int] = []
    gold_in_top20 = 0
    recall_short = 0
    rerank_fail = 0
    latencies: list[float] = []
    window_cur_ranks: list[int] = []
    window_rr_ranks: list[int] = []
    wf_ranks: list[int] = []
    wf_blend_ranks: list[int] = []
    blend_ranks: dict[str, list[int]] = {}
    window_blend_ranks: dict[str, list[int]] = {}
    rerank_ms: list[float] = []
    vector_cov: dict[str, dict] = {}
    detail: list[dict] = []
    window_gold = 0
    valid = 0

    for chat_id, chat_items in by_chat.items():
        rows = rc.load_rows(chat_id)
        if not rows:
            print("[warn] empty rows chat=" + chat_id)
            continue
        texts = [rc._norm(row.get("content") or "") for row in rows]
        vector_stats: dict = {}
        for item in chat_items:
            gold = gold_index(item, rows)
            question = str(item.get("question") or "")
            started = time.perf_counter()
            route_ranks: dict[str, list[int]] = {}
            route_ranks["keyword"] = rc.keyword_ranks(rows, question)
            try:
                route_ranks["fts"] = rc.fts_ranks(rows, question, texts=texts, cache_key=chat_id)
            except Exception as exc:  # noqa: BLE001
                print("[warn] fts failed " + str(exc))
            try:
                route_ranks["vector"] = rc.vector_ranks(
                    rows, question, cache_key=chat_id, stats=vector_stats
                )
            except Exception as exc:  # noqa: BLE001
                print("[warn] vector failed " + str(exc))
            try:
                route_ranks["struct"] = rc.struct_ranks(rows, question, texts=texts)
            except Exception as exc:  # noqa: BLE001
                print("[warn] struct failed " + str(exc))
            fused = rc.fuse(route_ranks)
            fused_order = [index for index, _ in sorted(fused.items(), key=lambda kv: -kv[1][0])]
            top20 = fused_order[:TOP_N]
            route_hits = {name: set(order[:K]) for name, order in route_ranks.items()}
            rr_started = time.perf_counter()
            reranked_pairs = rerank(rows, texts, question, top20, route_hits, weights)
            alt_pairs = rerank(rows, texts, question, top20, route_hits, alt)
            rr_elapsed = (time.perf_counter() - rr_started) * 1000.0
            rerank_ms.append(rr_elapsed)
            reranked_order = [index for index, _ in reranked_pairs]
            fused_scores = {index: fused.get(index, (0.0, ()))[0] for index in top20}
            max_fused = max(fused_scores.values()) if fused_scores else 1.0
            max_rr = max((score for _, score in reranked_pairs), default=1.0) or 1.0
            blends = {
                "blend50": {index: 0.5 * (fused_scores.get(index, 0) / max_fused) + 0.5 * (score / max_rr) for index, score in reranked_pairs},
                "blend70": {index: 0.7 * (fused_scores.get(index, 0) / max_fused) + 0.3 * (score / max_rr) for index, score in reranked_pairs},
                "blend30": {index: 0.3 * (fused_scores.get(index, 0) / max_fused) + 0.7 * (score / max_rr) for index, score in reranked_pairs},
            }
            blend_orders = {
                name: [index for index, _ in sorted(scores.items(), key=lambda kv: -kv[1])]
                for name, scores in blends.items()
            }
            alt_order = [index for index, _ in alt_pairs]
            latencies.append((time.perf_counter() - started) * 1000.0)

            current_position = rank_of(fused_order, gold) if gold >= 0 else 0
            top20_position = rank_of(reranked_order, gold) if gold >= 0 else 0
            alt_position = rank_of(alt_order, gold) if gold >= 0 else 0

            if gold >= 0:
                hits_cur = rc.chain_rank(rows, question, cache_key=chat_id)
                for pos, hit in enumerate(hits_cur, 1):
                    if hit.start <= gold < hit.end:
                        window_gold += 1
                        window_cur_ranks.append(pos)
                        break
                else:
                    window_cur_ranks.append(0)
                for name, order in blend_orders.items():
                    position = rank_of(order, gold)
                    blend_ranks.setdefault(name, []).append(position)
                for name, scores in blends.items():
                    ordered_hits = sorted(hits_cur, key=lambda hit: -scores.get(hit.row_index, -1.0))
                    for pos, hit in enumerate(ordered_hits, 1):
                        if hit.start <= gold < hit.end:
                            window_blend_ranks.setdefault(name, []).append(pos)
                            break
                    else:
                        window_blend_ranks.setdefault(name, []).append(0)
                wf_scores = window_feature_scores(texts, hits_cur, question)
                wf_order = sorted(hits_cur, key=lambda hit: -wf_scores.get(hit.row_index, -1.0))
                for pos, hit in enumerate(wf_order, 1):
                    if hit.start <= gold < hit.end:
                        wf_ranks.append(pos)
                        break
                else:
                    wf_ranks.append(0)
                wf_blend = {hit.row_index: wf_scores.get(hit.row_index, 0.0) + 0.5 * hit.score for hit in hits_cur}
                wf_blend_order = sorted(hits_cur, key=lambda hit: -wf_blend.get(hit.row_index, -1.0))
                for pos, hit in enumerate(wf_blend_order, 1):
                    if hit.start <= gold < hit.end:
                        wf_blend_ranks.append(pos)
                        break
                else:
                    wf_blend_ranks.append(0)
                fused_rr = {row: (score, ("rerank",)) for row, score in reranked_pairs}
                hits_rr = rc.build_windows(rows, fused_rr, span=6, topk=3)
                for pos, hit in enumerate(hits_rr, 1):
                    if hit.start <= gold < hit.end:
                        window_rr_ranks.append(pos)
                        break
                else:
                    window_rr_ranks.append(0)

            if gold >= 0:
                valid += 1
                current_ranks.append(current_position)
                top20_ranks.append(top20_position)
                alt_ranks.append(alt_position)
                if gold in top20:
                    gold_in_top20 += 1
                else:
                    recall_short += 1
                if gold in top20 and not (1 <= top20_position <= 3):
                    rerank_fail += 1
            detail.append(
                {
                    "item_id": item.get("item_id"),
                    "question_kind": item.get("question_kind"),
                    "gold": gold,
                    "current_rank": current_position,
                    "top20_rank": top20_position,
                    "alt_rank": alt_position,
                    "gold_in_top20": (gold in top20) if gold >= 0 else None,
                }
            )
        vector_cov[chat_id] = {
            "known": vector_stats.get("vector_rows_known"),
            "missing": vector_stats.get("vector_rows_missing"),
            "mode": vector_stats.get("vector_cache_mode"),
        }

    lat_sorted = sorted(latencies)
    p50 = statistics.median(lat_sorted) if lat_sorted else 0.0
    p90 = lat_sorted[min(len(lat_sorted) - 1, int(len(lat_sorted) * 0.9))] if lat_sorted else 0.0

    print("== (i) current fused order (row-level, full list) ==")
    print(metrics(current_ranks))
    print("== (i) current score -> top-20 -> mechanical rerank (weights v1) ==")
    print(metrics(top20_ranks))
    print("== (i) same, weights v2 (ngram+term only) ==")
    print(metrics(alt_ranks))
    print("gold_in_top20 = %d/%d (%.1f%%)" % (gold_in_top20, valid, 100.0 * gold_in_top20 / max(1, valid)))
    print("recall_short (gold not in top-20, rerank cannot help) = %d" % recall_short)
    print("rerank_fail (gold in top-20 but not top-3) = %d" % rerank_fail)
    print("production window gold_in = %d/%d (%.1f%%)" % (window_gold, valid, 100.0 * window_gold / max(1, valid)))
    print("== window-level: current chain (struct on) ==")
    print(metrics(window_cur_ranks))
    for name in ("blend50", "blend70", "blend30"):
        print("== row-level blend " + name + " (fused + features) ==")
        print(metrics(blend_ranks.get(name, [])))
        print("== window reorder by blend " + name + " ==")
        print(metrics(window_blend_ranks.get(name, [])))
    rr_sorted = sorted(rerank_ms)
    print("rerank-only latency p50=%.2fms p90=%.2fms" % (statistics.median(rr_sorted) if rr_sorted else 0.0, rr_sorted[min(len(rr_sorted) - 1, int(len(rr_sorted) * 0.9))] if rr_sorted else 0.0))
    print("== window-level: rerank windows by window features (pure) ==")
    print(metrics(wf_ranks))
    print("== window-level: window features + 0.5*current window score ==")
    print(metrics(wf_blend_ranks))
    print("== window-level: rerank top-20 -> rebuild windows (span 6, topk 3) ==")
    print(metrics(window_rr_ranks))
    print("rerank latency p50=%.2fms p90=%.2fms" % (p50, p90))
    print("vector coverage " + json.dumps(vector_cov, ensure_ascii=True))
    print("details:")
    for row in detail:
        print(
            "  %-28s %-9s gold=%-5s cur=%-4s top20=%-4s alt=%-4s in20=%s"
            % (
                str(row["item_id"])[:28],
                str(row["question_kind"]),
                row["gold"],
                row["current_rank"],
                row["top20_rank"],
                row["alt_rank"],
                row["gold_in_top20"],
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
