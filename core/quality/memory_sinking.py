"""检索上沉（L4 → L1，批 3b）：反复被检索命中的东西，沉淀成卡片/词典。

业务目标（`docs/群聊理解修复_批3设计.md` §4）：同一个词条（群黑话、反复被问的
事实）被**反复现查**，说明它该进 L1 了——沉淀一次，下次不必再查、且她**从第一轮
就懂**。

**只统计，不问模型**：候选来自两条可量化的重复信号，次数本身就是证据：

1. **检索反复命中**：`logs/memory_recall_trace.jsonl` 里归一化后的 query 在窗口内
   出现 ≥ ``MIN_OCCURRENCES`` 次，且命中分 ≥ 闸门阈值；
2. **工具反复现查**：`terminal_events` 里同一 query 的工具调用重复同样多次。

候选**只写 `memory_proposals`（pending）**，主人点头才生效——本模块不写任何记忆。

纪律（沿用 `docs/记忆的定义.md` §3）：

- 玩笑 / 一次性黑话**不许**上沉（这是红线：误上沉 = 把废话固化成知识）；
- 去重走既有 `_duplicate`；每人/每群有上限；
- 只读 + 失败降级为「无候选」。

归属：`scheduler/main.py`（可选调度）+ `scripts/audit_sinking.py`（离线审计）；
开关 `YINYUE_FEATURE_MEMORY_SINKING`；设计 `docs/群聊理解修复_批3设计.md` §4。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from core import temporal
from db.models import is_group_chat_id

import structlog
from safelog import warn
from core.infra.envflags import flag, text as _text

log = structlog.get_logger()

# 判定「反复」的门槛：同一词条至少出现这么多次才算值得上沉
MIN_OCCURRENCES = 3
# 命中分下限：低于它说明检索本来就没找到东西，沉淀无从谈起
MIN_SCORE = 0.45
MAX_CONTENT = 60
# 一句话黑话/术语的长度上界：太长的不是「词条」而是句子，不适合进词典。
# 实测教训：设成 12 时 `@鲸鲸你记得我说过什么吗`（12 字）照样漏进来，
# 所以再收紧，并且**额外**用「像一句话」的启发式挡掉。
MAX_TERM_CHARS = 8
# 一次最多产出的候选数（防洪水）
MAX_PER_RUN = 10

# 测试夹具会污染 trace（chat_id=owner_* / memory_id=proj:lex*），必须排除。
_FIXTURE_CHAT_PREFIXES = ("owner", "test", "tmp", "12345678")
_FIXTURE_MEMORY_PREFIXES = ("proj:lex", "proj:semantic", "proj:lexical", "mem:")

# 评测探针/夹具文本（评测在真实 chat_id 里跑，光靠 chat 前缀挡不住）
_FIXTURE_TEXTS = (
    "第一行", "第二行", "第三行", "拍了拍我", "不对不是", "不对我没说",
    "帮我看看这个", "总结一下", "看看图", "别聊这个", "我是学委吗",
    "记得我说过什么", "修好了吗",
)
# 系统/事件文本：拍一拍、撤回、入群等是事件记录，不是「术语」
# （2026-09-23 审计实测：「P5拍了拍鲸鲸」×6 曾混进候选——红线要求误上沉=0）
_EVENT_TEXTS = (
    "拍了拍", "拍一拍", "撤回", "加入了群聊", "领取了", "邀请", "红包", "转账",
)
# 像「一句话」而不是「一个词条」的句子标记
_SENTENCE_MARKERS = (
    "我", "你", "他", "她", "吗", "呢", "吧", "什么", "怎么", "为什么",
    "帮我", "看看", "一下", "是不是", "对不对", "觉得", "应该",
    # 2026-09-23 审计实测：「在干嘛呀」×32（私聊寒暄）曾混进候选
    "干嘛", "在吗", "呀", "嘛",
)

# 归一化时去掉的噪声
_PUNCT_RE = re.compile(r"[\s,，。！？!?~～、；;:：\"'“”‘’()（）\[\]【】]+")


def sinking_enabled() -> bool:
    return flag("YINYUE_FEATURE_MEMORY_SINKING")


_MEDIA_PLACEHOLDERS = frozenset({
    "图片", "表情", "表情包", "动画表情", "视频", "语音", "文件", "链接", "转账", "红包", "位置",
})


def normalize_term(query: Any) -> str:
    """把一句检索词归一成「词条」；**不像词条的一律返回 ""**。

    上沉的红线是「误上沉 = 0」：把一句话、一条命令、一个评测探针固化成
    「群内术语」，比不沉淀坏得多。所以这里的拒绝条件比接受条件更用力：

    - 空 / 太长（> ``MAX_TERM_CHARS``）；
    - 以 ``@`` 开头：那是对她说的话/指令，不是群里的术语；
    - 含句子标记（我/你/吗/帮我/看看…）：那是在说话，不是在用词；
    - 命中已知评测夹具文本。
    """
    text = _PUNCT_RE.sub("", str(query or "").strip())
    if not text or len(text) > MAX_TERM_CHARS:
        return ""
    if text.startswith("@"):
        return ""
    if any(marker in text for marker in _SENTENCE_MARKERS):
        return ""
    if any(marker in text for marker in _EVENT_TEXTS):
        return ""
    if any(text.startswith(fix) or fix in text for fix in _FIXTURE_TEXTS):
        return ""
    # 2026-11-23：媒体占位符不是术语。「群内术语『图片』」这种提案没有可沉淀的释义。
    if text in _MEDIA_PLACEHOLDERS:
        return ""
    return text


def is_fixture(record: dict) -> bool:
    """测试夹具还是真实流量？（§5 数据坑：trace 无测试隔离）。"""
    chat_id = str(record.get("chat_id") or "")
    if any(chat_id.startswith(p) for p in _FIXTURE_CHAT_PREFIXES):
        return True
    if str(record.get("bot_account_id") or "") in ("bot_a", "bot_b", ""):
        return True
    for hit in record.get("hits") or []:
        mid = str((hit or {}).get("memory_id") or "")
        if any(mid.startswith(p) for p in _FIXTURE_MEMORY_PREFIXES):
            return True
    return False


def collect_candidates(
    records: Iterable[dict],
    *,
    min_occurrences: int = MIN_OCCURRENCES,
    min_score: float = MIN_SCORE,
) -> list[dict]:
    """从召回记录里数出「反复命中」的候选（纯函数，可测）。

    返回 ``[{chat_id, term, occurrences, best_score, memory_ids}]``，按次数降序。
    夹具记录被排除；命中分取该词条窗口内的**最高**分（有强命中才算数）。
    """
    seen: dict[tuple[str, str], dict] = {}
    for record in records:
        if not isinstance(record, dict) or is_fixture(record):
            continue
        chat_id = str(record.get("chat_id") or "")
        # 术语只服务群聊（设计 §4.1「群内术语/黑话」；私聊没有「群内」语义）
        if not is_group_chat_id(chat_id):
            continue
        term = normalize_term(record.get("query"))
        if not term:
            continue
        hits = [h for h in (record.get("hits") or []) if isinstance(h, dict)]
        top = max((float(h.get("score") or 0.0) for h in hits), default=0.0)
        if top < float(min_score):
            continue
        key = (chat_id, term)
        slot = seen.setdefault(
            key,
            {"chat_id": chat_id, "term": term, "occurrences": 0, "best_score": 0.0, "memory_ids": []},
        )
        slot["occurrences"] += 1
        slot["best_score"] = max(slot["best_score"], top)
        for hit in hits:
            mid = str(hit.get("memory_id") or "")
            if mid and mid not in slot["memory_ids"] and not any(
                mid.startswith(p) for p in _FIXTURE_MEMORY_PREFIXES
            ):
                slot["memory_ids"].append(mid)
    out = [v for v in seen.values() if v["occurrences"] >= int(min_occurrences)]
    out.sort(key=lambda v: (-v["occurrences"], v["term"]))
    return out


# ------------------------------------------------------ 第二触发：工具反复现查
# 外部现查工具里只认「词条式」的查询参数；长句 / 问句不进候选（否则会把句子沉淀成词条）。
# 长信息需求由 knowledge_docs（web_search 结果入库）承接，不在这里重复沉淀。
_TOOL_QUERY_ARGS = {
    "web_search": "query",
    "acg_lookup": "keyword",
}

# 同一次调用会被双写进 terminal_events（间隔毫秒级）；计数时按「同群+同词条+时间窗」折一条
_TOOL_DEDUPE_SECONDS = 60.0


def _action_term(row: dict) -> str:
    """从 action 行解析出词条式查询；非目标工具 / 非词条形状 → ""。"""
    tool = str(row.get("content") or row.get("tool_name") or "")
    arg = _TOOL_QUERY_ARGS.get(tool)
    if not arg:
        return ""
    payload = row.get("action_payload_json") or row.get("tool_args") or ""
    try:
        data = json.loads(payload) if isinstance(payload, str) else payload
    except Exception:  # noqa: BLE001
        warn("core/quality/memory_sinking.py:189 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""
    if not isinstance(data, dict):
        return ""
    inputs = data.get("input") if isinstance(data.get("input"), dict) else data
    return normalize_term(inputs.get(arg))


def collect_tool_candidates(
    rows: Iterable[dict],
    *,
    min_occurrences: int = MIN_OCCURRENCES,
    dedupe_seconds: float = _TOOL_DEDUPE_SECONDS,
) -> list[dict]:
    """第二触发：terminal_events 里同一群 + 同一词条的重复现查（双写去重）。

    rows: ``{chat_id, content(工具名), action_payload_json, occurred_at}``。
    只统计群聊——私聊没有「群内术语」语义（与第一触发一致）。
    """
    seen: dict[tuple[str, str], dict] = {}
    last_at: dict[tuple[str, str], datetime] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        chat = str(row.get("chat_id") or "")
        if not is_group_chat_id(chat):
            continue
        term = _action_term(row)
        if not term:
            continue
        key = (chat, term)
        at = _parse_at(row.get("occurred_at"))
        prev = last_at.get(key)
        if at is not None and prev is not None and (at - prev).total_seconds() < float(dedupe_seconds):
            continue
        slot = seen.setdefault(
            key,
            {
                "chat_id": chat,
                "term": term,
                "occurrences": 0,
                "best_score": 0.0,
                "memory_ids": [],
                "source": "tool",
            },
        )
        slot["occurrences"] += 1
        if at is not None:
            last_at[key] = at
    out = [v for v in seen.values() if v["occurrences"] >= int(min_occurrences)]
    out.sort(key=lambda v: (-v["occurrences"], v["term"]))
    return out


def merge_candidates(*groups: Iterable[dict]) -> list[dict]:
    """合并多来源候选（召回 trace + 工具现查）：同群同词条计数相加，保留最高分与来源。"""
    merged: dict[tuple[str, str], dict] = {}
    for group in groups:
        for cand in group or []:
            if not isinstance(cand, dict):
                continue
            chat = str(cand.get("chat_id") or "")
            term = str(cand.get("term") or "")
            if not term:
                continue
            slot = merged.setdefault(
                (chat, term),
                {
                    "chat_id": chat,
                    "term": term,
                    "occurrences": 0,
                    "best_score": 0.0,
                    "memory_ids": [],
                    "sources": [],
                },
            )
            slot["occurrences"] += int(cand.get("occurrences") or 0)
            slot["best_score"] = max(slot["best_score"], float(cand.get("best_score") or 0.0))
            for mid in cand.get("memory_ids") or []:
                if str(mid) not in slot["memory_ids"]:
                    slot["memory_ids"].append(str(mid))
            src = str(cand.get("source") or "recall")
            if src not in slot["sources"]:
                slot["sources"].append(src)
    out = [v for v in merged.values() if v["occurrences"] >= MIN_OCCURRENCES]
    out.sort(key=lambda v: (-v["occurrences"], v["term"]))
    return out


def load_tool_action_rows(*, days: int = 14) -> list[dict]:
    """读 terminal_events 的 action 行（只读；任何失败返回空，不影响主链）。"""
    try:
        from core.terminal.store_port import get_meta_repo
        from sqlalchemy import text as _sql

        since = (datetime.now() - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d %H:%M:%S")
        with get_meta_repo().engine.connect() as conn:
            rows = conn.execute(
                _sql(
                    "select chat_id, content, action_payload_json, occurred_at "
                    "from terminal_events "
                    "where event_type = 'action' and occurred_at >= :since "
                    "and content in ('web_search', 'acg_lookup') "
                    "order by occurred_at"
                ),
                {"since": since},
            ).mappings().all()
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.warning("tool_query_rows_failed", error=str(exc)[:120])
        return []


# ------------------------------------------------------------------ io
def _trace_path() -> Path:
    import os as _os

    override = (_text("YINYUE_MEMORY_RECALL_TRACE_PATH")).strip()
    if override:
        return Path(override)
    from core.config.settings import config

    return Path(config.paths.log_dir) / "memory_recall_trace.jsonl"


def load_records(*, days: int = 14, path: Path | None = None) -> list[dict]:
    """读召回 trace（只读）。缺失/损坏一律返回已解析到的部分。

    ⚠️ trace 的 ``at`` 带 UTC 偏移（``+00:00``），而窗口用本地墙钟。两者直接
    比较会抛 ``TypeError``（aware vs naive），**被异常兜底静默吞掉 → 恒返回 0 条**。
    这里先统一成 naive 本地时间再比（§7.7 的老坑）。
    """
    target = path or _trace_path()
    cutoff = datetime.now() - timedelta(days=max(1, int(days)))
    out: list[dict] = []
    try:
        if not target.exists():
            return []
        for line in target.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except Exception:
                warn("core/quality/memory_sinking.py:333 Exception", err=repr(__import__("sys").exc_info()[1]))
                continue
            at = _parse_at(record.get("at"))
            if at is not None and at < cutoff:
                continue
            out.append(record)
    except Exception:
        warn("core/quality/memory_sinking.py:339 Exception", err=repr(__import__("sys").exc_info()[1]))
        return out
    return out


def _parse_at(raw: Any) -> datetime | None:
    """解析 ``at`` 并统一成 **naive 本地时间**（换算口径唯一 owner = ``temporal``）。

    与旧实现等价（``fromisoformat`` + 带偏移的转本地），但换算那一句不再自写：
    全仓只允许 ``temporal.to_naive_local`` / ``temporal.parse_naive_local`` 做这件事
    （AST 守卫见 ``tests/test_time_convention.py``）。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        warn("core/quality/memory_sinking.py:356 ValueError", err=repr(__import__("sys").exc_info()[1]))
        return None
    if moment.tzinfo is not None:
        moment = temporal.to_naive_local(moment)
    return moment


