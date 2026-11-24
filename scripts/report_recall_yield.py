#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""召回空转归因（task-37）——只读、可复算。

回答四件事：
  1) ``needs_memory_recall`` 里 ``kinds=[]`` 是从哪来的（N1 没给 vs 给了被丢）；
  2) ``hits == 0`` 的回合归到 a/b/c/d 四类（另加 e=被 feature flag 静默关掉）；
  3) 按 kind 的零命中率表 —— 哪些 kind 是「接了但永远查不到」；
  4) 整改候选按「预计减少多少次空转」排序（只给读数，不改代码）。

口径（红线）：生产库只读 ``sqlite3 mode=ro``；不写库、不改 ``.env``、不打 LLM、
不写产品代码。样本窗口由 ``--hours`` 决定，默认全量（全部 ``logs/yinyue.log*``）。

复现::

    & .\.venv_it\Scripts\python.exe -X utf8 scripts\report_recall_yield.py
    & .\.venv_it\Scripts\python.exe -X utf8 scripts\report_recall_yield.py --sections logs
    & .\.venv_it\Scripts\python.exe -X utf8 scripts\report_recall_yield.py --hours 24
"""

from __future__ import annotations

import argparse
import collections
import datetime as _dt
import glob
import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LOGS_DIR = ROOT / "logs"
LOG_GLOB = "yinyue.log*"
META_DB = ROOT / "data" / "yinyue_meta.db"
COPY_DIR = ROOT / ".test_tmp" / "recall_yield"
EVENT = "needs_memory_recall"

#: N1 的 kind 闭集（``core/context/scene_read.py`` 的 MEMORY_KINDS；下面会与源码核对）。
MEMORY_KINDS_FALLBACK = ("fact", "episode", "person", "meme", "quote")

#: N1 的 kind → hub 的物理取数口（``core/memory/hub.py::KIND_SOURCES``）。
KIND_SOURCES_FALLBACK: dict[str, tuple[str, ...]] = {
    "fact": ("personal", "knowledge"),
    "episode": ("history", "recall_chain"),
    "person": ("person", "claim"),
    "meme": ("meme",),
    "quote": ("history",),
    "notes": ("notes",),
}

SECTIONS = ("logs", "gates", "sources", "attrib")


def _hr(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _parse_ts(raw: str) -> _dt.datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    text = re.sub(r"\.(\d{6})\d+", r".\1", text)
    try:
        return _dt.datetime.fromisoformat(text)
    except ValueError:
        return None


# --------------------------------------------------------------------------- 静态事实
def load_memory_kinds() -> tuple[str, ...]:
    try:
        sys.path.insert(0, str(ROOT))
        from core.context.scene_read import MEMORY_KINDS

        return tuple(MEMORY_KINDS)
    except Exception:
        return MEMORY_KINDS_FALLBACK


def load_kind_sources() -> dict[str, tuple[str, ...]]:
    try:
        sys.path.insert(0, str(ROOT))
        from core.memory.hub import KIND_SOURCES

        return {str(k): tuple(v) for k, v in KIND_SOURCES.items()}
    except Exception:
        return dict(KIND_SOURCES_FALLBACK)


def docenv_truth() -> dict[str, str]:
    """临时把 ``.env`` 的 ``YINYUE_*`` 灌进 os.environ，问 features 真实档位后还原。"""
    out: dict[str, str] = {}
    env_path = ROOT / ".env"
    if not env_path.exists():
        return {"<env>": "missing"}
    snapshot = dict(os.environ)
    try:
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            key = key.strip()
            if key.startswith("YINYUE_"):
                os.environ.setdefault(key, value.strip().strip('"').strip("'"))
        from core import features as F

        for name in (
            "PERSON_MEMORY",
            "PERSON_RECALL",
            "CLAIM_LEDGER",
            "RECALL_CHAIN",
            "RECALL_EPISODE",
            "MEDIA_RECALL",
            "NOTES_GATE",
        ):
            attr = getattr(F, name, None)
            if attr is None:
                out[name] = "absent"
                continue
            try:
                if attr in F.TRI_STATE_FLAGS:
                    out[name] = str(F.mode(attr, F.MODE_OFF))
                else:
                    flag = "on" if F.enabled(attr) else "off"
                    out[name] = f"{flag} (bool, default={F.enabled(attr)})"
            except Exception as exc:  # noqa: BLE001 - 读数失败不影响报告
                out[name] = f"err:{type(exc).__name__}"
    finally:
        os.environ.clear()
        os.environ.update(snapshot)
    return out


def probe(path: str, needle: str) -> tuple[int, str] | None:
    """在源码里找一行（返回行号 + 去空白文本），用来给结论盖 ``路径:行号``。"""
    file_path = ROOT / path
    if not file_path.exists():
        return None
    for number, line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), 1):
        if needle in line:
            return number, line.strip()
    return None


# --------------------------------------------------------------------------- 日志
def log_files() -> list[Path]:
    files = [Path(p) for p in glob.glob(str(LOGS_DIR / LOG_GLOB)) if Path(p).is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime)


def load_rows(hours: float = 0.0) -> tuple[list[Path], collections.Counter, list[dict]]:
    cutoff = None
    if hours and hours > 0:
        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=float(hours))
    rows: list[dict] = []
    per_file: collections.Counter = collections.Counter()
    files = log_files()
    for file in files:
        with file.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if EVENT not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("event") != EVENT:
                    continue
                if cutoff is not None:
                    at = _parse_ts(str(rec.get("timestamp") or ""))
                    if at is None or at < cutoff:
                        continue
                rows.append(rec)
                per_file[file.name] += 1
    return files, per_file, rows


def kinds_asked(row: dict, memory_kinds: tuple[str, ...]) -> list[str]:
    asked = [str(k) for k in (row.get("kinds") or [])]
    return asked or list(memory_kinds)


def normal_query(text: str) -> str:
    return re.sub(r"[\s,，。、;；:：!！?？~·]+", "", str(text or "")).lower()


def section_logs(hours: float, memory_kinds: tuple[str, ...]) -> dict:
    files, per_file, rows = load_rows(hours)
    _hr("1) 日志读数（logs/yinyue.log* 全量扫描）")
    print(f"文件 {len(files)} 个，event == {EVENT!r} 的行合计 {len(rows)} 条")
    for file in files:
        print(f"  - {file.name:18s} {file.stat().st_size / 1e6:6.1f} MB  {per_file[file.name]:4d} 条")
    if not rows:
        print("（这个窗口里没有任何 needs_memory_recall，后面的归因跳过）")
        return {"rows": [], "files": files}

    hits = collections.Counter(int(r.get("hits") or 0) for r in rows)
    zero = hits.get(0, 0)
    print()
    print(f"hits 分布: {dict(sorted(hits.items()))}")
    print(f"hits == 0: {zero} / {len(rows)} = {100.0 * zero / len(rows):.1f}%")
    empty_kinds = sum(1 for r in rows if not (r.get("kinds") or []))
    print(f"kinds == []: {empty_kinds} / {len(rows)} = {100.0 * empty_kinds / len(rows):.1f}%")
    print(f"errors 非空: {sum(1 for r in rows if r.get('errors'))}")
    print(f"principal_source: {dict(collections.Counter(r.get('principal_source') for r in rows))}")
    chats = collections.Counter(str(r.get("chat_id") or "") for r in rows)
    print(f"chat: {len(chats)} 个，Top3 = {chats.most_common(3)}")
    print(f"query 唯一值 {len({normal_query(r.get('query')) for r in rows})} 个，"
          f"长度中位 {_median([len(str(r.get('query') or '')) for r in rows])}")

    # ---- 按 kind 的零命中率
    _hr("2) 按 kind 的零命中率（分母 = 这一格被问到的回合数）")
    asked_n = collections.Counter()
    zero_n = collections.Counter()
    items_sum = collections.Counter()
    folded = collections.Counter()
    missing_key = collections.Counter()
    for row in rows:
        asked = kinds_asked(row, memory_kinds)
        by_kind = row.get("by_kind") or {}
        for kind in asked:
            asked_n[kind] += 1
            if kind in by_kind:
                count = int(by_kind.get(kind) or 0)
                items_sum[kind] += count
                if count == 0:
                    zero_n[kind] += 1
            elif kind == "quote" and "episode" in asked:
                # hub 按 (provider_kind, index) 去重：episode 已问过 history，quote 不再问，
                # 于是 by_kind 里根本没有 quote 这个键 —— 不是「查了没有」，是「记在 episode 名下」。
                folded[kind] += 1
            else:
                missing_key[kind] += 1
    print(f"{'kind':10s} {'被问':>6s} {'零命中':>7s} {'零命中率':>9s} {'条目合计':>9s} {'平均':>6s} {'by_kind 缺键':>12s}")
    for kind in list(memory_kinds) + [k for k in asked_n if k not in memory_kinds]:
        if not asked_n[kind]:
            continue
        known = asked_n[kind] - folded[kind] - missing_key[kind]
        rate = f"{100.0 * zero_n[kind] / known:8.1f}%" if known else "   n/a  "
        avg = f"{items_sum[kind] / known:6.2f}" if known else "   n/a"
        note = folded[kind] + missing_key[kind]
        print(f"{kind:10s} {asked_n[kind]:6d} {zero_n[kind]:7d} {rate:>9s} "
              f"{items_sum[kind]:9d} {avg:>6s} {note:12d}")
    if folded:
        print(f"注：quote 有 {folded['quote']} 个回合的取数记在 episode 名下（hub 对 history 只问一次），"
              "别把 quote 的 0 当成「查不到」。")

    # ---- kinds 为空 vs 给了
    _hr("3) kinds 为空 vs 明确给了 kind（空 = N1 说不准，按全集查）")
    cross = collections.Counter()
    for row in rows:
        given = bool(row.get("kinds") or [])
        cross[(given, int(row.get("hits") or 0) == 0)] += 1
    for given in (True, False):
        total = cross[(given, True)] + cross[(given, False)]
        label = "明确给了 kind" if given else "kinds=[]（全查）"
        print(f"{label:14s}: n={total:4d}  零命中 {cross[(given, True)]:4d}"
              f"  ({100.0 * cross[(given, True)] / total:.1f}%)" if total else f"{label}: n=0")

    # ---- 重复零命中 query（太泛）
    _hr("4) 同一个 query 反复零命中（「太泛」候选）")
    dup = collections.Counter()
    dup_zero = collections.Counter()
    for row in rows:
        key = normal_query(row.get("query"))
        dup[key] += 1
        if int(row.get("hits") or 0) == 0:
            dup_zero[key] += 1
    repeats = [(k, dup_zero[k], dup[k]) for k in dup_zero if dup_zero[k] >= 2]
    repeats.sort(key=lambda item: (-item[1], item[0]))
    print(f"零命中 ≥2 次的 query: {len(repeats)} 个，合计 {sum(r[1] for r in repeats)} 次空转")
    for key, zero_times, asked_times in repeats[:15]:
        print(f"  {key[:40]!r:44s} 零命中 {zero_times} / 问到 {asked_times}")
    return {
        "rows": rows,
        "files": files,
        "per_file": per_file,
        "zero": zero,
        "asked_n": asked_n,
        "zero_n": zero_n,
        "items_sum": items_sum,
        "folded": folded,
        "dup_zero": dup_zero,
        "dup_all": dup,
    }


def _median(values: list[int]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


# --------------------------------------------------------------------------- 门
def section_gates(memory_kinds: tuple[str, ...], kind_sources: dict[str, tuple[str, ...]]) -> dict:
    _hr("5) 接线与门（静态事实，每行都给 路径:行号）")
    print(f"N1 的 kind 闭集 = {tuple(memory_kinds)}"
          "（core/context/scene_read.py::MEMORY_KINDS）")
    for kind in memory_kinds:
        print(f"  {kind:8s} -> provider kind {kind_sources.get(kind, ())}")
    print()
    for path, needle, why in (
        ("core/context/intent.py", "for k in ((_nm or {}).get(\"kinds\") or [])",
         "kinds 的来源：N1 的 section.needs_memory.kinds，按 MEMORY_KINDS 闭集过滤"),
        ("core/context/intent.py", "kinds=_nm_kinds or list(MEMORY_KINDS)",
         "kinds 为空 = 显式展开成全集（不是丢失）"),
        ("core/context/intent.py", "kinds=_nm_kinds,",
         "日志里的 kinds 是**请求侧**（空 = 全集查），不是实际问到的 provider"),
        ("core/context/intent.py", "kinds 拿不准给空数组=全查",
         "N1 的 prompt 原文就写着「拿不准给空数组=全查」"),
    ):
        found = probe(path, needle)
        if found:
            print(f"  {path}:{found[0]}  {why}")
        else:
            print(f"  {path}: <没找到 {needle!r}>  {why}")
    print()
    env = docenv_truth()
    print("线上档位（.env 只读灌入后问 features）：")
    for name, value in env.items():
        print(f"  {name:16s} = {value}")
    print()
    gate_rows = []
    for path, needle, kind, why in (
        ("core/memory/providers.py", "if not features.enabled(features.PERSON_MEMORY)",
         "person", "PersonMemoryProvider：两个开关任一关 → 直接 return []，不写日志"),
        ("core/memory/providers.py", "if features.mode(features.CLAIM_LEDGER",
         "person", "ClaimMemoryProvider：CLAIM_LEDGER=off → 一条不吐"),
        ("core/memory/providers.py", "_KNOWLEDGE_MIN_SCORE = ",
         "fact", "KnowledgeMemoryProvider：覆盖率分 < 该阈值就丢"),
        ("core/memory/providers.py", "return overlap / max(len(q_tokens), 1) >= 0.35",
         "*", "各家共用的 _match()：query 词与正文重合率 < 35% → 丢"),
        ("core/memory/hub.py", "PER_PROVIDER_MIN: int = 5",
         "*", "每家 provider 至少取 5 条（不是截断成因，但决定合并池大小）"),
    ):
        found = probe(path, needle)
        gate_rows.append((path, found[0] if found else None, kind,
                          (found[1] if found else "<未找到>")[:78], why))
    print("门与阈值：")
    for path, number, kind, line, why in gate_rows:
        print(f"  {path}:{number}  [{kind}]  {line}")
        print(f"      -> {why}")
    print()
    person_closed = not str(env.get("PERSON_MEMORY", "")).startswith("on")
    print(f"person 这一格的判定：PERSON_MEMORY={env.get('PERSON_MEMORY')} / "
          f"PERSON_RECALL={env.get('PERSON_RECALL')} ⇒ "
          f"{'硬关着（providers.py 第 584 行起直接 return []）' if person_closed else '开着'}")
    return {"env": env, "gates": gate_rows, "person_closed": person_closed}


# --------------------------------------------------------------------------- 源表
def _connect_ro() -> tuple[sqlite3.Connection, str]:
    uri = f"file:{META_DB.as_posix()}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=5)
        con.execute("select 1").fetchone()
        return con, "live(mode=ro)"
    except sqlite3.Error:
        COPY_DIR.mkdir(parents=True, exist_ok=True)
        target = COPY_DIR / "meta_copy.db"
        if not target.exists() or target.stat().st_mtime < META_DB.stat().st_mtime:
            shutil.copy2(META_DB, target)
        con = sqlite3.connect(target)
        return con, f"copy({target.name})"


def _count(con: sqlite3.Connection, sql: str) -> int | None:
    try:
        return int(con.execute(sql).fetchone()[0])
    except sqlite3.Error:
        return None


def section_sources() -> dict:
    _hr("6) 各 provider 的源表（只读；数的是「库里到底有没有东西」）")
    con, how = _connect_ro()
    print(f"meta 库连接 = {how}；库 = data/yinyue_meta.db")
    counts = {
        "person_entities": _count(con, "select count(*) from person_entities"),
        "person_observations": _count(con, "select count(*) from person_observations"),
        "claim_ledger": _count(con, "select count(*) from claim_ledger"),
        "claim_ledger_valid": _count(
            con,
            "select count(*) from claim_ledger where superseded_at is null and withdrawn_at is null",
        ),
        "knowledge_docs": _count(con, "select count(*) from knowledge_docs"),
        "memory_projections": _count(con, "select count(*) from memory_projections"),
        "core_memories": _count(con, "select count(*) from core_memories"),
        "chat_sessions": _count(con, "select count(*) from chat_sessions"),
    }
    try:
        con.close()
    except Exception:  # noqa: BLE001 - 只读连接关不掉不影响读数
        pass
    for name, value in counts.items():
        print(f"  {name:22s} {value}")
    web_index = ROOT / "data" / "web_memes" / "memes_index.json"
    web_count = None
    if web_index.exists():
        try:
            payload = json.loads(web_index.read_text(encoding="utf-8", errors="replace"))
            if isinstance(payload, dict):
                payload = next(
                    (payload[key] for key in ("entries", "memes", "items") if key in payload),
                    None,
                )
            web_count = len(payload) if isinstance(payload, list) else None
        except (ValueError, OSError):
            web_count = None
    print(f"  web_memes/memes_index.json  {web_count if web_count is not None else 'n/a'}")
    chain_dir = ROOT / "data" / "recall_chain"
    chain_files = sorted(chain_dir.glob("*")) if chain_dir.exists() else []
    print(f"  data/recall_chain/  {len(chain_files)} 个文件"
          f"（{', '.join(p.name for p in chain_files[:4])}）")
    return counts


# --------------------------------------------------------------------------- 归因
def _gate_blocks(kind: str, env: dict[str, str]) -> bool:
    if kind == "person":
        return not (
            str(env.get("PERSON_MEMORY", "")).startswith("on")
            and str(env.get("PERSON_RECALL", "")).startswith("on")
        )
    return False


def _load_knowledge_docs() -> list[tuple[int, str, str, str]]:
    """只读取 knowledge_docs（fact 格的第二臂）；取不到返回 []。"""
    try:
        conn, _origin = _connect_ro()
    except Exception:  # pragma: no cover - 库不在就退化成空
        return []
    try:
        return [
            (int(r[0]), str(r[1] or ""), str(r[2] or ""), str(r[3] or ""))
            for r in conn.execute(
                "select id, coalesce(title, ''), coalesce(content, ''), "
                "coalesce(created_at, '') from knowledge_docs"
            )
        ]
    except Exception:
        return []
    finally:
        conn.close()


def _meme_matcher() -> Any:
    """直接用生产的 WebMemeMemoryProvider 判「索引里有没有这个词条」。"""
    try:
        from core.memory.providers import WebMemeMemoryProvider

        provider = WebMemeMemoryProvider()
        provider._load()
        if not provider._cache:
            return None
    except Exception:  # pragma: no cover - 索引不在就退化成 None
        return None

    def count(query: str, limit: int = 5) -> int:
        try:
            return len(provider.recall(None, str(query or ""), limit=limit))
        except Exception:
            return 0

    return count


def _knowledge_scorer() -> tuple[Any, float]:
    """复算 knowledge 臂的分（标题×2 / 正文×1 覆盖率），返回 (score_fn, 阈值)。"""
    try:
        from core.memory.providers import (
            _KNOWLEDGE_MIN_SCORE,
            _content_query_tokens,
            _knowledge_score,
            _tokens,
        )
    except Exception:  # pragma: no cover
        return None, 0.0
    docs = _load_knowledge_docs()

    def score(query: str, cutoff: str = "") -> float:
        tokens = _content_query_tokens(str(query or "")) or _tokens(str(query or ""))
        best = 0.0
        for _did, title, content, created in docs:
            if cutoff and created and created > cutoff:
                continue
            if not content:
                continue
            best = max(best, _knowledge_score(tokens, title, content))
        return best

    return score, float(_KNOWLEDGE_MIN_SCORE)


def _local_cutoff(record: dict) -> str:
    """把日志里的 UTC ISO 时刻换成库里的本地 naive 时间（+8h），用来卡文档新旧。"""
    raw = str(record.get("timestamp") or "")
    if not raw:
        return ""
    try:
        utc = _dt.datetime.strptime(raw.replace("Z", ""), "%Y-%m-%dT%H:%M:%S.%")
    except ValueError:
        return ""
    return (utc + _dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def section_attrib(
    log_data: dict,
    gate_data: dict,
    source_counts: dict,
    memory_kinds: tuple[str, ...],
    kind_sources: dict[str, tuple[str, ...]],
) -> None:
    rows = log_data.get("rows") or []
    _hr("7) 零命中逐条归因（每一格都由**实测的机制**判定，不用推断）")
    if not rows:
        print("（没有样本）")
        return
    env = gate_data.get("env") or {}
    dup_zero = log_data.get("dup_zero") or collections.Counter()
    meme_count = _meme_matcher()
    know_score, know_min = _knowledge_scorer()

    _hr("7.1 三条硬事实（先看这个）")
    print("  · kinds=[] 的语义：N1 的 prompt 写着「拿不准给空数组=全查」"
          "（core/context/intent.py:1935），intent 把它展开成全集"
          "（core/context/intent.py:3655）⇒ 空 kinds 不是被丢，是照办。")
    print("  · fact 格的第二臂 = knowledge_docs；阈值 _KNOWLEDGE_MIN_SCORE="
          f"{know_min}（core/memory/providers.py:906），文档 {len(_load_knowledge_docs())} 篇。")
    print("  · meme 格的取数口是 WebMemeMemoryProvider：靠 key/aliases **子串**命中"
          "（core/memory/providers.py:1161-1167），不是覆盖率阈值。")
    print()

    buckets: collections.Counter = collections.Counter()
    lines: list[str] = []
    for row in rows:
        if int(row.get("hits") or 0) != 0:
            continue
        asked = kinds_asked(row, memory_kinds)
        query = str(row.get("query") or "")
        by_kind = row.get("by_kind") or {}
        unwired = [k for k in asked if not kind_sources.get(k)]
        person_gated = "person" in asked and _gate_blocks("person", env)
        kn = know_score(query, _local_cutoff(row)) if ("fact" in asked and know_score) else -1.0
        meme_hit = meme_count(query) if ("meme" in asked and meme_count) else -1
        generic = dup_zero.get(normal_query(query), 0) >= 2

        if unwired:
            label = "a_unwired"
            note = f"没接线的 kind {unwired}"
        elif "fact" in asked and kn >= know_min:
            label = "x_fact_silent_miss"
            note = (f"知识库有 ≥{know_min} 的文档（最高 {kn:.3f}）却没进结果 ⇒ "
                    "knowledge 臂静默失效（providers.py:934-939 的两条静默 return）")
        elif "meme" in asked and meme_hit > 0:
            label = "x_meme_silent_miss"
            note = f"梗索引里能命中 {meme_hit} 条却没进结果 ⇒ meme 臂静默失效"
        elif person_gated:
            label = "e_person_gate"
            note = "person 开关关着（core/memory/providers.py:584 直接 return []）"
        elif "meme" in asked and meme_hit == 0:
            label = "b_meme_no_entry"
            note = "梗索引里没有这个词条（key/aliases 子串全不匹配）"
        elif "fact" in asked:
            label = "c_fact_below_threshold"
            note = f"全部文档分数 < {know_min}（最高 {max(kn, 0.0):.3f}）"
        elif "episode" in asked:
            label = "c_episode_filtered"
            note = "episode 有源（history/recall_chain），被 _match≥0.35 或排序截断吃掉"
        elif generic:
            label = "d_query_generic"
            note = "同一个 query 反复零命中"
        else:
            label = "c_other"
            note = "其余（有源、无线索）"
        buckets[label] += 1
        lines.append(
            f"  [{label:18s}] kinds={asked} by_kind={by_kind or '-'} "
            f"query={query[:28]!r} | {note}"
        )

    total_zero = sum(buckets.values())
    print(f"零命中 {total_zero} 条，按实测机制归因：")
    order = (
        "x_fact_silent_miss",
        "x_meme_silent_miss",
        "e_person_gate",
        "b_meme_no_entry",
        "c_fact_below_threshold",
        "c_episode_filtered",
        "c_other",
        "d_query_generic",
        "a_unwired",
    )
    for label in order:
        if buckets[label]:
            print(f"  {label:20s} {buckets[label]:4d}  "
                  f"({100.0 * buckets[label] / total_zero:.1f}%)")
    print()
    for line in lines:
        print(line)

    _hr("7.2 每个机制涉及多少次零命中（可重叠：一回合可能同时踩两个门）")
    involve: collections.Counter = collections.Counter()
    for row in rows:
        if int(row.get("hits") or 0) != 0:
            continue
        asked = kinds_asked(row, memory_kinds)
        query = str(row.get("query") or "")
        if "fact" in asked and know_score:
            kn = know_score(query, _local_cutoff(row))
            involve["fact_silent_miss" if kn >= know_min else "fact_below_threshold"] += 1
        if "person" in asked and _gate_blocks("person", env):
            involve["person_gate"] += 1
        if "meme" in asked and meme_count:
            involve["meme_no_entry" if meme_count(query) == 0 else "meme_silent_miss"] += 1
        if "episode" in asked:
            involve["episode_asked"] += 1
    for label, n in involve.most_common():
        print(f"  {label:22s} {n:4d}  ({100.0 * n / total_zero:.1f}% of {total_zero})")
    print()

    _hr("7.3 整改候选（按预计减少的空转次数排序；只读数、不改代码）")
    print(f"  候选 1（让 knowledge 臂不再静默）：涉及 {buckets['x_fact_silent_miss']} 次零命中。"
          "改法 = core/memory/providers.py:934-939 的两个 `except Exception: return []` / "
          "`if principal is None: return []` 各补一行 log；不动 recall_chain.py。")
    print(f"  候选 2（person 格）：涉及 {buckets['e_person_gate']} 次。要么开 PERSON_MEMORY+"
          "PERSON_RECALL，要么让 N1 别再问 person（providers.py:584 现在静默返回 []）。")
    print(f"  候选 3（meme 格）：涉及 {buckets['b_meme_no_entry']} 次。梗索引 2111 条 key 与群内梗"
          "不重合 ⇒ 该格基本不可能命中；改成用 episode 兜底或直接不查，属产品决策。")
    print(f"  候选 4（fact 阈值）：涉及 {buckets['c_fact_below_threshold']} 次。全是最高分 < {know_min}"
          " 的弱相关命中，下调阈值 = 灌噪声，不建议。")
    print("  候选 5（读数）：hub 的 detail 已经把 errors 记下来（core/memory/hub.py:220-224），"
          "但 provider 自己吞掉的异常进不去；只有补上候选 1 才能看见。")
    print()
    print("复算：& .\\.venv_it\\Scripts\\python.exe -X utf8 scripts\\report_recall_yield.py "
          "--sections attrib")

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="召回空转归因（只读）")
    parser.add_argument("--sections", default=",".join(SECTIONS),
                        help=f"逗号分隔，默认 {','.join(SECTIONS)}；可选 {','.join(SECTIONS)}")
    parser.add_argument("--hours", type=float, default=0.0,
                        help="只看最近 N 小时（默认 0 = 全量扫全部 rotate 文件）")
    args = parser.parse_args(argv)
    wanted = {part.strip() for part in str(args.sections).split(",") if part.strip()}
    unknown = wanted - set(SECTIONS)
    if unknown:
        parser.error(f"未知段 {sorted(unknown)}；可选 {list(SECTIONS)}")

    memory_kinds = load_memory_kinds()
    kind_sources = load_kind_sources()
    log_data: dict = {}
    gate_data: dict = {}
    source_counts: dict = {}
    if "logs" in wanted:
        log_data = section_logs(args.hours, memory_kinds)
    if "gates" in wanted:
        gate_data = section_gates(memory_kinds, kind_sources)
    if "sources" in wanted:
        source_counts = section_sources()
    if "attrib" in wanted:
        if not log_data:
            log_data = {"rows": load_rows(args.hours)[2]}
        if not gate_data:
            gate_data = section_gates(memory_kinds, kind_sources)
        if not source_counts:
            con, _how = _connect_ro()
            source_counts = {
                "person_observations": _count(con, "select count(*) from person_observations"),
                "claim_ledger_valid": _count(
                    con,
                    "select count(*) from claim_ledger"
                    " where superseded_at is null and withdrawn_at is null",
                ),
                "knowledge_docs": _count(con, "select count(*) from knowledge_docs"),
                "memory_projections": _count(con, "select count(*) from memory_projections"),
            }
            try:
                con.close()
            except Exception:  # noqa: BLE001 - 只读连接
                pass
        section_attrib(log_data, gate_data, source_counts, memory_kinds, kind_sources)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