def sink_candidates(
    *,
    days: int = 14,
    min_occurrences: int = MIN_OCCURRENCES,
    limit: int = MAX_PER_RUN,
    path: Path | None = None,
) -> list[dict]:
    """读召回 trace + 工具反复现查 → 候选（不写库）。scripts/audit_sinking.py 与调度共用。"""
    if not sinking_enabled():
        return []
    records = load_records(days=days, path=path)
    recall = collect_candidates(records, min_occurrences=min_occurrences)
    tool = collect_tool_candidates(
        load_tool_action_rows(days=days), min_occurrences=min_occurrences
    )
    return merge_candidates(recall, tool)[: max(1, int(limit))]


def propose_sinking(
    *,
    days: int = 14,
    min_occurrences: int = MIN_OCCURRENCES,
    limit: int = MAX_PER_RUN,
    path: Path | None = None,
) -> dict:
    """把上沉候选写进 `memory_proposals`（slot=lexicon, status=pending）。

    只写 pending；批准才落 L1。返回统计。**本函数不生效任何记忆。**
    """
    from core.terminal.store_port import get_meta_repo
    from sqlalchemy import text as _sql

    stats = {"scanned": 0, "candidates": 0, "staged": 0, "skipped": 0}
    if not sinking_enabled():
        return stats
    try:
        candidates = sink_candidates(
            days=days, min_occurrences=min_occurrences, limit=limit, path=path
        )
        stats["candidates"] = len(candidates)
        if not candidates:
            return stats
        existing = _existing_lexicon()
        with get_meta_repo().engine.begin() as conn:
            for cand in candidates:
                content = _content_for(cand)
                if not content or content in existing:
                    stats["skipped"] += 1
                    continue
                conn.execute(
                    _sql(
                        "insert into memory_proposals "
                        "(slot, subject_name, subject_key, chat_id, platform, content, reason, "
                        " evidence_json, status, created_at) "
                        "values ('lexicon', '', '', :chat_id, '', :content, 'repeat', "
                        " :evidence, 'pending', :now)"
                    ),
                    {
                        "chat_id": cand["chat_id"],
                        "content": content,
                        "evidence": json.dumps(
                            {
                                "ids": cand["memory_ids"][:6],
                                "text": f"检索 {cand['occurrences']} 次命中（最高 {cand['best_score']:.2f}）",
                                "occurrences": cand["occurrences"],
                            },
                            ensure_ascii=False,
                        ),
                        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    },
                )
                existing.add(content)
                stats["staged"] += 1
        stats["scanned"] = sum(c["occurrences"] for c in candidates)
    except Exception as exc:  # noqa: BLE001
        log.warning("memory_sinking_failed", error=str(exc)[:160])
    return stats


def _content_for(cand: dict) -> str:
    """候选 → 提案正文。

    注意：这里**只提「该沉淀什么词」**，释义留空由主人填/批准时补——
    因为自动化系统没资格替词条下定义（错的定义比没有定义更危险）。
    """
    term = str(cand.get("term") or "")[:MAX_TERM_CHARS]
    if not term:
        return ""
    return f"群内术语「{term}」被反复检索（{cand['occurrences']} 次），建议沉淀其释义"[:MAX_CONTENT + 20]


def _existing_lexicon() -> set[str]:
    from core.terminal.store_port import get_meta_repo
    from sqlalchemy import text as _sql

    out: set[str] = set()
    with get_meta_repo().engine.connect() as conn:
        for row in conn.execute(
            _sql(
                "select content from memory_proposals "
                "where slot = 'lexicon' and status in ('pending','approved')"
            )
        ).mappings():
            out.add(str(row["content"] or ""))
    return out


__all__ = [
    "MAX_PER_RUN",
    "MIN_OCCURRENCES",
    "MIN_SCORE",
    "collect_candidates",
    "is_fixture",
    "load_records",
    "normalize_term",
    "propose_sinking",
    "sink_candidates",
    "sinking_enabled",
    "collect_tool_candidates",
    "load_tool_action_rows",
    "merge_candidates",
]
